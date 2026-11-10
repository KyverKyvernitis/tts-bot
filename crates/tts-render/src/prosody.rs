//! Speech and singing planning; no platform calls or synthesis processes.
use crate::phonetics::Mora;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RenderNote {
    #[serde(flatten)]
    pub mora: Mora,
    pub pitch: String,
    pub pitchbend: String,
    pub gain: f64,
    pub contour: String,
    pub pitch_start_cents: i32,
    pub pitch_end_cents: i32,
    pub pitch_peak_cents: Option<i32>,
}
impl Default for RenderNote {
    fn default() -> Self {
        note_from_mora(Mora::default(), "C4".into())
    }
}
pub fn note_from_mora(mora: Mora, pitch: String) -> RenderNote {
    RenderNote {
        mora,
        pitch,
        pitchbend: "AA".into(),
        gain: 1.0,
        contour: "neutral".into(),
        pitch_start_cents: 0,
        pitch_end_cents: 0,
        pitch_peak_cents: None,
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ProsodySettings {
    pub base_pitch: String,
    pub speech_rate: f64,
    pub tempo: i32,
    pub pitch_offset_semitones: f64,
    pub speech_profile: String,
}
impl Default for ProsodySettings {
    fn default() -> Self {
        Self {
            base_pitch: "C4".into(),
            speech_rate: 1.0,
            tempo: 140,
            pitch_offset_semitones: 0.0,
            speech_profile: String::new(),
        }
    }
}

/// Python's round uses ties-to-even, unlike Rust's f64::round.
pub fn round_even(value: f64) -> f64 {
    let low = value.floor();
    let fraction = value - low;
    if fraction < 0.5 {
        low
    } else if fraction > 0.5 {
        low + 1.0
    } else if low % 2.0 == 0.0 {
        low
    } else {
        low + 1.0
    }
}
fn iround(value: f64) -> i32 {
    round_even(value) as i32
}
fn smoothstep(value: f64) -> f64 {
    let v = value.clamp(0.0, 1.0);
    v * v * (3.0 - 2.0 * v)
}
pub fn pitch_midi(value: &str) -> i32 {
    let v = value.trim();
    let mut chars = v.chars();
    let Some(first) = chars.next() else { return 60 };
    let name = match first.to_ascii_uppercase() {
        'C' => 0,
        'D' => 2,
        'E' => 4,
        'F' => 5,
        'G' => 7,
        'A' => 9,
        'B' => 11,
        _ => return 60,
    };
    let tail = chars.as_str();
    let (alter, octave) = if let Some(o) = tail.strip_prefix('#') {
        (1, o)
    } else if let Some(o) = tail.strip_prefix('b') {
        (-1, o)
    } else {
        (0, tail)
    };
    let digits = octave.strip_prefix('-').unwrap_or(octave);
    if !(1..=2).contains(&digits.len()) || !digits.bytes().all(|x| x.is_ascii_digit()) {
        return 60;
    }
    octave
        .parse::<i32>()
        .map(|o| (o + 1) * 12 + name + alter)
        .unwrap_or(60)
}
pub fn relative_pitch(value: &str, semitones: i32) -> String {
    const NAMES: [&str; 12] = [
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
    ];
    let number = (pitch_midi(value) + semitones).clamp(0, 127);
    format!("{}{}", NAMES[(number % 12) as usize], number / 12 - 1)
}
pub fn encode_pitchbend(values: &[i32]) -> String {
    const B64: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut result = String::new();
    let mut index = 0;
    while index < values.len() {
        let value = values[index].clamp(-2048, 2047).rem_euclid(4096) as usize;
        let code = String::from_utf8(vec![B64[(value >> 6) & 63], B64[value & 63]]).unwrap();
        let mut run = 1;
        while index + run < values.len() && values[index + run] == values[index] {
            run += 1;
        }
        if run >= 3 {
            result.push_str(&format!("{code}#{run}#"));
        } else {
            for _ in 0..run {
                result.push_str(&code);
            }
        }
        index += run;
    }
    if result.is_empty() {
        "AA".into()
    } else {
        result
    }
}
fn pitch_shape(start: i32, peak: i32, end: i32, duration: f64, tempo: i32, line: bool) -> String {
    let points = iround(duration.max(1.0) * tempo as f64 * 96.0 / 60000.0).clamp(8, 64) as usize;
    let peak_at = (iround(points as f64 * 0.48) as usize).clamp(2, points - 2);
    let values = (0..points)
        .map(|index| {
            let cents = if line {
                start as f64 + (end - start) as f64 * smoothstep(index as f64 / (points - 1) as f64)
            } else if index <= peak_at {
                start as f64 + (peak - start) as f64 * smoothstep(index as f64 / peak_at as f64)
            } else {
                peak as f64
                    + (end - peak) as f64
                        * smoothstep((index - peak_at) as f64 / (points - 1 - peak_at) as f64)
            };
            iround(cents / 2.0) * 2
        })
        .collect::<Vec<_>>();
    encode_pitchbend(&values)
}
fn phrase_spans(moras: &[Mora]) -> Vec<(usize, usize)> {
    let mut result = vec![];
    let mut start = 0;
    for (i, m) in moras.iter().enumerate() {
        if !m.phrase_end.is_empty() {
            result.push((start, i));
            start = i + 1
        }
    }
    if start < moras.len() {
        result.push((start, moras.len() - 1));
    }
    result
}
fn centers(moras: &[Mora]) -> Vec<i32> {
    let mut values = vec![0; moras.len()];
    for (start, end) in phrase_spans(moras) {
        let active = (start..=end)
            .filter(|&i| moras[i].role != "vowel-exit")
            .collect::<Vec<_>>();
        let ending = moras[end].phrase_end.as_str();
        let english = moras[start..=end]
            .iter()
            .any(|m| m.profile == "english-cvvc");
        let terminal = (start..=end)
            .rev()
            .find(|&i| moras[i].role == "nucleus")
            .unwrap_or(end);
        let (first, last) = match (english, ending) {
            (true, "?") => (4.0, 14.0),
            (true, "," | ";" | ":") => (5.0, -1.0),
            (true, "!") => (7.0, -8.0),
            (true, _) => (5.0, -12.0),
            (false, "?") => (6.0, 20.0),
            (false, "," | ";" | ":") => (8.0, -2.0),
            (false, "!") => (12.0, -10.0),
            _ => (9.0, -18.0),
        };
        for (local, &index) in active.iter().enumerate() {
            let mora = &moras[index];
            let mut center = first
                + (last - first) * local as f64 / active.len().saturating_sub(1).max(1) as f64;
            if mora.role != "nucleus" {
                center -= if english { 3.0 } else { 8.0 }
            } else if mora.stressed {
                center += if english { 19.0 } else { 34.0 }
            } else if mora.deaccented {
                center -= if english { 6.0 } else { 10.0 }
            } else if mora.word_moras > 1 {
                center -= if english { 2.0 } else { 3.0 }
            }
            if let Some(&next) = active.get(local + 1) {
                if moras[next].stressed && moras[next].word_index == mora.word_index {
                    center += if english { 5.0 } else { 7.0 }
                }
            }
            if mora.word_end && mora.phrase_end.is_empty() {
                center -= if english { 2.0 } else { 3.0 }
            }
            values[index] = iround(center);
        }
        for index in start..=end {
            if moras[index].role == "vowel-exit" {
                let neighbors = [
                    active.iter().rev().find(|&&i| i < index),
                    active.iter().find(|&&i| i > index),
                ]
                .into_iter()
                .flatten()
                .map(|&i| values[i])
                .collect::<Vec<_>>();
                values[index] = if neighbors.is_empty() {
                    0
                } else {
                    iround(neighbors.iter().sum::<i32>() as f64 / neighbors.len() as f64)
                };
            }
        }
        let previous = (start..terminal)
            .rev()
            .find(|&i| moras[i].role == "nucleus");
        match ending {
            "?" => {
                if let Some(i) = previous {
                    values[i] = values[i].max(if english { 14 } else { 24 });
                }
                values[terminal] = values[terminal].max(if english { 38 } else { 64 });
            }
            "." | "\n" => {
                if let Some(i) = previous {
                    values[i] = values[i].min(if english { -8 } else { -14 });
                }
                values[terminal] = values[terminal].min(if english { -24 } else { -42 });
            }
            "!" => {
                if let Some(i) = previous {
                    values[i] = values[i].max(if english { 15 } else { 24 });
                }
                values[terminal] = values[terminal].min(if english { -12 } else { -18 });
            }
            "" => values[terminal] = values[terminal].min(if english { -16 } else { -28 }),
            _ => {}
        }
        for i in terminal + 1..=end {
            values[i] = iround(values[terminal] as f64 * 0.82);
        }
    }
    let limit = if moras.iter().any(|m| m.profile == "english-cvvc") {
        52
    } else {
        84
    };
    values.into_iter().map(|v| v.clamp(-limit, limit)).collect()
}
fn pururu_targets(moras: &[Mora]) -> (Vec<i32>, Vec<i32>, Vec<i32>) {
    let mut centers_out = vec![0; moras.len()];
    let mut starts = centers_out.clone();
    let mut ends = starts.clone();
    for (first, last) in phrase_spans(moras) {
        let anchors = (first..=last)
            .filter(|&i| moras[i].role == "nucleus")
            .collect::<Vec<_>>();
        if anchors.is_empty() {
            continue;
        }
        let syllables = anchors
            .iter()
            .enumerate()
            .map(|(n, &i)| {
                let mut m = moras[i].clone();
                m.profile = "standard".into();
                m.word_moras = m.word_syllables.max(1);
                m.word_end =
                    n == anchors.len() - 1 || moras[anchors[n + 1]].word_index != m.word_index;
                m.phrase_end = if n == anchors.len() - 1 {
                    moras[last].phrase_end.clone()
                } else {
                    String::new()
                };
                m
            })
            .collect::<Vec<_>>();
        let targets = centers(&syllables);
        for (n, &i) in anchors.iter().enumerate() {
            let center = targets[n];
            let previous = targets[n.saturating_sub(1)];
            let next = targets[(n + 1).min(targets.len() - 1)];
            centers_out[i] = center;
            starts[i] = iround((previous + center) as f64 / 2.0);
            ends[i] = iround((center + next) as f64 / 2.0);
        }
        for i in first..=last {
            if moras[i].role != "nucleus" {
                let right = anchors.partition_point(|&v| v <= i);
                let previous = targets[right.saturating_sub(1)];
                let next = targets[right.min(targets.len() - 1)];
                let boundary = iround((previous + next) as f64 / 2.0);
                centers_out[i] = boundary;
                starts[i] = boundary;
                ends[i] = boundary;
            }
        }
    }
    (centers_out, starts, ends)
}
fn duration_for(moras: &[Mora], index: usize) -> i32 {
    let m = &moras[index];
    let english = m.profile == "english-cvvc";
    let mut duration = m.duration_ms;
    duration *= match m.role.as_str() {
        "epenthetic" => 0.90,
        "transition" | "vowel-exit" => {
            if english {
                0.84
            } else {
                0.92
            }
        }
        "cluster" => {
            if english {
                0.82
            } else {
                0.90
            }
        }
        "coda" => {
            if english {
                0.84
            } else {
                0.96
            }
        }
        "glide" => {
            if english {
                0.82
            } else {
                0.94
            }
        }
        "nasal" => {
            if english {
                0.88
            } else {
                0.98
            }
        }
        _ => {
            if english {
                if m.stressed {
                    1.05
                } else if m.deaccented {
                    0.94
                } else if m.word_syllables > 1 {
                    0.98
                } else {
                    1.0
                }
            } else if m.stressed {
                1.14
            } else if m.deaccented {
                0.88
            } else if m.word_moras > 1 {
                0.90
            } else {
                1.0
            }
        }
    };
    if m.role == "nucleus" {
        if let Some(next) = moras[index + 1..].iter().find(|x| x.role != "vowel-exit") {
            if next.stressed && next.word_index == m.word_index {
                duration *= if english { 0.97 } else { 0.96 };
            }
        }
    }
    duration *= match m.phrase_end.as_str() {
        "." | "!" | "?" | "\n" => {
            if english {
                1.06
            } else {
                1.08
            }
        }
        "," | ";" | ":" => {
            if english {
                1.015
            } else {
                1.02
            }
        }
        _ => {
            if m.word_end && m.role == "nucleus" {
                if english {
                    1.01
                } else {
                    1.015
                }
            } else {
                1.0
            }
        }
    };
    let (min, max) = match m.role.as_str() {
        "epenthetic" => (30, 76),
        "transition" | "vowel-exit" | "cluster" => (30, 78),
        "coda" | "glide" => (32, 84),
        "nasal" => (42, 84),
        _ => (68, 220),
    };
    iround(duration).clamp(min, max)
}
pub fn build_notes(moras: &[Mora], settings: &ProsodySettings) -> Result<Vec<RenderNote>, String> {
    if !["", "pururu-ptbr-v1"].contains(&settings.speech_profile.as_str()) {
        return Err("perfil de prosódia TTV desconhecido".into());
    }
    if moras.is_empty() {
        return Ok(vec![]);
    }
    let pururu = settings.speech_profile == "pururu-ptbr-v1";
    let rate = if settings.speech_rate.is_finite() && settings.speech_rate > 0.0 {
        settings.speech_rate.clamp(0.5, 2.0)
    } else {
        1.0
    };
    let tempo = settings.tempo.clamp(60, 240);
    let offset = if settings.pitch_offset_semitones.is_finite() {
        (round_even(settings.pitch_offset_semitones * 2.0) / 2.0).clamp(-4.0, 4.0)
    } else {
        0.0
    };
    let coarse = offset.trunc() as i32;
    let residual = iround((offset - coarse as f64) * 100.0);
    let pitch = relative_pitch(&settings.base_pitch, coarse);
    let (centers, starts, ends) = if pururu {
        pururu_targets(moras)
    } else {
        (centers(moras), vec![], vec![])
    };
    let mut terminal = vec![String::new(); moras.len()];
    for (first, last) in phrase_spans(moras) {
        let i = (first..=last)
            .rev()
            .find(|&i| moras[i].role == "nucleus")
            .unwrap_or(last);
        terminal[i] = moras[last].phrase_end.clone();
    }
    Ok(moras
        .iter()
        .enumerate()
        .map(|(i, m)| {
            let minimum = match m.role.as_str() {
                "epenthetic" => 32,
                "transition" | "vowel-exit" | "cluster" => 34,
                "coda" | "glide" => 36,
                "nasal" => 44,
                _ => 70,
            };
            let mut duration = iround(duration_for(moras, i) as f64 / rate).clamp(minimum, 500);
            let pause = iround(m.pause_after_ms / rate).clamp(0, 1000);
            let center = centers[i];
            let prev = (0..i).rev().find(|&j| moras[j].role != "vowel-exit");
            let next = (i + 1..moras.len()).find(|&j| moras[j].role != "vowel-exit");
            let previous = prev
                .filter(|&j| moras[j].phrase_end.is_empty())
                .map(|j| centers[j])
                .unwrap_or(center);
            let following = if m.phrase_end.is_empty() {
                next.map(|j| centers[j]).unwrap_or(center)
            } else {
                center
            };
            let mut start = iround((previous + center) as f64 / 2.0);
            let mut end = iround((center + following) as f64 / 2.0);
            if pururu {
                start = starts[i];
                end = ends[i]
            }
            let english = m.profile == "english-cvvc" && !pururu;
            let mut peak = center
                + if m.role != "nucleus" {
                    0
                } else if m.stressed {
                    if english {
                        7
                    } else {
                        12
                    }
                } else if m.deaccented {
                    1
                } else if english {
                    2
                } else {
                    3
                };
            if terminal[i] == "?" {
                peak = peak.max(if english { 42 } else { 68 });
            }
            if [".", "\n"].contains(&terminal[i].as_str()) {
                peak = peak.min(center + 1);
            }
            if pururu {
                start = start.clamp(-84, 84);
                peak = peak.clamp(-84, 84);
                end = end.clamp(-84, 84);
            }
            let continuous = m.profile == "english-cvvc"
                && m.role != "nucleus"
                && ["cvvc-transition", "cluster-hit", "approximation"]
                    .contains(&m.coverage.as_str());
            let bend = pitch_shape(
                start + residual,
                peak + residual,
                end + residual,
                duration as f64,
                tempo,
                continuous,
            );
            if continuous {
                duration = duration.min(match m.role.as_str() {
                    "transition" | "vowel-exit" | "glide" => 44,
                    "cluster" => 42,
                    "nasal" => 52,
                    _ => 46,
                });
            }
            let mut gain: f64 = match m.role.as_str() {
                "epenthetic" => 0.76,
                "transition" | "vowel-exit" => {
                    if continuous {
                        0.89
                    } else {
                        0.92
                    }
                }
                "cluster" => {
                    if continuous {
                        0.87
                    } else {
                        0.91
                    }
                }
                "coda" => {
                    if continuous {
                        0.86
                    } else {
                        0.84
                    }
                }
                "glide" => {
                    if continuous {
                        0.87
                    } else {
                        0.86
                    }
                }
                "nasal" => {
                    if continuous {
                        0.89
                    } else {
                        0.90
                    }
                }
                _ => {
                    if english && m.stressed {
                        1.035
                    } else if english && m.deaccented {
                        0.97
                    } else if m.stressed {
                        1.055
                    } else if m.deaccented {
                        0.955
                    } else if (if pururu {
                        m.word_syllables
                    } else {
                        m.word_moras
                    }) > 1
                    {
                        0.99
                    } else {
                        1.0
                    }
                }
            };
            if [".", "?", "!", "\n"].contains(&m.phrase_end.as_str()) {
                gain *= 0.985;
            }
            let contour = if m.role != "nucleus" {
                m.role.clone()
            } else {
                match terminal[i].as_str() {
                    "?" => "question-rise",
                    "." | "\n" => "statement-fall",
                    "!" => "exclamation-fall",
                    "," | ";" | ":" => "continuation",
                    _ => {
                        if m.stressed {
                            "stress"
                        } else if m.deaccented {
                            "deaccented"
                        } else {
                            "neutral"
                        }
                    }
                }
                .into()
            };
            let mut mora = m.clone();
            mora.duration_ms = duration as f64;
            mora.pause_after_ms = pause as f64;
            RenderNote {
                mora,
                pitch: pitch.clone(),
                pitchbend: bend,
                gain: gain.clamp(0.68, 1.10),
                contour,
                pitch_start_cents: start + residual,
                pitch_end_cents: end + residual,
                pitch_peak_cents: Some(peak + residual),
            }
        })
        .collect())
}

pub fn is_default_score(score: &Value) -> bool {
    score
        .as_object()
        .map(|o| {
            o.len() == 2
                && o.get("version").and_then(Value::as_i64) == Some(1)
                && o.get("preset").and_then(Value::as_str) == Some("ttv-demo-v1")
        })
        .unwrap_or(false)
}
pub fn default_melody_score(count: usize, boundaries: &[usize]) -> Result<Value, String> {
    if !(1..=128).contains(&count) {
        return Err("a letra deve ocupar entre 1 e 128 notas".into());
    }
    let pattern = [(60, 850), (64, 850), (67, 850), (64, 1050)];
    let mut cursor = 200;
    let mut notes = vec![];
    for n in 0..count {
        if boundaries.contains(&n) {
            cursor += 180
        }
        let (pitch, duration) = pattern[n % 4];
        notes.push(
            json!({"midi_note":pitch,"start_ms":cursor,"duration_ms":duration,"velocity":100}),
        );
        cursor += duration;
    }
    if cursor + 200 > 60000 {
        return Err(
            "a letra passa de 60 segundos com a melodia padrão; divida o canto em duas mensagens"
                .into(),
        );
    }
    Ok(
        json!({"version":1,"track_index":0,"duration_ms":cursor+200,"lyrics_source":"message","notes":notes}),
    )
}
fn normalized_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}
fn integer(value: Option<&Value>) -> Option<i64> {
    value.and_then(Value::as_i64)
}
fn numeric(value: Option<&Value>) -> Option<f64> {
    value.and_then(Value::as_f64).filter(|v| v.is_finite())
}
pub fn speech_effect_segments(segments: &Value) -> Result<Value, String> {
    let values = segments.as_array().ok_or("trechos de efeitos inválidos")?;
    let mut spoken: Vec<Value> = vec![];
    let mut anchor: Option<usize> = None;
    for segment in values {
        let token = segment
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if token == "|" {
            continue;
        }
        if token == "~" {
            if segment.get("hold") == Some(&Value::Bool(true)) {
                let index =
                    anchor.ok_or("~ precisa vir depois de uma sílaba, sem atravessar uma pausa")?;
                let current = &mut spoken[index];
                current["hold_count"] = json!(current["hold_count"].as_u64().unwrap_or(0) + 1);
                for field in ["fade_in", "fade_out"] {
                    current[field] = json!(current[field] == true || segment[field] == true)
                }
                if integer(segment.get("pause_after_ms")).unwrap_or(0) != 0 {
                    current["pause_after_ms"] = segment["pause_after_ms"].clone();
                    anchor = None;
                }
            }
            continue;
        }
        let mut current = segment.clone();
        current["hold_count"] = json!(if current["hold"] == true { 1 } else { 0 });
        anchor = if integer(current.get("pause_after_ms")).unwrap_or(0) != 0 {
            None
        } else {
            Some(spoken.len())
        };
        spoken.push(current);
    }
    Ok(Value::Array(spoken))
}
pub fn validate_performance(
    text: &str,
    score: &Option<Value>,
    segments: &Option<Value>,
) -> Result<(), String> {
    if text.chars().count() > if score.is_some() { 2048 } else { 180 } {
        return Err("texto TTV grande demais para esta apresentação".into());
    }
    if score.is_none() && segments.is_none() {
        return Err("apresentação TTV exige MIDI ou efeitos no texto".into());
    }
    if let Some(score) = score {
        if is_default_score(score) {
            if text.trim().is_empty() {
                return Err("escreva a letra depois de /cantar".into());
            }
        } else {
            let object = score.as_object().ok_or("partitura TTV inválida")?;
            if object.keys().any(|k| {
                ![
                    "version",
                    "track_index",
                    "duration_ms",
                    "lyrics_source",
                    "notes",
                ]
                .contains(&k.as_str())
            }) {
                return Err("partitura TTV inválida".into());
            }
            if integer(score.get("version")) != Some(1) {
                return Err("versão da partitura TTV não suportada".into());
            }
            if !integer(score.get("track_index"))
                .map(|v| (0..64).contains(&v))
                .unwrap_or(false)
            {
                return Err("faixa MIDI inválida".into());
            }
            if !score
                .get("lyrics_source")
                .and_then(Value::as_str)
                .map(|v| ["message", "midi", "vowel"].contains(&v))
                .unwrap_or(false)
            {
                return Err("origem da letra MIDI inválida".into());
            }
            let duration = numeric(score.get("duration_ms"))
                .filter(|v| (40.0..=60000.0).contains(v))
                .ok_or("MIDI deve durar até 60 segundos")?;
            let notes = score
                .get("notes")
                .and_then(Value::as_array)
                .filter(|v| (1..=128).contains(&v.len()))
                .ok_or("MIDI deve conter entre 1 e 128 notas")?;
            let mut previous_end = 0.0;
            for note in notes {
                let o = note.as_object().ok_or("nota MIDI inválida")?;
                if o.keys().any(|k| {
                    !["midi_note", "start_ms", "duration_ms", "velocity", "lyric"]
                        .contains(&k.as_str())
                }) {
                    return Err("nota MIDI inválida".into());
                }
                if !integer(note.get("midi_note"))
                    .map(|v| (36..=96).contains(&v))
                    .unwrap_or(false)
                {
                    return Err("notas MIDI devem estar entre C2 e C7".into());
                }
                if !integer(note.get("velocity"))
                    .map(|v| (1..=127).contains(&v))
                    .unwrap_or(false)
                {
                    return Err("intensidade MIDI inválida".into());
                }
                let start = numeric(note.get("start_ms")).ok_or("tempo da nota MIDI inválido")?;
                let length =
                    numeric(note.get("duration_ms")).ok_or("tempo da nota MIDI inválido")?;
                if start < previous_end - 0.001
                    || length < 40.0
                    || start + length > duration + 0.001
                {
                    return Err(
                        "MIDI exige uma melodia sem notas sobrepostas e notas de pelo menos 40 ms"
                            .into(),
                    );
                }
                previous_end = start + length;
                if let Some(lyric) = note.get("lyric") {
                    if !lyric
                        .as_str()
                        .map(|s| s.chars().count() <= 128)
                        .unwrap_or(false)
                    {
                        return Err("letra incorporada ao MIDI inválida".into());
                    }
                }
            }
        }
    }
    if let Some(segments) = segments {
        let array = segments
            .as_array()
            .filter(|v| (1..=128).contains(&v.len()))
            .ok_or("efeitos TTV devem conter entre 1 e 128 trechos")?;
        for segment in array {
            let o = segment
                .as_object()
                .ok_or("trecho de efeitos TTV inválido")?;
            if o.len() != 5
                || !["text", "fade_in", "fade_out", "hold", "pause_after_ms"]
                    .iter()
                    .all(|k| o.contains_key(*k))
            {
                return Err("trecho de efeitos TTV inválido".into());
            }
            if !segment["text"]
                .as_str()
                .map(|s| !s.trim().is_empty() && s.chars().count() <= 2048)
                .unwrap_or(false)
            {
                return Err("texto do trecho TTV inválido".into());
            }
            if ["fade_in", "fade_out", "hold"]
                .iter()
                .any(|k| !segment[k].is_boolean())
            {
                return Err("efeito TTV inválido".into());
            }
            if !integer(segment.get("pause_after_ms"))
                .map(|v| v == 0 || v == 180)
                .unwrap_or(false)
            {
                return Err("pausa TTV inválida".into());
            }
        }
        let joined = array
            .iter()
            .map(|s| normalized_text(s["text"].as_str().unwrap()))
            .collect::<Vec<_>>()
            .join(" ");
        if joined != normalized_text(text) {
            return Err("trechos de efeitos TTV não correspondem ao texto".into());
        }
        if score.is_none() {
            speech_effect_segments(segments)?;
        }
    }
    if text.trim().is_empty() && score.is_none() {
        return Err("texto TTV vazio".into());
    }
    Ok(())
}
pub fn performance_hash(
    text: &str,
    score: &Option<Value>,
    segments: &Option<Value>,
) -> Result<String, String> {
    let payload =
        json!({"version":1,"text":normalized_text(text),"score":score,"segments":segments});
    let raw = serde_json::to_vec(&payload).map_err(|e| e.to_string())?;
    Ok(format!("{:x}", Sha256::digest(raw)))
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Placement {
    pub start_ms: f64,
    pub lead_ms: f64,
    #[serde(default)]
    pub anchor_ms: f64,
    #[serde(default)]
    pub score_group: Option<Vec<usize>>,
}
fn events(score: &Value) -> Result<&Vec<Value>, String> {
    score
        .get("notes")
        .and_then(Value::as_array)
        .filter(|v| !v.is_empty())
        .ok_or("partitura sem notas".into())
}
fn event_num(event: &Value, key: &str) -> f64 {
    event.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}
pub fn singing_pitch_at(score: &Value, time: f64) -> Result<f64, String> {
    let events = events(score)?;
    let index = events
        .partition_point(|e| event_num(e, "start_ms") <= time)
        .saturating_sub(1)
        .min(events.len() - 1);
    let event = &events[index];
    let elapsed = time - event_num(event, "start_ms");
    let duration = event_num(event, "duration_ms");
    let mut pitch = event_num(event, "midi_note") * 100.0;
    if index > 0 && (0.0..12.0).contains(&elapsed) {
        let previous = &events[index - 1];
        if event_num(event, "start_ms")
            - event_num(previous, "start_ms")
            - event_num(previous, "duration_ms")
            <= 20.0
        {
            let prior = event_num(previous, "midi_note") * 100.0;
            pitch = prior + (pitch - prior) * smoothstep(elapsed / 12.0);
        }
    }
    if duration >= 700.0 && elapsed >= 550.0 {
        pitch += 12.0
            * smoothstep((elapsed - 550.0) / 180.0)
            * smoothstep((duration - elapsed) / 100.0)
            * (std::f64::consts::TAU * 5.0 * (elapsed - 550.0) / 1000.0).sin();
    }
    Ok(pitch)
}
pub fn align_singing_syllables(
    moras: &[Mora],
    score: &Value,
    slots: Option<&[Option<usize>]>,
) -> Result<Vec<Vec<usize>>, String> {
    let nuclei = moras
        .iter()
        .filter(|m| m.role == "nucleus")
        .collect::<Vec<_>>();
    let events = events(score)?;
    if events.iter().any(|e| event_num(e, "duration_ms") > 18000.0) {
        return Err("uma vogal contínua pode durar até 18 segundos no ESPER; divida a nota com outra sílaba ou uma pausa MIDI".into());
    }
    if nuclei.is_empty() {
        return Err("a letra não contém vogais que a voz possa cantar".into());
    }
    if nuclei.len() > events.len() {
        return Err(format!(
            "letra contém {} sílabas para {} notas; encurte a letra ou acrescente notas",
            nuclei.len(),
            events.len()
        ));
    }
    let groups = if let Some(slots) = slots {
        if slots.len() != events.len() {
            return Err(format!(
                "letra com ~ ocupa {} notas; o MIDI contém {}",
                slots.len(),
                events.len()
            ));
        }
        let mut groups = vec![vec![]; nuclei.len()];
        let mut active = None;
        for (i, slot) in slots.iter().enumerate() {
            if slot.is_some() {
                active = *slot
            }
            let group = active
                .filter(|&v| v < groups.len())
                .ok_or("~ precisa vir depois de uma sílaba")?;
            groups[group].push(i);
        }
        if groups.iter().any(Vec::is_empty) {
            return Err("cada sílaba precisa de uma nota MIDI".into());
        }
        groups
    } else {
        let count = nuclei.len();
        let total = events.len();
        let weights = nuclei
            .iter()
            .map(|m| if m.stressed { 1.15 } else { 1.0 })
            .collect::<Vec<_>>();
        let voiced = events
            .iter()
            .map(|e| event_num(e, "duration_ms"))
            .sum::<f64>();
        let desired = weights
            .iter()
            .map(|w| voiced * w / weights.iter().sum::<f64>())
            .collect::<Vec<_>>();
        let mut costs = vec![vec![None::<(f64, Vec<Vec<usize>>)>; total + 1]; count + 1];
        costs[0][0] = Some((0.0, vec![]));
        for syllable in 0..count {
            for used in 0..=total {
                let Some((cost, path)) = costs[syllable][used].clone() else {
                    continue;
                };
                let mut duration = 0.0;
                let mut pitches = vec![];
                for end in used + 1..=total - (count - syllable - 1) {
                    if end > used + 1 {
                        let left = &events[end - 2];
                        let right = &events[end - 1];
                        if event_num(right, "start_ms")
                            - event_num(left, "start_ms")
                            - event_num(left, "duration_ms")
                            > 20.0
                        {
                            break;
                        }
                    }
                    duration += event_num(&events[end - 1], "duration_ms");
                    if event_num(&events[end - 1], "start_ms")
                        + event_num(&events[end - 1], "duration_ms")
                        - event_num(&events[used], "start_ms")
                        > 18000.0
                    {
                        break;
                    }
                    pitches.push(event_num(&events[end - 1], "midi_note") as i32);
                    if pitches.iter().max().unwrap() - pitches.iter().min().unwrap() > 40 {
                        break;
                    }
                    let value = cost
                        + ((duration - desired[syllable]) / desired[syllable].max(40.0)).powi(2);
                    if costs[syllable + 1][end]
                        .as_ref()
                        .map(|old| value < old.0)
                        .unwrap_or(true)
                    {
                        let mut path = path.clone();
                        path.push((used..end).collect());
                        costs[syllable + 1][end] = Some((value, path));
                    }
                }
            }
        }
        costs[count][total].take().ok_or("a letra não encaixa nas frases do MIDI; acrescente sílabas nas pausas ou divida vogais acima de 18 segundos; use ~ para indicar notas ligadas")?.1
    };
    for group in &groups {
        let first = &events[group[0]];
        let last = &events[*group.last().unwrap()];
        if event_num(last, "start_ms") + event_num(last, "duration_ms")
            - event_num(first, "start_ms")
            > 18000.0
        {
            return Err("uma vogal contínua pode durar até 18 segundos no ESPER; divida a letra com outra sílaba ou uma pausa MIDI".into());
        }
        for pair in group.windows(2) {
            let l = &events[pair[0]];
            let r = &events[pair[1]];
            if event_num(r, "start_ms") - event_num(l, "start_ms") - event_num(l, "duration_ms")
                > 20.0
            {
                return Err("~ não pode prolongar uma vogal através de uma pausa MIDI".into());
            }
        }
        let pitches = group
            .iter()
            .map(|&i| event_num(&events[i], "midi_note") as i32)
            .collect::<Vec<_>>();
        if pitches.iter().max().unwrap() - pitches.iter().min().unwrap() > 40 {
            return Err(
                "melisma abrange mais de 40 semitons; divida a vogal em outro ataque".into(),
            );
        }
    }
    Ok(groups)
}
pub fn align_singing_pitch(
    notes: &[RenderNote],
    placements: &[Placement],
    score: &Value,
    preroll_ms: f64,
    tempo: i32,
) -> Result<Vec<RenderNote>, String> {
    if notes.len() != placements.len() {
        return Err("notas e posições Teto têm tamanhos diferentes".into());
    }
    let interval = 60000.0 / (tempo.clamp(60, 240) as f64 * 96.0);
    let mut result = vec![];
    for (note, p) in notes.iter().zip(placements) {
        let base = pitch_midi(&note.pitch) * 100;
        let start = p.start_ms - preroll_ms;
        let duration = note.mora.duration_ms + p.lead_ms;
        let points = ((duration / interval).ceil() as usize + 1).max(2);
        let local = if let Some(group) = &p.score_group {
            let source = events(score)?;
            let mut events = vec![];
            for &i in group {
                events.push(source.get(i).ok_or("grupo MIDI fora da partitura")?.clone());
            }
            json!({"notes":events})
        } else {
            score.clone()
        };
        let values = (0..points)
            .map(|i| {
                singing_pitch_at(&local, start + i as f64 * interval)
                    .map(|v| iround(v - base as f64))
            })
            .collect::<Result<Vec<_>, _>>()?;
        if values.iter().any(|&v| !(-2048..=2047).contains(&v)) {
            return Err(
                "salto de notas grande demais para um único trecho; divida a sílaba".into(),
            );
        }
        let mut n = note.clone();
        n.pitchbend = encode_pitchbend(&values);
        n.pitch_start_cents = values[0];
        n.pitch_end_cents = *values.last().unwrap();
        result.push(n);
    }
    Ok(result)
}
pub fn align_pitch_to_timeline(
    notes: &[RenderNote],
    placements: &[Placement],
    tempo: i32,
) -> Result<Vec<RenderNote>, String> {
    if notes.len() != placements.len() {
        return Err("notas e posições Teto têm tamanhos diferentes".into());
    }
    if !notes.iter().any(|n| n.mora.profile == "english-cvvc") {
        return Ok(notes.to_vec());
    }
    let interval = 60000.0 / (tempo.clamp(60, 240) as f64 * 96.0);
    let mut result = notes.to_vec();
    let moras = notes.iter().map(|n| n.mora.clone()).collect::<Vec<_>>();
    for (first, last) in phrase_spans(&moras) {
        let mut nuclei = (first..=last)
            .filter(|&i| notes[i].mora.role == "nucleus")
            .collect::<Vec<_>>();
        if nuclei.is_empty() {
            nuclei = (first..=last).collect()
        }
        let initial = nuclei[0];
        let terminal = *nuclei.last().unwrap();
        let mut knots = vec![(
            placements[initial].anchor_ms,
            (pitch_midi(&notes[initial].pitch) * 100 + notes[initial].pitch_start_cents) as f64,
        )];
        for &i in &nuclei {
            let n = &notes[i];
            let peak = n
                .pitch_peak_cents
                .map(|v| v as f64)
                .unwrap_or((n.pitch_start_cents + n.pitch_end_cents) as f64 / 2.0);
            knots.push((
                placements[i].anchor_ms + n.mora.duration_ms * 0.48,
                pitch_midi(&n.pitch) as f64 * 100.0 + peak,
            ));
        }
        knots.push((
            placements[terminal].anchor_ms + notes[terminal].mora.duration_ms,
            (pitch_midi(&notes[terminal].pitch) * 100 + notes[terminal].pitch_end_cents) as f64,
        ));
        knots.sort_by(|a, b| a.0.total_cmp(&b.0));
        let mut unique: Vec<(f64, f64)> = vec![];
        for k in knots {
            if unique.last().map(|last| last.0 == k.0).unwrap_or(false) {
                *unique.last_mut().unwrap() = k
            } else {
                unique.push(k)
            }
        }
        let pitch_at = |t: f64| {
            let right = unique.partition_point(|k| k.0 <= t);
            if right == 0 {
                unique[0].1
            } else if right == unique.len() {
                unique.last().unwrap().1
            } else {
                let (a, b) = (unique[right - 1], unique[right]);
                a.1 + (b.1 - a.1) * smoothstep((t - a.0) / (b.0 - a.0))
            }
        };
        for i in first..=last {
            let n = &notes[i];
            if n.mora.profile != "english-cvvc" {
                continue;
            }
            let p = &placements[i];
            let duration = n.mora.duration_ms + p.lead_ms.max(0.0);
            let base = pitch_midi(&n.pitch) as f64 * 100.0;
            let points = ((duration / interval).ceil() as usize + 1).max(2);
            let values = (0..points)
                .map(|point| iround(pitch_at(p.start_ms + point as f64 * interval) - base))
                .collect::<Vec<_>>();
            result[i].pitchbend = encode_pitchbend(&values);
            result[i].pitch_start_cents = iround(pitch_at(p.start_ms) - base);
            result[i].pitch_end_cents = iround(pitch_at(p.start_ms + duration) - base);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn assert_close(actual: &Value, expected: &Value, path: &str) {
        match (actual, expected) {
            (Value::Number(a), Value::Number(b)) => assert!(
                (a.as_f64().unwrap() - b.as_f64().unwrap()).abs() < 1e-9,
                "{path}: {actual} != {expected}"
            ),
            (Value::Array(a), Value::Array(b)) => {
                assert_eq!(a.len(), b.len(), "{path}");
                for (i, (a, b)) in a.iter().zip(b).enumerate() {
                    assert_close(a, b, &format!("{path}[{i}]"));
                }
            }
            (Value::Object(a), Value::Object(b)) => {
                assert_eq!(a.len(), b.len(), "{path}: {actual} != {expected}");
                for (k, b) in b {
                    assert_close(a.get(k).unwrap_or(&Value::Null), b, &format!("{path}.{k}"));
                }
            }
            _ => assert_eq!(actual, expected, "{path}"),
        }
    }
    #[test]
    fn differential_current_python_speech_planner() {
        let cases: Value =
            serde_json::from_str(include_str!("../tests/fixtures/prosody-contract.json")).unwrap();
        for (index, case) in cases.as_array().unwrap().iter().enumerate() {
            let moras: Vec<Mora> = serde_json::from_value(case["moras"].clone()).unwrap();
            let settings: ProsodySettings =
                serde_json::from_value(case["settings"].clone()).unwrap();
            let actual = serde_json::to_value(build_notes(&moras, &settings).unwrap()).unwrap();
            assert_close(&actual, &case["notes"], &format!("case {index}"));
        }
    }
    #[test]
    fn differential_current_python_singing_pitch_and_identity() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/prosody-singing-contract.json"
        ))
        .unwrap();
        for case in fixture["hashes"].as_array().unwrap() {
            let score = if case["score"].is_null() {
                None
            } else {
                Some(case["score"].clone())
            };
            let segments = if case["segments"].is_null() {
                None
            } else {
                Some(case["segments"].clone())
            };
            assert_eq!(
                performance_hash(case["text"].as_str().unwrap(), &score, &segments).unwrap(),
                case["expected"].as_str().unwrap()
            );
        }
        for case in fixture["alignments"].as_array().unwrap() {
            let moras: Vec<Mora> = serde_json::from_value(case["moras"].clone()).unwrap();
            assert_eq!(
                serde_json::to_value(
                    align_singing_syllables(&moras, &case["score"], None).unwrap()
                )
                .unwrap(),
                case["expected"]
            );
            for sample in case["pitches"].as_array().unwrap() {
                let actual =
                    singing_pitch_at(&case["score"], sample["time_ms"].as_f64().unwrap()).unwrap();
                assert!((actual - sample["pitch"].as_f64().unwrap()).abs() < 1e-8);
            }
        }
        for case in fixture["timeline"].as_array().unwrap() {
            let notes: Vec<RenderNote> = serde_json::from_value(case["notes"].clone()).unwrap();
            let placements: Vec<Placement> =
                serde_json::from_value(case["placements"].clone()).unwrap();
            let actual =
                serde_json::to_value(align_pitch_to_timeline(&notes, &placements, 140).unwrap())
                    .unwrap();
            assert_close(&actual, &case["expected"], "timeline");
        }
    }
    #[test]
    fn pitch_values_and_runs() {
        assert_eq!(
            encode_pitchbend(&[0, 0, 0, -1, -1, 2047, -2048]),
            "AA#3#////f/gA"
        );
        assert_eq!(relative_pitch("Bb3", 2), "C4");
        assert_eq!(iround(2.5), 2);
        assert_eq!(iround(-1.5), -2);
    }
    #[test]
    fn reject_bool_numeric_and_overlapping_score() {
        let mut score = default_melody_score(2, &[]).unwrap();
        validate_performance("olá", &Some(score.clone()), &None).unwrap();
        score["notes"][0]["midi_note"] = json!(true);
        assert!(validate_performance("olá", &Some(score), &None).is_err());
    }
    #[test]
    fn holding_cannot_cross_rest() {
        let score = json!({"notes":[{"midi_note":60,"start_ms":0,"duration_ms":500},{"midi_note":60,"start_ms":800,"duration_ms":500}]});
        let moras = vec![Mora::default()];
        assert!(align_singing_syllables(&moras, &score, None).is_err());
    }
    #[test]
    fn whole_note_vibrato_and_legato() {
        let score = json!({"notes":[{"midi_note":60,"start_ms":0,"duration_ms":900},{"midi_note":64,"start_ms":900,"duration_ms":900}]});
        assert_eq!(singing_pitch_at(&score, 0.0).unwrap(), 6000.0);
        assert_eq!(singing_pitch_at(&score, 900.0).unwrap(), 6000.0);
        assert_eq!(singing_pitch_at(&score, 912.0).unwrap(), 6400.0);
    }
    #[test]
    fn speech_defaults_match_reference() {
        let mut m = Mora::default();
        m.candidates = vec!["a".into()];
        m.stressed = true;
        m.word_end = true;
        m.phrase_end = ".".into();
        let result = build_notes(&[m], &ProsodySettings::default()).unwrap();
        assert_eq!(result[0].mora.duration_ms, 166.0);
        assert_eq!(result[0].pitch, "C4");
        assert_eq!(result[0].contour, "statement-fall");
        assert_eq!(result[0].pitch_start_cents, -42);
    }
}
