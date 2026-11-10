//! Worker configuration projection. Each character owns its voicebank; only
//! the acoustic settings explicitly shared in the existing worker inherit.
use crate::{
    prosody::{self, ProsodySettings},
    python_round,
    renderer::RenderRequest,
    voicebank::{VoicebankIndex, VoicebankOptions},
};
use serde_json::Value;
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
};
const SHARED: &[&str] = &[
    "BASE_PITCH",
    "SPEECH_RATE",
    "FLAGS",
    "VELOCITY",
    "MODULATION",
    "TEMPO",
    "RENDER_THREADS",
    "FRAGMENT_CACHE_MB",
    "PCM_CACHE_MB",
    "STATUS_CACHE_SECONDS",
    "NATIVE_ANALYSIS_CACHE_MB",
];
fn string(v: &Value) -> Option<String> {
    match v {
        Value::String(v) => Some(v.clone()),
        Value::Number(v) => Some(v.to_string()),
        _ => None,
    }
}
fn setting(env: &HashMap<String, String>, prefix: &str, suffix: &str, default: &str) -> String {
    if let Some(v) = env.get(&format!("PHONE_WORKER_{prefix}_{suffix}")) {
        if !v.trim().is_empty() || suffix == "FLAGS" {
            return v.clone();
        }
    }
    if prefix != "TETO" && SHARED.contains(&suffix) {
        if let Some(v) = env.get(&format!("PHONE_WORKER_TETO_{suffix}")) {
            if !v.trim().is_empty() || suffix == "FLAGS" {
                return v.clone();
            }
        }
    }
    default.into()
}
fn number(v: &str, default: f64) -> f64 {
    v.parse::<f64>()
        .ok()
        .filter(|v| v.is_finite())
        .unwrap_or(default)
}
fn enabled(env: &HashMap<String, String>, prefix: &str) -> bool {
    env.get(&format!("PHONE_WORKER_{prefix}_ENABLED"))
        .is_some_and(|s| {
            matches!(
                s.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
}
fn expand(path: &str, home: &Path) -> PathBuf {
    if path == "~" {
        home.to_owned()
    } else if let Some(s) = path.strip_prefix("~/") {
        home.join(s)
    } else {
        PathBuf::from(path)
    }
}
fn pitch(value: Option<&Value>) -> f64 {
    let Some(value) = value.and_then(string) else {
        return 0.0;
    };
    let mut s = value
        .trim()
        .to_lowercase()
        .replace(',', ".")
        .replace(['−', '–', '—'], "-");
    for suffix in ["semitones", "semitone", "semitons", "semitom", "st"] {
        if let Some(t) = s.strip_suffix(suffix) {
            s = t.trim().to_owned();
            break;
        }
    }
    python_round(number(&s, 0.0).clamp(-4.0, 4.0) * 2.0) / 2.0
}
pub fn request_from_worker(
    body: &Value,
    env: &HashMap<String, String>,
) -> Result<RenderRequest, String> {
    if !body.is_object() {
        return Err("requisição TTV deve ser objeto".into());
    }
    let voice = body.get("ttv_voice_id").map(|v|v.as_str().ok_or("vocaloid TTV desconhecida ou indisponível")).transpose()?.unwrap_or("kasane-teto")
        .trim()
        .to_lowercase();
    let prefix = match voice.as_str() {
        "kasane-teto" => "TETO",
        "hatsune-miku" => "MIKU",
        "anna-nyui" => "ANNA",
        "pururu-purpura" => "PURURU",
        _ => return Err("vocaloid TTV desconhecida ou indisponível".into()),
    };
    if !enabled(env, prefix) {
        return Err(format!("PHONE_WORKER_{prefix}_ENABLED=false"));
    }
    let language = body.get("ttv_language").map(|v|v.as_str().ok_or("idioma TTV deve ser Português adaptado ou Inglês")).transpose()?.unwrap_or("pt-BR")
        .trim()
        .to_lowercase()
        .replace('_', "-");
    let language = match language.as_str() {
        "pt" | "pt-br" => "pt-BR",
        "en" | "en-us" => "en-US",
        _ => return Err("idioma TTV deve ser Português adaptado ou Inglês".into()),
    };
    if voice == "pururu-purpura" && language != "pt-BR" {
        return Err("Pururu Púrpura aceita somente português brasileiro (pt-BR)".into());
    }
    let home = env
        .get("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let score = body.get("ttv_score").filter(|v| !v.is_null()).cloned();
    let segments = body.get("ttv_segments").filter(|v| !v.is_null()).cloned();
    let text = body
        .get("text")
        .and_then(Value::as_str)
        .ok_or("texto TTV inválido")?
        .trim()
        .to_owned();
    let profile = if prefix == "PURURU" {
        setting(env, prefix, "PROFILE", "legacy")
            .trim()
            .to_lowercase()
    } else {
        "legacy".into()
    };
    if !matches!(profile.as_str(), "legacy" | "pururu-ptbr-v1") {
        return Err("perfil da Pururu desconhecido; use legacy ou pururu-ptbr-v1".into());
    }
    let acoustic = |suffix: &str, default: &str| {
        if profile == "pururu-ptbr-v1" {
            if let Some(v) = env.get(&format!("PHONE_WORKER_PURURU_{suffix}")) {
                if !v.trim().is_empty() || suffix == "FLAGS" {
                    return v.clone();
                }
            }
            match suffix {
                "BASE_PITCH" => "C4".into(),
                "SPEECH_RATE" => "1.0".into(),
                "FLAGS" => "g5 B50".into(),
                "VELOCITY" => "100".into(),
                "MODULATION" => "0".into(),
                "TEMPO" => "125".into(),
                _ => setting(env, prefix, suffix, default),
            }
        } else {
            setting(env, prefix, suffix, default)
        }
    };
    let rate = if score.is_some() {
        1.0
    } else if let Some(raw) = body.get("ttv_speech_rate") {
        let v = match raw {
            Value::Number(n) => n.as_f64(),
            Value::String(s) => s.parse::<f64>().ok(),
            _ => None,
        }
        .ok_or("velocidade TTV deve estar entre 0.5 e 1.5")?;
        if !v.is_finite() || !(0.5..=1.5).contains(&v) {
            return Err("velocidade TTV deve estar entre 0.5 e 1.5".into());
        }
        v
    } else {
        number(&acoustic("SPEECH_RATE", "1.0"), 1.0).clamp(0.75, 1.5)
    };
    let pitch = if score.is_some() {
        0.0
    } else {
        pitch(
            body.get("ttv_pitch_semitones")
                .or_else(|| body.get("teto_pitch_semitones")),
        )
    };
    if score.is_some() || segments.is_some() {
        prosody::validate_performance(&text, &score, &segments)?;
        let digest = prosody::performance_hash(&text, &score, &segments)?;
        if let Some(hash) = body.get("ttv_performance_hash") {
            if hash.as_str() != Some(&digest) {
                return Err(
                    "identidade da performance TTV difere do texto, MIDI ou efeitos".into(),
                );
            }
        }
    } else if body.get("ttv_performance_hash").is_some() {
        return Err("identidade TTV recebida sem MIDI ou efeitos".into());
    }
    let default_bank = match prefix {
        "PURURU" => home
            .join("voicebanks/pururu-purpura")
            .to_string_lossy()
            .into_owned(),
        _ => String::new(),
    };
    let standard = expand(&setting(env, prefix, "VOICEBANK_DIR", &default_bank), &home);
    let english_default = match prefix {
        "MIKU" => {
            if standard.as_os_str().is_empty() {
                home.join("voicebanks/hatsune-miku-english")
            } else {
                standard.clone()
            }
        }
        "ANNA" => {
            if standard.as_os_str().is_empty() {
                home.join("voicebanks/anna-nyui-diva")
            } else {
                standard.clone()
            }
        }
        _ => home.join("voicebanks/kasane-teto-english"),
    };
    let english = expand(
        &setting(
            env,
            prefix,
            "ENGLISH_VOICEBANK_DIR",
            &english_default.to_string_lossy(),
        ),
        &home,
    );
    let mode = if matches!(prefix, "MIKU" | "ANNA") {
        "english".into()
    } else {
        setting(env, prefix, "VOICEBANK_MODE", "auto")
            .trim()
            .to_lowercase()
    };
    let suffix = if prefix == "MIKU" {
        setting(env, prefix, "ALIAS_SUFFIX", "_D4")
    } else if prefix == "ANNA" || prefix == "PURURU" {
        String::new()
    } else {
        setting(env, prefix, "ALIAS_SUFFIX", "")
    };
    let english_min = number(
        &setting(
            env,
            prefix,
            "ENGLISH_MIN_ALIASES",
            if prefix == "PURURU" { "100" } else { "500" },
        ),
        500.0,
    )
    .max(1.0) as usize;
    let standard_min = number(
        &setting(
            env,
            prefix,
            "MIN_ALIASES",
            if prefix == "PURURU" { "100" } else { "10" },
        ),
        10.0,
    )
    .max(1.0) as usize;
    let mut candidates = Vec::new();
    if prefix == "PURURU" {
        candidates.push((standard, "english-cvvc", standard_min));
    } else if language == "en-US" || score.is_some() || mode == "english" {
        candidates.push((english, "english-cvvc", english_min));
    } else {
        if mode != "standard" {
            candidates.push((english, "english-cvvc", english_min));
        }
        if mode != "english" && !standard.as_os_str().is_empty() {
            candidates.push((standard, "standard", standard_min));
        }
    }
    let mut selected = None;
    let mut errors = Vec::new();
    for (path, profile, min) in candidates {
        let options = VoicebankOptions {
            minimum_aliases: min,
            alias_suffix: suffix.clone(),
            case_sensitive: prefix != "TETO",
            pitch_map: prefix == "ANNA",
        };
        match VoicebankIndex::load(&path, &options) {
            Ok(_) => {
                selected = Some((path, profile, options));
                break;
            }
            Err(e) => {
                errors.push(e);
                if mode != "auto" && language != "en-US" && score.is_none() {
                    break;
                }
            }
        }
    }
    let (voicebank, bank_profile, voicebank_options) = selected.ok_or_else(|| {
        format!(
            "banco de voz da {voice} indisponível: {}",
            errors.join("; ")
        )
    })?;
    let mut raw_command = setting(env, prefix, "NATIVE_RESAMPLER_COMMAND", "");
    if raw_command.trim().is_empty() {
        raw_command = setting(env, prefix, "RESAMPLER_COMMAND", "");
    }
    if raw_command.trim().is_empty() && prefix != "TETO" {
        let baseline = setting(
            env,
            "TETO",
            "NATIVE_RESAMPLER_COMMAND",
            &setting(env, "TETO", "RESAMPLER_COMMAND", ""),
        );
        if let Ok(c) = shell_words::split(&baseline) {
            if c.len() == 1
                && Path::new(&c[0])
                    .file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| n.starts_with("straycat"))
            {
                raw_command = baseline;
            }
        }
    }
    let mut resampler =
        shell_words::split(&raw_command).map_err(|e| format!("comando nativo inválido: {e}"))?;
    for c in &mut resampler {
        if c.starts_with("~/") {
            *c = expand(c, &home).to_string_lossy().into_owned();
        }
    }
    let mut native_backend = setting(
        env,
        prefix,
        "NATIVE_BACKEND",
        if resampler
            .iter()
            .any(|s| s.contains("Esper.Resident") || s == "--_resident")
        {
            "esper-resident"
        } else {
            "command"
        },
    );
    let backend=setting(env,"TETO","BACKEND","utau").trim().to_lowercase().replace('_',"-");
    if prefix=="TETO"&&!matches!(backend.as_str(),"utau"|"voicepeak"|"worldline-r"){return Err("PHONE_WORKER_TETO_BACKEND deve ser utau ou worldline-r".into());}
    let installed = resampler.iter().any(|s| s.ends_with("resampler.py"))
        || resampler.is_empty() && home.join(".esper-utau/releases/v2.5.0/ESPER-Utau").exists()
        || native_backend == "esper-installed";
    if installed {
        native_backend = "esper-installed".into();
    }
    if prefix=="TETO"&&backend=="worldline-r"{native_backend="worldline-r".into();}
    let mut engine_environment: HashMap<String, String> = env
        .iter()
        .filter(|(k, _)| {
                k.starts_with("PHONE_WORKER_ESPER_") || k.starts_with("PHONE_WORKER_WORLDLINE_") || k.starts_with("PHONE_WORKER_TETO_")
                || matches!(k.as_str(), "HOME" | "PATH" | "TMPDIR" | "PREFIX")
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    engine_environment
        .entry("HOME".into())
        .or_insert_with(|| home.to_string_lossy().into_owned());
    if let Some(position)=resampler.iter().position(|s|native_backend!="worldline-r" && Path::new(s).file_name().and_then(|s|s.to_str())==Some("resampler.py")){
        use sha2::{Digest,Sha256};
        let data=std::fs::read(&resampler[position]).map_err(|e|format!("ponte ESPER legada indisponível: {e}"))?;
        let digest=format!("{:x}",Sha256::digest(&data));
        if data.len()>128*1024 || !["75a256a704afca3b835fd2fc8e273f3a6f488fd37c1f180291992aa2289a5698","b80dc4c34717611a6f7dd7056dcc6c31f1e59b3b0d660b9fe746773ce16e4f74","5b7d7c95a7e3c35eeb00a3ccddba3bcaa0ac7ccc6a2fb64584372239bd9f2ea6"].contains(&digest.as_str()){
            return Err("ponte ESPER personalizada exige NATIVE_RESAMPLER_COMMAND nativo explícito".into());
        }
        let mut i=position+1;let mut seen=std::collections::HashSet::new();
        while i<resampler.len(){let(arg,inline)=resampler[i].split_once('=').map(|(a,b)|(a,Some(b))).unwrap_or((&resampler[i],None));if !seen.insert(arg.to_owned()){return Err("opção da ponte ESPER repetida".into());}let value=if let Some(v)=inline{v.to_owned()}else{i+=1;resampler.get(i).cloned().ok_or("opção da ponte ESPER sem valor")?};
            let key=match arg{"--root"=>Some("PHONE_WORKER_ESPER_ROOT"),"--cache-dir"=>Some("PHONE_WORKER_ESPER_CACHE_DIR"),"--container"=>Some("PHONE_WORKER_ESPER_CONTAINER"),"--timeout"=>Some("PHONE_WORKER_ESPER_TIMEOUT"),"--implementation-id"=>{if value.to_lowercase()!=digest{return Err("identidade da ponte ESPER mudou".into());}None},_=>return Err("opção da ponte ESPER personalizada não suportada nativamente".into())};
            if let Some(key)=key{engine_environment.insert(key.into(),value);}i+=1;
        }
    }
    let esper_root = expand(
        engine_environment.get("PHONE_WORKER_ESPER_ROOT")
            .map(String::as_str)
            .unwrap_or("~/.esper-utau"),
        &home,
    );
    let esper_cache = expand(
        engine_environment.get("PHONE_WORKER_ESPER_CACHE_DIR")
            .map(String::as_str)
            .unwrap_or(&esper_root.join("cache").to_string_lossy()),
        &home,
    );

    let cache_name = match prefix {
        "MIKU" => "miku-fragments",
        "ANNA" => "anna-fragments",
        "PURURU" => "pururu-fragments",
        _ => "teto-fragments",
    };
    let cache = expand(
        &setting(
            env,
            prefix,
            "FRAGMENT_CACHE_DIR",
            &home
                .join("phone-worker/cache")
                .join(cache_name)
                .to_string_lossy(),
        ),
        &home,
    );
    let length_mode = setting(env, prefix, "LENGTH_MODE", "auto");
    let post_consonant = length_mode == "post-consonant"
        || length_mode == "auto"
            && resampler
                .first()
                .and_then(|s| Path::new(s).file_name())
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("straycat"));
    let timeout = body
        .get("timeout_seconds")
        .and_then(Value::as_f64)
        .unwrap_or(if score.is_some() { 120.0 } else { 25.0 });
    let max_audio_bytes = body
        .get("max_audio_bytes")
        .and_then(Value::as_u64)
        .unwrap_or(8 * 1024 * 1024)
        .min(64 * 1024 * 1024) as usize;
    Ok(RenderRequest {
        text,
        voice_id: voice,
        voicebank,
        voicebank_options,
        voicebank_profile: bank_profile.into(),
        reading_language: language.into(),
        inventory_profile: match prefix {
            "MIKU" => "miku-v4-english",
            "ANNA" => "anna-nyui-diva",
            "PURURU" => "pururu-purpura",
            _ => "",
        }
        .into(),
        prosody: ProsodySettings {
            base_pitch: acoustic("BASE_PITCH", "C4"),
            speech_rate: rate,
            tempo: number(&acoustic("TEMPO", "140"), 140.0).clamp(60.0, 240.0) as i32,
            pitch_offset_semitones: pitch,
            speech_profile: if profile == "pururu-ptbr-v1" {
                profile.clone()
            } else {
                String::new()
            },
        },
        score,
        segments,
        velocity: number(&acoustic("VELOCITY", "100"), 100.0).clamp(1.0, 200.0),
        modulation: number(&acoustic("MODULATION", "15"), 15.0).clamp(0.0, 100.0),
        flags: acoustic("FLAGS", ""),
        post_consonant_length: post_consonant,
        timeout_seconds: timeout,
        max_audio_bytes,
        max_audio_seconds: number(&setting(env, prefix, "MAX_AUDIO_SECONDS", "20"), 20.0).max(2.0),
        max_moras: number(&setting(env, prefix, "MAX_PHONEMES", "240"), 240.0).max(8.0) as usize,
        max_characters: number(&setting(env,prefix,"MAX_CHARACTERS","180"),180.0).max(16.0)as usize,
        render_threads:number(&setting(env,prefix,"RENDER_THREADS",if prefix=="MIKU"{"4"}else{"2"}),2.0).max(1.0)as usize,
        resampler,
        fragment_cache: Some(cache.clone()),
        fragment_cache_max_bytes: number(&setting(env, prefix, "FRAGMENT_CACHE_MB", "256"), 256.0)
            .max(8.0) as u64
            * 1024
            * 1024,
        unit_gain: body
            .get("unit_gain")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        native_backend,
        native_source_cache: Some(if installed {
            esper_cache.join("sources")
        } else {
            cache.join("native-sources")
        }),
        engine_environment,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn missing_voice_never_borrows_teto() {
        let env = HashMap::from([
            ("PHONE_WORKER_MIKU_ENABLED".into(), "true".into()),
            (
                "PHONE_WORKER_TETO_VOICEBANK_DIR".into(),
                "/voice/teto".into(),
            ),
        ]);
        let error = request_from_worker(&json!({"text":"olá","ttv_voice_id":"hatsune-miku"}), &env)
            .unwrap_err();
        assert!(error.contains("hatsune-miku"));
        assert!(!error.contains("/voice/teto"));
    }
    #[test]
    fn pururu_cannot_read_english() {
        let env = HashMap::from([("PHONE_WORKER_PURURU_ENABLED".into(), "true".into())]);
        assert!(request_from_worker(
            &json!({"text":"hello","ttv_voice_id":"pururu-purpura","ttv_language":"en-US"}),
            &env
        )
        .unwrap_err()
        .contains("português"));
    }
}
