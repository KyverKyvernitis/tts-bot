//! HTTP transport for one Google Translate speech RPC.
use crate::{google::{audio_from_line, rpc_body}, AudioSink, ProviderError, ProviderRequest, ProviderTiming, RateLimit, Result};
use bytes::BytesMut;
use std::time::Instant;

static RATE_LIMIT: RateLimit = RateLimit::new();

pub(crate) fn check_cooldown() -> Result<()> { RATE_LIMIT.check() }

fn network_error(error: reqwest::Error) -> ProviderError {
    let kind = if error.is_timeout() { "timeout" } else { "network" };
    ProviderError::new(kind, error.without_url().to_string())
}

fn elapsed_ms(started: Instant) -> f64 { started.elapsed().as_secs_f64() * 1000.0 }

pub(crate) async fn synthesize_token(
    client: &reqwest::Client,
    request: &ProviderRequest,
    language: &str,
    token: &str,
    sink: &mut AudioSink,
    client_pool_hit: Option<bool>,
) -> Result<()> {
    check_cooldown()?;
    let origin = format!("https://translate.google.{}", request.tld);
    let form = rpc_body(token, language)?;
    let started = Instant::now();
    let response = client.post(format!("{origin}/_/TranslateWebserverUi/data/batchexecute"))
        .header("Content-Type", "application/x-www-form-urlencoded;charset=utf-8")
        .header("Referer", format!("{origin}/"))
        .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36")
        .body(form).send().await.map_err(network_error)?;
    let headers_ms = elapsed_ms(started);
    if response.status().as_u16() == 429 {
        RATE_LIMIT.pause(response.headers().get("retry-after").and_then(|value| value.to_str().ok()));
    }
    let response = response.error_for_status().map_err(|error| {
        let mut provider = ProviderError::new("http", "resposta HTTP gTTS recusada");
        provider.status = error.status().map(|value| value.as_u16());
        provider
    })?;
    let http_version = match response.version() {
        reqwest::Version::HTTP_10 => Some("http/1.0"),
        reqwest::Version::HTTP_11 => Some("http/1.1"),
        reqwest::Version::HTTP_2 => Some("h2"),
        _ => None,
    };
    let timing = ProviderTiming { headers_ms: Some(headers_ms), http_version, client_pool_hit, ..Default::default() };
    let limit = request.max_audio_bytes.saturating_mul(4) / 3 + 65_536;
    read_audio(response, sink, timing, started, limit).await
}

async fn read_audio(
    mut response: reqwest::Response,
    sink: &mut AudioSink,
    mut timing: ProviderTiming,
    started: Instant,
    response_limit: usize,
) -> Result<()> {
    let mut pending = BytesMut::new();
    let mut scanned = 0;
    let mut received = 0usize;
    let mut audio = false;
    while let Some(chunk) = response.chunk().await.map_err(network_error)? {
        if chunk.is_empty() { continue; }
        timing.first_body_ms.get_or_insert_with(|| elapsed_ms(started));
        received = received.saturating_add(chunk.len());
        if received > response_limit {
            return Err(ProviderError::new("limit", "resposta gTTS grande demais"));
        }
        pending.extend_from_slice(&chunk);
        // Keep the scan cursor across reads. Large fragmented base64 records
        // get parsed once, after the complete JSON line is available.
        while let Some(offset) = pending[scanned..].iter().position(|&byte| byte == b'\n') {
            let line = pending.split_to(scanned + offset + 1);
            emit_line(&line[..line.len() - 1], sink, &mut audio, &mut timing, started).await?;
            scanned = 0;
        }
        scanned = pending.len();
    }
    if !pending.is_empty() {
        emit_line(&pending, sink, &mut audio, &mut timing, started).await?;
    }
    if !audio {
        return Err(ProviderError::new("empty", "gTTS não enviou áudio"));
    }
    Ok(())
}

async fn emit_line(line: &[u8], sink: &mut AudioSink, audio: &mut bool, timing: &mut ProviderTiming, started: Instant) -> Result<()> {
    for data in audio_from_line(line)? {
        if !*audio {
            timing.first_audio_ms = elapsed_ms(started);
            sink.record_startup(timing.clone());
            *audio = true;
        }
        sink.emit(data).await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, RwLock};
    use tokio::{io::{AsyncReadExt, AsyncWriteExt}, net::TcpListener, sync::{mpsc, oneshot}};

    #[tokio::test]
    async fn fragmented_google_record_is_emitted_once_before_a_truncated_body_fails() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        let (advance, hold_response) = oneshot::channel();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut header = Vec::new();
            while !header.ends_with(b"\r\n\r\n") {
                header.push(socket.read_u8().await.unwrap());
            }
            let line = serde_json::to_vec(&serde_json::json!([["wrb.fr", "jQ1olc", "[\"bXAz\"]", null]])).unwrap();
            let header = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", line.len() + 12);
            socket.write_all(header.as_bytes()).await.unwrap();
            socket.write_all(&line[..line.len()/2]).await.unwrap();
            hold_response.await.unwrap();
            socket.write_all(&line[line.len()/2..]).await.unwrap();
            socket.write_all(b"\n").await.unwrap();
        });
        let started = Instant::now();
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let response = client.get(url).send().await.unwrap();
        let (sender, mut receiver) = mpsc::channel(4);
        let metadata = Arc::new(RwLock::new(crate::ProviderMetadata::default()));
        let mut sink = AudioSink { sender, cancellation: tokio_util::sync::CancellationToken::new(),
            limit: 1024, size: 0, metadata: metadata.clone() };
        let job = tokio::spawn(async move {
            read_audio(response, &mut sink, ProviderTiming::default(), started, 1024).await
        });
        let _ = tokio::task::yield_now().await;
        assert!(receiver.try_recv().is_err());
        advance.send(()).unwrap();
        server.await.unwrap();
        let result = job.await.unwrap();
        assert_eq!(result.unwrap_err().kind, "network");
        match receiver.recv().await {
            Some(crate::Record::Audio(data)) => assert_eq!(data.as_ref(), b"mp3"),
            _ => panic!("complete Google audio record must arrive once"),
        }
        assert!(receiver.recv().await.is_none());
        let timing = metadata.read().unwrap().startup.clone().unwrap();
        assert!(timing.first_audio_ms >= timing.first_body_ms.unwrap());
    }
}
