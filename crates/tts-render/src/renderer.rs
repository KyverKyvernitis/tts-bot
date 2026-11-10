//! Complete rendering orchestration. Voicebank samples and external native
//! engines remain outside the binary; all planning and PCM work is Rust.
pub use crate::environment::request_from_worker;
use crate::{
    dsp,
    phonetics::{self, AliasResolution, Mora, PhonemizeOptions},
    prosody::{self, ProsodySettings, RenderNote},
    python_round,
    timeline::{self, Placement, SingingSpan},
    voicebank::{OtoEntry, VoicebankIndex, VoicebankOptions},
    SAMPLE_RATE,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs,
    io::{Cursor, Read},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
    thread,
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RenderRequest {
    pub text: String,
    pub voice_id: String,
    pub voicebank: PathBuf,
    pub voicebank_options: VoicebankOptions,
    pub voicebank_profile: String,
    pub reading_language: String,
    pub inventory_profile: String,
    pub prosody: ProsodySettings,
    pub score: Option<Value>,
    pub segments: Option<Value>,
    pub velocity: f64,
    pub modulation: f64,
    pub flags: String,
    pub post_consonant_length: bool,
    pub timeout_seconds: f64,
    pub max_audio_bytes: usize,
    pub max_audio_seconds: f64,
    pub max_moras: usize,
    pub max_characters: usize,
    pub render_threads: usize,
    pub resampler: Vec<String>,
    pub fragment_cache: Option<PathBuf>,
    pub fragment_cache_max_bytes: u64,
    pub unit_gain: bool,
    /// `esper-resident` launches the pinned resident .NET host. `command` uses
    /// a native UTAU executable. Both reject Python commands.
    pub native_backend: String,
    pub native_source_cache: Option<PathBuf>,
    pub engine_environment: HashMap<String, String>,
}
impl Default for RenderRequest {
    fn default() -> Self {
        Self {
            text: String::new(),
            voice_id: "kasane-teto".into(),
            voicebank: PathBuf::new(),
            voicebank_options: Default::default(),
            voicebank_profile: "standard".into(),
            reading_language: "pt-BR".into(),
            inventory_profile: String::new(),
            prosody: Default::default(),
            score: None,
            segments: None,
            velocity: 100.0,
            modulation: 0.0,
            flags: String::new(),
            post_consonant_length: false,
            timeout_seconds: 25.0,
            max_audio_bytes: 8 * 1024 * 1024,
            max_audio_seconds: 20.0,
            max_moras: 240,
            max_characters: 180,
            render_threads: 2,
            resampler: Vec::new(),
            fragment_cache: None,
            fragment_cache_max_bytes: 256 * 1024 * 1024,
            unit_gain: false,
            native_backend: "command".into(),
            native_source_cache: None,
            engine_environment: HashMap::new(),
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Envelope {
    pub first: usize,
    pub end: usize,
    pub fade_in: bool,
    pub fade_out: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RenderPlan {
    pub notes: Vec<RenderNote>,
    pub entries: Vec<Option<OtoEntry>>,
    pub placements: Vec<Placement>,
    pub planned_ms: f64,
    pub preroll_ms: f64,
    pub score: Option<Value>,
    pub performance_hash: Option<String>,
    pub envelopes: Vec<Envelope>,
    pub voicebank_fingerprint: String,
    pub voicebank_name: String,
    pub aliases: usize,
    pub timeline: bool,
    pub melisma_notes: usize,
}
#[derive(Debug, Clone)]
pub struct ResampleRequest {
    pub entry: OtoEntry,
    pub note: RenderNote,
    pub required_length_ms: f64,
    pub velocity: f64,
    pub volume: f64,
    pub modulation: f64,
    pub tempo: i32,
    pub flags: String,
    pub timeout: Duration,
    pub singing: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RenderOutput {
    #[serde(skip)]
    pub audio: Vec<u8>,
    pub metadata: Value,
}
#[derive(Clone, Default)]
pub struct Cancellation(tts_engines::Cancellation);
impl Cancellation {
    pub fn cancel(&self) {
        self.0.cancel();
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.is_cancelled()
    }
    pub fn engine(&self) -> &tts_engines::Cancellation {
        &self.0
    }
    pub fn check(&self) -> Result<(), String> {
        if self.is_cancelled() {
            Err("renderização TTV cancelada".into())
        } else {
            Ok(())
        }
    }
}
fn value_num(v: &Value, key: &str) -> Result<f64, String> {
    v.get(key)
        .and_then(Value::as_f64)
        .ok_or_else(|| format!("missing numeric {key}"))
}
fn phonemize(
    text: &str,
    request: &RenderRequest,
    index: &VoicebankIndex,
    performance: bool,
) -> Result<Vec<Mora>, String> {
    let options = PhonemizeOptions {
        max_moras: if performance { 1025 } else { request.max_moras },
        voicebank_profile: if request.score.is_some() {
            "english-cvvc".into()
        } else {
            request.voicebank_profile.clone()
        },
        reading_language: request.reading_language.clone(),
        inventory_profile: request.inventory_profile.clone(),
    };
    let resolver = |candidates: &[String]| {
        index
            .resolve(candidates, None)
            .ok()
            .flatten()
            .map(|e| AliasResolution {
                alias: e.alias,
                preutterance_ms: e.preutterance_ms,
                overlap_ms: e.overlap_ms,
                consonant_ms: e.consonant_ms,
            })
    };
    if request.score.is_none() && request.prosody.speech_profile == "pururu-ptbr-v1" {
        phonetics::phonemize_pururu_profile(text, &options, Some(&resolver))
    } else {
        phonetics::phonemize(text, &options, Some(&resolver))
    }
}
fn performance_tokens(text: &str, segments: Option<&Value>) -> Vec<String> {
    if let Some(segments) = segments.and_then(Value::as_array) {
        let mut tokens = Vec::new();
        for segment in segments {
            for token in segment["text"].as_str().unwrap_or("").split_whitespace() {
                if token == "~" && !segment["hold"].as_bool().unwrap_or(false) {
                    continue;
                }
                if token != "|" {
                    tokens.push(token.to_owned());
                }
            }
            if segment["pause_after_ms"].as_i64().unwrap_or(0) != 0 {
                tokens.push("|".into());
            }
        }
        tokens
    } else {
        text.split_whitespace().map(str::to_owned).collect()
    }
}
fn steady_candidates(mora: &Mora, request: &RenderRequest) -> Vec<String> {
    if request.voice_id == "pururu-purpura" {
        phonetics::pururu_vowel_candidates(&mora.source_phonemes)
    } else if request.voice_id == "anna-nyui" {
        phonetics::english_vowel_candidates(&mora.source_phonemes, "anna-nyui-diva")
    } else {
        let mut out = Vec::new();
        for c in &mora.candidates {
            if let Some(v) = c.split_whitespace().last() {
                if !out.iter().any(|s| s == v) {
                    out.push(v.to_owned());
                }
            }
        }
        out
    }
}

fn singing_lyrics(
    request: &RenderRequest,
    index: &VoicebankIndex,
) -> Result<(Value, Vec<Mora>, Vec<Vec<usize>>), String> {
    let original = request.score.as_ref().ok_or("missing score")?;
    let default = prosody::is_default_score(original);
    let mut score = original.clone();
    let source = if default {
        "message"
    } else {
        score["lyrics_source"]
            .as_str()
            .ok_or("invalid lyrics source")?
    }
    .to_owned();
    let tokens = if source == "message" {
        performance_tokens(&request.text, request.segments.as_ref())
    } else {
        let events = score["notes"].as_array().ok_or("missing MIDI notes")?;
        if source == "vowel" {
            events
                .iter()
                .enumerate()
                .map(|(i, e)| {
                    if i > 0
                        && value_num(e, "start_ms").unwrap_or(0.0)
                            - value_num(&events[i - 1], "start_ms").unwrap_or(0.0)
                            - value_num(&events[i - 1], "duration_ms").unwrap_or(0.0)
                            <= 20.0
                    {
                        "~".into()
                    } else {
                        "a".into()
                    }
                })
                .collect()
        } else {
            events
                .iter()
                .map(|e| {
                    e["lyric"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or("~")
                        .trim()
                        .to_owned()
                })
                .collect()
        }
    };
    let mut moras: Vec<Mora> = Vec::new();
    let mut slots: Vec<Option<usize>> = Vec::new();
    let mut boundaries = Vec::new();
    let (mut offset, mut explicit, mut after_boundary) = (0usize, false, false);
    for (token_index, token) in tokens.iter().enumerate() {
        if token == "|" {
            if default && (slots.is_empty() || after_boundary) {
                return Err("use | entre duas palavras do canto".into());
            }
            boundaries.push(slots.len());
            if let Some(last) = moras.last_mut() {
                last.phrase_end = "|".into();
                last.pause_after_ms = 0.0;
            }
            after_boundary = true;
            continue;
        }
        let mut token = token.clone();
        if matches!(token.as_str(), "~" | "-" | "_") {
            if slots.is_empty() || default && after_boundary {
                return Err("use ~ depois de uma sílaba, sem atravessar uma pausa".into());
            }
            let events = score["notes"].as_array();
            let after_rest = source == "midi"
                && token_index > 0
                && events.is_some_and(|e| {
                    token_index < e.len()
                        && value_num(&e[token_index], "start_ms").unwrap_or(0.0)
                            - value_num(&e[token_index - 1], "start_ms").unwrap_or(0.0)
                            - value_num(&e[token_index - 1], "duration_ms").unwrap_or(0.0)
                            > 20.0
                });
            if after_rest {
                if let Some(nucleus) = moras.iter().rfind(|m| m.role == "nucleus") {
                    let candidates = steady_candidates(nucleus, request);
                    if index.resolve(&candidates, None)?.is_none() {
                        return Err("a voicebank não contém a vogal isolada depois da pausa; escreva a sílaba nessa nota MIDI".into());
                    }
                    let first = moras.iter().filter(|m| m.role == "nucleus").count();
                    let mut n = nucleus.clone();
                    n.candidates = candidates;
                    n.word_index = offset;
                    n.syllable_index = 0;
                    n.word_moras = 1;
                    n.mora_index = 0;
                    n.word_end = true;
                    n.pause_after_ms = 0.0;
                    n.phrase_end.clear();
                    if matches!(request.voice_id.as_str(), "anna-nyui" | "pururu-purpura") {
                        n.source_phonemes = phonetics::english_vowel_source(&n.source_phonemes);
                    } else if let Some(v) = n.source_phonemes.last().cloned() {
                        n.source_phonemes = vec![v];
                    }
                    moras.push(n);
                    offset += 1;
                    slots.push(Some(first));
                    continue;
                } else {
                    token = "a".into();
                }
            } else {
                explicit = true;
                slots.push(None);
                continue;
            }
        }
        let mut chunk = phonemize(token.trim_matches('-'), request, index, true)?;
        let nuclei = chunk.iter().filter(|m| m.role == "nucleus").count();
        if source == "midi" && nuclei != 1 {
            return Err("cada letra MIDI deve conter uma sílaba; envie a letra na mensagem para alinhamento automático".into());
        }
        if nuclei == 0 {
            continue;
        }
        let first = moras.iter().filter(|m| m.role == "nucleus").count();
        slots.extend((first..first + nuclei).map(Some));
        let words = chunk.iter().map(|m| m.word_index).max().unwrap_or(0) + 1;
        for m in &mut chunk {
            m.word_index += offset;
            m.pause_after_ms = 0.0;
            m.phrase_end.clear();
        }
        offset += words;
        moras.extend(chunk);
        after_boundary = false;
    }
    if moras.len() > 1024 {
        return Err("letra MIDI contém fonemas demais".into());
    }
    if slots.is_empty() || default && after_boundary {
        return Err("a letra precisa conter uma vogal e terminar em uma sílaba".into());
    }
    if default {
        score = prosody::default_melody_score(
            if explicit || !boundaries.is_empty() {
                slots.len()
            } else {
                slots.len().max(4)
            },
            &boundaries,
        )?;
    }
    let groups = prosody::align_singing_syllables(
        &moras,
        &score,
        if explicit || source == "midi" {
            Some(&slots)
        } else {
            None
        },
    )?;
    Ok((score, moras, groups))
}
pub fn plan(request: &RenderRequest) -> Result<RenderPlan, String> {
    if !request.timeout_seconds.is_finite()
        || request.timeout_seconds <= 0.0
        || request.timeout_seconds > 120.0
        || !request.velocity.is_finite()
        || !request.modulation.is_finite()
    {
        return Err("invalid renderer deadline or controls".into());
    }
    if request.max_audio_bytes < 44
        || !request.max_audio_seconds.is_finite()
        || request.max_audio_seconds <= 0.0
        || request.max_audio_seconds > 120.0
    {
        return Err("invalid renderer output budget".into());
    }
    let performance = request.score.is_some() || request.segments.is_some();
    if performance {
        prosody::validate_performance(&request.text, &request.score, &request.segments)?;
    } else if request.text.trim().is_empty()
        || request
            .text
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .chars()
            .count()
            > request.max_characters
    {
        return Err("texto vazio ou grande demais para TTV".into());
    }
    if request.score.is_some()
        && (request.prosody.pitch_offset_semitones != 0.0 || request.prosody.speech_rate != 1.0)
    {
        return Err("canto MIDI preserva tom e andamento; use tom 0 e velocidade 1".into());
    }
    let index = VoicebankIndex::load(&request.voicebank, &request.voicebank_options)?;
    let mut score = None;
    let mut groups = None;
    let mut spans: Option<Vec<SingingSpan>> = None;
    let mut effect_ranges: Vec<(usize, usize, Value)> = Vec::new();
    let mut notes = if request.score.is_some() {
        let (s, moras, g) = singing_lyrics(request, &index)?;
        let events = s["notes"].as_array().unwrap();
        let lookup: HashMap<_, _> = moras
            .iter()
            .filter(|m| m.role == "nucleus")
            .enumerate()
            .map(|(i, m)| ((m.word_index, m.syllable_index), i))
            .collect();
        let mut notes = Vec::new();
        let mut local_spans = Vec::new();
        for mora in moras {
            let group = &g[*lookup
                .get(&(mora.word_index, mora.syllable_index))
                .ok_or("auxiliary without nucleus")?];
            let first = &events[group[0]];
            let last = &events[*group.last().unwrap()];
            let start = value_num(first, "start_ms")?;
            let end = value_num(last, "start_ms")? + value_num(last, "duration_ms")?;
            let pitches: Vec<_> = group
                .iter()
                .map(|&i| events[i]["midi_note"].as_i64().unwrap())
                .collect();
            let pitch = python_round(
                (*pitches.iter().min().unwrap() + *pitches.iter().max().unwrap()) as f64 / 2.0,
            ) as i32;
            let mut mora = mora;
            mora.duration_ms = if mora.role == "nucleus" {
                python_round(end - start).max(1.0)
            } else {
                mora.duration_ms.clamp(30.0, 70.0)
            };
            mora.pause_after_ms = 0.0;
            let mut note = prosody::note_from_mora(mora, prosody::relative_pitch("C4", pitch - 60));
            note.contour = "singing".into();
            notes.push(note);
            local_spans.push(SingingSpan {
                start_ms: start,
                end_ms: end,
                group: group.clone(),
            });
        }
        spans = Some(local_spans);
        score = Some(s);
        groups = Some(g);
        notes
    } else if let Some(segments) = request.segments.as_ref() {
        let spoken = prosody::speech_effect_segments(segments)?;
        let spoken = spoken.as_array().ok_or("invalid speech segments")?;
        let text = spoken
            .iter()
            .map(|s| {
                format!(
                    "{}{}",
                    s["text"].as_str().unwrap_or(""),
                    if !request.prosody.speech_profile.is_empty()
                        && s["pause_after_ms"].as_i64().unwrap_or(0) != 0
                    {
                        " |"
                    } else {
                        ""
                    }
                )
            })
            .collect::<Vec<_>>()
            .join(" ");
        let mut moras = phonemize(&text, request, &index, true)?;
        let mut offset = 0;
        for segment in spoken {
            let chunk = phonemize(
                segment["text"].as_str().unwrap_or(""),
                request,
                &index,
                true,
            )?;
            let count = chunk
                .iter()
                .map(|m| m.word_index)
                .max()
                .map(|n| n + 1)
                .unwrap_or(0);
            let selected: Vec<_> = moras
                .iter()
                .enumerate()
                .filter(|(_, m)| m.word_index >= offset && m.word_index < offset + count)
                .map(|(i, _)| i)
                .collect();
            if let (Some(&first), Some(&last)) = (selected.first(), selected.last()) {
                if segment["pause_after_ms"].as_i64().unwrap_or(0) != 0 {
                    moras[last].pause_after_ms = 180.0;
                    moras[last].phrase_end = "|".into();
                }
                effect_ranges.push((first, last + 1, segment.clone()));
            }
            offset += count;
        }
        let mut notes = prosody::build_notes(&moras, &request.prosody)?;
        for (first, end, segment) in &effect_ranges {
            let hold = segment["hold_count"].as_u64().unwrap_or(0);
            if hold > 0 {
                if let Some(n) = notes[*first..*end]
                    .iter_mut()
                    .rfind(|n| n.mora.role == "nucleus")
                {
                    n.mora.duration_ms += 1000.0 * hold as f64;
                }
            }
        }
        notes
    } else {
        let moras = phonemize(&request.text, request, &index, false)?;
        prosody::build_notes(&moras, &request.prosody)?
    };
    if notes.is_empty() {
        return Err("texto não gerou vogais compatíveis".into());
    }
    let cvvc = request.score.is_some() || request.voicebank_profile == "english-cvvc";
    let mut entries = notes
        .iter()
        .map(|n| {
            index.resolve(
                &n.mora.candidates,
                if request.voice_id == "anna-nyui" {
                    Some(&n.pitch)
                } else {
                    None
                },
            )
        })
        .collect::<Result<Vec<_>, _>>()?;
    if performance
        && entries
            .iter()
            .zip(&notes)
            .any(|(e, n)| e.is_none() && n.mora.role == "nucleus")
    {
        return Err("a voicebank não contém uma vogal necessária para esta apresentação".into());
    }
    if request.voice_id == "pururu-purpura" && entries.iter().any(Option::is_none) {
        return Err(
            "o banco da Pururu não contém um fonema necessário para esta apresentação".into(),
        );
    }
    if score.is_some() {
        for (i, n) in notes.iter().enumerate() {
            if n.mora.role != "nucleus" || entries[i].is_none() {
                continue;
            }
            let entry = entries[i].as_ref().unwrap();
            if n.mora.source_phonemes.len() == 1
                || request.voice_id == "anna-nyui"
                    && phonetics::english_vowel_source(&n.mora.source_phonemes)
                        == n.mora.source_phonemes
            {
                if let Some(mut vowel) = index.resolve(
                    &steady_candidates(&n.mora, request),
                    if request.voice_id == "anna-nyui" {
                        Some(&n.pitch)
                    } else {
                        None
                    },
                )? {
                    let useful = if vowel.cutoff_ms < 0.0 {
                        -vowel.cutoff_ms
                    } else {
                        let wav =
                            hound::WavReader::open(&vowel.wav_path).map_err(|e| e.to_string())?;
                        wav.duration() as f64 * 1000.0 / wav.spec().sample_rate as f64
                            - vowel.offset_ms
                            - vowel.cutoff_ms
                    };
                    if useful > 500.0 {
                        vowel.cutoff_ms = -250.0;
                    }
                    vowel.consonant_ms = if request.voice_id == "pururu-purpura" {
                        45.0
                    } else {
                        vowel.consonant_ms.max(45.0)
                    };
                    entries[i] = Some(vowel);
                }
            } else if n.mora.duration_ms >= 400.0 {
                let mut entry = entry.clone();
                let mut fixed = entry.consonant_ms.max(entry.preutterance_ms + 30.0);
                if entry.cutoff_ms < 0.0 {
                    fixed = fixed.min(entry.consonant_ms.max(-entry.cutoff_ms - 25.0));
                }
                entry.consonant_ms = fixed;
                entries[i] = Some(entry);
            }
        }
    }
    if cvvc {
        for (i, n) in notes.iter().enumerate() {
            if let Some(e) = &entries[i] {
                entries[i] = Some(timeline::render_entry(e, n, request.velocity, true)?);
            }
        }
    }
    let (placements, planned_ms, preroll) = if let Some(s) = score.as_ref() {
        let (p, preroll) = timeline::singing_placements(
            &notes,
            &entries,
            spans.as_ref().unwrap(),
            request.velocity,
        )?;
        for (n, p) in notes.iter_mut().zip(&p) {
            if let Some(d) = p.duration_ms {
                n.mora.duration_ms = d;
            }
        }
        let legacy: Vec<_> = p
            .iter()
            .map(|p| prosody::Placement {
                start_ms: p.start_ms,
                anchor_ms: p.anchor_ms,
                lead_ms: p.lead_ms,
                score_group: p.score_group.clone(),
            })
            .collect();
        notes = prosody::align_singing_pitch(&notes, &legacy, s, preroll, request.prosody.tempo)?;
        (p, value_num(s, "duration_ms")? + preroll, preroll)
    } else if cvvc {
        for (i, n) in notes.iter_mut().enumerate() {
            if let Some(e) = &entries[i] {
                let fixed = (e.consonant_ms * 2f64.powf(1.0 - request.velocity / 100.0) / 5.0)
                    .floor()
                    * 5.0;
                let required = n
                    .mora
                    .duration_ms
                    .max((fixed - timeline::lead_ms(e, n, request.velocity, true) + 5.0).ceil());
                if !performance && required > 500.0 {
                    return Err("região consonantal OTO longa demais".into());
                }
                n.mora.duration_ms = required;
            }
        }
        let (p, ms) = timeline::english_placements(&notes, &entries, request.velocity)?;
        let legacy: Vec<_> = p
            .iter()
            .map(|p| prosody::Placement {
                start_ms: p.start_ms,
                anchor_ms: p.anchor_ms,
                lead_ms: p.lead_ms,
                score_group: None,
            })
            .collect();
        notes = prosody::align_pitch_to_timeline(&notes, &legacy, request.prosody.tempo)?;
        (p, ms, 0.0)
    } else {
        let mut cursor = 0.0;
        let p = notes
            .iter()
            .map(|n| {
                let p = Placement {
                    anchor_ms: cursor,
                    start_ms: cursor,
                    lead_ms: 0.0,
                    fade_ms: 4.0,
                    ..Default::default()
                };
                cursor += n.mora.duration_ms + n.mora.pause_after_ms;
                p
            })
            .collect();
        (p, cursor, 0.0)
    };
    let limit = if score.is_some() {
        60_350.0
    } else {
        request.max_audio_seconds * 1000.0
    };
    if (cvvc || performance)
        && (planned_ms > limit
            || python_round(planned_ms * SAMPLE_RATE as f64 / 1000.0) * 2.0 + 44.0
                > request.max_audio_bytes as f64)
    {
        return Err("áudio TTV planejado excedeu os limites".into());
    }
    let mut envelopes = Vec::new();
    if score.is_some() {
        if let Some(segments) = request.segments.as_ref().and_then(Value::as_array) {
            let mut word = 0;
            for segment in segments {
                let text = segment["text"].as_str().unwrap_or("").trim();
                if matches!(text, "|" | "~") {
                    continue;
                }
                let chunk = phonemize(text, request, &index, true)?;
                let count = chunk
                    .iter()
                    .map(|m| m.word_index)
                    .max()
                    .map(|n| n + 1)
                    .unwrap_or(0);
                let ids: Vec<_> = notes
                    .iter()
                    .enumerate()
                    .filter(|(_, n)| n.mora.word_index >= word && n.mora.word_index < word + count)
                    .map(|(i, _)| i)
                    .collect();
                if !ids.is_empty() {
                    let first = ids
                        .iter()
                        .map(|&i| placements[i].start_ms)
                        .fold(f64::INFINITY, f64::min);
                    let end = ids
                        .iter()
                        .map(|&i| spans.as_ref().unwrap()[i].end_ms + preroll)
                        .fold(0.0f64, f64::max);
                    envelopes.push(Envelope {
                        first: frames(first),
                        end: frames(end),
                        fade_in: segment["fade_in"].as_bool().unwrap_or(false),
                        fade_out: segment["fade_out"].as_bool().unwrap_or(false),
                    });
                }
                word += count;
            }
        }
    } else {
        for (first, end, segment) in effect_ranges {
            let a = placements[first..end]
                .iter()
                .map(|p| p.start_ms)
                .fold(f64::INFINITY, f64::min);
            let b = (first..end)
                .map(|i| placements[i].anchor_ms + notes[i].mora.duration_ms)
                .fold(0.0f64, f64::max);
            envelopes.push(Envelope {
                first: frames(a),
                end: frames(b),
                fade_in: segment["fade_in"].as_bool().unwrap_or(false),
                fade_out: segment["fade_out"].as_bool().unwrap_or(false),
            });
        }
    }
    Ok(RenderPlan {
        notes,
        entries,
        placements,
        planned_ms,
        preroll_ms: preroll,
        score,
        performance_hash: if performance {
            Some(prosody::performance_hash(
                &request.text,
                &request.score,
                &request.segments,
            )?)
        } else {
            None
        },
        envelopes,
        voicebank_fingerprint: index.fingerprint.clone(),
        voicebank_name: index.name.clone(),
        aliases: index.alias_count(),
        timeline: cvvc || performance,
        melisma_notes: groups
            .map(|g| g.iter().map(|g| g.len().saturating_sub(1)).sum())
            .unwrap_or(0),
    })
}
fn frames(ms: f64) -> usize {
    python_round(ms * SAMPLE_RATE as f64 / 1000.0).max(0.0) as usize
}
pub fn encode_wav(samples: &[i16]) -> Result<Vec<u8>, String> {
    let mut output = Cursor::new(Vec::new());
    {
        let mut wav = hound::WavWriter::new(
            &mut output,
            hound::WavSpec {
                channels: 1,
                sample_rate: SAMPLE_RATE,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            },
        )
        .map_err(|e| e.to_string())?;
        for &sample in samples {
            wav.write_sample(sample).map_err(|e| e.to_string())?;
        }
        wav.finalize().map_err(|e| e.to_string())?;
    }
    Ok(output.into_inner())
}
pub fn decode_wav(bytes: &[u8]) -> Result<Vec<i16>, String> {
    let mut wav = hound::WavReader::new(Cursor::new(bytes)).map_err(|e| e.to_string())?;
    let spec = wav.spec();
    if spec.channels != 1
        || spec.sample_rate != SAMPLE_RATE
        || spec.bits_per_sample != 16
        || spec.sample_format != hound::SampleFormat::Int
    {
        return Err("fragmento WAV inesperado: exige mono PCM16 44100Hz".into());
    }
    let expected = wav.len() as usize;
    let samples = wav
        .samples::<i16>()
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    if samples.len() != expected || samples.is_empty() {
        return Err("fragmento WAV truncado ou vazio".into());
    }
    Ok(samples)
}

fn native_request(
    request: &RenderRequest,
    plan: &RenderPlan,
    index: usize,
    timeout: Duration,
) -> ResampleRequest {
    let note = &plan.notes[index];
    let entry = plan.entries[index]
        .as_ref()
        .expect("native request has a resolved entry");
    ResampleRequest {
        entry: entry.clone(),
        note: note.clone(),
        required_length_ms: timeline::resampler_length(
            entry,
            note,
            request.velocity,
            request.score.is_some() || request.voicebank_profile == "english-cvvc",
            request.post_consonant_length,
        ),
        velocity: request.velocity,
        volume: if plan.performance_hash.is_some() {
            50.0
        } else {
            100.0
        },
        modulation: if plan.score.is_some() {
            0.0
        } else {
            request.modulation
        },
        tempo: request.prosody.tempo.clamp(60, 240),
        flags: request.flags.clone(),
        timeout,
        singing: plan.score.is_some(),
    }
}
fn fragment_key(
    request: &RenderRequest,
    voicebank: &str,
    native: &ResampleRequest,
) -> Result<String, String> {
    let payload = json!({"renderer":"rust-ttv-fragment-v1","voicebank":voicebank,"entry":native.entry.cache_identity(),"pitch":native.note.pitch,"pitchbend":native.note.pitchbend,"duration_ms":native.note.mora.duration_ms,"role":native.note.mora.role,"required_length_ms":native.required_length_ms,"velocity":native.velocity,"volume":native.volume,"modulation":native.modulation,"tempo":native.tempo,"flags":native.flags,"backend":request.native_backend,"resampler":request.resampler,"executable_stamp":executable_stamp(&request.resampler)});
    Ok(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&payload).map_err(|e| e.to_string())?)
    ))
}
fn fragment_path(request: &RenderRequest, key: &str) -> Option<PathBuf> {
    request.fragment_cache.as_ref().map(|root| {
        root.join("rust-v1")
            .join(&key[..2])
            .join(format!("{key}.wav"))
    })
}

/// Native render callback permits resident ESPER and isolated WORLDLINE without
/// coupling the phonetics crate to platform process management.
pub fn render_with<F>(
    request: &RenderRequest,
    cancel: &Cancellation,
    resample: F,
) -> Result<RenderOutput, String>
where
    F: FnMut(&ResampleRequest) -> Result<Vec<i16>, String>,
{
    render_with_emitter(request, cancel, resample, None, None)
}

pub type PcmEmitter<'a> = dyn FnMut(&[u8], &Value) -> Result<(), String> + 'a;

pub fn render_stream_with<F>(
    request: &RenderRequest,
    cancel: &Cancellation,
    resample: F,
    emit: &mut PcmEmitter<'_>,
) -> Result<RenderOutput, String>
where
    F: FnMut(&ResampleRequest) -> Result<Vec<i16>, String>,
{
    let mut request = request.clone();
    request.unit_gain = true;
    render_with_emitter(&request, cancel, resample, Some(emit), None)
}

fn sealed_range(
    combined: &[i16],
    first: usize,
    end: usize,
    intervals: &[(isize, isize)],
    plan: &RenderPlan,
    total: usize,
) -> Result<Vec<i16>, String> {
    let mut samples = combined[first..end].to_vec();
    let mut intervals = intervals.to_vec();
    intervals.sort();
    let mut regions: Vec<(usize, usize)> = Vec::new();
    for (a, b) in intervals {
        let a = a.max(0) as usize;
        let b = (b.max(0) as usize).min(combined.len());
        if b <= a {
            continue;
        }
        if let Some(last) = regions.last_mut() {
            if a <= last.1 {
                last.1 = last.1.max(b);
                continue;
            }
        }
        regions.push((a, b));
    }
    let edge = frames(4.0);
    let pause = frames(24.0);
    for pair in regions.windows(2) {
        let (a, b) = pair[0];
        let (c, d) = pair[1];
        if c - b < pause {
            continue;
        }
        for (length, boundary, step) in [
            (edge.min((b - a) / 2), b - 1, -1isize),
            (edge.min((d - c) / 2), c, 1),
        ] {
            for offset in 0..length {
                let p = (boundary as isize + step * offset as isize) as usize;
                if p >= first && p < end {
                    let gain = (offset as f64 / length.saturating_sub(1).max(1) as f64
                        * std::f64::consts::FRAC_PI_2)
                        .sin()
                        .powi(2);
                    samples[p - first] = python_round(samples[p - first] as f64 * gain) as i16;
                }
            }
        }
    }
    let phrase_edge = (combined.len() / 2).min(edge);
    for p in first..end.min(phrase_edge) {
        samples[p - first] =
            (samples[p - first] as f64 * (p + 1) as f64 / phrase_edge.max(1) as f64).trunc() as i16;
    }
    if let Some(score) = &plan.score {
        let events: Vec<dsp::VelocityEvent> =
            serde_json::from_value(score["notes"].clone()).map_err(|e| e.to_string())?;
        dsp::apply_midi_velocity(&mut samples, &events, plan.preroll_ms, first, total)?;
    }
    for e in &plan.envelopes {
        dsp::performance_envelope(
            &mut samples,
            e.first as isize,
            e.end as isize,
            e.fade_in,
            e.fade_out,
            first,
            total,
        );
    }
    Ok(samples)
}

fn emit_samples(
    emit: &mut PcmEmitter<'_>,
    samples: &[i16],
    metadata: &Value,
    cancel: &Cancellation,
) -> Result<(), String> {
    for chunk in samples.chunks(SAMPLE_RATE as usize / 2) {
        cancel.check()?;
        emit(&dsp::encode_pcm16(chunk), metadata)?;
    }
    Ok(())
}

fn render_with_emitter<F>(
    request: &RenderRequest,
    cancel: &Cancellation,
    mut resample: F,
    mut emit: Option<&mut PcmEmitter<'_>>,
    prepared: Option<RenderPlan>,
) -> Result<RenderOutput, String>
where
    F: FnMut(&ResampleRequest) -> Result<Vec<i16>, String>,
{
    let started = Instant::now();
    cancel.check()?;
    let plan = if let Some(plan) = prepared {
        plan
    } else {
        plan(request)?
    };
    let deadline = started + Duration::from_secs_f64(request.timeout_seconds);
    let performance = plan.performance_hash.is_some();
    if emit.is_some() && plan.score.is_none() {
        return Err("PCM progressivo exige uma partitura TTV".into());
    }
    let target_frames = frames(plan.planned_ms);
    let stream_metadata = json!({"pcm_protocol":1,"audio_format":"pcm_s16le","sample_rate":SAMPLE_RATE,"channels":1,"sample_width_bytes":2,"normalization_gain":1.0,"midi_preroll_ms":python_round(plan.preroll_ms*1000.0)/1000.0,"voice_id":request.voice_id,"reading_language":request.reading_language,"pitch_offset_semitones":request.prosody.pitch_offset_semitones,"speech_rate":request.prosody.speech_rate,"ttv_performance_hash":plan.performance_hash,"expected_frames":target_frames,"duration_seconds":target_frames as f64/SAMPLE_RATE as f64,"pcm_normalization_policy":"unitgain-native-v1"});
    if let Some(emit) = emit.as_mut() {
        emit(&[], &stream_metadata)?;
    }
    let mut future_starts = vec![target_frames; plan.notes.len() + 1];
    for i in (0..plan.notes.len()).rev() {
        future_starts[i] = future_starts[i + 1].min(if plan.entries[i].is_some() {
            frames(plan.placements[i].start_ms + plan.placements[i].trim_lead_ms.unwrap_or(0.0))
        } else {
            target_frames
        });
    }
    let mut stream_cursor = 0usize;
    let mut first_pcm_ms = None;
    let mut combined = Vec::new();
    let mut intervals = Vec::new();
    let mut rendered = 0usize;
    let mut missing = Vec::new();
    let mut repairs = 0usize;
    let mut max_db = 0.0f64;
    let mut resampler_calls = 0usize;
    let mut hits = 0usize;
    for (i, note) in plan.notes.iter().enumerate() {
        cancel.check()?;
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("renderização TTV excedeu o timeout")?;
        let Some(entry) = &plan.entries[i] else {
            missing.push(note.mora.candidates.first().cloned().unwrap_or("?".into()));
            if !plan.timeline {
                combined.resize(
                    combined.len()
                        + (SAMPLE_RATE as f64 * note.mora.duration_ms.min(160.0) / 1000.0) as usize,
                    0,
                );
            }
            continue;
        };
        let native = native_request(request, &plan, i, remaining);
        let key = fragment_key(request, &plan.voicebank_fingerprint, &native)?;
        let cache = fragment_path(request, &key);
        let saved = cache
            .as_ref()
            .and_then(|p| fs::read(p).ok())
            .and_then(|b| decode_wav(&b).ok());
        let fragment = if let Some(pcm) = saved {
            hits += 1;
            pcm
        } else {
            resampler_calls += 1;
            let pcm = resample(&native)?;
            cancel.check()?;
            if let Some(target) = &cache {
                let _ = publish_fragment(target, &pcm, request.fragment_cache_max_bytes);
            }
            pcm
        };
        let mut fragment = dsp::apply_gain(&fragment, note.gain)?;
        if plan.timeline {
            let p = &plan.placements[i];
            fragment.truncate(frames(note.mora.duration_ms + p.lead_ms).max(1));
            let mut start = python_round(p.start_ms * SAMPLE_RATE as f64 / 1000.0) as isize;
            let trim = frames(p.trim_lead_ms.unwrap_or(0.0)).min(fragment.len());
            if trim > 0 {
                fragment.drain(..trim);
                start += trim as isize;
            }
            let fade = frames(p.fade_ms);
            let (matched, db, repaired) =
                dsp::match_fragment_energy(&combined, &fragment, start, fade, &note.mora.role);
            fragment = matched;
            repairs += usize::from(repaired);
            max_db = max_db.max(db);
            dsp::place_timeline_fragment(&mut combined, &fragment, start, fade);
            intervals.push((start.max(0), start + fragment.len() as isize));
        } else {
            let mut overlap = entry
                .overlap_ms
                .max(entry.preutterance_ms * 0.45)
                .max(16.0)
                .clamp(10.0, 55.0);
            match note.mora.role.as_str() {
                "epenthetic" => overlap = overlap.max(note.mora.duration_ms * 0.58).min(60.0),
                "coda" | "glide" | "nasal" => {
                    overlap = overlap.max(note.mora.duration_ms * 0.42).min(58.0)
                }
                _ => (),
            };
            dsp::append_crossfade(
                &mut combined,
                &fragment,
                (SAMPLE_RATE as f64 * overlap / 1000.0) as usize,
            );
            if note.mora.pause_after_ms >= 24.0 {
                combined.resize(
                    combined.len()
                        + (SAMPLE_RATE as f64 * note.mora.pause_after_ms / 1000.0) as usize,
                    0,
                );
            }
        }
        rendered += 1;
        if let Some(emit) = emit.as_mut() {
            let reserve = frames(4.0);
            let end = target_frames
                .min(combined.len().saturating_sub(reserve))
                .min(future_starts[i + 1].saturating_sub(reserve));
            if end.saturating_sub(stream_cursor) >= SAMPLE_RATE as usize / 10 {
                let samples = sealed_range(
                    &combined,
                    stream_cursor,
                    end,
                    &intervals,
                    &plan,
                    target_frames,
                )?;
                emit_samples(*emit, &samples, &stream_metadata, cancel)?;
                stream_cursor = end;
                first_pcm_ms.get_or_insert_with(|| started.elapsed().as_secs_f64() * 1000.0);
            }
        }
        if combined.len() * 2 + 44 > request.max_audio_bytes {
            return Err("áudio TTV grande demais".into());
        }
    }
    if rendered == 0 {
        return Err("nenhum alias da voicebank correspondeu ao texto".into());
    }
    let gap_fades = if plan.timeline {
        let n = dsp::fade_timeline_gaps(&mut combined, &intervals);
        dsp::phrase_micro_fades(&mut combined);
        n
    } else {
        0
    };
    let normalization_gain;
    if performance {
        combined.resize(frames(plan.planned_ms), 0);
        let peak = combined
            .iter()
            .map(|x| (*x as i32).abs())
            .max()
            .unwrap_or(0);
        if peak == 0 {
            return Err("a apresentação TTV gerou áudio silencioso".into());
        }
        let scale = if request.unit_gain {
            1.0
        } else {
            (30000.0 / peak as f64).min(3.6)
        };
        normalization_gain = if (scale - 1.0).abs() < 0.002 {
            1.0
        } else {
            scale.clamp(0.60, 1.20)
        };
        combined = dsp::apply_gain(&combined, scale)?;
        if let Some(score) = &plan.score {
            let events: Vec<dsp::VelocityEvent> =
                serde_json::from_value(score["notes"].clone()).map_err(|e| e.to_string())?;
            let total = combined.len();
            dsp::apply_midi_velocity(&mut combined, &events, plan.preroll_ms, 0, total)?;
        }
        for e in &plan.envelopes {
            let total = combined.len();
            dsp::performance_envelope(
                &mut combined,
                e.first as isize,
                e.end as isize,
                e.fade_in,
                e.fade_out,
                0,
                total,
            );
        }
    } else {
        if combined.len() > frames(request.max_audio_seconds * 1000.0) {
            return Err("áudio TTV excedeu duração máxima".into());
        }
        normalization_gain = dsp::normalize_peak(&mut combined);
    }
    cancel.check()?;
    let audio = encode_wav(&combined)?;
    if audio.len() > request.max_audio_bytes {
        return Err("áudio TTV grande demais".into());
    }
    let sha = format!("{:x}", Sha256::digest(&audio));
    if let Some(emit) = emit.as_mut() {
        emit_samples(*emit, &combined[stream_cursor..], &stream_metadata, cancel)?;
        stream_cursor = combined.len();
        first_pcm_ms.get_or_insert_with(|| started.elapsed().as_secs_f64() * 1000.0);
    }
    let mut metadata = json!({"audio_format":"wav","voice_id":request.voice_id,"reading_language":request.reading_language,"voicebank":plan.voicebank_name,"voicebank_fingerprint":plan.voicebank_fingerprint,"renderer_version":"rust-ttv-v1","phonemizer_version":"rust-phonetics-v1","aliases":plan.aliases,"rendered_phonemes":rendered,"missing_phonemes":missing,"fragment_cache_hits":hits,"fragment_cache_misses":resampler_calls,"resampler_calls":resampler_calls,"normalization_gain":normalization_gain,"timeline_mode":if plan.score.is_some(){"midi-vowel-continuous"}else if performance{"phrase-effects-continuous"}else if plan.timeline{"phrase-continuous"}else{"serial"},"timeline_planned_ms":plan.planned_ms,"timeline_audio_ms":combined.len()as f64*1000.0/SAMPLE_RATE as f64,"midi_preroll_ms":plan.preroll_ms,"melisma_notes":plan.melisma_notes,"continuity_repairs":repairs,"energy_boundary_max_db":max_db,"silence_gap_fades":gap_fades,"duration_seconds":combined.len()as f64/SAMPLE_RATE as f64,"sample_rate":SAMPLE_RATE,"channels":1,"sample_width_bytes":2,"sha256":sha,"worker_synth_ms":started.elapsed().as_secs_f64()*1000.0});
    if let Some(hash) = plan.performance_hash {
        metadata["ttv_performance_hash"] = json!(hash);
        metadata["performance_version"] = json!("rust-ttv-performance-v1");
        metadata["performance_mode"] = json!(if plan.score.is_some() {
            "singing"
        } else {
            "speech-effects"
        });
    }
    if let Some(score) = plan.score {
        metadata["effective_score"] = score;
    }
    if emit.is_some() {
        metadata["pcm_protocol"] = json!(1);
        metadata["pcm_normalization_policy"] = json!("unitgain-native-v1");
        metadata["first_pcm_ms"] = json!(first_pcm_ms.unwrap_or(0.0));
        metadata["streamed_frames"] = json!(stream_cursor);
    }
    Ok(RenderOutput { audio, metadata })
}
fn executable_stamp(command: &[String]) -> Value {
    let Some(program) = command.first() else {
        return Value::Null;
    };
    let path = if Path::new(program).components().count() > 1 {
        PathBuf::from(program)
    } else {
        std::env::var_os("PATH")
            .and_then(|p| {
                std::env::split_paths(&p)
                    .map(|p| p.join(program))
                    .find(|p| p.is_file())
            })
            .unwrap_or_else(|| PathBuf::from(program))
    };
    fs::metadata(&path).map(|m|json!({"path":path,"bytes":m.len(),"mtime_ns":m.modified().ok().and_then(|t|t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d|d.as_nanos().to_string())})).unwrap_or(Value::Null)
}
fn publish_fragment(target: &Path, pcm: &[i16], max: u64) -> Result<(), String> {
    let bytes = encode_wav(pcm)?;
    if bytes.len() as u64 > max {
        return Ok(());
    }
    let parent = target.parent().ok_or("invalid cache target")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    // Rust owns its versioned shard directory. A decoded fragment is already
    // immutable in RAM, so pruning never invalidates another render's samples.
    let root = parent.parent().ok_or("invalid cache shard")?;
    use fs2::FileExt;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(root.join("quota.lock"))
        .map_err(|e| e.to_string())?;
    lock.lock_exclusive().map_err(|e| e.to_string())?;
    let mut files = Vec::new();
    let mut total = 0u64;
    for shard in fs::read_dir(root).map_err(|e| e.to_string())? {
        let shard = shard.map_err(|e| e.to_string())?;
        let name = shard.file_name();
        let name = name.to_string_lossy();
        if name.len() != 2
            || !name.bytes().all(|b| b.is_ascii_hexdigit())
            || !shard.file_type().map_err(|e| e.to_string())?.is_dir()
        {
            continue;
        }
        for file in fs::read_dir(shard.path()).map_err(|e| e.to_string())? {
            let file = file.map_err(|e| e.to_string())?;
            let name = file.file_name();
            let name = name.to_string_lossy();
            if name.len() != 68
                || !name.ends_with(".wav")
                || !name[..64].bytes().all(|b| b.is_ascii_hexdigit())
            {
                continue;
            }
            let info = file.metadata().map_err(|e| e.to_string())?;
            if !info.is_file() {
                continue;
            }
            if file.path() != target {
                total = total.saturating_add(info.len());
                files.push((info.modified().ok(), file.path(), info.len()));
            }
        }
    }
    files.sort_by_key(|(time, path, _)| (*time, path.clone()));
    for (_, path, size) in files {
        if total + bytes.len() as u64 <= max {
            break;
        }
        match fs::remove_file(path) {
            Ok(()) => total = total.saturating_sub(size),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                total = total.saturating_sub(size)
            }
            Err(_) => (),
        }
    }
    if total + bytes.len() as u64 > max {
        return Ok(());
    }
    let mut file = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
    std::io::Write::write_all(&mut file, &bytes).map_err(|e| e.to_string())?;
    file.persist(target).map_err(|e| e.to_string())?;
    Ok(())
}
/// Validate the configured executable, avoiding bridges that would quietly
/// reintroduce the Python engine path.
fn native_command(args: &[String]) -> Result<Command, String> {
    if args.is_empty() {
        return Err("resampler nativo não configurado".into());
    }
    let forbidden = regex::Regex::new(r"(?i)(?:^|[^a-z])python[0-9.]*\b|\.py(?:$|\s)").unwrap();
    if args.iter().any(|s| forbidden.is_match(s)) {
        return Err("renderer Rust não executa motores/bridges Python".into());
    }
    let name = Path::new(&args[0])
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    if matches!(name, "sh" | "bash" | "zsh" | "dash") {
        return Err("configure o executável nativo diretamente, sem shell".into());
    }
    let mut command = Command::new(&args[0]);
    command.args(&args[1..]);
    Ok(command)
}
fn utau_args(native: &ResampleRequest, source: &Path, output: &Path) -> Vec<String> {
    vec![
        source.to_string_lossy().into_owned(),
        output.to_string_lossy().into_owned(),
        native.note.pitch.clone(),
        number(native.velocity),
        native.flags.clone(),
        number(native.entry.offset_ms),
        number(native.required_length_ms),
        number(native.entry.consonant_ms),
        number(native.entry.cutoff_ms),
        number(native.volume),
        number(native.modulation),
        format!("!{}", native.tempo),
        native.note.pitchbend.clone(),
    ]
}

/// A source analysis lease protects native sidecars. Original voicebank WAVs
/// remain read-only even when ESPER/Straycat creates adjacent analysis files.
struct SourceLease {
    path: PathBuf,
    _lock: fs::File,
    committed: bool,
}
/// Group physical recordings, including aliases reached through hard links.
/// Independent files with equal PCM still get independent analysis leases.
#[derive(Debug, Clone, Hash, PartialEq, Eq, Serialize)]
struct SourceIdentity {
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(not(unix))]
    path: PathBuf,
}
fn source_identity(source: &Path) -> Result<SourceIdentity, String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = fs::metadata(source).map_err(|e| e.to_string())?;
        Ok(SourceIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
    #[cfg(not(unix))]
    {
        Ok(SourceIdentity {
            path: fs::canonicalize(source).map_err(|e| e.to_string())?,
        })
    }
}
impl Drop for SourceLease {
    fn drop(&mut self) {
        if !self.committed {
            if let Some(parent) = self.path.parent() {
                for name in [
                    "source.esp",
                    "source_wav.frq",
                    "source_wav.sc",
                    "analysis-ready.json",
                ] {
                    let _ = fs::remove_file(parent.join(name));
                }
            }
        }
    }
}
fn source_lease(
    root: &Path,
    source: &Path,
    command: &[String],
    deadline: Instant,
    cancel: &Cancellation,
) -> Result<SourceLease, String> {
    use fs2::FileExt;
    let before = fs::metadata(source).map_err(|e| e.to_string())?;
    if !before.is_file() || before.len() > 64 * 1024 * 1024 {
        return Err("fonte musical grande demais ou inválida".into());
    }
    let bytes = fs::read(source).map_err(|e| e.to_string())?;
    let after = fs::metadata(source).map_err(|e| e.to_string())?;
    if before.len() != after.len()
        || before.modified().ok() != after.modified().ok()
        || bytes.len() as u64 != after.len()
    {
        return Err("fonte da voicebank mudou durante leitura".into());
    }
    let mut digest = Sha256::new();
    digest.update(b"rust-private-native-source-v2\0");
    digest.update(
        serde_json::to_vec(&(command, executable_stamp(command), source_identity(source)?))
            .map_err(|e| e.to_string())?,
    );
    digest.update(&bytes);
    let key = format!("{:x}", digest.finalize());
    let directory = root.join(&key[..2]).join(key);
    fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
    for parent in directory.ancestors() {
        if fs::symlink_metadata(parent)
            .map_err(|e| e.to_string())?
            .file_type()
            .is_symlink()
        {
            return Err("cache de análise nativa não pode atravessar symlinks".into());
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
            .map_err(|e| e.to_string())?;
    }
    let mut options = fs::OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW).mode(0o600);
    }
    let lock = options
        .open(directory.join("analysis.lock"))
        .map_err(|e| e.to_string())?;
    loop {
        cancel.check()?;
        if Instant::now() >= deadline {
            return Err("prazo da análise nativa esgotado".into());
        }
        match lock.try_lock_exclusive() {
            Ok(()) => break,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10))
            }
            Err(e) => return Err(e.to_string()),
        }
    }
    let path = directory.join("source.wav");
    if fs::symlink_metadata(&path).is_ok_and(|m| !m.is_file()) {
        return Err("fonte privada de análise nativa inválida".into());
    }
    let same = fs::read(&path).map(|v| v == bytes).unwrap_or(false);
    if !same {
        if path.exists() {
            return Err("fonte privada do cache mudou; use outra pasta de cache".into());
        }
        let mut temporary =
            tempfile::NamedTempFile::new_in(&directory).map_err(|e| e.to_string())?;
        std::io::Write::write_all(&mut temporary, &bytes).map_err(|e| e.to_string())?;
        temporary.persist(&path).map_err(|e| e.to_string())?;
    }
    for (name, max) in [
        ("source.esp", 256 * 1024 * 1024),
        ("source_wav.frq", 64 * 1024 * 1024),
        ("source_wav.sc", 256 * 1024 * 1024),
    ] {
        let p = directory.join(name);
        if let Ok(m) = fs::symlink_metadata(&p) {
            if !m.is_file() || m.len() > max {
                return Err("análise nativa em cache inválida ou grande demais".into());
            }
        }
    }
    Ok(SourceLease {
        path,
        _lock: lock,
        committed: false,
    })
}
type SharedResident = Arc<std::sync::Mutex<Option<tts_engines::esper::EsperResident>>>;
static SESSIONS: std::sync::OnceLock<std::sync::Mutex<HashMap<String, SharedResident>>> =
    std::sync::OnceLock::new();
fn resident_for(command: &[String], slot: usize) -> Result<SharedResident, String> {
    let key = serde_json::to_string(&(command, slot)).map_err(|e| e.to_string())?;
    let mut sessions = SESSIONS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| "resident registry poisoned")?;
    if let Some(session) = sessions.get(&key) {
        return Ok(session.clone());
    }
    if sessions.len() >= 8 {
        return Err("limite de motores residentes distintos atingido".into());
    }
    let session = Arc::new(std::sync::Mutex::new(None));
    sessions.insert(key, session.clone());
    Ok(session)
}
pub fn close_residents() {
    if let Some(registry) = SESSIONS.get() {
        if let Ok(mut sessions) = registry.lock() {
            for (_, slot) in sessions.drain() {
                if let Ok(mut slot) = slot.lock() {
                    if let Some(mut engine) = slot.take() {
                        engine.close();
                    }
                }
            }
        }
    }
}
pub fn render_command(
    request: &RenderRequest,
    cancel: &Cancellation,
) -> Result<RenderOutput, String> {
    render_command_with_emitter(request, cancel, None)
}
/// Emit committed native PCM prefixes before later fragments are synthesized.
/// Empty bytes carry the immutable format metadata; success is confirmed only
/// by the returned RenderOutput. A callback error cancels the presentation.
pub fn render_command_stream(
    request: &RenderRequest,
    cancel: &Cancellation,
    emit: &mut PcmEmitter<'_>,
) -> Result<RenderOutput, String> {
    let mut request = request.clone();
    request.unit_gain = true;
    render_command_with_emitter(&request, cancel, Some(emit))
}
fn render_command_with_emitter(
    request: &RenderRequest,
    cancel: &Cancellation,
    emit: Option<&mut PcmEmitter<'_>>,
) -> Result<RenderOutput, String> {
    cancel.check()?;
    if request.native_backend == "worldline-r" {
        if emit.is_some() {
            return Err("PCM progressivo de canto exige ESPER; WORLDLINE usa API de frase".into());
        }
        return crate::worldline::render_worldline(request, cancel);
    }
    let mut resolved = request.clone();
    // Validate limits before converting a timeout or starting an engine.
    let plan = plan(&resolved)?;
    if emit.is_some() && plan.score.is_none() {
        return Err("PCM progressivo exige uma partitura TTV".into());
    }
    if resolved.native_backend == "esper-installed" {
        let root = resolved
            .native_source_cache
            .as_ref()
            .and_then(|p| p.parent())
            .map(Path::to_owned)
            .ok_or("cache ESPER nativo não configurado")?;
        fs::create_dir_all(root.join("jobs")).map_err(|e| e.to_string())?;
        resolved.resampler = tts_engines::esper::installed_resident_command(
            &resolved.engine_environment,
            &root,
            Duration::from_secs_f64(resolved.timeout_seconds),
            cancel.engine(),
        )
        .map_err(|e| e.to_string())?;
        resolved.native_backend = "esper-resident".into();
    }
    let request = &resolved;
    native_command(&request.resampler)?;
    if !matches!(
        request.native_backend.as_str(),
        "command" | "esper-resident"
    ) {
        return Err("backend de resampler nativo desconhecido".into());
    }
    let work = if request.native_backend == "esper-resident" {
        let jobs = request
            .native_source_cache
            .as_ref()
            .and_then(|p| p.parent())
            .ok_or("cache ESPER nativo não configurado")?
            .join("jobs");
        fs::create_dir_all(&jobs).map_err(|e| e.to_string())?;
        tempfile::tempdir_in(jobs).map_err(|e| e.to_string())?
    } else {
        tempfile::tempdir().map_err(|e| e.to_string())?
    };
    let source_root = request
        .native_source_cache
        .clone()
        .or_else(|| {
            request
                .fragment_cache
                .as_ref()
                .map(|p| p.join("native-sources"))
        })
        .unwrap_or_else(|| work.path().join("native-sources"));
    let deadline = Instant::now() + Duration::from_secs_f64(request.timeout_seconds);
    let mut by_source: HashMap<SourceIdentity, Vec<(usize, ResampleRequest, String)>> =
        HashMap::new();
    let mut key_index = HashMap::new();
    for i in 0..plan.notes.len() {
        if let Some(entry) = &plan.entries[i] {
            let native = native_request(
                request,
                &plan,
                i,
                deadline.saturating_duration_since(Instant::now()),
            );
            let key = fragment_key(request, &plan.voicebank_fingerprint, &native)?;
            if key_index.contains_key(&key) {
                continue;
            }
            key_index.insert(key.clone(), i);
            if fragment_path(request, &key)
                .and_then(|p| fs::read(p).ok())
                .and_then(|b| decode_wav(&b).ok())
                .is_none()
            {
                by_source
                    .entry(source_identity(&entry.wav_path)?)
                    .or_default()
                    .push((i, native, key));
            }
        }
    }
    let mut groups: Vec<_> = by_source.into_values().collect();
    for group in &mut groups {
        group.sort_by(|a, b| {
            plan.placements[a.0]
                .start_ms
                .total_cmp(&plan.placements[b.0].start_ms)
                .then(a.0.cmp(&b.0))
        });
    }
    groups.sort_by(|a, b| {
        plan.placements[a[0].0]
            .start_ms
            .total_cmp(&plan.placements[b[0].0].start_ms)
            .then(a[0].0.cmp(&b[0].0))
    });
    let scheduled: std::collections::HashSet<_> = groups
        .iter()
        .flat_map(|g| g.iter().map(|(_, _, key)| key.clone()))
        .collect();
    let source_groups = groups.len();
    let workers = request
        .render_threads
        .max(1)
        .min(if request.voice_id == "hatsune-miku" {
            4
        } else {
            2
        })
        .min(source_groups.max(1));
    let queue = std::sync::Mutex::new(std::collections::VecDeque::from(groups));
    let output = std::thread::scope(|scope| {
        let (tx, rx) =
            std::sync::mpsc::sync_channel::<(String, Result<PathBuf, String>)>(workers * 2);
        let mut handles = Vec::new();
        for slot in 0..workers {
            let tx = tx.clone();
            let queue = &queue;
            let work = work.path();
            let source_root = &source_root;
            handles.push(scope.spawn(move || -> Result<(), String> {
                loop {
                    if cancel.is_cancelled() {
                        return Ok(());
                    }
                    let group = queue
                        .lock()
                        .map_err(|_| "fila de fragmentos indisponível")?
                        .pop_front();
                    let Some(group) = group else {
                        return Ok(());
                    };
                    for (i, mut native, key) in group {
                        if cancel.is_cancelled() {
                            return Ok(());
                        }
                        native.timeout = deadline.saturating_duration_since(Instant::now());
                        let result = execute_native_fragment(
                            request,
                            &native,
                            work,
                            source_root,
                            slot,
                            i,
                            cancel,
                        );
                        let failure = result.as_ref().err().cloned();
                        let sent = tx.send((key, result)).is_ok();
                        if let Some(error) = failure {
                            return Err(error);
                        }
                        if !sent {
                            return Ok(());
                        }
                    }
                }
            }));
        }
        drop(tx);
        let mut pending: HashMap<String, PathBuf> = HashMap::new();
        let result = render_with_emitter(
            request,
            cancel,
            |native| {
                let key = fragment_key(request, &plan.voicebank_fingerprint, native)?;
                if !scheduled.contains(&key) {
                    let i = *key_index
                        .get(&key)
                        .ok_or("fragmento sem identidade no plano")?;
                    let path = execute_native_fragment(
                        request,
                        native,
                        work.path(),
                        &source_root,
                        0,
                        i,
                        cancel,
                    )?;
                    return decode_wav(&fs::read(path).map_err(|e| e.to_string())?);
                }
                loop {
                    cancel.check()?;
                    if let Some(path) = pending.get(&key) {
                        return decode_wav(&fs::read(path).map_err(|e| e.to_string())?);
                    }
                    if Instant::now() >= deadline {
                        return Err("renderização TTV excedeu o timeout".into());
                    }
                    match rx.recv_timeout(Duration::from_millis(25)) {
                        Ok((key, result)) => {
                            pending.insert(key, result?);
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => (),
                        Err(_) => {
                            return Err("grupo nativo terminou sem entregar o fragmento".into())
                        }
                    }
                }
            },
            emit,
            Some(plan.clone()),
        );
        if result.is_err() {
            cancel.cancel();
        }
        drop(rx);
        // Join even after cancellation: the caller's physical resource lease
        // must outlive every child process and source-analysis lock. Also check
        // failures whose fragment was concurrently supplied by another cache.
        let mut worker_failure = None;
        for handle in handles {
            match handle.join() {
                Ok(Ok(())) => (),
                Ok(Err(error)) => {
                    worker_failure.get_or_insert(error);
                    cancel.cancel();
                }
                Err(_) => {
                    worker_failure.get_or_insert_with(|| {
                        "worker de renderização nativa encerrou inesperadamente".into()
                    });
                    cancel.cancel();
                }
            }
        }
        match (result, worker_failure) {
            (Ok(_), Some(error)) => Err(error),
            (result, _) => result,
        }
    });
    let mut output = output?;
    output.metadata["render_workers"] = json!(workers);
    output.metadata["source_groups"] = json!(source_groups);
    Ok(output)
}

fn execute_native_fragment(
    request: &RenderRequest,
    native: &ResampleRequest,
    work: &Path,
    source_root: &Path,
    slot: usize,
    sequence: usize,
    cancel: &Cancellation,
) -> Result<PathBuf, String> {
    let output = work.join(format!("{sequence}.wav"));
    let deadline = Instant::now() + native.timeout;
    let mut source = source_lease(
        source_root,
        &native.entry.wav_path,
        &request.resampler,
        deadline,
        cancel,
    )?;
    let args = utau_args(native, &source.path, &output);
    let resident = if request.native_backend == "esper-resident" {
        Some(resident_for(&request.resampler, slot)?)
    } else {
        None
    };
    let mut session_guard = None;
    let result = (|| {
        if let Some(resident) = &resident {
            let mut session = loop {
                cancel.check()?;
                if Instant::now() >= deadline {
                    return Err("prazo do motor residente esgotado".into());
                }
                match resident.try_lock() {
                    Ok(g) => break g,
                    Err(std::sync::TryLockError::WouldBlock) => {
                        thread::sleep(Duration::from_millis(10))
                    }
                    Err(_) => return Err("motor residente indisponível".into()),
                }
            };
            if session.as_ref().and_then(|s| s.process_id()).is_none() {
                *session = Some(
                    tts_engines::esper::EsperResident::spawn(
                        &mut native_command(&request.resampler)?,
                        deadline.saturating_duration_since(Instant::now()),
                        cancel.engine(),
                    )
                    .map_err(|e| e.to_string())?,
                );
            }
            // Store the guard before any fallible engine work: every failure,
            // including decode failure, closes this physical session while its
            // source-analysis lease is still owned by this call.
            session_guard = Some(session);
            session_guard
                .as_mut()
                .unwrap()
                .as_mut()
                .unwrap()
                .render_utau(
                    &args,
                    deadline.saturating_duration_since(Instant::now()),
                    cancel.engine(),
                )
                .map_err(|e| e.to_string())?;
        } else {
            tts_engines::process::run_resampler(
                &mut native_command(&request.resampler)?,
                &args,
                deadline.saturating_duration_since(Instant::now()),
                cancel.engine(),
            )
            .map_err(|e| e.to_string())?;
        }
        cancel.check()?;
        let bytes = fs::read(&output).map_err(|e| e.to_string())?;
        let target = if decode_wav(&bytes).is_ok() {
            output.clone()
        } else {
            let normalized = work.join(format!("{sequence}-normalized.wav"));
            let args = vec![
                "-nostdin".into(),
                "-hide_banner".into(),
                "-loglevel".into(),
                "error".into(),
                "-y".into(),
                "-i".into(),
                output.to_string_lossy().into_owned(),
                "-ac".into(),
                "1".into(),
                "-ar".into(),
                SAMPLE_RATE.to_string(),
                "-c:a".into(),
                "pcm_s16le".into(),
                normalized.to_string_lossy().into_owned(),
            ];
            run_child("ffmpeg", &args, deadline, cancel)?;
            decode_wav(&fs::read(&normalized).map_err(|e| e.to_string())?)?;
            normalized
        };
        cancel.check()?;
        Ok(target)
    })();
    if result.is_ok() {
        source.committed = true;
    } else if let Some(session) = session_guard.as_mut() {
        if let Some(mut engine) = session.take() {
            engine.close();
        }
    }
    result
}

fn number(n: f64) -> String {
    if n.fract() == 0.0 {
        format!("{n:.0}")
    } else {
        format!("{n:.3}")
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_string()
    }
}
fn run_child(
    program: &str,
    args: &[String],
    deadline: Instant,
    cancel: &Cancellation,
) -> Result<std::process::ExitStatus, String> {
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().map_err(|e| format!("{program}: {e}"))?;
    let stderr = child.stderr.take();
    let reader = thread::spawn(move || {
        let mut tail = Vec::new();
        if let Some(mut stderr) = stderr {
            let mut b = [0; 4096];
            while let Ok(n) = stderr.read(&mut b) {
                if n == 0 {
                    break;
                }
                tail.extend_from_slice(&b[..n]);
                if tail.len() > 8192 {
                    tail.drain(..tail.len() - 8192);
                }
            }
        }
        tail
    });
    let result = loop {
        if cancel.is_cancelled() {
            break Err("renderização TTV cancelada".to_string());
        }
        if Instant::now() >= deadline {
            break Err("renderização TTV excedeu o timeout".to_string());
        }
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => thread::sleep(Duration::from_millis(10)),
            Err(e) => break Err(e.to_string()),
        }
    };
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.kill();
    let _ = child.wait();
    let bytes = reader.join().unwrap_or_default();
    let status = result?;
    if !status.success() {
        return Err(format!(
            "{program}: {status}: {}",
            String::from_utf8_lossy(&bytes)
        ));
    }
    Ok(status)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn wait_for_file(path: &Path) {
        let deadline = Instant::now() + Duration::from_secs(3);
        while !path.exists() {
            assert!(
                Instant::now() < deadline,
                "fixture did not reach {}",
                path.display()
            );
            thread::sleep(Duration::from_millis(5));
        }
    }
    // The Rust test executable is also a native UTAU fixture. No interpreter,
    // shell, installed voicebank or compiler is needed by these process tests.
    #[test]
    fn native_resampler_process() {
        let args: Vec<String> = std::env::args().collect();
        let Some(config) = args.iter().find_map(|a| a.strip_prefix("fixture-config=")) else {
            return;
        };
        let config: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
        let directory = PathBuf::from(config["directory"].as_str().unwrap());
        let args = &args[args.len() - 13..];
        let source = Path::new(&args[0]);
        let output = Path::new(&args[1]);
        let sequence = output.file_stem().unwrap().to_str().unwrap();
        let active = source.parent().unwrap().join("fixture-active");
        let _active = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&active)
            .expect("same physical source was analyzed concurrently");
        for name in ["source.esp", "source_wav.frq", "source_wav.sc"] {
            fs::write(source.parent().unwrap().join(name), b"fixture-analysis").unwrap();
        }
        fs::write(
            directory.join(format!("started-{sequence}")),
            std::process::id().to_string(),
        )
        .unwrap();
        let mode = config["mode"].as_str().unwrap();
        if mode == "barrier" {
            let expected = config["expected"].as_u64().unwrap() as usize;
            let deadline = Instant::now() + Duration::from_secs(3);
            while fs::read_dir(&directory)
                .unwrap()
                .filter_map(Result::ok)
                .filter(|p| p.file_name().to_string_lossy().starts_with("started-"))
                .count()
                < expected
            {
                assert!(
                    Instant::now() < deadline,
                    "independent native sources did not overlap"
                );
                thread::sleep(Duration::from_millis(5));
            }
        }
        if mode == "block" || (mode == "wait-late" && config["sequence"].as_str() == Some(sequence))
        {
            wait_for_file(&directory.join("release"));
        }
        assert_ne!(mode, "fail", "controlled native resampler failure");
        let count = frames(args[6].parse().unwrap());
        let samples: Vec<i16> = (0..count)
            .map(|i| ((i * 97) % 20001) as i16 - 10000)
            .collect();
        fs::write(output, encode_wav(&samples).unwrap()).unwrap();
        fs::remove_file(active).unwrap();
    }
    fn command_fixture(
        voice: &str,
        sources: usize,
        mode: &str,
    ) -> (tempfile::TempDir, RenderRequest, PathBuf) {
        let (bank, mut request) = bank_request(
            voice,
            if voice == "hatsune-miku" {
                "miku-v4-english"
            } else {
                ""
            },
        );
        request.text = "a olá mundo amor brasil".into();
        let original = plan(&request).unwrap();
        let mut aliases = HashMap::new();
        for entry in original.entries.iter().flatten() {
            let next = aliases.len();
            aliases.entry(entry.alias.clone()).or_insert(next);
        }
        assert!(aliases.len() >= sources);
        let bytes = fs::read(bank.path().join("source.wav")).unwrap();
        for source in 0..sources {
            fs::write(bank.path().join(format!("source-{source}.wav")), &bytes).unwrap();
        }
        let oto = fs::read_to_string(bank.path().join("oto.ini"))
            .unwrap()
            .lines()
            .map(|line| {
                let (_, entry) = line.split_once('=').unwrap();
                let alias = entry.split(',').next().unwrap();
                format!(
                    "source-{}.wav={entry}\n",
                    aliases.get(alias).copied().unwrap_or(0) % sources
                )
            })
            .collect::<String>();
        fs::write(bank.path().join("oto.ini"), oto).unwrap();
        let directory = bank.path().join("coordination");
        fs::create_dir(&directory).unwrap();
        let config = bank.path().join("fixture.json");
        fs::write(&config,json!({"directory":directory,"mode":mode,"expected":sources.min(if voice=="hatsune-miku"{4}else{2})}).to_string()).unwrap();
        request.resampler = vec![
            std::env::current_exe()
                .unwrap()
                .to_string_lossy()
                .into_owned(),
            "--exact".into(),
            "renderer::tests::native_resampler_process".into(),
            "--nocapture".into(),
            "--".into(),
            format!("fixture-config={}", config.display()),
        ];
        request.native_source_cache = Some(bank.path().join("native-sources"));
        request.fragment_cache = Some(bank.path().join("fragments"));
        request.render_threads = 99;
        request.timeout_seconds = 8.0;
        (bank, request, directory)
    }
    #[test]
    fn native_source_groups_overlap_with_legacy_two_and_four_worker_limits() {
        for (voice, workers) in [("kasane-teto", 2), ("hatsune-miku", 4)] {
            let (_bank, request, directory) = command_fixture(voice, 4, "barrier");
            let output = render_command(&request, &Cancellation::default()).unwrap();
            assert_eq!(output.metadata["render_workers"], workers);
            assert_eq!(output.metadata["source_groups"], 4);
            assert!(
                fs::read_dir(directory)
                    .unwrap()
                    .filter_map(Result::ok)
                    .filter(|p| p.file_name().to_string_lossy().starts_with("started-"))
                    .count()
                    >= 4
            );
        }
    }
    #[test]
    fn native_scheduled_pcm_precedes_late_fragment_and_equals_final_wav() {
        let (bank, request, directory) = command_fixture("hatsune-miku", 1, "wait-late");
        let plan = plan(&request).unwrap();
        let mut seen = std::collections::HashSet::new();
        let mut last = 0;
        for i in 0..plan.notes.len() {
            if plan.entries[i].is_some() {
                let native = native_request(&request, &plan, i, Duration::from_secs(1));
                if seen
                    .insert(fragment_key(&request, &plan.voicebank_fingerprint, &native).unwrap())
                {
                    last = i;
                }
            }
        }
        let config = bank.path().join("fixture.json");
        let mut settings: Value = serde_json::from_slice(&fs::read(&config).unwrap()).unwrap();
        settings["sequence"] = json!(last.to_string());
        fs::write(config, settings.to_string()).unwrap();
        let mut pcm = Vec::new();
        let mut early = false;
        let output = render_command_stream(&request, &Cancellation::default(), &mut |bytes, _| {
            if !bytes.is_empty() {
                if pcm.is_empty() {
                    early = true;
                    fs::write(directory.join("release"), b"first PCM committed").unwrap();
                }
                pcm.extend_from_slice(bytes);
            }
            Ok(())
        })
        .unwrap();
        assert!(early);
        assert_eq!(
            dsp::decode_pcm16(&pcm).unwrap(),
            decode_wav(&output.audio).unwrap()
        );
        assert_eq!(
            output.metadata["streamed_frames"].as_u64().unwrap() as usize,
            pcm.len() / 2
        );
    }
    #[test]
    fn failed_native_process_publishes_no_fragment_or_success_and_removes_analysis() {
        let (bank, request, _directory) = command_fixture("kasane-teto", 1, "fail");
        let original = fs::read(bank.path().join("source-0.wav")).unwrap();
        assert!(render_command(&request, &Cancellation::default()).is_err());
        assert!(!request
            .fragment_cache
            .as_ref()
            .unwrap()
            .join("rust-v1")
            .exists());
        for entry in walkdir::WalkDir::new(request.native_source_cache.as_ref().unwrap())
            .into_iter()
            .filter_map(Result::ok)
        {
            assert!(!["source.esp", "source_wav.frq", "source_wav.sc"]
                .iter()
                .any(|name| entry.file_name() == *name));
        }
        assert_eq!(
            fs::read(bank.path().join("source-0.wav")).unwrap(),
            original
        );
    }
    #[cfg(unix)]
    #[test]
    fn cancellation_reaps_native_process_before_analysis_lock_is_released() {
        use fs2::FileExt;
        let (_bank, request, directory) = command_fixture("kasane-teto", 1, "block");
        let cancel = Cancellation::default();
        let token = cancel.clone();
        let rendering = thread::spawn(move || render_command(&request, &token));
        let deadline = Instant::now() + Duration::from_secs(3);
        let marker = loop {
            if let Some(marker) = fs::read_dir(&directory)
                .unwrap()
                .filter_map(Result::ok)
                .find(|p| p.file_name().to_string_lossy().starts_with("started-"))
            {
                break marker.path();
            }
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(5));
        };
        let pid: i32 = fs::read_to_string(marker).unwrap().parse().unwrap();
        let cache = directory.parent().unwrap().join("native-sources");
        let lock_path = walkdir::WalkDir::new(&cache)
            .into_iter()
            .filter_map(Result::ok)
            .find(|p| p.file_name() == "analysis.lock")
            .unwrap()
            .into_path();
        let lock = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(lock_path)
            .unwrap();
        assert_eq!(
            lock.try_lock_exclusive().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
        cancel.cancel();
        assert!(rendering.join().unwrap().is_err());
        assert_eq!(
            unsafe { libc::kill(pid, 0) },
            -1,
            "child must be physically reaped before return"
        );
        lock.try_lock_exclusive().unwrap();
        for entry in walkdir::WalkDir::new(cache)
            .into_iter()
            .filter_map(Result::ok)
        {
            assert!(!["source.esp", "source_wav.frq", "source_wav.sc"]
                .iter()
                .any(|name| entry.file_name() == *name));
        }
    }
    #[cfg(unix)]
    #[test]
    fn hardlink_aliases_share_one_source_analysis_lease() {
        let bank = tempfile::tempdir().unwrap();
        let source = bank.path().join("source.wav");
        let alias = bank.path().join("alias.wav");
        fs::write(&source, encode_wav(&[1; 4410]).unwrap()).unwrap();
        fs::hard_link(&source, &alias).unwrap();
        assert_eq!(
            source_identity(&source).unwrap(),
            source_identity(&alias).unwrap()
        );
        let cache = bank.path().join("cache");
        let cancel = Cancellation::default();
        let command = vec!["native".into()];
        let _lease = source_lease(
            &cache,
            &source,
            &command,
            Instant::now() + Duration::from_secs(1),
            &cancel,
        )
        .unwrap();
        assert!(source_lease(
            &cache,
            &alias,
            &command,
            Instant::now() + Duration::from_millis(75),
            &cancel
        )
        .is_err());
    }
    fn bank_request(voice: &str, inventory: &str) -> (tempfile::TempDir, RenderRequest) {
        let temp = tempfile::tempdir().unwrap();
        let mut aliases = std::collections::BTreeSet::new();
        let options = PhonemizeOptions {
            max_moras: 1025,
            voicebank_profile: "english-cvvc".into(),
            reading_language: "pt-BR".into(),
            inventory_profile: inventory.into(),
        };
        for text in ["a", "olá", "mundo", "brasil", "amor"] {
            for mora in phonetics::phonemize(text, &options, None).unwrap() {
                for c in mora.candidates {
                    aliases.insert(c);
                }
            }
        }
        let samples: Vec<i16> = (0..22050)
            .map(|i| ((i * 97) % 20001) as i16 - 10000)
            .collect();
        fs::write(
            temp.path().join("source.wav"),
            encode_wav(&samples).unwrap(),
        )
        .unwrap();
        let suffix = if voice == "hatsune-miku" {
            "_D4"
        } else if voice == "anna-nyui" {
            "_C4"
        } else {
            ""
        };
        let oto = aliases
            .iter()
            .map(|a| format!("source.wav={a}{suffix},0,45,-400,30,10\n"))
            .collect::<String>();
        fs::write(temp.path().join("oto.ini"), oto).unwrap();
        if voice == "anna-nyui" {
            fs::write(temp.path().join("prefix.map"), "C4\t\t_C4\n").unwrap();
        }
        let request = RenderRequest {
            text: "a a a a".into(),
            voice_id: voice.into(),
            voicebank: temp.path().to_owned(),
            voicebank_options: VoicebankOptions {
                minimum_aliases: 1,
                alias_suffix: if voice == "hatsune-miku" {
                    "_D4".into()
                } else {
                    String::new()
                },
                case_sensitive: voice != "kasane-teto",
                pitch_map: voice == "anna-nyui",
            },
            voicebank_profile: "english-cvvc".into(),
            inventory_profile: inventory.into(),
            score: Some(json!({"version":1,"preset":"ttv-demo-v1"})),
            timeout_seconds: 20.0,
            ..Default::default()
        };
        (temp, request)
    }
    fn synthetic_fragment(native: &ResampleRequest) -> Vec<i16> {
        let count = frames(native.required_length_ms);
        (0..count)
            .map(|i| ((i * 97) % 20001) as i16 - 10000)
            .collect()
    }
    #[test]
    fn all_four_voices_render_default_singing_in_native_pipeline() {
        for (voice, inventory) in [
            ("kasane-teto", ""),
            ("hatsune-miku", "miku-v4-english"),
            ("anna-nyui", "anna-nyui-diva"),
            ("pururu-purpura", "pururu-purpura"),
        ] {
            let (_bank, request) = bank_request(voice, inventory);
            let mut calls = 0;
            let output = render_with(&request, &Cancellation::default(), |n| {
                calls += 1;
                Ok(synthetic_fragment(n))
            })
            .unwrap_or_else(|e| panic!("{voice}: {e}"));
            let pcm = decode_wav(&output.audio).unwrap();
            assert!(calls > 0);
            assert!(pcm.iter().any(|x| *x != 0));
            assert_eq!(output.metadata["voice_id"], voice);
            assert_eq!(output.metadata["performance_mode"], "singing");
            assert_eq!(
                output.metadata["effective_score"]["notes"]
                    .as_array()
                    .unwrap()
                    .len(),
                4
            );
        }
    }
    #[test]
    fn progressive_pcm_is_early_immutable_and_equals_final_unit_gain_pcm() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let (_bank, mut request) = bank_request("hatsune-miku", "miku-v4-english");
        request.unit_gain = true;
        let calls = AtomicUsize::new(0);
        let mut bytes = Vec::new();
        let mut first_count = None;
        let mut metadata = None;
        let output = render_stream_with(
            &request,
            &Cancellation::default(),
            |n| {
                calls.fetch_add(1, Ordering::SeqCst);
                Ok(synthetic_fragment(n))
            },
            &mut |pcm, meta| {
                metadata = Some(meta.clone());
                if !pcm.is_empty() {
                    first_count.get_or_insert(calls.load(Ordering::SeqCst));
                    bytes.extend_from_slice(pcm);
                }
                Ok(())
            },
        )
        .unwrap();
        assert!(
            first_count.unwrap() < calls.load(Ordering::SeqCst),
            "first PCM must precede final render"
        );
        assert_eq!(
            dsp::decode_pcm16(&bytes).unwrap(),
            decode_wav(&output.audio).unwrap()
        );
        let meta = metadata.unwrap();
        assert_eq!(meta["sample_rate"], SAMPLE_RATE);
        assert_eq!(meta["normalization_gain"], 1.0);
        assert_eq!(
            meta["expected_frames"].as_u64().unwrap() as usize,
            bytes.len() / 2
        );
    }
    #[test]
    fn interrupted_render_publishes_no_false_completion() {
        let (_bank, request) = bank_request("kasane-teto", "");
        let mut calls = 0;
        let mut bytes = 0;
        let result = render_stream_with(
            &request,
            &Cancellation::default(),
            |n| {
                calls += 1;
                if calls >= 4 {
                    return Err("controlled native engine failure".into());
                }
                Ok(synthetic_fragment(n))
            },
            &mut |pcm, _| {
                bytes += pcm.len();
                Ok(())
            },
        );
        assert!(result.is_err());
        assert!(
            bytes > 0,
            "fixture must exercise failure after committed PCM"
        );
    }
    #[test]
    fn failed_native_analysis_is_removed_before_source_lease_release() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("original.wav");
        fs::write(&source, encode_wav(&[1; 4410]).unwrap()).unwrap();
        let cache = temp.path().join("cache");
        let path;
        {
            let lease = source_lease(
                &cache,
                &source,
                &["native".into()],
                Instant::now() + Duration::from_secs(1),
                &Cancellation::default(),
            )
            .unwrap();
            path = lease.path.clone();
            fs::write(path.parent().unwrap().join("source.esp"), b"partial").unwrap();
            fs::write(path.parent().unwrap().join("source_wav.frq"), b"partial").unwrap();
        }
        assert!(!path.parent().unwrap().join("source.esp").exists());
        assert!(!path.parent().unwrap().join("source_wav.frq").exists());
        assert_eq!(fs::read(&source).unwrap(), encode_wav(&[1; 4410]).unwrap());
    }
    #[test]
    fn wav_is_exact_pcm16_roundtrip() {
        let samples = [-32768, -1, 0, 1, 32767];
        let wav = encode_wav(&samples).unwrap();
        assert_eq!(decode_wav(&wav).unwrap(), samples);
    }
    #[test]
    fn malformed_wav_is_rejected() {
        let mut wav = encode_wav(&[1, 2, 3]).unwrap();
        wav.truncate(wav.len() - 1);
        assert!(decode_wav(&wav).is_err());
    }
}
