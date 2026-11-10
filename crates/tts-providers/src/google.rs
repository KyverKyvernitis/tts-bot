//! Google Translate speech protocol compatible with gTTS 2.5.4.
use crate::{AudioSink, ProviderError, ProviderRequest, Result};
use base64::Engine;
use std::sync::OnceLock;

const PUNCTUATION: &str = "?!？！.,¡()[]¿…‥،;:—。，、：\n";
fn clean(text: &str) -> Option<String> {
    if text.chars().all(|c| PUNCTUATION.contains(c) || " \t\n\r\u{000b}\u{000c}".contains(c)) { None }
    else { Some(text.trim().to_owned()) }
}

pub fn tokenize(text: &str) -> Result<Vec<String>> {
    static ABBREVIATIONS: OnceLock<regex::Regex> = OnceLock::new();
    static BOUNDARIES: OnceLock<fancy_regex::Regex> = OnceLock::new();
    let mut text = text.trim().to_owned();
    for mark in ['?', '!', '？', '！'] { text = text.replace(mark, &format!("{mark} ")); }
    text = text.replace("-\n", "");
    let regex = ABBREVIATIONS.get_or_init(|| regex::Regex::new(r"(?i)(dr|jr|mr|mrs|ms|msgr|prof|sr|st)\.").unwrap());
    text = regex.replace_all(&text, "$1").into_owned();
    // PreProcessorSub is case-insensitive and matches literal substrings.
    static SUB: OnceLock<regex::Regex> = OnceLock::new();
    text = SUB.get_or_init(|| regex::Regex::new(r"(?i)Esq\.").unwrap()).replace_all(&text, "Esquire").into_owned();
    if text.chars().count() <= 100 { return Ok(clean(&text).into_iter().collect()); }
    let regex = BOUNDARIES.get_or_init(|| fancy_regex::Regex::new(
        r"(?i)(?<=[?!？！]).|(?<!\.[a-z])[.,] |(?<!\d):|[¡()\[\]¿…‥،;—。，、：\n]"
    ).unwrap());
    let mut tokens = Vec::new();
    let mut start = 0;
    let mut raw = Vec::new();
    for found in regex.find_iter(&text) {
        let found = found.map_err(|e| ProviderError::new("tokenizer", e.to_string()))?;
        raw.push(&text[start..found.start()]);
        start = found.end();
    }
    raw.push(&text[start..]);
    for token in raw.into_iter().filter_map(clean) {
        let mut chars: Vec<char> = token.chars().collect();
        loop {
            if chars.first() == Some(&' ') { chars.remove(0); }
            if chars.len() <= 100 {
                if !chars.is_empty() { tokens.push(chars.iter().collect()); }
                break;
            }
            let cut = chars[..100].iter().rposition(|c| *c == ' ').unwrap_or(100);
            if cut == 0 { return Err(ProviderError::new("tokenizer", "token inválido")); }
            tokens.push(chars[..cut].iter().collect());
            chars.drain(..cut);
        }
    }
    Ok(tokens)
}

pub fn rpc_body(text: &str, language: &str) -> Result<String> {
    let parameter = serde_json::to_string(&serde_json::json!([text, language, null, "null"]))
        .map_err(|e| ProviderError::new("protocol", e.to_string()))?;
    let rpc = serde_json::to_string(&serde_json::json!([[["jQ1olc", parameter, null, "generic"]]]))
        .map_err(|e| ProviderError::new("protocol", e.to_string()))?;
    let encoded: String = url::form_urlencoded::byte_serialize(rpc.as_bytes()).collect();
    Ok(format!("f.req={}&", encoded.replace('+', "%20")))
}

/// Parse batchexecute JSON records, not an unrestricted base64 substring.
pub fn audio_from_line(line: &[u8]) -> Result<Vec<Vec<u8>>> {
    if !line.windows(6).any(|w| w == b"jQ1olc") { return Ok(Vec::new()); }
    let value: serde_json::Value = serde_json::from_slice(line)
        .map_err(|e| ProviderError::new("protocol", format!("resposta gTTS inválida: {e}")))?;
    let mut output = Vec::new();
    let records = value.as_array().ok_or_else(|| ProviderError::new("protocol", "gTTS não retornou registros"))?;
    for record in records {
        if record.get(1).and_then(|v| v.as_str()) != Some("jQ1olc") { continue; }
        let inner: serde_json::Value = serde_json::from_str(record.get(2).and_then(|v| v.as_str())
            .ok_or_else(|| ProviderError::new("protocol", "gTTS não retornou áudio"))?)
            .map_err(|e| ProviderError::new("protocol", e.to_string()))?;
        let encoded = inner.get(0).and_then(|v| v.as_str())
            .ok_or_else(|| ProviderError::new("protocol", "gTTS não retornou base64"))?;
        let bytes = base64::engine::general_purpose::STANDARD.decode(encoded)
            .map_err(|e| ProviderError::new("protocol", e.to_string()))?;
        if bytes.is_empty() { return Err(ProviderError::new("empty", "gTTS retornou áudio vazio")); }
        output.push(bytes);
    }
    if output.is_empty() { return Err(ProviderError::new("protocol", "RPC de áudio gTTS ausente")); }
    Ok(output)
}

pub async fn synthesize(client: &reqwest::Client, request: &ProviderRequest, sink: &mut AudioSink) -> Result<()> {
    if request.tld.is_empty() || request.tld.len() > 64 || request.tld.split('.').any(|s| s.is_empty() || !s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')) {
        return Err(ProviderError::new("validation", "domínio gTTS inválido"));
    }
    let language = request.language.to_lowercase().replace('_', "-");
    let language = if language == "pt-br" { "pt" } else { &language };
    if language.is_empty() || !language.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-') {
        return Err(ProviderError::new("validation", "idioma gTTS inválido"));
    }
    let tokens = tokenize(&request.text)?;
    if tokens.is_empty() { return Err(ProviderError::new("empty", "texto sem conteúdo pronunciável")); }
    let client = if request.source_ip.is_some() || request.proxy.is_some() {
        let mut builder = reqwest::Client::builder().connect_timeout(std::time::Duration::from_millis(3500));
        if let Some(ip) = request.source_ip { builder = builder.local_address(ip); }
        if let Some(proxy) = &request.proxy {
            builder = builder.proxy(reqwest::Proxy::all(proxy).map_err(|e| ProviderError::new("configuration", e.to_string()))?);
        }
        builder.build().map_err(|e| ProviderError::new("configuration", e.to_string()))?
    } else { client.clone() };
    for token in tokens {
        let url = format!("https://translate.google.{}/_/TranslateWebserverUi/data/batchexecute", request.tld);
        let mut response = client.post(&url)
            .header("Content-Type", "application/x-www-form-urlencoded;charset=utf-8")
            .header("Referer", format!("https://translate.google.{}/", request.tld))
            .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36")
            .body(rpc_body(&token, language)?).send().await
            .map_err(|e| ProviderError::new("network", e.to_string()))?
            .error_for_status().map_err(|e| ProviderError::new("http", e.to_string()))?;
        let mut pending = Vec::new();
        let mut audio = false;
        let mut received = 0usize;
        let response_limit = request.max_audio_bytes.saturating_mul(4) / 3 + 65_536;
        while let Some(chunk) = response.chunk().await.map_err(|e| ProviderError::new("network", e.to_string()))? {
            received = received.saturating_add(chunk.len());
            if received > response_limit { return Err(ProviderError::new("limit", "resposta gTTS grande demais")); }
            pending.extend_from_slice(&chunk);
            while let Some(index) = pending.iter().position(|v| *v == b'\n') {
                for data in audio_from_line(&pending[..index])? { sink.emit(data).await?; audio = true; }
                pending.drain(..=index);
            }
        }
        if !pending.is_empty() {
            for data in audio_from_line(&pending)? { sink.emit(data).await?; audio = true; }
        }
        if !audio { return Err(ProviderError::new("empty", "gTTS não enviou áudio")); }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_limit_counts_characters() {
        let parts = tokenize(&"á".repeat(201)).unwrap();
        assert_eq!(parts.iter().map(|p| p.chars().count()).collect::<Vec<_>>(), vec![100, 100, 1]);
    }
    #[test]
    fn decimal_abbreviation_and_preprocessing() {
        assert_eq!(tokenize(" Dr. Silva?!\nEsq. com-\npleto 10.5 U.S.A.").unwrap(),
            vec!["Dr Silva? ! \nEsquire completo 10.5 U.S.A."]);
    }
    #[test]
    fn protocol_parses_base64_and_rejects_bad_rpc() {
        let line = serde_json::to_vec(&serde_json::json!([["wrb.fr", "jQ1olc", "[\"bXAz\"]", null]])).unwrap();
        assert_eq!(audio_from_line(&line).unwrap(), vec![b"mp3".to_vec()]);
        assert!(audio_from_line(br#"[["wrb.fr","jQ1olc","[]"]]"#).is_err());
        assert!(audio_from_line(b"123").unwrap().is_empty());
    }
}
