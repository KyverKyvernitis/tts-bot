//! TTV effect markup with literal masks for Discord, URLs, quotations and code.
use regex::Regex;
use serde_json::{json, Value};
use std::sync::OnceLock;

fn normalize(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}
fn literal_characters(text: &str) -> (Vec<char>, Vec<bool>) {
    static PATTERNS: OnceLock<Vec<Regex>> = OnceLock::new();
    let patterns = PATTERNS.get_or_init(|| {
        vec![
            Regex::new(r"<(?:@!?|@&|#)\d+>|<a?:[A-Za-z0-9_]+:\d+>|<t:\d+(?::[tTdDfFR])?>").unwrap(),
            Regex::new(r"(?i)<?(?:https?://|www\.)\S+").unwrap(),
            Regex::new(r#""(?:\\.|[^"\\])*"|“[^”]*”"#).unwrap(),
        ]
    });
    let chars: Vec<char> = text.chars().collect();
    let mut protected = vec![false; chars.len()];
    for pattern in patterns {
        for matched in pattern.find_iter(text) {
            let a = text[..matched.start()].chars().count();
            let b = a + matched.as_str().chars().count();
            protected[a..b].fill(true);
        }
    }
    let mut offset = 0;
    while offset < chars.len() {
        if chars[offset] != '`' || protected[offset] {
            offset += 1;
            continue;
        }
        let mut end = offset;
        while end < chars.len() && chars[end] == '`' {
            end += 1;
        }
        let fence = &chars[offset..end];
        let closing = (end..=chars.len().saturating_sub(fence.len()))
            .find(|i| chars[*i..*i + fence.len()] == *fence);
        end = closing.map(|i| i + fence.len()).unwrap_or(chars.len());
        protected[offset..end].fill(true);
        offset = end;
    }
    let mut result = Vec::new();
    let mut literals = Vec::new();
    offset = 0;
    while offset < chars.len() {
        if chars[offset] == '\\'
            && !protected[offset]
            && offset + 1 < chars.len()
            && !protected[offset + 1]
            && "<>|~\\".contains(chars[offset + 1])
        {
            result.push(chars[offset + 1]);
            literals.push(true);
            offset += 2;
        } else {
            result.push(chars[offset]);
            literals.push(protected[offset]);
            offset += 1;
        }
    }
    (result, literals)
}
pub fn singing_request(text: &str) -> (String, bool) {
    let normalized = normalize(text);
    let (chars, mask) = literal_characters(&normalized);
    if chars.last() != Some(&'~') || mask.last() == Some(&true) {
        return (text.into(), false);
    }
    let literal: String = chars.iter().collect();
    let prefix = "/cantar";
    if literal.to_lowercase().starts_with(prefix)
        && (chars.len() == prefix.len()
            || chars.get(prefix.len()).is_some_and(|c| c.is_whitespace()))
    {
        let mut end = prefix.len();
        while end < chars.len() && chars[end].is_whitespace() {
            end += 1;
        }
        if !mask[..end].iter().any(|b| *b) {
            let clean = normalized
                .chars()
                .skip(end)
                .collect::<String>()
                .trim()
                .to_string();
            return (clean, true);
        }
    }
    (normalized, true)
}
fn segment(text: &str, fade_in: bool, fade_out: bool, hold: bool) -> Value {
    json!({"text":text,"fade_in":fade_in,"fade_out":fade_out,"hold":hold,"pause_after_ms":0})
}
pub fn parse(text: &str, singing: bool) -> Result<(String, Option<Vec<Value>>), String> {
    let (chars, literals) = literal_characters(&normalize(text));
    let mut units: Vec<(Vec<char>, Vec<bool>)> = Vec::new();
    let mut start = 0;
    while start < chars.len() {
        if chars[start].is_whitespace() {
            start += 1;
            continue;
        }
        let mut end = start;
        while end < chars.len() && !chars[end].is_whitespace() {
            end += 1;
        }
        let mut part = start;
        for offset in start..end {
            if chars[offset] == '|' && !literals[offset] {
                if part < offset {
                    units.push((
                        chars[part..offset].to_vec(),
                        literals[part..offset].to_vec(),
                    ));
                }
                units.push((vec!['|'], vec![false]));
                part = offset + 1;
            }
        }
        if part < end {
            units.push((chars[part..end].to_vec(), literals[part..end].to_vec()));
        }
        start = end;
    }
    static COMPARISON: OnceLock<Regex> = OnceLock::new();
    let comparison_re =
        COMPARISON.get_or_init(|| Regex::new(r"\A[\w.,+\-]+[<>]=?[\w.,+\-]+\z").unwrap());
    let mut result: Vec<Value> = Vec::new();
    let (mut has_effects, mut has_literal, mut boundary) = (false, false, false);
    for (index, (token, mask)) in units.iter().enumerate() {
        let active: Vec<bool> = token
            .iter()
            .enumerate()
            .map(|(i, c)| "<>|~".contains(*c) && !mask[i])
            .collect();
        let any_active = active.iter().any(|b| *b);
        let value: String = token.iter().collect();
        if singing && (value == "~" || value == "|") && !any_active {
            has_literal = true;
        }
        if value == "|" && any_active {
            if result.is_empty() || boundary || index == units.len() - 1 {
                return Err("Use | entre duas palavras, como: olá | tudo bem.".into());
            }
            result.last_mut().unwrap()["pause_after_ms"] = json!(180);
            if singing {
                result.push(segment("|", false, false, false));
            }
            boundary = true;
            has_effects = true;
            continue;
        }
        if !token.is_empty() && token.iter().all(|c| *c == '~') && active.iter().all(|b| *b) {
            if result.is_empty() || boundary {
                return Err("Use ~ depois de uma sílaba para continuar sua vogal.".into());
            }
            for _ in token {
                result.push(segment("~", false, false, true));
            }
            has_effects = true;
            continue;
        }
        let spaced = (value == "<" || value == ">")
            && index > 0
            && index < units.len() - 1
            && !matches!(
                units[index - 1].0.iter().collect::<String>().as_str(),
                "|" | "<" | ">" | "~"
            )
            && !matches!(
                units[index + 1].0.iter().collect::<String>().as_str(),
                "|" | "<" | ">" | "~"
            );
        if spaced || comparison_re.is_match(&value) {
            result.push(segment(&value, false, false, false));
            boundary = false;
            continue;
        }
        let mut left = 0;
        let mut right = token.len();
        while right > left && !mask[right - 1] && ".,!?;:)]}".contains(token[right - 1]) {
            right -= 1;
        }
        let punctuation: String = token[right..].iter().collect();
        let fade_in = active.first() == Some(&true) && token.first() == Some(&'<');
        if fade_in {
            left += 1;
        }
        let mut fade_out = false;
        let mut holds = 0;
        while right > left && active[right - 1] && ">~".contains(token[right - 1]) {
            if token[right - 1] == '>' {
                if fade_out {
                    return Err("Use apenas um > no final da palavra, como: olá>.".into());
                }
                fade_out = true;
            } else {
                holds += 1;
            }
            right -= 1;
        }
        if (left..right).any(|i| active[i] && token[i] != '~') {
            return Err("Use < antes da palavra, > ou ~ depois, e | entre palavras.".into());
        }
        let mut pieces: Vec<(String, usize)> = Vec::new();
        let mut part = left;
        for offset in left..right {
            if !active[offset] || token[offset] != '~' {
                continue;
            }
            if part < offset {
                pieces.push((token[part..offset].iter().collect(), 0));
            }
            if let Some(previous) = pieces.last_mut() {
                previous.1 += 1;
            } else {
                if result.is_empty() || boundary {
                    return Err("Use ~ depois de uma sílaba para continuar sua vogal.".into());
                }
                result.push(segment("~", false, false, true));
                has_effects = true;
            }
            part = offset + 1;
        }
        if part < right {
            pieces.push((token[part..right].iter().collect(), 0));
        }
        if pieces.is_empty() {
            if fade_in || fade_out || holds > 0 || any_active {
                return Err("O efeito precisa de uma palavra: <olá, olá> ou olá~.".into());
            }
            pieces.push((String::new(), 0));
        }
        let last = pieces.last_mut().unwrap();
        last.0.push_str(&punctuation);
        last.1 += holds;
        if (fade_in || fade_out || holds > 0 || any_active)
            && !pieces
                .iter()
                .any(|(s, _)| s.chars().any(char::is_alphanumeric))
        {
            return Err("O efeito precisa de uma palavra: <olá, olá> ou olá~.".into());
        }
        for (number, (word, count)) in pieces.iter().enumerate() {
            result.push(segment(
                word,
                fade_in && number == 0,
                fade_out && number == pieces.len() - 1,
                *count > 0,
            ));
            for _ in 1..*count {
                result.push(segment("~", false, false, true));
            }
        }
        has_effects =
            has_effects || fade_in || fade_out || pieces.iter().any(|(_, count)| *count > 0);
        boundary = false;
    }
    let clean = result
        .iter()
        .map(|v| v["text"].as_str().unwrap())
        .collect::<Vec<_>>()
        .join(" ");
    if !has_effects && !has_literal {
        return Ok((clean, None));
    }
    if result.len() > 128 {
        return Err("Use até 128 trechos por apresentação do TTV.".into());
    }
    if !singing && clean.chars().count() > 180 {
        return Err("Para usar efeitos na fala, envie até 180 caracteres por mensagem.".into());
    }
    Ok((clean, Some(result)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn literal_and_effects() {
        assert_eq!(parse("https://example.org/a~", false).unwrap().1, None);
        assert!(!singing_request("`olá~`").1);
        assert!(singing_request("/cantar olá~").1);
        let (clean, parts) = parse("<olá~ mundo> | fim", false).unwrap();
        assert_eq!(clean, "olá mundo fim");
        let parts = parts.unwrap();
        assert_eq!(parts[0]["hold"], true);
        assert_eq!(parts[1]["pause_after_ms"], 180);
        assert!(parse("~olá", false).is_err());
    }
    #[test]
    fn compact_continuations() {
        let (a, x) = parse("a~~mor~!", true).unwrap();
        assert_eq!(a, "a ~ mor!");
        assert_eq!(x.unwrap().len(), 3);
        assert!(parse("olá || mundo", true).is_err());
    }
}
