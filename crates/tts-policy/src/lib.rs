//! Pure TTS decisions and a bounded, generation-aware queue. No Python or I/O.
use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::OnceLock;
pub mod markup;
pub mod melody;
pub mod pending;

fn catalog() -> &'static Value {
    static CATALOG: OnceLock<Value> = OnceLock::new();
    CATALOG.get_or_init(|| {
        serde_json::from_str(include_str!("../../../shared/ttv_catalog.json"))
            .expect("bundled TTV catalog")
    })
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Number(n) => n.as_f64().unwrap_or(0.0) != 0.0,
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        _ => v.to_string(),
    }
}
fn get(v: &Value, key: &str) -> Value {
    v.get(key).cloned().unwrap_or(Value::Null)
}
fn string(v: &Value, default: &str) -> String {
    if truthy(v) {
        text(v)
    } else {
        default.into()
    }
}
fn pick(v: &Value, names: &[&str], default: &str) -> String {
    names
        .iter()
        .map(|k| get(v, k))
        .find(truthy)
        .map(|v| text(&v))
        .unwrap_or_else(|| default.into())
}
fn trimmed_pick(v: &Value, names: &[&str], default: &str) -> String {
    let selected = pick(v, names, default);
    if selected.trim().is_empty() {
        default.into()
    } else {
        selected.trim().into()
    }
}
fn signs(s: &str) -> String {
    s.replace(['−', '–', '—'], "-")
}
pub fn normalize_engine(raw: &Value, default: &str) -> String {
    let s = string(raw, default).trim().to_lowercase().replace('-', "_");
    match s.as_str() {
        "google" | "google_tts" | "googlecloud" | "google_cloud" | "gcloud" => "gtts".into(),
        "edge_tts" => "edge".into(),
        "android" | "android_tts" | "native" | "native_android" => "android_native".into(),
        "kasane_teto" | "teto_utau" | "utau" => "teto".into(),
        "" => default.into(),
        _ => s,
    }
}
pub fn normalize_edge(raw: &Value, pitch: bool) -> String {
    let mut s = signs(string(raw, "").trim())
        .replace(' ', "")
        .replace('％', "%");
    if pitch {
        if s.to_lowercase().ends_with("hz") {
            s.truncate(s.len() - 2);
        }
    } else if s.ends_with('%') {
        s.pop();
    }
    let suffix = if pitch { "Hz" } else { "%" };
    if !s.starts_with(['+', '-']) {
        s.insert(0, '+');
    }
    static DIGITS: OnceLock<Regex> = OnceLock::new();
    let digits=DIGITS.get_or_init(||Regex::new(r"\A[\p{Nd}\u{00B2}-\u{00B3}\u{00B9}\u{1369}-\u{1371}\u{19DA}\u{2070}\u{2074}-\u{2079}\u{2080}-\u{2089}\u{2460}-\u{2468}\u{2474}-\u{247C}\u{2488}-\u{2490}\u{24EA}\u{24F5}-\u{24FD}\u{24FF}\u{2776}-\u{277E}\u{2780}-\u{2788}\u{278A}-\u{2792}\u{10A40}-\u{10A43}\u{10E60}-\u{10E68}\u{11052}-\u{1105A}\u{1F100}-\u{1F10A}]+\z").unwrap());
    if s.len() < 2 || !digits.is_match(&s[1..]) {
        return format!("+0{suffix}");
    }
    format!("{s}{suffix}")
}
pub fn normalize_gtts(raw: &Value) -> String {
    let s = string(raw, "pt").trim().to_lowercase().replace('_', "-");
    match s.as_str() {
        "" | "pt-br" => "pt".into(),
        _ => s,
    }
}
pub fn sanitize_cache_key(raw: &Value) -> Result<String, String> {
    let s: String = string(raw, "")
        .trim()
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '_' || *c == '-')
        .take(96)
        .collect();
    if s.len() < 16 {
        Err("cache_key inválida/curta".into())
    } else {
        Ok(s)
    }
}
fn number(v: &Value) -> Option<f64> {
    if v.is_boolean() || v.is_null() {
        return None;
    }
    if let Some(n) = v.as_f64() {
        return Some(n);
    }
    text(v).trim().replace(',', ".").parse::<f64>().ok()
}
fn pitch_number(v: &Value) -> Option<f64> {
    let mut s = signs(&text(v).trim().to_lowercase()).replace(',', ".");
    for suffix in ["semitones", "semitone", "semitons", "semitom", "st"] {
        if s.ends_with(suffix) {
            s.truncate(s.len() - suffix.len());
            s = s.trim().into();
            break;
        }
    }
    if s.is_empty() {
        Some(0.0)
    } else {
        s.parse().ok()
    }
}
// Python's round() uses ties-to-even; f64::round() does not.
fn round_even(x: f64) -> f64 {
    let lo = x.floor();
    let fraction = x - lo;
    if fraction < 0.5 {
        lo
    } else if fraction > 0.5 || lo % 2.0 != 0.0 {
        lo + 1.0
    } else {
        lo
    }
}
pub fn normalize_pitch(v: &Value) -> Result<String, String> {
    let n = if v.is_null() {
        Some(0.0)
    } else {
        pitch_number(v)
    };
    let p = n.ok_or("O tom deve ser um número de −4 a +4 semitons.")?;
    let rounded = round_even(p * 2.0) / 2.0;
    if !p.is_finite() || !(-4.0..=4.0).contains(&p) || (p - rounded).abs() > 1e-9 {
        return Err("O tom deve ficar entre −4 e +4 semitons, em passos de 0,5.".into());
    }
    Ok(format!(
        "{:+.1}",
        if rounded == 0.0 { 0.0 } else { rounded }
    ))
}
pub fn normalize_worker_pitch(v: &Value, default: f64) -> f64 {
    let p = pitch_number(if v.is_null() || v == "" {
        &Value::Null
    } else {
        v
    })
    .filter(|n| n.is_finite())
    .unwrap_or(default);
    let rounded = round_even(p.clamp(-4.0, 4.0) * 2.0) / 2.0;
    if rounded == 0.0 {
        0.0
    } else {
        rounded
    }
}
pub fn normalize_voice(v: &Value, worker: bool) -> Result<String, String> {
    let mut s = if v.is_null() && !worker {
        "".into()
    } else {
        text(v).trim().to_lowercase()
    };
    if !worker {
        s = match s.as_str() {
            "" => catalog()["defaultVoiceId"].as_str().unwrap().into(),
            "teto" | "kasane_teto" | "kasane-teto-standard" | "kasane-teto-english-cvvc" => {
                "kasane-teto".into()
            }
            _ => s,
        };
    }
    if ["kasane-teto", "hatsune-miku", "anna-nyui", "pururu-purpura"].contains(&s.as_str()) {
        Ok(s)
    } else {
        Err(if worker {
            "vocaloid TTV desconhecida ou indisponível"
        } else {
            "Vocaloid desconhecida ou ainda indisponível."
        }
        .into())
    }
}
pub fn normalize_language(v: &Value, worker: bool) -> Result<String, String> {
    let s = if v.is_null() && !worker {
        "".into()
    } else {
        text(v).trim().to_lowercase().replace('_', "-")
    };
    match s.as_str() {
        "pt" | "pt-br" => Ok("pt-BR".into()),
        "en" | "en-us" => Ok("en-US".into()),
        "" if !worker => Ok("pt-BR".into()),
        _ => Err(if worker {
            "idioma TTV deve ser Português adaptado ou Inglês"
        } else {
            "Escolha Português adaptado ou Inglês para o idioma do TTV."
        }
        .into()),
    }
}
fn voice_language(voice: &str, language: &Value) -> Result<String, String> {
    let lang = normalize_language(language, false)?;
    let entry = catalog()["voices"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["id"].as_str() == Some(voice))
        .unwrap();
    if !entry["languages"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v.as_str() == Some(&lang))
    {
        Err(format!(
            "{} está disponível somente em Português brasileiro. Escolha pt-BR.",
            entry["name"].as_str().unwrap()
        ))
    } else {
        Ok(lang)
    }
}
pub fn normalize_rate(v: &Value, default: f64, worker: bool) -> Result<f64, String> {
    let v = if !worker && (v.is_null() || v.as_str().is_some_and(|s| s.trim().is_empty())) {
        json!(default)
    } else {
        v.clone()
    };
    let error = if worker {
        "velocidade TTV deve estar entre 0.5 e 1.5"
    } else {
        "A velocidade deve ficar entre 50% e 150%."
    };
    let n = number(&v)
        .filter(|n| n.is_finite() && (0.5..=1.5).contains(n))
        .ok_or(error)?;
    Ok(n)
}
fn present(v: &Value) -> bool {
    !v.is_null() && !text(v).trim().is_empty()
}
pub fn resolve_preferences(v: &Value, default: &Value) -> Result<Value, String> {
    let voice = normalize_voice(&get(v, "ttv_voice_id"), false)?;
    let language = voice_language(&voice, &get(v, "ttv_language"))?;
    let pitch = [
        get(v, "ttv_pitch_semitones"),
        get(v, "teto_pitch_semitones"),
        default.clone(),
    ]
    .iter()
    .filter(|p| present(p))
    .find_map(|p| normalize_pitch(p).ok())
    .unwrap_or_else(|| "+0.0".into());
    let rate = normalize_rate(&get(v, "ttv_speech_rate"), 1.0, false).unwrap_or(1.0);
    Ok(
        json!({"ttv_voice_id":voice,"ttv_language":language,"ttv_pitch_semitones":pitch,"ttv_speech_rate":rate}),
    )
}
pub fn inherit_preferences(personal: &Value, server: &Value, default: &Value) -> Value {
    let mut values = Map::new();
    for key in ["ttv_voice_id", "ttv_language"] {
        let p = get(personal, key);
        values.insert(key.into(), if present(&p) { p } else { get(server, key) });
    }
    if !present(&get(personal, "ttv_language")) {
        if let (Ok(voice), Ok(language)) = (
            normalize_voice(values.get("ttv_voice_id").unwrap(), false),
            normalize_language(values.get("ttv_language").unwrap(), false),
        ) {
            if voice == "pururu-purpura" && language != "pt-BR" {
                values.insert("ttv_language".into(), json!("pt-BR"));
            }
        }
    }
    for p in [
        get(personal, "ttv_pitch_semitones"),
        get(personal, "teto_pitch_semitones"),
        get(server, "ttv_pitch_semitones"),
        get(server, "teto_pitch_semitones"),
        default.clone(),
    ] {
        if present(&p) {
            if let Ok(pitch) = normalize_pitch(&p) {
                values.insert("ttv_pitch_semitones".into(), json!(pitch));
                break;
            }
        }
    }
    for p in [
        get(personal, "ttv_speech_rate"),
        get(server, "ttv_speech_rate"),
        json!(1.0),
    ] {
        if present(&p) {
            if let Ok(rate) = normalize_rate(&p, 1.0, false) {
                values.insert("ttv_speech_rate".into(), json!(rate));
                break;
            }
        }
    }
    Value::Object(values)
}
/// Resolve the normalized SettingsDB snapshots without owning its cache or Mongo.
pub fn resolve_tts(
    personal: &Value,
    server: &Value,
    default_pitch: &Value,
) -> Result<Value, String> {
    let choose = |key: &str, fallback: &str| -> String {
        for source in [personal, server] {
            let value = string(&get(source, key), "").trim().to_string();
            if !value.is_empty() {
                return value;
            }
        }
        fallback.into()
    };
    let mut engine = choose("engine", "gtts").to_lowercase().replace('-', "_");
    if ["atts", "android", "android_tts", "native", "native_android"].contains(&engine.as_str()) {
        engine = "android_native".into();
    }
    if !["android_native", "edge", "gtts"].contains(&engine.as_str()) {
        engine = "gtts".into();
    }
    let mut resolved = json!({"engine":engine,"voice":choose("voice","pt-BR-FranciscaNeural"),"language":choose("language","pt-br"),"rate":choose("rate","+0%"),"pitch":choose("pitch","+0Hz"),
        "android_voice":choose("android_voice",""),"android_language":choose("android_language","pt-BR"),"android_rate":choose("android_rate","1.0"),"android_pitch":choose("android_pitch","1.0"),
        "advanced_nightcore_level":string(&get(personal,"advanced_nightcore_level"),"0"),"advanced_slowed_level":string(&get(personal,"advanced_slowed_level"),"0"),"advanced_reverb_level":string(&get(personal,"advanced_reverb_level"),"0"),"speaker_name":string(&get(personal,"speaker_name"),""),
        "bot_prefix":string(&get(server,"bot_prefix"),"_"),"tts_prefix":string(&get(server,"tts_prefix"),","),"atts_prefix":string(&get(server,"atts_prefix"),"%"),"teto_prefix":string(&get(server,"teto_prefix"),"'"),"gtts_prefix":string(&get(server,"gtts_prefix"),"."),"edge_prefix":string(&get(server,"edge_prefix"),","),
        "speech_limit_seconds":get(server,"speech_limit_seconds").as_i64().filter(|n|*n!=0).unwrap_or(30),
        "ttv_melody_id":string(&get(personal,"ttv_melody_id"),"").trim(),"ttv_random_cd":get(personal,"ttv_random_cd")==Value::Bool(true)});
    let inherited = inherit_preferences(personal, server, default_pitch);
    let mut non_character = inherited.clone();
    non_character["ttv_voice_id"] = json!("");
    non_character["ttv_language"] = json!("");
    let mut prefs = resolve_preferences(&non_character, default_pitch)?;
    for (key, normalizer) in [
        (
            "ttv_voice_id",
            normalize_voice as fn(&Value, bool) -> Result<String, String>,
        ),
        ("ttv_language", normalize_language),
    ] {
        let value = get(&inherited, key);
        prefs[key] =
            json!(normalizer(&value, false).unwrap_or_else(|_| text(&value).trim().into()));
    }
    resolved
        .as_object_mut()
        .unwrap()
        .extend(prefs.as_object().unwrap().clone());
    resolved["teto_pitch_semitones"] = resolved["ttv_pitch_semitones"].clone();
    Ok(resolved)
}
pub fn effective_settings(v: &Value, default: f64) -> Result<Value, String> {
    let voice = normalize_voice(
        &v.get("ttv_voice_id")
            .cloned()
            .unwrap_or(json!("kasane-teto")),
        true,
    )?;
    let language = normalize_language(
        &v.get("ttv_language").cloned().unwrap_or(json!("pt-BR")),
        true,
    )?;
    if voice == "pururu-purpura" && language != "pt-BR" {
        return Err("Pururu Púrpura aceita somente português brasileiro (pt-BR)".into());
    }
    let scored = v.get("ttv_score").is_some_and(|s| !s.is_null());
    let pitch = if scored {
        0.0
    } else {
        normalize_worker_pitch(
            &v.get("ttv_pitch_semitones")
                .cloned()
                .unwrap_or(get(v, "teto_pitch_semitones")),
            0.0,
        )
    };
    let rate = if scored {
        1.0
    } else if let Some(r) = v.get("ttv_speech_rate") {
        normalize_rate(r, 1.0, true)?
    } else {
        if default.is_finite() {
            default.clamp(0.75, 1.5)
        } else {
            1.0
        }
    };
    Ok(
        json!({"ttv_voice_id":voice,"ttv_language":language,"ttv_pitch_semitones":pitch,"ttv_speech_rate":rate}),
    )
}
pub fn available_engines(v: &Value) -> Vec<String> {
    [
        ("teto_tts", "teto"),
        ("android_native_tts", "android_native"),
        ("edge_tts", "edge"),
        ("gtts", "gtts"),
    ]
    .into_iter()
    .filter(|(k, _)| truthy(&get(v, k)))
    .map(|(_, s)| s.into())
    .collect()
}
pub fn engine_order(body: &Value, available: &[String], default: &Value) -> Vec<String> {
    let requested = normalize_engine(&get(body, "engine"), "gtts");
    let preferred = normalize_engine(
        &if truthy(&get(body, "preferred_engine")) {
            get(body, "preferred_engine")
        } else {
            default.clone()
        },
        "auto",
    );
    let fallback = normalize_engine(&get(body, "fallback_engine"), "gtts");
    let mut order = Vec::new();
    if requested == "teto" {
        order.push("teto".into());
    } else if preferred != "auto" {
        order.push(preferred);
    }
    order.extend([
        requested,
        fallback,
        "android_native".into(),
        "edge".into(),
        "gtts".into(),
    ]);
    let mut seen = HashSet::new();
    order
        .into_iter()
        .filter(|s| {
            available.contains(s)
                && ["teto", "android_native", "edge", "gtts"].contains(&s.as_str())
                && seen.insert(s.clone())
        })
        .collect()
}
pub fn standard_cache_key(
    body: &Value,
    engine: &str,
    fingerprint: &str,
    base_pitch: &str,
    default_rate: f64,
    performance: Option<&str>,
) -> Result<String, String> {
    cache_key_with_normalizers(
        body,
        engine,
        fingerprint,
        base_pitch,
        default_rate,
        performance,
        None,
    )
}
fn cache_key_with_normalizers(
    body: &Value,
    engine: &str,
    fingerprint: &str,
    base_pitch: &str,
    default_rate: f64,
    performance: Option<&str>,
    normalizers: Option<&Value>,
) -> Result<String, String> {
    let engine = normalize_engine(
        &json!(if engine.is_empty() {
            pick(body, &["engine"], "gtts")
        } else {
            engine.into()
        }),
        "gtts",
    );
    let requested = normalize_engine(&get(body, "engine"), &engine);
    if requested == engine && engine != "teto" {
        if let Ok(key) = sanitize_cache_key(&get(body, "cache_key")) {
            return Ok(key);
        }
    }
    let mut t = pick(body, &["text"], "").trim().to_string();
    let payload = match engine.as_str() {
        "teto" => {
            if (body.get("ttv_score").is_some_and(|v| !v.is_null())
                || body.get("ttv_segments").is_some_and(|v| !v.is_null()))
                && performance.is_none()
            {
                return Err(
                    "performance must be validated before constructing its cache identity".into(),
                );
            }
            if performance.is_some() {
                t = t.split_whitespace().collect::<Vec<_>>().join(" ");
            }
            let controls = effective_settings(body, default_rate)?;
            let pitch = controls["ttv_pitch_semitones"].as_f64().unwrap();
            let part = if performance.is_some()
                || body.get("ttv_pitch_semitones").is_some()
                || body.get("teto_pitch_semitones").is_some()
            {
                format!("|pitch={pitch:+.1}")
            } else {
                String::new()
            };
            let mut p = format!(
                "teto|{}|{}|{}|{}{part}|{t}",
                if fingerprint.is_empty() {
                    "unavailable"
                } else {
                    fingerprint
                },
                trimmed_pick(body, &["voice"], "kasane-teto-standard"),
                trimmed_pick(body, &["language"], "pt-BR"),
                if base_pitch.is_empty() {
                    "C4"
                } else {
                    base_pitch
                }
            );
            if performance.is_some()
                || [
                    "ttv_voice_id",
                    "ttv_pitch_semitones",
                    "ttv_speech_rate",
                    "ttv_language",
                ]
                .iter()
                .any(|k| body.get(k).is_some())
            {
                let rate = controls["ttv_speech_rate"].as_f64().unwrap();
                p = format!(
                    "ttv-v1|voice={}|pitch={pitch:+.1}|rate={rate:?}|{p}",
                    controls["ttv_voice_id"].as_str().unwrap()
                );
                if performance.is_some() || body.get("ttv_language").is_some() {
                    p = format!("reading={}|{p}", controls["ttv_language"].as_str().unwrap());
                }
            }
            if let Some(hash) = performance {
                p = format!("ttv-performance-v1|{hash}|{p}");
            }
            p
        }
        "android_native" => format!(
            "android_native|{}|{}|{}|{}|{t}",
            trimmed_pick(body, &["language", "fallback_language"], "pt-BR").replace('_', "-"),
            trimmed_pick(body, &["voice"], "auto"),
            trimmed_pick(body, &["rate"], "1.0"),
            trimmed_pick(body, &["pitch"], "1.0")
        ),
        "edge" => format!(
            "edge|{}|{}|{}|{t}",
            trimmed_pick(body, &["voice", "fallback_voice"], "pt-BR-FranciscaNeural"),
            normalizers
                .and_then(|v| v.get("rate"))
                .map(text)
                .unwrap_or_else(|| normalize_edge(&get(body, "rate"), false)),
            normalizers
                .and_then(|v| v.get("pitch"))
                .map(text)
                .unwrap_or_else(|| normalize_edge(&get(body, "pitch"), true))
        ),
        _ => format!(
            "gtts|{}|{}|{t}",
            normalizers
                .and_then(|v| v.get("language"))
                .map(text)
                .unwrap_or_else(|| normalize_gtts(&json!(pick(
                    body,
                    &["language", "fallback_language"],
                    "pt"
                )))),
            pick(body, &["tld"], "com")
        ),
    };
    Ok(format!(
        "{:x}",
        Sha256::digest(format!("tts-v2|{payload}").as_bytes())
    ))
}

pub fn split_text(raw: &str, escaped: bool, limit: usize, max_parts: usize) -> Vec<String> {
    let normalized = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    let chars: Vec<char> = normalized.chars().collect();
    if chars.is_empty() {
        return Vec::new();
    }
    let mut sizes = vec![0usize];
    for c in &chars {
        let size = if escaped {
            match c {
                '&' => 5,
                '<' => 4,
                '>' => 4,
                _ => c.len_utf8(),
            }
        } else {
            1
        };
        sizes.push(sizes.last().unwrap() + size);
    }
    if *sizes.last().unwrap() <= limit {
        return vec![normalized];
    }
    let boundaries: Vec<usize> = (0..chars.len().saturating_sub(1))
        .filter(|i| ".!?;:".contains(chars[*i]) && chars[*i + 1].is_whitespace())
        .map(|i| i + 1)
        .collect();
    let mut out = Vec::new();
    let mut start = 0;
    while start < chars.len() && out.len() < max_parts.max(1) {
        let mut cut = sizes
            .partition_point(|n| *n <= sizes[start].saturating_add(limit))
            .saturating_sub(1)
            .min(chars.len());
        let truncated = out.len() == max_parts.max(1) - 1 && cut < chars.len();
        if truncated {
            let reserve = if escaped { 3 } else { 1 };
            if limit < reserve {
                break;
            }
            cut = sizes
                .partition_point(|n| *n <= sizes[start] + limit - reserve)
                .saturating_sub(1);
        } else if cut < chars.len() {
            let minimum = start + ((cut - start) / 3).max(1);
            let sentence = boundaries
                .iter()
                .copied()
                .filter(|b| *b <= cut && *b >= minimum)
                .last();
            let word = (start..=cut.min(chars.len() - 1))
                .filter(|i| chars[*i] == ' ' && *i >= minimum)
                .last();
            if let Some(s) = sentence.or(word) {
                cut = s;
            }
        }
        if cut <= start {
            break;
        }
        let piece = chars[start..cut]
            .iter()
            .collect::<String>()
            .trim_end()
            .to_string();
        if !piece.is_empty() {
            out.push(if truncated {
                format!("{piece}…")
            } else {
                piece
            });
        }
        if truncated {
            break;
        }
        start = cut;
        while start < chars.len() && chars[start].is_whitespace() {
            start += 1;
        }
    }
    out
}
fn shell_quote(s: &str) -> String {
    if !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c))
    {
        return s.into();
    }
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}
pub fn compose_audio_filters(options: &str, effect: &str) -> Result<(String, String), String> {
    let tokens = shell_words::split(options).map_err(|e| e.to_string())?;
    let re = Regex::new(r"^-filter:a(?::\d+)?$").unwrap();
    let mut kept = Vec::new();
    let mut configured = String::new();
    let mut index = 0;
    while index < tokens.len() {
        let token = &tokens[index];
        let (name, value) = token
            .split_once('=')
            .map(|(k, v)| (k, Some(v)))
            .unwrap_or((token.as_str(), None));
        if name == "-af" || re.is_match(name) {
            if let Some(v) = value {
                configured = v.into();
            } else {
                index += 1;
                if index == tokens.len() {
                    return Err(format!("Missing audio filter after {name}"));
                }
                configured = tokens[index].clone();
            }
        } else {
            kept.push(token.clone());
        }
        index += 1;
    }
    if effect.is_empty() {
        return Ok((options.into(), configured));
    }
    let combined = if configured.is_empty() {
        effect.into()
    } else {
        format!("{configured},{effect}")
    };
    kept.extend(["-af".into(), combined.clone()]);
    Ok((
        kept.iter()
            .map(|s| shell_quote(s))
            .collect::<Vec<_>>()
            .join(" "),
        combined,
    ))
}
pub fn dispatch(op: &str, p: &Value) -> Result<Value, String> {
    let raw = get(p, "value");
    let default = get(p, "default");
    Ok(match op {
        "normalize_engine" => json!(normalize_engine(&raw, &string(&default, "gtts"))),
        "normalize_edge_rate" => json!(normalize_edge(&raw, false)),
        "normalize_edge_pitch" => json!(normalize_edge(&raw, true)),
        "normalize_gtts_language" => json!(normalize_gtts(&raw)),
        "sanitize_cache_key" => json!(sanitize_cache_key(&raw)?),
        "normalize_cache_format" => json!(match string(&raw, "mp3")
            .trim()
            .to_lowercase()
            .replace('.', "")
            .as_str()
        {
            "wav" | "wave" => "wav",
            "ogg" | "opus" => "ogg",
            _ => "mp3",
        }),
        "normalize_pitch" => json!(normalize_pitch(&raw)?),
        "normalize_teto_pitch_semitones" => json!(normalize_worker_pitch(
            &raw,
            number(&default).unwrap_or(0.0)
        )),
        "normalize_voice_id" => json!(normalize_voice(&raw, false)?),
        "normalize_ttv_voice_id" => json!(normalize_voice(&raw, true)?),
        "normalize_language" => json!(normalize_language(&raw, false)?),
        "normalize_ttv_language" => json!(normalize_language(&raw, true)?),
        "normalize_voice_language" => {
            let voice = normalize_voice(&get(p, "voice_id"), false)?;
            json!(voice_language(&voice, &raw)?)
        }
        "normalize_speech_rate" => json!(normalize_rate(
            &raw,
            number(&default).unwrap_or(1.0),
            false
        )?),
        "normalize_ttv_speech_rate" => json!(normalize_rate(&raw, 1.0, true)?),
        "resolve_preferences" => resolve_preferences(
            &get(p, "mapping"),
            &p.get("default_pitch").cloned().unwrap_or(json!(0.0)),
        )?,
        "resolve_tts" => resolve_tts(
            &get(p, "personal"),
            &get(p, "server"),
            &p.get("default_pitch").cloned().unwrap_or(json!(0.0)),
        )?,
        "inherit_server_preferences" => inherit_preferences(
            &get(p, "personal"),
            &get(p, "server"),
            &p.get("default_pitch").cloned().unwrap_or(json!(0.0)),
        ),
        "effective_ttv_settings" => effective_settings(
            &get(p, "body"),
            number(&get(p, "default_speech_rate")).unwrap_or(1.0),
        )?,
        "available_engines" => json!(available_engines(&get(p, "deps"))),
        "engine_order" => json!(engine_order(
            &get(p, "body"),
            &serde_json::from_value::<Vec<String>>(get(p, "available"))
                .map_err(|e| e.to_string())?,
            &get(p, "preferred_default")
        )),
        "preferred_engine" => {
            let available: Vec<String> =
                serde_json::from_value(get(p, "available")).map_err(|e| e.to_string())?;
            let requested = normalize_engine(&get(p, "requested"), "auto");
            json!(if requested != "auto" && available.contains(&requested) {
                requested
            } else {
                ["android_native", "edge", "gtts"]
                    .into_iter()
                    .find(|s| available.iter().any(|a| a == s))
                    .unwrap_or("")
                    .into()
            })
        }
        "cache_mode_allows_read" | "cache_mode_allows_store" => {
            let mode = pick(&get(p, "body"), &["cache_mode"], "prefer")
                .trim()
                .to_lowercase();
            json!(
                !["0", "false", "off", "disabled", "none", "bypass"].contains(&mode.as_str())
                    && if op.ends_with("read") {
                        mode != "refresh"
                    } else {
                        !["no_store", "only", "cache_only"].contains(&mode.as_str())
                    }
            )
        }
        "standard_cache_key" => json!(cache_key_with_normalizers(
            &get(p, "body"),
            &string(&get(p, "engine"), ""),
            &string(&get(p, "teto_fingerprint"), "unavailable"),
            &string(&get(p, "teto_base_pitch"), "C4"),
            number(&get(p, "teto_speech_rate")).unwrap_or(1.0),
            p.get("performance_hash").and_then(Value::as_str),
            p.get("normalizers")
        )?),
        "split_text" => json!(split_text(
            &string(&get(p, "text"), ""),
            get(p, "escaped_utf8").as_bool().unwrap_or(false),
            get(p, "limit").as_u64().unwrap_or(0) as usize,
            get(p, "max_parts").as_u64().unwrap_or(1) as usize
        )),
        "compose_audio_filters" => json!(compose_audio_filters(
            &string(&get(p, "options"), ""),
            &string(&get(p, "effect_filter"), "")
        )?),
        "singing_request" => json!(markup::singing_request(&string(&get(p, "text"), ""))),
        "parse_performance_text" => {
            let raw = get(p, "text");
            let text = raw
                .as_str()
                .ok_or("O texto do TTV deve ser uma mensagem de texto.")?;
            json!(markup::parse(
                text,
                get(p, "singing").as_bool().unwrap_or(false)
            )?)
        }
        "performance_hash" => {
            let score = get(p, "score");
            let segments = get(p, "segments");
            if score.is_null() && !truthy(&segments) {
                json!("")
            } else {
                let text = string(&get(p, "text"), "")
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ");
                let payload = json!({"version":1,"text":text,"score":score,"segments":segments});
                json!(format!(
                    "{:x}",
                    Sha256::digest(payload.to_string().as_bytes())
                ))
            }
        }
        "catalog_score" => melody::catalog_score(&get(p, "entry")),
        "validate_melody_score" => melody::validate_score(&get(p, "entry"))?,
        "score_sha256" => json!(format!(
            "{:x}",
            Sha256::digest(get(p, "score").to_string().as_bytes())
        )),
        "minimum_singing_syllables" => json!(melody::minimum_singing_syllables(&get(p, "score"))?),
        "fit_arrangement_lyrics" => {
            melody::fit_arrangement(&get(p, "text"), &get(p, "segments"), &get(p, "arrangement"))?
        }
        _ => return Err(format!("unknown TTS policy operation: {op}")),
    })
}

#[derive(Default)]
struct GuildQueue {
    generation: u64,
    pending: VecDeque<Value>,
    active: Option<Value>,
    bytes: usize,
}
/// One active item per guild, bounded queue and atomic admission of all split parts.
pub struct QueueCoordinator {
    max_items: usize,
    max_bytes: usize,
    dedup_limit: usize,
    guilds: HashMap<u64, GuildQueue>,
    seen: HashSet<(u64, u64)>,
    seen_order: VecDeque<(u64, u64)>,
}
impl QueueCoordinator {
    pub fn new(max_items: usize, max_bytes: usize, dedup_limit: usize) -> Result<Self, String> {
        if max_items == 0 || max_bytes == 0 || dedup_limit == 0 {
            return Err("queue limits must be positive".into());
        }
        Ok(Self {
            max_items,
            max_bytes,
            dedup_limit,
            guilds: HashMap::new(),
            seen: HashSet::new(),
            seen_order: VecDeque::new(),
        })
    }
    pub fn admit(
        &mut self,
        guild: u64,
        message: u64,
        mut items: Vec<Value>,
    ) -> Result<Vec<String>, String> {
        if message != 0 && self.seen.contains(&(guild, message)) {
            return Ok(vec![]);
        }
        if items.is_empty() {
            return Ok(vec![]);
        }
        let state = self.guilds.entry(guild).or_default();
        let mut request_ids = HashSet::new();
        let mut added = 0usize;
        for item in &mut items {
            let object = item.as_object_mut().ok_or("queue item must be an object")?;
            let id = object
                .get("request_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .ok_or("queue item needs request_id")?
                .to_string();
            if !request_ids.insert(id.clone())
                || state
                    .pending
                    .iter()
                    .chain(state.active.iter())
                    .any(|i| i.get("request_id").and_then(Value::as_str) == Some(&id))
            {
                return Err("duplicate request_id".into());
            }
            if object
                .get("generation")
                .and_then(Value::as_u64)
                .is_some_and(|g| g != state.generation)
            {
                return Err("stale queue generation".into());
            }
            object.insert("generation".into(), json!(state.generation));
            added = added
                .checked_add(serde_json::to_vec(item).map_err(|e| e.to_string())?.len())
                .ok_or("queue byte limit")?;
        }
        if state.pending.len() + usize::from(state.active.is_some()) + items.len() > self.max_items
            || state
                .bytes
                .checked_add(added)
                .is_none_or(|n| n > self.max_bytes)
        {
            return Err("queue capacity exceeded".into());
        }
        let ids = items
            .iter()
            .map(|v| v["request_id"].as_str().unwrap().to_string())
            .collect();
        state.bytes += added;
        state.pending.extend(items);
        if message != 0 {
            self.seen.insert((guild, message));
            self.seen_order.push_back((guild, message));
            while self.seen_order.len() > self.dedup_limit {
                if let Some(key) = self.seen_order.pop_front() {
                    self.seen.remove(&key);
                }
            }
        }
        Ok(ids)
    }
    pub fn dequeue(&mut self, guild: u64) -> Option<Value> {
        let state = self.guilds.get_mut(&guild)?;
        if state.active.is_some() {
            return None;
        }
        let index = state
            .pending
            .iter()
            .position(|i| i.get("priority").and_then(Value::as_str) != Some("prefetch"))
            .unwrap_or(0);
        let item = state.pending.remove(index)?;
        state.active = Some(item.clone());
        Some(item)
    }
    pub fn complete(&mut self, guild: u64, request: &str) -> bool {
        let Some(state) = self.guilds.get_mut(&guild) else {
            return false;
        };
        if state.active.as_ref().and_then(|v| v["request_id"].as_str()) != Some(request) {
            return false;
        }
        if let Some(item) = state.active.take() {
            state.bytes = state
                .bytes
                .saturating_sub(serde_json::to_vec(&item).unwrap().len());
        }
        true
    }
    pub fn promote(&mut self, guild: u64, request: &str) -> Result<bool, String> {
        let Some(state) = self.guilds.get_mut(&guild) else {
            return Ok(false);
        };
        let Some(item) = state
            .pending
            .iter_mut()
            .find(|i| i["request_id"].as_str() == Some(request))
        else {
            return Ok(false);
        };
        let original = item.clone();
        let old = serde_json::to_vec(item).unwrap().len();
        item["priority"] = json!("foreground");
        let new_bytes = state.bytes.saturating_sub(old) + serde_json::to_vec(item).unwrap().len();
        if new_bytes > self.max_bytes {
            *item = original;
            return Err("queue capacity exceeded".into());
        }
        state.bytes = new_bytes;
        Ok(true)
    }
    pub fn cancel_request(&mut self, guild: u64, request: &str) -> bool {
        let Some(state) = self.guilds.get_mut(&guild) else {
            return false;
        };
        if state.active.as_ref().and_then(|v| v["request_id"].as_str()) == Some(request) {
            return self.complete(guild, request);
        }
        let Some(index) = state
            .pending
            .iter()
            .position(|i| i["request_id"].as_str() == Some(request))
        else {
            return false;
        };
        let item = state.pending.remove(index).unwrap();
        state.bytes = state
            .bytes
            .saturating_sub(serde_json::to_vec(&item).unwrap().len());
        true
    }
    pub fn cancel_guild(&mut self, guild: u64) -> u64 {
        let s = self.guilds.entry(guild).or_default();
        s.generation = s.generation.saturating_add(1);
        s.pending.clear();
        s.active = None;
        s.bytes = 0;
        s.generation
    }
    pub fn snapshot(&self, guild: u64) -> Value {
        if let Some(s) = self.guilds.get(&guild) {
            json!({"generation":s.generation,"pending":s.pending.len(),"active":s.active.as_ref().and_then(|v|v["request_id"].as_str()),"bytes":s.bytes,"max_items":self.max_items,"max_bytes":self.max_bytes})
        } else {
            json!({"generation":0,"pending":0,"active":null,"bytes":0,"max_items":self.max_items,"max_bytes":self.max_bytes})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frozen_worker_cache_keys() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/tts_policy_contract.json"
        ))
        .unwrap();
        for case in fixture["cache_keys"].as_array().unwrap() {
            assert_eq!(
                standard_cache_key(
                    &case["body"],
                    case["engine"].as_str().unwrap(),
                    fixture["teto_fingerprint"].as_str().unwrap(),
                    fixture["teto_base_pitch"].as_str().unwrap(),
                    1.0,
                    None
                )
                .unwrap(),
                case["key"].as_str().unwrap(),
                "{case}"
            );
        }
    }
    #[test]
    fn queue_atomic_dedup_generation() {
        let mut q = QueueCoordinator::new(2, 4096, 10).unwrap();
        let group = vec![json!({"request_id":"a"}), json!({"request_id":"b"})];
        assert_eq!(q.admit(1, 50, group.clone()).unwrap().len(), 2);
        assert!(q.admit(1, 50, group).unwrap().is_empty());
        assert!(q.admit(1, 51, vec![json!({"request_id":"c"})]).is_err());
        assert_eq!(q.dequeue(1).unwrap()["request_id"], "a");
        assert!(q.dequeue(1).is_none());
        assert!(!q.complete(1, "b"));
        assert!(q.complete(1, "a"));
        assert_eq!(q.cancel_guild(1), 1);
        assert!(q
            .admit(1, 51, vec![json!({"request_id":"c","generation":0})])
            .is_err());
        assert_eq!(q.snapshot(1)["bytes"], 0);
    }
    #[test]
    fn priority_and_budget() {
        let mut q = QueueCoordinator::new(3, 2048, 2).unwrap();
        q.admit(
            1,
            1,
            vec![
                json!({"request_id":"a","priority":"prefetch"}),
                json!({"request_id":"b"}),
            ],
        )
        .unwrap();
        assert!(q.promote(1, "a").unwrap());
        assert_eq!(q.dequeue(1).unwrap()["request_id"], "a");
        assert!(q.cancel_request(1, "a"));
        assert!(!q.complete(1, "a"));
        assert_eq!(q.dequeue(1).unwrap()["request_id"], "b");
    }
    #[test]
    fn text_and_filters() {
        assert_eq!(
            split_text("Olá mundo. Outro & texto", true, 16, 2),
            vec!["Olá mundo.", "Outro & t…"]
        );
        assert_eq!(
            compose_audio_filters("-vn -af volume=0.5 -b:a 96k", "aecho=1").unwrap(),
            (
                "-vn -b:a 96k -af volume=0.5,aecho=1".into(),
                "volume=0.5,aecho=1".into()
            )
        );
        assert!(compose_audio_filters("-vn -af", "effect").is_err());
    }
    #[test]
    fn personal_controls() {
        assert_eq!(normalize_pitch(&json!("−0.0")).unwrap(), "+0.0");
        assert!(normalize_pitch(&json!(0.25)).is_err());
        assert_eq!(normalize_worker_pitch(&json!(0.25), 0.0), 0.0);
        assert_eq!(normalize_worker_pitch(&json!(0.75), 0.0), 1.0);
        assert_eq!(resolve_preferences(&json!({"ttv_pitch_semitones":"invalid","teto_pitch_semitones":"+2.5","ttv_speech_rate":"invalid"}),&json!(0.0)).unwrap()["ttv_pitch_semitones"],"+2.5");
    }
}
