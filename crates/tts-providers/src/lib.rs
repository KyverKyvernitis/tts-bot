//! Native, bounded provider transports. An explicit terminal record distinguishes
//! successful EOF from a disconnected or cancelled synthesis.
pub mod edge;
pub mod edge_token;
pub mod google;

use bytes::Bytes;
use serde::{Deserialize, Serialize};
use std::{fmt, sync::{Arc, RwLock}, time::Duration};
use tokio::sync::{mpsc, OwnedSemaphorePermit, Semaphore};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ProviderRequest {
    pub engine: String,
    pub text: String,
    pub voice: String,
    pub language: String,
    pub rate: String,
    pub pitch: String,
    pub tld: String,
    pub timeout_seconds: f64,
    pub connect_timeout_seconds: f64,
    pub receive_timeout_seconds: f64,
    pub max_audio_bytes: usize,
    pub endpoint: String,
    pub source_ip: Option<std::net::IpAddr>,
    pub proxy: Option<String>,
    pub android_base_url: String,
    pub android_payload: Option<serde_json::Value>,
}

impl Default for ProviderRequest {
    fn default() -> Self {
        Self { engine: "gtts".into(), text: String::new(),
            voice: "pt-BR-FranciscaNeural".into(), language: "pt".into(),
            rate: "+0%".into(), pitch: "+0Hz".into(), tld: "com".into(),
            timeout_seconds: 20.0, connect_timeout_seconds: 4.0,
            receive_timeout_seconds: 15.0, max_audio_bytes: 8 * 1024 * 1024,
            endpoint: "services".into(), source_ip: None, proxy: None,
            android_base_url: "http://127.0.0.1:8877".into(), android_payload: None }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ProviderError { pub kind: &'static str, pub message: String, pub status: Option<u16> }
impl ProviderError {
    pub fn new(kind: &'static str, message: impl Into<String>) -> Self {
        Self { kind, message: message.into(), status: None }
    }
    pub fn with_status(mut self, status: u16) -> Self { self.status = Some(status); self }
}
impl fmt::Display for ProviderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind, self.message)
    }
}
impl std::error::Error for ProviderError {}
pub type Result<T> = std::result::Result<T, ProviderError>;

enum Record { Audio(Bytes), Complete, Failed(ProviderError) }

#[derive(Clone, Debug, Default, Serialize)]
pub struct ProviderMetadata {
    pub engine: String,
    pub audio_format: String,
    pub content_type: String,
    pub voice: String,
    pub locale: String,
    pub android_synth_ms: String,
    pub sha256: String,
}

pub struct NativeStream {
    receiver: mpsc::Receiver<Record>,
    cancellation: CancellationToken,
    terminal: bool,
    failure: Option<ProviderError>,
    metadata: Arc<RwLock<ProviderMetadata>>,
}
impl NativeStream {
    pub async fn next(&mut self) -> Result<Option<Bytes>> {
        if self.cancellation.is_cancelled() {
            return Err(ProviderError::new("cancelled", "TTS cancelado"));
        }
        if let Some(error) = &self.failure { return Err(error.clone()); }
        if self.terminal { return Ok(None); }
        let record = tokio::select! {
            biased;
            _ = self.cancellation.cancelled() => return Err(ProviderError::new("cancelled", "TTS cancelado")),
            record = self.receiver.recv() => record,
        };
        match record {
            Some(Record::Audio(data)) => Ok(Some(data)),
            Some(Record::Complete) => { self.terminal = true; Ok(None) },
            Some(Record::Failed(error)) => { self.failure = Some(error.clone()); Err(error) },
            None => {
                let error = ProviderError::new("incomplete", "provider encerrou sem confirmar conclusão");
                self.failure = Some(error.clone()); Err(error)
            }
        }
    }
    pub fn cancel(&self) { self.cancellation.cancel(); }
    pub fn cancellation(&self) -> CancellationToken { self.cancellation.clone() }
    pub fn metadata(&self) -> ProviderMetadata { self.metadata.read().unwrap_or_else(|e| e.into_inner()).clone() }
}
impl Drop for NativeStream { fn drop(&mut self) { self.cancellation.cancel(); } }

pub struct AudioSink {
    sender: mpsc::Sender<Record>, cancellation: CancellationToken,
    limit: usize, size: usize,
    metadata: Arc<RwLock<ProviderMetadata>>,
}
impl AudioSink {
    pub async fn emit(&mut self, data: impl Into<Bytes>) -> Result<()> {
        let data = data.into();
        if self.size.checked_add(data.len()).is_none_or(|n| n > self.limit) {
            return Err(ProviderError::new("limit", "áudio TTS excedeu o orçamento de bytes"));
        }
        for part in data.chunks(16_384) {
            if part.is_empty() { continue; }
            tokio::select! {
                biased;
                _ = self.cancellation.cancelled() => return Err(ProviderError::new("cancelled", "TTS cancelado")),
                sent = self.sender.send(Record::Audio(Bytes::copy_from_slice(part))) => {
                    sent.map_err(|_| ProviderError::new("cancelled", "consumidor TTS encerrado"))?;
                    self.size += part.len();
                }
            }
        }
        Ok(())
    }
    pub fn size(&self) -> usize { self.size }
}

#[derive(Clone)]
pub struct ProviderPool {
    client: reqwest::Client,
    edge: Arc<Semaphore>, google: Arc<Semaphore>, android: Arc<Semaphore>,
}
impl ProviderPool {
    pub async fn save(&self, request: ProviderRequest, target: &std::path::Path) -> Result<ProviderMetadata> {
        use tokio::io::AsyncWriteExt;
        struct Staging(std::path::PathBuf);
        impl Drop for Staging { fn drop(&mut self) { let _ = std::fs::remove_file(&self.0); } }
        let parent = target.parent().unwrap_or_else(|| std::path::Path::new("."));
        let staging = Staging(parent.join(format!(".tts-provider-{}.part", uuid::Uuid::new_v4().simple())));
        let mut options = tokio::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)] options.mode(0o600);
        let mut output = options.open(&staging.0).await.map_err(|e| ProviderError::new("io", e.to_string()))?;
        let mut stream = self.start(request).await?;
        while let Some(data) = stream.next().await? {
            output.write_all(&data).await.map_err(|e| ProviderError::new("io", e.to_string()))?;
        }
        output.flush().await.map_err(|e| ProviderError::new("io", e.to_string()))?;
        output.sync_all().await.map_err(|e| ProviderError::new("io", e.to_string()))?;
        drop(output);
        tokio::fs::rename(&staging.0, target).await.map_err(|e| ProviderError::new("io", e.to_string()))?;
        Ok(stream.metadata())
    }
    pub fn new(edge_concurrency: usize, google_concurrency: usize) -> Result<Self> {
        if edge_concurrency == 0 || google_concurrency == 0 {
            return Err(ProviderError::new("configuration", "concorrência precisa ser positiva"));
        }
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_millis(3500))
            .pool_idle_timeout(Duration::from_secs(90))
            .user_agent("CoreTTS-Rust/0.1")
            .build().map_err(|e| ProviderError::new("configuration", e.to_string()))?;
        Ok(Self { client, edge: Arc::new(Semaphore::new(edge_concurrency)),
            google: Arc::new(Semaphore::new(google_concurrency)), android: Arc::new(Semaphore::new(2)) })
    }
    pub fn client(&self) -> &reqwest::Client { &self.client }
    pub async fn voices(&self) -> Result<serde_json::Value> {
        self.client.get(edge::VOICE_LIST).send().await
            .map_err(|e| ProviderError::new("network", e.to_string()))?
            .error_for_status().map_err(|e| ProviderError::new("http", e.to_string()))?
            .json().await.map_err(|e| ProviderError::new("protocol", e.to_string()))
    }
    pub async fn start(&self, request: ProviderRequest) -> Result<NativeStream> {
        if request.text.trim().is_empty() || request.text.chars().count() > 100_000 {
            return Err(ProviderError::new("validation", "tamanho de texto inválido"));
        }
        if !request.timeout_seconds.is_finite() || !(0.001..=3600.0).contains(&request.timeout_seconds)
            || !request.connect_timeout_seconds.is_finite() || !(0.001..=3600.0).contains(&request.connect_timeout_seconds)
            || !request.receive_timeout_seconds.is_finite() || !(0.001..=3600.0).contains(&request.receive_timeout_seconds)
            || request.max_audio_bytes == 0 || request.max_audio_bytes > 256 * 1024 * 1024 {
            return Err(ProviderError::new("validation", "prazo ou orçamento inválido"));
        }
        let semaphore = match request.engine.as_str() {
            "edge" => &self.edge, "gtts" => &self.google,
            "android_native" | "android-native" => &self.android,
            _ => return Err(ProviderError::new("validation", "motor nativo desconhecido")),
        };
        let permit = semaphore.clone().try_acquire_owned()
            .map_err(|_| ProviderError::new("busy", "transporte TTS ocupado"))?;
        let cancellation = CancellationToken::new();
        let (sender, receiver) = mpsc::channel(8);
        let android = matches!(request.engine.as_str(), "android_native" | "android-native");
        let metadata = Arc::new(RwLock::new(ProviderMetadata {
            engine: request.engine.clone(), audio_format: if android { "wav" } else { "mp3" }.into(),
            content_type: if android { "audio/wav" } else { "audio/mpeg" }.into(), ..Default::default()
        }));
        let sink = AudioSink { sender, cancellation: cancellation.clone(), limit: request.max_audio_bytes, size: 0, metadata: metadata.clone() };
        let pool = self.clone();
        let stop = cancellation.clone();
        tokio::spawn(async move { pool.run(request, sink, stop, permit).await; });
        Ok(NativeStream { receiver, cancellation, terminal: false, failure: None, metadata })
    }
    async fn run(&self, request: ProviderRequest, mut sink: AudioSink, stop: CancellationToken, _permit: OwnedSemaphorePermit) {
        let duration = Duration::from_secs_f64(request.timeout_seconds);
        let result = tokio::select! {
            biased;
            _ = stop.cancelled() => Err(ProviderError::new("cancelled", "TTS cancelado")),
            timed = tokio::time::timeout(duration, async {
                match request.engine.as_str() {
                    "edge" => edge::synthesize(&request, &mut sink).await,
                    "gtts" => google::synthesize(&self.client, &request, &mut sink).await,
                    _ => self.android(&request, &mut sink).await,
                }
            }) => timed.unwrap_or_else(|_| Err(ProviderError::new("timeout", "prazo total do TTS esgotado"))),
        };
        let record = match result {
            Ok(()) if sink.size > 0 => Record::Complete,
            Ok(()) => Record::Failed(ProviderError::new("empty", "engine não enviou áudio")),
            Err(error) => Record::Failed(error),
        };
        // A dropped or cancelled consumer cannot leave the physical permit held.
        tokio::select! { biased; _ = stop.cancelled() => {}, _ = sink.sender.send(record) => {} }
    }
    async fn android(&self, request: &ProviderRequest, sink: &mut AudioSink) -> Result<()> {
        let payload = request.android_payload.clone().unwrap_or_else(|| serde_json::json!({
            "text": request.text, "language": request.language, "locale": request.language,
            "voice": request.voice, "rate": request.rate, "pitch": request.pitch,
            "timeout_ms": (request.timeout_seconds * 1000.0) as u64,
            "max_audio_bytes": request.max_audio_bytes, "prefer_local_voice": true
        }));
        let base = request.android_base_url.trim_end_matches('/');
        let raw = self.client.post(format!("{base}/native-tts/synthesize.raw")).json(&payload).send().await;
        if let Ok(response) = raw {
            if response.status().is_success() && !response.headers().get("content-type")
                .and_then(|v| v.to_str().ok()).unwrap_or("").contains("json") {
                let header = |name: &str| response.headers().get(name).and_then(|v| v.to_str().ok()).unwrap_or("").to_owned();
                {
                    let mut meta = sink.metadata.write().unwrap_or_else(|e| e.into_inner());
                    let format = header("X-Core-Worker-Audio-Format");
                    if !format.is_empty() { meta.audio_format = format; }
                    meta.content_type = header("content-type");
                    meta.voice = header("X-Core-Worker-Voice");
                    meta.locale = header("X-Core-Worker-Locale");
                    meta.android_synth_ms = header("X-Core-Worker-Android-Synth-Ms");
                    meta.sha256 = header("X-Core-Worker-Sha256");
                }
                return read_audio(response, sink).await;
            }
        }
        // No audio has been emitted: the existing APK JSON compatibility path is safe.
        let response = self.client.post(format!("{base}/native-tts/synthesize")).json(&payload)
            .send().await.map_err(|e| ProviderError::new("network", e.to_string()))?
            .error_for_status().map_err(|e| ProviderError::new("http", e.to_string()))?;
        let limit = request.max_audio_bytes.saturating_mul(4) / 3 + 65_536;
        let raw = limited_response(response, limit).await?;
        let value: serde_json::Value = serde_json::from_slice(&raw).map_err(|e| ProviderError::new("protocol", e.to_string()))?;
        if value.get("ok").and_then(|v| v.as_bool()) == Some(false) {
            return Err(ProviderError::new("engine", value["error"].as_str().unwrap_or("Android TTS falhou")));
        }
        use base64::Engine;
        let data = base64::engine::general_purpose::STANDARD.decode(value["data_b64"].as_str().unwrap_or(""))
            .map_err(|e| ProviderError::new("protocol", e.to_string()))?;
        {
            let mut meta = sink.metadata.write().unwrap_or_else(|e| e.into_inner());
            if let Some(format) = value["audio_format"].as_str() { meta.audio_format = format.to_owned(); }
            meta.voice = value["voice"].as_str().unwrap_or("").into();
            meta.locale = value["locale"].as_str().unwrap_or("").into();
            meta.sha256 = value["sha256"].as_str().unwrap_or("").into();
            meta.android_synth_ms = value["android_synth_ms"].to_string();
        }
        sink.emit(data).await
    }
}

pub async fn limited_response(mut response: reqwest::Response, limit: usize) -> Result<Vec<u8>> {
    let mut output = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|e| ProviderError::new("network", e.to_string()))? {
        if output.len().saturating_add(chunk.len()) > limit {
            return Err(ProviderError::new("limit", "resposta excedeu o limite"));
        }
        output.extend_from_slice(&chunk);
    }
    Ok(output)
}
async fn read_audio(mut response: reqwest::Response, sink: &mut AudioSink) -> Result<()> {
    while let Some(data) = response.chunk().await.map_err(|e| ProviderError::new("network", e.to_string()))? {
        sink.emit(data).await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn disconnect_is_not_successful_eof() {
        let (sender, receiver) = mpsc::channel(1);
        sender.send(Record::Audio(Bytes::from_static(b"mp3"))).await.unwrap();
        drop(sender);
        let mut stream = NativeStream { receiver, cancellation: CancellationToken::new(), terminal: false, failure: None, metadata: Arc::default() };
        assert_eq!(stream.next().await.unwrap().unwrap(), b"mp3"[..]);
        assert_eq!(stream.next().await.unwrap_err().kind, "incomplete");
    }
    #[tokio::test]
    async fn cancellation_unblocks_full_queue() {
        let (sender, _receiver) = mpsc::channel(1);
        let token = CancellationToken::new();
        let mut sink = AudioSink { sender, cancellation: token.clone(), limit: 65536, size: 0, metadata: Arc::default() };
        sink.emit(b"one".to_vec()).await.unwrap();
        let handle = tokio::spawn(async move { sink.emit(b"two".to_vec()).await });
        token.cancel();
        assert_eq!(handle.await.unwrap().unwrap_err().kind, "cancelled");
    }
    #[tokio::test]
    async fn invalid_request_rejected_before_start() {
        let pool = ProviderPool::new(1, 1).unwrap();
        assert!(pool.start(ProviderRequest::default()).await.is_err());
        let mut request = ProviderRequest { text: "olá".into(), ..Default::default() };
        request.timeout_seconds = f64::NAN;
        assert!(pool.start(request).await.is_err());
    }
}
