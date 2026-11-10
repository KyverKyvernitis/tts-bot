//! CVVC scheduling uses recorded preutterance before lexical vowel anchors.
use crate::{prosody::RenderNote, voicebank::OtoEntry};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Placement {
    pub anchor_ms: f64,
    pub start_ms: f64,
    pub lead_ms: f64,
    pub fade_ms: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trim_lead_ms: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub score_group: Option<Vec<usize>>,
}
pub fn lead_ms(entry: &OtoEntry, note: &RenderNote, velocity: f64, cvvc: bool) -> f64 {
    if !cvvc {
        return 0.0;
    }
    let stretch = 2f64.powf(1.0 - velocity / 100.0);
    let cap = match note.mora.role.as_str() {
        "nucleus" => 180.0,
        "transition" | "vowel-exit" | "cluster" => 120.0,
        "coda" | "glide" | "nasal" => 90.0,
        _ => 70.0,
    };
    (entry.preutterance_ms.max(entry.overlap_ms).max(0.0) * stretch).min(cap)
}
pub fn fade_ms(entry: Option<&OtoEntry>, lead: f64) -> f64 {
    entry
        .map(|e| e.overlap_ms.max((lead * 0.28).min(24.0)).clamp(4.0, 48.0))
        .unwrap_or(0.0)
}
pub fn render_entry(
    entry: &OtoEntry,
    note: &RenderNote,
    velocity: f64,
    cvvc: bool,
) -> Result<OtoEntry, String> {
    if !cvvc {
        return Ok(entry.clone());
    }
    let stretch = 2f64.powf(1.0 - velocity / 100.0);
    let trim = (entry.preutterance_ms.max(entry.overlap_ms).max(0.0)
        - lead_ms(entry, note, velocity, cvvc) / stretch)
        .max(0.0);
    if trim < 0.001 {
        return Ok(entry.clone());
    }
    let mut out = entry.clone();
    if out.cutoff_ms < 0.0 {
        out.cutoff_ms += trim;
        if out.cutoff_ms >= 0.0 {
            return Err(format!(
                "OTO sem região útil após preutterance: {}",
                entry.alias
            ));
        }
    }
    out.offset_ms += trim;
    out.consonant_ms = (out.consonant_ms - trim).max(0.0);
    out.preutterance_ms = (out.preutterance_ms - trim).max(0.0);
    out.overlap_ms = (out.overlap_ms - trim).max(0.0);
    Ok(out)
}
pub fn resampler_length(
    entry: &OtoEntry,
    note: &RenderNote,
    velocity: f64,
    cvvc: bool,
    post_consonant: bool,
) -> f64 {
    let target = note.mora.duration_ms + lead_ms(entry, note, velocity, cvvc);
    if !post_consonant {
        return target;
    }
    let fixed =
        (entry.consonant_ms.max(0.0) * 2f64.powf(1.0 - velocity / 100.0) / 5.0).floor() * 5.0;
    ((target / 5.0).ceil() * 5.0 - fixed).max(5.0)
}
pub fn english_placements(
    notes: &[RenderNote],
    entries: &[Option<OtoEntry>],
    velocity: f64,
) -> Result<(Vec<Placement>, f64), String> {
    if notes.len() != entries.len() {
        return Err("entries/notes length mismatch".into());
    }
    let leads: Vec<_> = notes
        .iter()
        .zip(entries)
        .map(|(n, e)| {
            e.as_ref()
                .map(|e| lead_ms(e, n, velocity, true))
                .unwrap_or(0.0)
        })
        .collect();
    let fades: Vec<_> = entries
        .iter()
        .zip(&leads)
        .map(|(e, l)| fade_ms(e.as_ref(), *l))
        .collect();
    let mut anchors = vec![0.0; notes.len()];
    let mut trims = vec![0.0; notes.len()];
    let (mut cursor, mut protected, mut sample_end) = (0.0f64, 0.0f64, 0.0f64);
    let mut pause: Option<f64> = None;
    let mut lexical = false;
    let mut onsets = Vec::new();
    let mut exits = Vec::new();
    for (i, note) in notes.iter().enumerate() {
        let n = &note.mora;
        if entries[i].is_none() && n.role != "nucleus" {
            anchors[i] = cursor;
            if n.pause_after_ms >= 24.0 {
                pause = Some(cursor.max(sample_end) + n.pause_after_ms);
                cursor = pause.unwrap();
            }
            continue;
        }
        if matches!(n.role.as_str(), "transition" | "cluster") {
            onsets.push(i);
            continue;
        }
        if n.role == "vowel-exit" {
            exits.push(i);
            continue;
        }
        if n.role == "nucleus" {
            let mut onset_starts = Vec::new();
            let mut next_start = cursor - leads[i];
            let mut next_fade = fades[i];
            for &j in onsets.iter().rev() {
                let overlap = next_fade.min(notes[j].mora.duration_ms * 0.35);
                next_start += overlap - leads[j] - notes[j].mora.duration_ms;
                onset_starts.push((j, next_start));
                next_fade = fades[j];
            }
            let mut floor = if lexical {
                protected - next_fade
            } else {
                next_start
            };
            if let Some(p) = pause {
                floor = floor.max(p);
            }
            let shift = (floor - next_start).max(0.0);
            cursor += shift;
            for (j, s) in onset_starts {
                anchors[j] = s + shift + leads[j];
                sample_end = sample_end.max(anchors[j] + notes[j].mora.duration_ms);
            }
            onsets.clear();
            for j in exits.drain(..) {
                anchors[j] = cursor - leads[i];
                let raw = anchors[j] - leads[j];
                let mut floor = if lexical {
                    protected - fades[j]
                } else {
                    anchors[j]
                };
                if let Some(p) = pause {
                    floor = floor.max(p);
                }
                trims[j] = (floor - raw).max(0.0).min(leads[j]);
                sample_end = sample_end.max(anchors[j] + notes[j].mora.duration_ms);
            }
            anchors[i] = cursor;
            protected = cursor + 45.0f64.min(n.duration_ms * 0.40);
            lexical = true;
            pause = None;
            cursor += n.duration_ms;
        } else {
            if lexical {
                cursor = cursor.max(protected + leads[i] - fades[i]);
            }
            if let Some(p) = pause {
                cursor = cursor.max(p + leads[i]);
            }
            anchors[i] = cursor;
            protected = cursor + n.duration_ms;
            lexical = true;
            pause = None;
            cursor += match n.role.as_str() {
                "nasal" => (n.duration_ms * 0.34).clamp(10.0, 26.0),
                "coda" | "glide" => (n.duration_ms * 0.30).clamp(8.0, 22.0),
                _ => 0.0,
            };
        }
        sample_end = sample_end.max(anchors[i] + n.duration_ms);
        if n.pause_after_ms >= 24.0 {
            pause = Some(cursor.max(sample_end) + n.pause_after_ms);
            cursor = pause.unwrap();
        }
    }
    for j in onsets {
        let mut start = (cursor - leads[j]).max(protected - fades[j]);
        if let Some(p) = pause {
            start = start.max(p);
        }
        anchors[j] = start + leads[j];
        protected = anchors[j] + notes[j].mora.duration_ms;
        cursor = protected;
        sample_end = sample_end.max(cursor);
    }
    for j in exits {
        anchors[j] = cursor;
        let mut floor = if lexical {
            protected - fades[j]
        } else {
            cursor
        };
        if let Some(p) = pause {
            floor = floor.max(p);
        }
        trims[j] = (floor - cursor + leads[j]).max(0.0).min(leads[j]);
        sample_end = sample_end.max(cursor + notes[j].mora.duration_ms);
    }
    let mut placements: Vec<_> = (0..notes.len())
        .map(|i| Placement {
            anchor_ms: anchors[i],
            start_ms: anchors[i] - leads[i],
            lead_ms: leads[i],
            fade_ms: fades[i],
            trim_lead_ms: (notes[i].mora.role == "vowel-exit").then_some(trims[i]),
            ..Default::default()
        })
        .collect();
    let min = placements
        .iter()
        .map(|p| p.start_ms + p.trim_lead_ms.unwrap_or(0.0))
        .fold(0.0f64, f64::min);
    let shift = (-min).max(0.0);
    for p in &mut placements {
        p.start_ms += shift;
        p.anchor_ms += shift;
    }
    Ok((placements, cursor.max(sample_end) + shift))
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SingingSpan {
    pub start_ms: f64,
    pub end_ms: f64,
    pub group: Vec<usize>,
}
#[derive(Default)]
struct Group {
    start: f64,
    end: f64,
    nucleus: usize,
    onsets: Vec<usize>,
    tails: Vec<usize>,
    exits: Vec<usize>,
    attack: f64,
    attack_scale: f64,
    protected: f64,
    tail: f64,
    tail_scale: f64,
    attack_start: f64,
}
pub fn singing_placements(
    notes: &[RenderNote],
    entries: &[Option<OtoEntry>],
    spans: &[SingingSpan],
    velocity: f64,
) -> Result<(Vec<Placement>, f64), String> {
    if notes.len() != entries.len() || notes.len() != spans.len() {
        return Err("singing plan length mismatch".into());
    }
    let leads: Vec<_> = notes
        .iter()
        .zip(entries)
        .map(|(n, e)| {
            e.as_ref()
                .map(|e| lead_ms(e, n, velocity, true))
                .unwrap_or(0.0)
        })
        .collect();
    let fades: Vec<_> = entries
        .iter()
        .zip(&leads)
        .map(|(e, l)| fade_ms(e.as_ref(), *l))
        .collect();
    let mut groups: BTreeMap<Vec<usize>, Group> = BTreeMap::new();
    let mut p = Vec::new();
    for (i, (note, span)) in notes.iter().zip(spans).enumerate() {
        let group = groups.entry(span.group.clone()).or_insert_with(|| Group {
            start: span.start_ms,
            end: span.end_ms,
            ..Default::default()
        });
        if note.mora.role == "nucleus" {
            group.nucleus = i;
        } else if entries[i].is_some() {
            match note.mora.role.as_str() {
                "transition" | "cluster" | "epenthetic" => group.onsets.push(i),
                "vowel-exit" => group.exits.push(i),
                _ => group.tails.push(i),
            }
        }
        p.push(Placement {
            anchor_ms: span.start_ms,
            start_ms: span.start_ms - leads[i],
            lead_ms: leads[i],
            fade_ms: fades[i],
            score_group: Some(span.group.clone()),
            ..Default::default()
        });
    }
    let mut groups: Vec<_> = groups.into_values().collect();
    groups.sort_by(|a, b| a.start.total_cmp(&b.start));
    for g in &mut groups {
        g.attack = leads[g.nucleus];
        let mut next = fades[g.nucleus];
        for &i in g.onsets.iter().rev() {
            g.attack +=
                leads[i] + notes[i].mora.duration_ms - next.min(notes[i].mora.duration_ms * 0.35);
            next = fades[i];
        }
        g.attack_scale = 1.0;
        g.protected = g.start + 45.0f64.min((g.end - g.start) * 0.40);
        g.tail = g.tails.iter().map(|&i| notes[i].mora.duration_ms).sum();
        g.tail_scale = if g.tail != 0.0 {
            ((g.end - g.protected) / g.tail).min(1.0)
        } else {
            1.0
        };
    }
    for i in 0..groups.len() {
        if i == 0 {
            if groups[i].attack != 0.0 {
                groups[i].attack_scale = ((groups[i].start + 350.0) / groups[i].attack).min(1.0);
            }
            continue;
        }
        let gap = groups[i].start - groups[i - 1].end;
        if gap > 20.0 {
            if groups[i].attack != 0.0 {
                groups[i].attack_scale = (gap / groups[i].attack).min(1.0);
            }
        } else {
            let required = groups[i - 1].tail * groups[i - 1].tail_scale + groups[i].attack;
            let available = (groups[i].start - groups[i - 1].protected).max(0.0);
            let scale = if required != 0.0 {
                (available / required).min(1.0)
            } else {
                1.0
            };
            groups[i - 1].tail_scale *= scale;
            groups[i].attack_scale = scale;
        }
    }
    fn place(
        p: &mut [Placement],
        leads: &[f64],
        i: usize,
        anchor: f64,
        audible: f64,
        duration: Option<f64>,
        fade: Option<f64>,
    ) {
        p[i].anchor_ms = anchor;
        p[i].start_ms = anchor - leads[i];
        p[i].trim_lead_ms = Some((audible - p[i].start_ms).clamp(0.0, leads[i]));
        p[i].duration_ms = duration;
        if let Some(f) = fade {
            p[i].fade_ms = f;
        }
    }
    for g in &mut groups {
        let n = g.nucleus;
        let scale = g.attack_scale;
        let mut cursor = g.start - leads[n] * scale;
        place(
            &mut p,
            &leads,
            n,
            g.start,
            cursor,
            None,
            Some(fades[n] * scale),
        );
        let mut next = fades[n] * scale;
        for &i in g.onsets.iter().rev() {
            let duration = (notes[i].mora.duration_ms * scale).floor().max(1.0);
            let fade = (fades[i] * scale).min(duration * 0.35);
            let overlap = next.min(duration * 0.35);
            let anchor = cursor + overlap - duration;
            cursor = anchor - leads[i] * scale;
            place(
                &mut p,
                &leads,
                i,
                anchor,
                cursor,
                Some(duration),
                Some(fade),
            );
            next = fade;
        }
        g.attack_start = cursor;
        for &i in &g.exits {
            let boundary = g.start - leads[n] * scale;
            let fade = fades[i] * scale;
            place(&mut p, &leads, i, boundary, cursor - fade, None, Some(fade));
        }
    }
    for i in 0..groups.len() {
        let g = &groups[i];
        let mut end = g.end;
        if let Some(next) = groups.get(i + 1) {
            if next.start - end <= 20.0 {
                end = end.min(next.attack_start);
            }
        }
        let durations: Vec<_> = g
            .tails
            .iter()
            .map(|&n| (notes[n].mora.duration_ms * g.tail_scale).floor().max(1.0))
            .collect();
        let mut cursor = end - durations.iter().sum::<f64>();
        let mut protected = g.protected;
        let mut previous: Option<f64> = None;
        for (&n, duration) in g.tails.iter().zip(durations) {
            let mut fade = fades[n].min(duration * 0.35);
            if let Some(d) = previous {
                fade = fade.min(d * 0.35);
            }
            place(
                &mut p,
                &leads,
                n,
                cursor,
                (cursor - leads[n]).max(protected - fade),
                Some(duration),
                Some(fade),
            );
            protected = cursor + duration;
            previous = Some(duration);
            cursor += duration;
        }
    }
    let min = p
        .iter()
        .zip(entries)
        .filter(|(_, e)| e.is_some())
        .map(|(p, _)| p.start_ms + p.trim_lead_ms.unwrap_or(0.0))
        .fold(0.0f64, f64::min);
    let preroll = (-min).max(0.0);
    if preroll > 350.0 + 1e-6 {
        return Err("ataque consonantal longo demais para o MIDI".into());
    }
    for q in &mut p {
        q.anchor_ms += preroll;
        q.start_ms += preroll;
    }
    Ok((p, preroll))
}

#[cfg(test)]mod tests{
 use super::*;
 #[derive(Deserialize)]struct Case{velocity:f64,notes:Vec<RenderNote>,entries:Vec<Option<OtoEntry>>,expected:Vec<Placement>,end:f64}
 #[test]fn production_cvvc_placements(){let cases:Vec<Case>=serde_json::from_str(include_str!("../tests/fixtures/timeline-contract.json")).unwrap();for(i,c)in cases.into_iter().enumerate(){let(actual,end)=english_placements(&c.notes,&c.entries,c.velocity).unwrap();assert!((end-c.end).abs()<1e-8,"duration case {i}");for(a,e)in actual.iter().zip(&c.expected){for(x,y)in[(a.anchor_ms,e.anchor_ms),(a.start_ms,e.start_ms),(a.lead_ms,e.lead_ms),(a.fade_ms,e.fade_ms),(a.trim_lead_ms.unwrap_or(0.0),e.trim_lead_ms.unwrap_or(0.0))]{assert!((x-y).abs()<1e-8,"placement case {i}: {a:?} != {e:?}");}}}}
}
