//! Google Translate speech protocol compatible with gTTS 2.5.4.
use crate::{google_fetch::synthesize_token, segments, AudioSink, ProviderError, ProviderRequest, Result};
use base64::Engine;
use std::sync::{Arc, OnceLock};
use tokio::sync::Semaphore;

const PUNCTUATION: &str = "?!？！.,¡()[]¿…‥،;:—。，、：\n";
fn clean(text: &str) -> Option<String> {
    if text.chars().all(|c| PUNCTUATION.contains(c) || " \t\n\r\u{000b}\u{000c}".contains(c)) { None }
    else { Some(text.trim().to_owned()) }
}

fn normalize_text(text: &str) -> String {
    static ABBREVIATIONS: OnceLock<regex::Regex> = OnceLock::new();
    let mut text = text.trim().to_owned();
    for mark in ['?', '!', '？', '！'] { text = text.replace(mark, &format!("{mark} ")); }
    text = text.replace("-\n", "");
    let regex = ABBREVIATIONS.get_or_init(|| regex::Regex::new(r"(?i)(dr|jr|mr|mrs|ms|msgr|prof|sr|st)\.").unwrap());
    text = regex.replace_all(&text, "$1").into_owned();
    // PreProcessorSub is case-insensitive and matches literal substrings.
    static SUB: OnceLock<regex::Regex> = OnceLock::new();
    SUB.get_or_init(|| regex::Regex::new(r"(?i)Esq\.").unwrap()).replace_all(&text, "Esquire").into_owned()
}

fn tokenize_normalized(text: &str) -> Result<Vec<String>> {
    static BOUNDARIES: OnceLock<fancy_regex::Regex> = OnceLock::new();
    if text.chars().count() <= 100 { return Ok(clean(text).into_iter().collect()); }
    let regex = BOUNDARIES.get_or_init(|| fancy_regex::Regex::new(
        r"(?i)(?<=[?!？！]).|(?<!\.[a-z])[.,] |(?<!\d):|[¡()\[\]¿…‥،;—。，、：\n]"
    ).unwrap());
    let mut tokens = Vec::new();
    let mut start = 0;
    let mut raw = Vec::new();
    for found in regex.find_iter(text) {
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

pub fn tokenize(text: &str) -> Result<Vec<String>> {
    tokenize_normalized(&normalize_text(text))
}

fn tokenize_for_startup(text: &str, enabled: bool) -> Result<(Vec<String>, bool)> {
    let normalized = normalize_text(text);
    if enabled {
        if let Some((lead, remainder)) = segments::startup_split(&normalized) {
            let mut prefix = tokenize_normalized(lead)?;
            let suffix = tokenize_normalized(remainder)?;
            if !prefix.is_empty() && !suffix.is_empty() {
                prefix.extend(suffix);
                return Ok((prefix, true));
            }
        }
    }
    Ok((tokenize_normalized(&normalized)?, false))
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

pub(crate) fn validate_tld(tld: &str) -> Result<()> {
    if tld.is_empty() || tld.len() > 64 || tld.split('.').any(|s| s.is_empty() || !s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')) {
        return Err(ProviderError::new("validation", "domínio gTTS inválido"));
    }
    Ok(())
}

pub(crate) async fn synthesize_with_client(
    client: &reqwest::Client,
    request: &ProviderRequest,
    sink: &mut AudioSink,
    slots: &Arc<Semaphore>,
    client_pool_hit: Option<bool>,
) -> Result<()> {
    validate_tld(&request.tld)?;
    let mut language = request.language.to_lowercase().replace('_', "-");
    if language == "pt-br" {
        language = "pt".into();
    }
    if language.is_empty() || !language.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-') {
        return Err(ProviderError::new("validation", "idioma gTTS inválido"));
    }
    let (tokens, split) = tokenize_for_startup(&request.text, request.startup_segments)?;
    if tokens.is_empty() { return Err(ProviderError::new("empty", "texto sem conteúdo pronunciável")); }
    if split {
        return segments::stream_ordered(tokens, sink, slots, |token, mut child| {
            let client = client.clone();
            let request = request.clone();
            let language = language.clone();
            async move {
                synthesize_token(&client, &request, &language, &token, &mut child, client_pool_hit).await
            }
        }).await;
    }
    for token in &tokens {
        synthesize_token(client, request, &language, token, sink, client_pool_hit).await?;
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
    fn startup_uses_the_short_first_part_and_gtts_size_limit() {
        let lead = "Agora começa uma frase suficientemente longa.";
        let tail = " Neste trecho existe uma série de palavras com letras acentuadas e cada uma delas precisa caber no limite de caracteres do provedor.";
        let text = format!("{lead}{tail}");
        let (tokens, split) = tokenize_for_startup(&text, true).unwrap();
        assert!(split);
        assert_eq!(tokens[0], lead);
        assert!(tokens.iter().all(|token| token.chars().count() <= 100));
        assert_eq!(
            tokenize_for_startup(&text, false).unwrap(),
            (tokenize(&text).unwrap(), false),
        );
        assert_eq!(
            tokenize_for_startup("Olá, tudo bem?", true).unwrap(),
            (tokenize("Olá, tudo bem?").unwrap(), false),
        );
    }
    #[test]
    fn protocol_parses_base64_and_rejects_bad_rpc() {
        let line = serde_json::to_vec(&serde_json::json!([["wrb.fr", "jQ1olc", "[\"bXAz\"]", null]])).unwrap();
        assert_eq!(audio_from_line(&line).unwrap(), vec![b"mp3".to_vec()]);
        assert!(audio_from_line(br#"[["wrb.fr","jQ1olc","[]"]]"#).is_err());
        assert!(audio_from_line(b"123").unwrap().is_empty());
    }
}
