//! Edge Read Aloud WebSocket transport. Both configured endpoints and a local
//! egress binding are per request; no process-global URL mutation is necessary.
use crate::{edge_token, AudioSink, ProviderError, ProviderRequest, Result};
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use std::{net::IpAddr, sync::{OnceLock, atomic::{AtomicI64, Ordering}}, time::{Duration, SystemTime, UNIX_EPOCH}};
use tokio::{io::{AsyncReadExt, AsyncWriteExt}, net::{TcpSocket, TcpStream}};
use tokio_tungstenite::{tungstenite::{client::IntoClientRequest, Message}, MaybeTlsStream, WebSocketStream};

const TOKEN: &str = "6A5AA1D4EAFF4E9FB37E23D68491D6F4";
pub const VOICE_LIST: &str = "https://speech.platform.bing.com/consumer/speech/synthesize/readaloud/voices/list?trustedclienttoken=6A5AA1D4EAFF4E9FB37E23D68491D6F4";
const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0";
static CLOCK_OFFSET: AtomicI64 = AtomicI64::new(0);

pub fn token_at_unix(seconds: u64) -> String {
    let seconds = u128::from(seconds) + 11_644_473_600;
    edge_token::generate_sec_ms_gec_v2((seconds - seconds % 300) * 10_000_000)
}
fn escape(text: &str) -> String {
    text.chars().map(|c| if matches!(c as u32, 0..=8 | 11..=12 | 14..=31) { ' ' } else { c })
        .fold(String::new(), |mut out, c| {
            match c { '&' => out.push_str("&amp;"), '<' => out.push_str("&lt;"), '>' => out.push_str("&gt;"), _ => out.push(c) };
            out
        })
}
pub fn text_parts(text: &str, max_bytes: usize) -> Vec<String> {
    let text = escape(text);
    let mut parts = Vec::new();
    let mut start = 0;
    while start < text.len() {
        let mut end = (start + max_bytes).min(text.len());
        while end > start && !text.is_char_boundary(end) { end -= 1; }
        if end == start { break; }
        if end < text.len() {
            if let Some(index) = text[start..end].rfind(' ') {
                if index > max_bytes / 3 { end = start + index; }
            }
            // An XML entity is indivisible, even when it crosses the byte limit.
            if let Some(index) = text[start..end].rfind('&') {
                if !text[start+index..end].contains(';') { end = start + index; }
            }
        }
        if end == start { break; }
        let part = text[start..end].trim();
        if !part.is_empty() { parts.push(part.to_owned()); }
        start = end;
        while start < text.len() && text[start..].starts_with(' ') { start += 1; }
    }
    parts
}
fn prosody(raw: &str, suffix: &str) -> Result<i32> {
    raw.strip_suffix(suffix).ok_or_else(|| ProviderError::new("validation", "controle Edge inválido"))?
        .parse::<i32>().map_err(|_| ProviderError::new("validation", "controle Edge inválido"))
}
pub fn ssml(request: &ProviderRequest, escaped_text: &str) -> Result<String> {
    if request.voice.is_empty() || !request.voice.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | ' ' | '(' | ')' | ',' | '.')) {
        return Err(ProviderError::new("validation", "voz Edge inválida"));
    }
    let rate = prosody(&request.rate, "%")?;
    let pitch = prosody(&request.pitch, "Hz")?;
    static SHORT_VOICE: OnceLock<regex::Regex> = OnceLock::new();
    let short_voice = SHORT_VOICE.get_or_init(|| regex::Regex::new(r"^([a-z]{2,})-([A-Z]{2,})-(.+Neural)$").unwrap());
    let voice = if let Some(parts) = short_voice.captures(&request.voice) {
        let lang = &parts[1];
        let region = &parts[2];
        let (extra, name) = parts[3].split_once('-').unwrap_or(("", &parts[3]));
        let region = if extra.is_empty() { region.to_owned() } else { format!("{region}-{extra}") };
        format!("Microsoft Server Speech Text to Speech Voice ({lang}-{region}, {name})")
    } else if request.voice.starts_with("Microsoft Server Speech Text to Speech Voice (") && request.voice.ends_with(')') && request.voice.contains(',') {
        request.voice.clone()
    } else { return Err(ProviderError::new("validation", "voz Edge inválida")); };
    Ok(format!("<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><voice name='{voice}'><prosody pitch='{pitch:+}Hz' rate='{rate:+}%' volume='+0%'>{escaped_text}</prosody></voice></speak>"))
}

fn javascript_date(now: SystemTime) -> String {
    let http = httpdate::fmt_http_date(now);
    let fields = http.split_whitespace().collect::<Vec<_>>();
    format!("{} {} {} {} {} GMT+0000 (Coordinated Universal Time)",
        fields[0].trim_end_matches(','), fields[2], fields[1], fields[3], fields[4])
}

fn endpoint_url(endpoint: &str) -> Result<url::Url> {
    let base = match endpoint {
        "services" => "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1",
        "bing" => "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1",
        value if value.starts_with("wss://") || value.starts_with("ws://127.0.0.1:") || value.starts_with("ws://localhost:") => value,
        _ => return Err(ProviderError::new("configuration", "endpoint Edge desconhecido")),
    };
    let mut url = url::Url::parse(base).map_err(|e| ProviderError::new("configuration", e.to_string()))?;
    let key = if endpoint == "services" { "Ocp-Apim-Subscription-Key" } else { "TrustedClientToken" };
    url.query_pairs_mut().append_pair(key, TOKEN);
    Ok(url)
}

fn proxy_for(url: &url::Url, explicit: Option<&str>) -> Option<String> {
    if let Some(proxy) = explicit { return Some(proxy.to_owned()); }
    let host = url.host_str().unwrap_or("");
    let bypass = std::env::var("no_proxy").or_else(|_| std::env::var("NO_PROXY")).unwrap_or_default();
    if bypass.split(',').any(|part| {
        let part = part.trim();
        let part = part.strip_prefix('.').unwrap_or(part);
        !part.is_empty() && (part == "*" || host == part || host.ends_with(&format!(".{part}")))
    }) { return None; }
    ["https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"].iter()
        .find_map(|key| std::env::var(key).ok().filter(|v| !v.is_empty()))
}

async fn connect_tcp(host: &str, port: u16, source: Option<IpAddr>) -> Result<TcpStream> {
    let addresses = tokio::net::lookup_host((host, port)).await
        .map_err(|e| ProviderError::new("network", e.to_string()))?;
    let mut last = "endereço de rede indisponível".to_owned();
    for address in addresses {
        if source.is_some_and(|ip| ip.is_ipv4() != address.is_ipv4()) { continue; }
        let socket = if address.is_ipv4() { TcpSocket::new_v4() } else { TcpSocket::new_v6() }
            .map_err(|e| ProviderError::new("network", e.to_string()))?;
        if let Some(ip) = source {
            socket.bind((ip, 0).into()).map_err(|e| ProviderError::new("source", e.to_string()))?;
        }
        match socket.connect(address).await {
            Ok(stream) => return Ok(stream), Err(error) => last = error.to_string(),
        }
    }
    Err(ProviderError::new("network", last))
}

async fn websocket(url: &url::Url, request: &ProviderRequest) -> Result<WebSocketStream<MaybeTlsStream<TcpStream>>> {
    let host = url.host_str().ok_or_else(|| ProviderError::new("configuration", "endpoint sem host"))?;
    let port = url.port_or_known_default().unwrap_or(if url.scheme() == "wss" { 443 } else { 80 });
    let stream = if let Some(proxy) = proxy_for(url, request.proxy.as_deref()) {
        if request.source_ip.is_some() {
            return Err(ProviderError::new("source", "proxy de ambiente incompatível com a origem Edge local explícita"));
        }
        let proxy = url::Url::parse(&proxy).map_err(|_| ProviderError::new("configuration", "proxy inválido"))?;
        if proxy.scheme() != "http" { return Err(ProviderError::new("configuration", "WebSocket nativo requer proxy HTTP CONNECT")); }
        let proxy_host = proxy.host_str().ok_or_else(|| ProviderError::new("configuration", "proxy sem host"))?;
        let mut stream = connect_tcp(proxy_host, proxy.port_or_known_default().unwrap_or(80), request.source_ip).await?;
        let target = format!("{host}:{port}");
        let auth = if !proxy.username().is_empty() {
            let value = format!("{}:{}", proxy.username(), proxy.password().unwrap_or(""));
            format!("Proxy-Authorization: Basic {}\r\n", base64::engine::general_purpose::STANDARD.encode(value))
        } else { String::new() };
        stream.write_all(format!("CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n").as_bytes()).await
            .map_err(|e| ProviderError::new("proxy", e.to_string()))?;
        let mut response = Vec::new();
        while !response.ends_with(b"\r\n\r\n") && response.len() < 16_384 {
            let byte = stream.read_u8().await.map_err(|e| ProviderError::new("proxy", e.to_string()))?;
            response.push(byte);
        }
        let first = String::from_utf8_lossy(&response).lines().next().unwrap_or("").to_owned();
        if first.split_whitespace().nth(1) != Some("200") || !response.ends_with(b"\r\n\r\n") {
            return Err(ProviderError::new("proxy", "HTTP CONNECT recusado"));
        }
        stream
    } else { connect_tcp(host, port, request.source_ip).await? };
    stream.set_nodelay(true).map_err(|e| ProviderError::new("network", e.to_string()))?;
    let mut handshake = url.as_str().into_client_request().map_err(|e| ProviderError::new("protocol", e.to_string()))?;
    let headers = handshake.headers_mut();
    headers.insert("User-Agent", USER_AGENT.parse().unwrap());
    headers.insert("Origin", "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold".parse().unwrap());
    headers.insert("Pragma", "no-cache".parse().unwrap());
    headers.insert("Cache-Control", "no-cache".parse().unwrap());
    headers.insert("Accept-Language", "en-US,en;q=0.9".parse().unwrap());
    let muid = uuid::Uuid::new_v4().simple().to_string().to_uppercase();
    headers.insert("Cookie", format!("muid={muid};").parse().unwrap());
    tokio_tungstenite::client_async_tls_with_config(handshake, stream, None, None).await
        .map(|(socket, _)| socket)
        .map_err(|e| {
            let mut error = ProviderError::new("edge_handshake", e.to_string());
            if let tokio_tungstenite::tungstenite::Error::Http(response) = &e {
                error.status = Some(response.status().as_u16());
                if response.status().as_u16() == 403 {
                    if let Some(date) = response.headers().get("date").and_then(|v| v.to_str().ok())
                        .and_then(|v| httpdate::parse_http_date(v).ok()) {
                        let server = date.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() as i64;
                        let local = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() as i64;
                        CLOCK_OFFSET.store(server.saturating_sub(local), Ordering::Relaxed);
                    }
                }
            }
            error
        })
}

pub fn binary_audio(data: &[u8]) -> Result<&[u8]> {
    if data.len() < 2 { return Err(ProviderError::new("protocol", "frame Edge sem cabeçalho")); }
    let size = u16::from_be_bytes([data[0], data[1]]) as usize;
    let end = size.checked_add(2).filter(|n| *n <= data.len())
        .ok_or_else(|| ProviderError::new("protocol", "cabeçalho Edge incompleto"))?;
    let header = std::str::from_utf8(&data[2..end]).map_err(|_| ProviderError::new("protocol", "cabeçalho Edge inválido"))?;
    if !header.split("\r\n").any(|line| line.trim() == "Path:audio") {
        return Err(ProviderError::new("protocol", "frame Edge não é áudio"));
    }
    Ok(&data[end..])
}

pub async fn synthesize(request: &ProviderRequest, sink: &mut AudioSink) -> Result<()> {
    let parts = text_parts(&request.text, 4096);
    if parts.is_empty() { return Err(ProviderError::new("empty", "texto Edge vazio")); }
    for part in parts {
        let mut connection = None;
        for attempt in 0..2 {
            let mut url = endpoint_url(&request.endpoint)?;
            let local = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() as i64;
            let now = local.saturating_add(CLOCK_OFFSET.load(Ordering::Relaxed)).max(0) as u64;
            url.query_pairs_mut().append_pair("ConnectionId", &uuid::Uuid::new_v4().simple().to_string())
                .append_pair("Sec-MS-GEC", &token_at_unix(now)).append_pair("Sec-MS-GEC-Version", "2-143.0.3650.75");
            let connect = tokio::time::timeout(Duration::from_secs_f64(request.connect_timeout_seconds), websocket(&url, request)).await
                .map_err(|_| ProviderError::new("timeout", "prazo de conexão Edge esgotado"))?;
            match connect {
                Ok(ws) => { connection = Some(ws); break; },
                Err(error) if error.status == Some(403) && attempt == 0 => continue,
                Err(error) => return Err(error),
            }
        }
        let mut ws = connection.ok_or_else(|| ProviderError::new("edge_handshake", "falha Edge após correção de relógio"))?;
        let timestamp = javascript_date(SystemTime::now());
        let config = format!("X-Timestamp:{timestamp}\r\nContent-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n{{\"context\":{{\"synthesis\":{{\"audio\":{{\"metadataoptions\":{{\"sentenceBoundaryEnabled\":\"false\",\"wordBoundaryEnabled\":\"true\"}},\"outputFormat\":\"audio-24khz-48kbitrate-mono-mp3\"}}}}}}}}\r\n");
        ws.send(Message::Text(config.into())).await.map_err(|e| ProviderError::new("network", e.to_string()))?;
        let payload = format!("X-RequestId:{}\r\nContent-Type:application/ssml+xml\r\nX-Timestamp:{timestamp}Z\r\nPath:ssml\r\n\r\n{}",
            uuid::Uuid::new_v4().simple(), ssml(request, &part)?);
        ws.send(Message::Text(payload.into())).await.map_err(|e| ProviderError::new("network", e.to_string()))?;
        let mut complete = false;
        let before = sink.size();
        loop {
            let message = tokio::time::timeout(Duration::from_secs_f64(request.receive_timeout_seconds), ws.next()).await
                .map_err(|_| ProviderError::new("timeout", "prazo de leitura Edge esgotado"))?;
            let Some(message) = message else { break; };
            match message.map_err(|e| ProviderError::new("network", e.to_string()))? {
                Message::Binary(data) => { sink.emit(binary_audio(&data)?.to_vec()).await?; }
                Message::Text(text) => {
                    let header = text.split("\r\n\r\n").next().unwrap_or("");
                    if header.split("\r\n").any(|line| line == "Path:turn.end") { complete = true; break; }
                }
                Message::Ping(data) => { ws.send(Message::Pong(data)).await.map_err(|e| ProviderError::new("network", e.to_string()))?; }
                Message::Close(_) => break,
                _ => {}
            }
        }
        if !complete { return Err(ProviderError::new("incomplete", "Edge desconectou antes de turn.end")); }
        if sink.size() == before { return Err(ProviderError::new("empty", "Edge não enviou áudio")); }
        // Closing cannot turn a complete synthesis into a second network wait.
        let _ = tokio::time::timeout(Duration::from_millis(100), ws.close(None)).await;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn binary_frames_are_validated_before_indexing() {
        assert!(binary_audio(&[]).is_err());
        assert!(binary_audio(&[0, 20, 1]).is_err());
        let header = b"Path:audio\r\nContent-Type:audio/mpeg\r\n";
        let mut data = (header.len() as u16).to_be_bytes().to_vec();
        data.extend_from_slice(header); data.extend_from_slice(b"mp3");
        assert_eq!(binary_audio(&data).unwrap(), b"mp3");
    }
    #[test]
    fn ssml_escapes_text_and_validates_attributes() {
        let request = ProviderRequest::default();
        assert!(ssml(&request, &text_parts("A < B & ç", 4096)[0]).unwrap().contains("A &lt; B &amp; ç"));
        assert!(ssml(&ProviderRequest { voice: "x' injected".into(), ..request }, "test").is_err());
        for part in text_parts(&"á&".repeat(5000), 4096) {
            assert!(part.len() <= 4096);
            assert!(!part.ends_with('&'));
        }
    }
    #[test]
    fn token_uses_five_minute_window_and_v2_shape() {
        let token = token_at_unix(1_793_746_800);
        assert_eq!(token.len(), 81);
        assert_eq!(&token[16..17], "-");
        assert_eq!(token, token_at_unix(1_793_746_801));
    }
}
