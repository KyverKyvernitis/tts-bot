//! gTTS connection pools scoped to the destination and effective routing policy.
use crate::{google, google_fetch, AudioSink, ProviderError, ProviderRequest, Result};
use std::{collections::VecDeque, ffi::OsString, net::IpAddr, sync::{Arc, Mutex}, time::Duration};
use tokio::sync::{OnceCell, Semaphore};

const MAX_CLIENTS: usize = 8;

#[derive(PartialEq, Eq)]
struct ClientRoute {
    tld: String,
    source: Option<IpAddr>,
    explicit_proxy: Option<String>,
    environment: Vec<Option<OsString>>,
    connect_timeout: Duration,
    receive_timeout: Duration,
}

impl ClientRoute {
    fn from_request(request: &ProviderRequest) -> Result<Self> {
        if request.proxy.as_ref().is_some_and(|proxy| proxy.len() > 2048) {
            return Err(ProviderError::new("configuration", "proxy gTTS excede o limite"));
        }
        google::validate_tld(&request.tld)?;
        Ok(Self {
            tld: request.tld.to_ascii_lowercase(),
            source: request.source_ip,
            explicit_proxy: request.proxy.clone(),
            environment: Self::environment(request.proxy.is_some()),
            connect_timeout: Duration::from_secs_f64(request.connect_timeout_seconds),
            receive_timeout: Duration::from_secs_f64(request.receive_timeout_seconds),
        })
    }

    fn environment(explicit_proxy: bool) -> Vec<Option<OsString>> {
        let mut values = ["SSL_CERT_FILE", "SSL_CERT_DIR"].into_iter().map(std::env::var_os).collect::<Vec<_>>();
        if !explicit_proxy {
            // Delegate actual precedence and NO_PROXY matching to reqwest.
            values.extend(["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                           "NO_PROXY", "no_proxy", "REQUEST_METHOD"].into_iter().map(std::env::var_os));
        }
        values
    }

    fn build_client(&self) -> Result<reqwest::Client> {
        let check_environment = || {
            if Self::environment(self.explicit_proxy.is_some()) == self.environment {
                Ok(())
            } else {
                Err(ProviderError::new("configuration", "configuração de rota gTTS mudou"))
            }
        };
        check_environment()?;
        let mut builder = reqwest::Client::builder()
            .connect_timeout(self.connect_timeout)
            .read_timeout(self.receive_timeout)
            .pool_idle_timeout(Duration::from_secs(60))
            .pool_max_idle_per_host(2);
        if let Some(ip) = self.source {
            builder = builder.local_address(ip);
        }
        if let Some(proxy) = &self.explicit_proxy {
            let proxy = reqwest::Proxy::all(proxy)
                .map_err(|_| ProviderError::new("configuration", "proxy gTTS inválido"))?;
            builder = builder.proxy(proxy);
        }
        let client = builder.build()
            .map_err(|error| ProviderError::new("configuration", error.without_url().to_string()))?;
        check_environment()?;
        Ok(client)
    }
}

type CachedClient = (Arc<ClientRoute>, Arc<OnceCell<reqwest::Client>>);

#[derive(Default)]
pub(crate) struct ClientPool {
    entries: Mutex<VecDeque<CachedClient>>,
}

impl ClientPool {
    fn entry(&self, route: ClientRoute) -> CachedClient {
        let mut entries = self.entries.lock().unwrap_or_else(|error| error.into_inner());
        let entry = entries.iter().position(|(current, _)| **current == route)
            .and_then(|index| entries.remove(index))
            .unwrap_or_else(|| (Arc::new(route), Arc::new(OnceCell::new())));
        entries.push_back(entry.clone());
        if entries.len() > MAX_CLIENTS {
            entries.pop_front();
        }
        entry
    }

    pub(crate) async fn synthesize(&self, request: &ProviderRequest, sink: &mut AudioSink, slots: &Arc<Semaphore>) -> Result<()> {
        google_fetch::check_cooldown()?;
        let (route, entry) = self.entry(ClientRoute::from_request(request)?);
        let hit = entry.initialized();
        let client = entry.get_or_try_init(|| async move {
            // Building a client may read OS CA certificates. Warm-route reads
            // stay outside the blocking queue and unrelated routes can proceed.
            tokio::task::spawn_blocking(move || route.build_client()).await
                .map_err(|_| ProviderError::new("configuration", "inicialização do cliente gTTS interrompida"))?
        }).await?;
        google::synthesize_with_client(client, request, sink, slots, Some(hit)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn route_retains_affinity_and_evicts_the_least_recently_used_client() {
        let pool = ClientPool::default();
        let request = ProviderRequest::default();
        let (_, first) = pool.entry(ClientRoute::from_request(&request).unwrap());
        let (_, again) = pool.entry(ClientRoute::from_request(&request).unwrap());
        assert!(Arc::ptr_eq(&first, &again));

        let cases = [
            ProviderRequest { source_ip: Some("127.0.0.1".parse().unwrap()), ..request.clone() },
            ProviderRequest { proxy: Some("http://127.0.0.1:80".into()), ..request.clone() },
            ProviderRequest { tld: "com.br".into(), ..request.clone() },
            ProviderRequest { connect_timeout_seconds: 3.0, ..request.clone() },
            ProviderRequest { receive_timeout_seconds: 10.0, ..request.clone() },
        ];
        for changed in cases {
            let (_, entry) = pool.entry(ClientRoute::from_request(&changed).unwrap());
            assert!(!Arc::ptr_eq(&first, &entry));
        }
        let mut changed = ClientRoute::from_request(&request).unwrap();
        changed.environment.push(Some(OsString::from("different")));
        let (_, entry) = pool.entry(changed);
        assert!(!Arc::ptr_eq(&first, &entry));

        for n in 0..MAX_CLIENTS {
            let changed = ProviderRequest { tld: format!("route-{n}"), ..request.clone() };
            pool.entry(ClientRoute::from_request(&changed).unwrap());
        }
        assert_eq!(pool.entries.lock().unwrap().len(), MAX_CLIENTS);
        let (_, reopened) = pool.entry(ClientRoute::from_request(&request).unwrap());
        assert!(!Arc::ptr_eq(&first, &reopened));
    }
}
