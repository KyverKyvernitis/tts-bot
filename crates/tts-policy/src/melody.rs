//! Pure immutable melody validation; filesystem/catalog ownership stays outside.
use serde_json::{json, Value};

pub fn catalog_score(entry: &Value) -> Value {
    json!({"version":1,"track_index":0,"duration_ms":entry["duration_ms"],"lyrics_source":"message","notes":entry["notes"]})
}
pub fn validate_score(entry: &Value) -> Result<Value, String> {
    let notes = entry["notes"]
        .as_array()
        .filter(|n| !n.is_empty() && n.len() <= 128)
        .ok_or("invalid_cd_score_notes")?;
    let duration = entry["duration_ms"]
        .as_i64()
        .filter(|n| (1..=60_000).contains(n))
        .ok_or("invalid_cd_score_duration")?;
    let mut previous_end = 0;
    for note in notes {
        if !note.is_object() {
            return Err("invalid_cd_score_note".into());
        }
        for (key, min, max) in [
            ("midi_note", 36, 96),
            ("start_ms", 0, duration),
            ("duration_ms", 40, duration),
            ("velocity", 1, 127),
        ] {
            if note[key].as_i64().is_none_or(|n| n < min || n > max) {
                return Err("invalid_cd_score_note".into());
            }
        }
        let start = note["start_ms"].as_i64().unwrap();
        if start < previous_end {
            return Err("invalid_cd_score_overlap".into());
        }
        previous_end = start + note["duration_ms"].as_i64().unwrap();
        if previous_end > duration {
            return Err("invalid_cd_score_end".into());
        }
        if let Some(lyric) = note.get("lyric") {
            if lyric.as_str().is_none_or(|s| s.chars().count() > 128) {
                return Err("invalid_cd_score_lyric".into());
            }
        }
    }
    Ok(catalog_score(entry))
}
pub fn minimum_singing_syllables(score: &Value) -> Result<usize, String> {
    let Some(notes) = score
        .get("notes")
        .and_then(Value::as_array)
        .filter(|n| !n.is_empty())
    else {
        return Ok(0);
    };
    let field = |note: &Value, key: &str| {
        note[key]
            .as_i64()
            .ok_or_else(|| format!("invalid score {key}"))
    };
    let mut count = 1;
    let mut first = 0;
    let mut low = field(&notes[0], "midi_note")?;
    let mut high = low;
    for (index, note) in notes.iter().enumerate().skip(1) {
        let pitch = field(note, "midi_note")?;
        let start = field(note, "start_ms")?;
        let end = start + field(note, "duration_ms")?;
        let previous = &notes[index - 1];
        if start - field(previous, "start_ms")? - field(previous, "duration_ms")? > 20
            || end - field(&notes[first], "start_ms")? > 18_000
            || high.max(pitch) - low.min(pitch) > 40
        {
            count += 1;
            first = index;
            low = pitch;
            high = pitch;
        } else {
            low = low.min(pitch);
            high = high.max(pitch);
        }
    }
    Ok(count)
}
pub fn fit_arrangement(
    text: &Value,
    segments: &Value,
    arrangement: &Value,
) -> Result<Value, String> {
    let leads: Vec<&Value> = arrangement
        .get("parts")
        .and_then(Value::as_array)
        .map(|parts| {
            parts
                .iter()
                .filter(|p| p.get("role").and_then(Value::as_str) == Some("lead"))
                .collect()
        })
        .unwrap_or_default();
    if leads.len() != 1
        || leads[0]
            .get("notes")
            .and_then(Value::as_array)
            .is_none_or(|n| n.is_empty())
    {
        return Err("O arranjo precisa de uma melodia principal válida.".into());
    }
    Ok(json!([text, segments]))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn score_limits_and_vowel_spans() {
        let score = json!({"duration_ms":20_000,"notes":[{"midi_note":60,"start_ms":0,"duration_ms":10_000,"velocity":100},{"midi_note":62,"start_ms":10_000,"duration_ms":10_000,"velocity":100}]});
        assert!(validate_score(&score).is_ok());
        assert_eq!(minimum_singing_syllables(&score).unwrap(), 2);
        let mut bad = score.clone();
        bad["notes"][1]["start_ms"] = json!(9999);
        assert_eq!(
            validate_score(&bad).unwrap_err(),
            "invalid_cd_score_overlap"
        );
    }
}
