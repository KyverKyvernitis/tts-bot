//! WORLDLINE phrase planning and synthesis. This preserves the separate native
//! phrase API instead of routing speech through the fragment/MIDI resampler.
use crate::{
    dsp,
    phonetics::{self, AliasResolution, PhonemizeOptions},
    prosody::{self, RenderNote},
    python_round,
    renderer::{Cancellation, RenderOutput, RenderRequest},
    timeline,
    voicebank::{OtoEntry, VoicebankIndex},
    SAMPLE_RATE,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
#[cfg(test)]
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};
use tts_engines::worldline::{WorldlineCurves, WorldlineJob, WorldlineRequest};

const FRAME_MS: f64 = 10.;
const ENVELOPE_MODE: &str = "complementary-native-envelopes";
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct PhrasePlacement {
    pub anchor_ms: f64,
    pub start_ms: f64,
    pub lead_ms: f64,
    pub fade_ms: f64,
    #[serde(default)]
    pub skip_ms: f64,
}
fn cpp_frame(ms: f64) -> i64 {
    (ms / FRAME_MS + 0.5).floor() as i64
}

pub fn placements(
    notes: &[RenderNote],
    entries: &[Option<OtoEntry>],
    velocity: f64,
    cvvc: bool,
) -> Result<(Vec<PhrasePlacement>, f64), String> {
    if notes.len() != entries.len() {
        return Err("WORLDLINE notes/entries length mismatch".into());
    }
    let (base, end) = if cvvc {
        timeline::english_placements(notes, entries, velocity)?
    } else {
        let mut base = Vec::new();
        let mut cursor = 0f64;
        let mut after_pause = true;
        for (note, entry) in notes.iter().zip(entries) {
            let join = entry
                .as_ref()
                .map(|e| {
                    e.overlap_ms
                        .max(e.preutterance_ms.max(0.) * 0.45)
                        .max(16.)
                        .clamp(10., 55.)
                })
                .unwrap_or(0.);
            let mut fade = if after_pause || entry.is_none() {
                0.
            } else {
                join.min(note.mora.duration_ms * 0.35)
            };
            if note.mora.role == "epenthetic" && !after_pause {
                fade = 60f64.min(note.mora.duration_ms * 0.58);
            }
            let start = (cursor - fade).max(0.);
            base.push(timeline::Placement {
                anchor_ms: start,
                start_ms: start,
                lead_ms: 0.,
                fade_ms: fade,
                ..Default::default()
            });
            cursor = start + note.mora.duration_ms;
            after_pause = note.mora.pause_after_ms >= 24.;
            if after_pause {
                cursor += note.mora.pause_after_ms;
            }
        }
        (base, cursor)
    };
    let mut placed = Vec::with_capacity(base.len());
    for p in base {
        let exit = p.trim_lead_ms.map(|_| p.anchor_ms);
        let trim = p.trim_lead_ms.unwrap_or(0.).clamp(0., p.lead_ms);
        let mut q = PhrasePlacement {
            start_ms: p.start_ms + trim,
            lead_ms: p.lead_ms - trim,
            fade_ms: p.fade_ms,
            skip_ms: trim,
            ..Default::default()
        };
        q.start_ms = python_round(q.start_ms / FRAME_MS) * FRAME_MS;
        q.lead_ms = python_round(q.lead_ms / FRAME_MS) * FRAME_MS;
        q.fade_ms = python_round(q.fade_ms / FRAME_MS) * FRAME_MS;
        if let Some(anchor) = exit {
            q.lead_ms = (python_round(anchor / FRAME_MS) * FRAME_MS - q.start_ms).max(0.);
        }
        q.anchor_ms = q.start_ms + q.lead_ms;
        if p.fade_ms > 0. {
            q.fade_ms = q.fade_ms.max(FRAME_MS);
        }
        if q.skip_ms > 0. {
            q.skip_ms = python_round(q.skip_ms / FRAME_MS) * FRAME_MS;
        }
        placed.push(q);
    }
    let end = placed
        .iter()
        .zip(notes)
        .map(|(p, n)| p.start_ms + p.lead_ms + n.mora.duration_ms)
        .fold(end, f64::max);
    Ok((placed, (end / FRAME_MS).ceil() * FRAME_MS))
}
pub fn curves(
    notes: &[RenderNote],
    placements: &[PhrasePlacement],
    end_ms: f64,
    pauses: &[[f64; 2]],
) -> WorldlineCurves {
    let count = (end_ms / FRAME_MS).ceil() as usize + 1;
    let mut f0 = vec![0.; count];
    let mut voicing = vec![0.; count];
    let mut first = 0;
    let mut spans = Vec::new();
    for (number, note) in notes.iter().enumerate() {
        if !note.mora.phrase_end.is_empty() {
            spans.push((first, number));
            first = number + 1;
        }
    }
    if first < notes.len() {
        spans.push((first, notes.len() - 1));
    }
    for (first, last) in spans {
        let mut nuclei: Vec<usize> = (first..=last)
            .filter(|&i| notes[i].mora.role == "nucleus")
            .collect();
        if nuclei.is_empty() {
            nuclei = (first..=last).collect();
        }
        let initial = nuclei[0];
        let terminal = *nuclei.last().unwrap();
        let mut knots = vec![(
            placements[initial].anchor_ms,
            prosody::pitch_midi(&notes[initial].pitch) as f64 * 100.
                + notes[initial].pitch_start_cents as f64,
        )];
        for &i in &nuclei {
            let n = &notes[i];
            let peak = n
                .pitch_peak_cents
                .map(|p| p as f64)
                .unwrap_or((n.pitch_start_cents + n.pitch_end_cents) as f64 / 2.);
            knots.push((
                placements[i].anchor_ms + n.mora.duration_ms * 0.48,
                prosody::pitch_midi(&n.pitch) as f64 * 100. + peak,
            ));
        }
        knots.push((
            placements[terminal].anchor_ms + notes[terminal].mora.duration_ms,
            prosody::pitch_midi(&notes[terminal].pitch) as f64 * 100.
                + notes[terminal].pitch_end_cents as f64,
        ));
        // Python dict(sorted(knots)) retains the final cents at duplicate times.
        knots.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.total_cmp(&b.1)));
        let mut ordered: Vec<(f64, f64)> = Vec::new();
        for knot in knots {
            if ordered.last().is_some_and(|last| last.0 == knot.0) {
                *ordered.last_mut().unwrap() = knot;
            } else {
                ordered.push(knot);
            }
        }
        let start = (first..=last)
            .map(|i| placements[i].start_ms)
            .fold(f64::INFINITY, f64::min);
        let stop = (first..=last)
            .map(|i| placements[i].start_ms + placements[i].lead_ms + notes[i].mora.duration_ms)
            .fold(0., f64::max);
        for frame in ((start / FRAME_MS).floor().max(0.) as usize)
            ..count.min((stop / FRAME_MS).ceil() as usize + 1)
        {
            let point = frame as f64 * FRAME_MS;
            let right = ordered.partition_point(|&(t, _)| t <= point);
            let cents = if right == 0 {
                ordered[0].1
            } else if right == ordered.len() {
                ordered[right - 1].1
            } else {
                let (lo, a) = ordered[right - 1];
                let (hi, b) = ordered[right];
                let ratio = ((point - lo) / (hi - lo)).clamp(0., 1.);
                let ratio = ratio * ratio * (3. - 2. * ratio);
                a + (b - a) * ratio
            };
            f0[frame] = 440. * 2f64.powf((cents / 100. - 69.) / 12.);
            voicing[frame] = 1.;
        }
    }
    for &[start, stop] in pauses {
        for frame in ((start / FRAME_MS).ceil().max(0.) as usize)
            ..count.min((stop / FRAME_MS).ceil().max(0.) as usize)
        {
            f0[frame] = 0.;
            voicing[frame] = 0.;
        }
    }
    WorldlineCurves {
        f0,
        gender: vec![0.5; count],
        tension: vec![0.5; count],
        breathiness: vec![0.5; count],
        voicing,
    }
}
pub fn balance_crossfades(requests: &mut [WorldlineRequest]) -> usize {
    let mut ordered: Vec<usize> = (0..requests.len()).collect();
    ordered.sort_by_key(|&i| {
        (
            cpp_frame(requests[i].position_ms),
            cpp_frame(requests[i].position_ms + requests[i].length_ms),
        )
    });
    let starts: Vec<i64> = ordered
        .iter()
        .map(|&i| cpp_frame(requests[i].position_ms))
        .collect();
    let ends: Vec<i64> = ordered
        .iter()
        .map(|&i| cpp_frame(requests[i].position_ms + requests[i].length_ms))
        .collect();
    let mut prefix = Vec::new();
    for &end in &ends {
        prefix.push(end.max(prefix.last().copied().unwrap_or(0)));
    }
    let mut pairs = 0;
    for number in 0..ordered.len().saturating_sub(1) {
        let left = ordered[number];
        let right = ordered[number + 1];
        let begin = starts[number + 1];
        let stop = ends[number];
        if !(starts[number] < begin && begin < stop && stop < ends[number + 1]) {
            continue;
        }
        if number > 0 && prefix[number - 1] > begin {
            continue;
        }
        if number + 2 < ordered.len() && starts[number + 2] < stop {
            continue;
        }
        let overlap = stop - begin;
        let right_fade =
            (cpp_frame(requests[right].position_ms + requests[right].fade_in_ms) - begin).max(1);
        if (right_fade - overlap).abs() > 1 {
            continue;
        }
        let left_fade_end = (starts[number] + 1).max(cpp_frame(
            requests[left].position_ms + requests[left].fade_in_ms,
        ));
        let right_fade_start = (ends[number + 1] - 1).min(cpp_frame(
            requests[right].position_ms + requests[right].length_ms - requests[right].fade_out_ms,
        ));
        if left_fade_end > begin || right_fade_start < stop {
            continue;
        }
        requests[left].fade_out_ms = overlap as f64 * FRAME_MS;
        requests[right].fade_in_ms = overlap as f64 * FRAME_MS;
        pairs += 1;
    }
    pairs
}
pub fn phrase_job(
    notes: &[RenderNote],
    entries: &[Option<OtoEntry>],
    sources: &HashMap<PathBuf, String>,
    request: &RenderRequest,
) -> Result<(WorldlineJob, usize), String> {
    let cvvc = request.voicebank_profile == "english-cvvc";
    let (placements, end) = placements(notes, entries, request.velocity, cvvc)?;
    if end > request.max_audio_seconds * 1000. {
        return Err("WORLDLINE planned phrase exceeds audio duration budget".into());
    }
    let mut requests = Vec::new();
    let mut pauses = Vec::new();
    for ((note, entry), p) in notes.iter().zip(entries).zip(&placements) {
        let length = ((note.mora.duration_ms + p.lead_ms) / FRAME_MS).ceil() * FRAME_MS;
        let stop = p.start_ms + length;
        if entry.is_none() && (!cvvc || note.mora.role == "nucleus") {
            pauses.push([p.start_ms, stop]);
        } else if let Some(e) = entry {
            requests.push(WorldlineRequest {
                sample_file: sources
                    .get(&e.wav_path)
                    .ok_or("decoded WORLDLINE source missing")?
                    .clone(),
                tone: prosody::pitch_midi(&note.pitch),
                con_vel: request.velocity.clamp(20., 200.),
                offset_ms: e.offset_ms,
                required_length_ms: length + p.skip_ms,
                consonant_ms: e.consonant_ms,
                cutoff_ms: e.cutoff_ms,
                volume: note.gain * 100.,
                modulation: request.modulation,
                tempo: request.prosody.tempo.clamp(60, 240) as f64,
                position_ms: p.start_ms,
                skip_ms: p.skip_ms,
                length_ms: length,
                fade_in_ms: p.fade_ms,
                fade_out_ms: 10.,
            });
        }
        if note.mora.pause_after_ms >= 24. {
            pauses.push([stop, end.min(stop + note.mora.pause_after_ms)]);
        }
    }
    if requests.is_empty() {
        return Err("nenhum alias da voicebank correspondeu ao texto".into());
    }
    let pairs = balance_crossfades(&mut requests);
    requests.sort_by(|a, b| {
        (a.position_ms + a.length_ms)
            .total_cmp(&(b.position_ms + b.length_ms))
            .then_with(|| a.position_ms.total_cmp(&b.position_ms))
    });
    pauses.sort_by(|a, b| a[0].total_cmp(&b[0]).then_with(|| a[1].total_cmp(&b[1])));
    let mut merged: Vec<[f64; 2]> = Vec::new();
    for pause in pauses {
        if pause[1] <= pause[0] {
            continue;
        }
        if let Some(last) = merged.last_mut() {
            if pause[0] <= last[1] {
                last[1] = last[1].max(pause[1]);
                continue;
            }
        }
        merged.push(pause);
    }
    let controls = curves(notes, &placements, end, &merged);
    Ok((
        WorldlineJob {
            schema_version: 1,
            sample_rate: SAMPLE_RATE,
            max_output_seconds: request.max_audio_seconds,
            requests,
            duration_ms: Some(end),
            curves: controls,
            silence_intervals_ms: merged,
        },
        pairs,
    ))
}

fn notes_for(
    request: &RenderRequest,
    index: &VoicebankIndex,
) -> Result<(Vec<RenderNote>, Vec<Option<OtoEntry>>), String> {
    let options = PhonemizeOptions {
        max_moras: request.max_moras + 1,
        voicebank_profile: request.voicebank_profile.clone(),
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
    let moras = phonetics::phonemize(&request.text, &options, Some(&resolver))?;
    if moras.len() > request.max_moras {
        return Err("texto excedeu o limite de fonemas WORLDLINE".into());
    }
    let mut notes = prosody::build_notes(&moras, &request.prosody)?;
    if notes.is_empty() {
        return Err("texto não gerou fonemas compatíveis".into());
    }
    let cvvc = request.voicebank_profile == "english-cvvc";
    let mut entries = Vec::with_capacity(notes.len());
    for note in &notes {
        entries.push(
            index
                .resolve(&note.mora.candidates, None)?
                .map(|entry| timeline::render_entry(&entry, note, request.velocity, cvvc))
                .transpose()?,
        );
    }
    let stretch = 2f64.powf(1. - request.velocity / 100.);
    for (note, entry) in notes.iter_mut().zip(&entries) {
        if let Some(e) = entry {
            let lead = timeline::lead_ms(e, note, request.velocity, cvvc);
            let fixed =
                ((e.consonant_ms * stretch - lead + 10.).max(0.) / FRAME_MS).ceil() * FRAME_MS;
            let duration = (note.mora.duration_ms.max(fixed) / FRAME_MS).ceil() * FRAME_MS;
            if duration > 500. {
                return Err(format!("região consonantal OTO longa demais: {}", e.alias));
            }
            note.mora.duration_ms = duration;
        }
    }
    Ok((notes, entries))
}
fn decode_sources(
    entries: &[Option<OtoEntry>],
    directory: &Path,
    deadline: Instant,
    cancel: &Cancellation,
) -> Result<HashMap<PathBuf, String>, String> {
    let mut sources = HashMap::new();
    let mut total = 0u64;
    let mut total_frames = 0u64;
    for entry in entries.iter().flatten() {
        if sources.contains_key(&entry.wav_path) {
            continue;
        }
        cancel.check()?;
        let metadata = fs::metadata(&entry.wav_path).map_err(|e| e.to_string())?;
        if !metadata.is_file() || metadata.len() > 30 * 1024 * 1024 {
            return Err("WORLDLINE source is missing or oversized".into());
        }
        let name = format!("source_{}.wav", sources.len());
        let output = directory.join(&name);
        let native = hound::WavReader::open(&entry.wav_path)
            .ok()
            .is_some_and(|reader| {
                let spec = reader.spec();
                spec.channels == 1
                    && spec.sample_rate == SAMPLE_RATE
                    && spec.bits_per_sample == 16
                    && spec.sample_format == hound::SampleFormat::Int
            });
        if native {
            let reader = hound::WavReader::open(&entry.wav_path).map_err(|e| e.to_string())?;
            if reader.duration() > SAMPLE_RATE * 20 {
                return Err("WORLDLINE source exceeds 20 seconds".into());
            }
            fs::copy(&entry.wav_path, &output).map_err(|e| e.to_string())?;
        } else {
            let mut command = Command::new("ffmpeg");
            command
                .args(["-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i"])
                .arg(&entry.wav_path)
                .args([
                    "-t",
                    "20.001",
                    "-ac",
                    "1",
                    "-ar",
                    "44100",
                    "-c:a",
                    "pcm_s16le",
                ])
                .arg(&output);
            tts_engines::process::run_command(
                &mut command,
                deadline.saturating_duration_since(Instant::now()),
                cancel.engine(),
            )
            .map_err(|e| e.to_string())?;
        }
        let reader = hound::WavReader::open(&output).map_err(|e| e.to_string())?;
        if reader.duration() > SAMPLE_RATE * 20
            || reader.spec().sample_rate != SAMPLE_RATE
            || reader.spec().channels != 1
            || reader.spec().bits_per_sample != 16
        {
            return Err(
                "decoded WORLDLINE source has an invalid format or exceeds 20 seconds".into(),
            );
        }
        total += fs::metadata(&output).map_err(|e| e.to_string())?.len();
        total_frames += reader.duration() as u64;
        if total > 64 * 1024 * 1024 || total_frames > SAMPLE_RATE as u64 * 180 {
            return Err("decoded WORLDLINE phrase sources exceed memory budget".into());
        }
        sources.insert(entry.wav_path.clone(), name);
    }
    Ok(sources)
}
fn value<'a>(request: &'a RenderRequest, keys: &[&str]) -> Option<&'a str> {
    keys.iter().find_map(|key| {
        request
            .engine_environment
            .get(*key)
            .map(String::as_str)
            .filter(|s| !s.trim().is_empty())
    })
}
fn library_path(request: &RenderRequest) -> Result<PathBuf, String> {
    let path = value(
        request,
        &[
            "PHONE_WORKER_WORLDLINE_LIBRARY",
            "PHONE_WORKER_TETO_WORLDLINE_LIBRARY",
        ],
    )
    .map(PathBuf::from)
    .unwrap_or_else(|| {
        PathBuf::from(
            value(request, &["HOME"])
                .map(str::to_owned)
                .or_else(|| std::env::var("HOME").ok())
                .unwrap_or_default(),
        )
        .join(".worldline-r/lib/libworldline.so")
    });
    if !path.is_absolute() {
        return Err("WORLDLINE library path must be absolute".into());
    }
    Ok(path)
}
fn verify_library(path: &Path) -> Result<(String, &'static str), String> {
    let metadata = fs::metadata(path).map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() > 8 * 1024 * 1024 {
        return Err("pinned WORLDLINE library missing or oversized".into());
    }
    let bytes = fs::read(path).map_err(|e| e.to_string())?;
    if bytes.len() < 64
        || &bytes[..6] != b"\x7fELF\x02\x01"
        || u16::from_le_bytes([bytes[16], bytes[17]]) != 3
    {
        return Err("WORLDLINE library must be ELF64 little-endian shared object".into());
    }
    let (architecture, expected) = match u16::from_le_bytes([bytes[18], bytes[19]]) {
        183 => (
            "arm64",
            "80fb77357de4fae608e2d2fa12db867d0c48d0366263e6a280eccd90a1584dfe",
        ),
        62 => (
            "x64",
            "33a4123188bcf7b0b8fec4ae6c5bf882fb16b1aaa5aec6cd3391eccb5a69c490",
        ),
        _ => return Err("unsupported WORLDLINE library architecture".into()),
    };
    let digest = format!("{:x}", Sha256::digest(&bytes));
    if digest != expected {
        return Err("WORLDLINE library differs from pinned OpenUtau 0.1.565".into());
    }
    Ok((digest, architecture))
}
fn guest_command(
    request: &RenderRequest,
    library: &Path,
    directory: &Path,
    timeout: Duration,
) -> Result<Command, String> {
    let helper = value(request, &["PHONE_WORKER_WORLDLINE_RUST_HELPER"])
        .unwrap_or("/usr/local/bin/tts-worldline-native");
    if !helper.starts_with('/')
        || Path::new(helper)
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
        || helper.contains('\0')
    {
        return Err("WORLDLINE Rust helper must be an absolute guest executable".into());
    }
    let direct = value(request, &["PHONE_WORKER_WORLDLINE_RUST_DIRECT"]) == Some("1");
    let mut command;
    if direct {
        command = Command::new(helper);
        command
            .arg("--library")
            .arg(library)
            .arg("--job")
            .arg(directory.join("job.json"))
            .arg("--output")
            .arg(directory.join("output.wav"));
    } else {
        let container = value(
            request,
            &[
                "PHONE_WORKER_WORLDLINE_CONTAINER",
                "PHONE_WORKER_TETO_WORLDLINE_CONTAINER",
            ],
        )
        .unwrap_or("voicepeak-arm64");
        if container.len() > 64
            || !container
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
            || !container
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
        {
            return Err("invalid WORLDLINE guest container".into());
        }
        if library.parent().unwrap().to_string_lossy().contains(':')
            || directory.to_string_lossy().contains(':')
        {
            return Err("WORLDLINE paths incompatible with proot bind".into());
        }
        command = Command::new("proot-distro");
        command
            .args(["login", container, "--bind"])
            .arg(format!(
                "{}:/opt/worldline-lib",
                library.parent().unwrap().display()
            ))
            .arg("--bind")
            .arg(format!("{}:/opt/worldline-job", directory.display()))
            .args([
                "--",
                "/usr/bin/env",
                "LANG=C",
                "LC_ALL=C",
                helper,
                "--library",
            ])
            .arg(format!(
                "/opt/worldline-lib/{}",
                library.file_name().unwrap().to_string_lossy()
            ))
            .args([
                "--job",
                "/opt/worldline-job/job.json",
                "--output",
                "/opt/worldline-job/output.wav",
            ]);
    }
    command
        .arg("--_child")
        .arg("--timeout")
        .arg(format!("{:.3}", timeout.as_secs_f64().clamp(0.001, 120.)));
    command.envs(&request.engine_environment);
    Ok(command)
}

fn render_plain(request: &RenderRequest, cancel: &Cancellation) -> Result<RenderOutput, String> {
    let started = Instant::now();
    if !request.timeout_seconds.is_finite()
        || !(0.001..=120.).contains(&request.timeout_seconds)
        || !request.max_audio_seconds.is_finite()
        || !(0.01..=120.).contains(&request.max_audio_seconds)
        || request.max_audio_bytes < 44
        || !request.velocity.is_finite()
        || !request.modulation.is_finite()
        || request.max_moras > 256
    {
        return Err("invalid WORLDLINE request budgets or controls".into());
    }
    let text = request
        .text
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let max_chars = value(request, &["PHONE_WORKER_TETO_MAX_CHARACTERS"])
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(180)
        .clamp(16, 2000);
    if text.is_empty() || text.chars().count() > max_chars {
        return Err("texto vazio ou grande demais para WORLDLINE".into());
    }
    let deadline = started + Duration::from_secs_f64(request.timeout_seconds);
    cancel.check()?;
    let library = library_path(request)?;
    let (digest, architecture) = verify_library(&library)?;
    let index = VoicebankIndex::load(&request.voicebank, &request.voicebank_options)?;
    let mut normalized = request.clone();
    normalized.text = text;
    normalized.velocity = request.velocity.clamp(20., 200.);
    normalized.modulation = request.modulation.clamp(0., 100.);
    let (notes, entries) = notes_for(&normalized, &index)?;
    let (_, planned) = placements(
        &notes,
        &entries,
        normalized.velocity,
        normalized.voicebank_profile == "english-cvvc",
    )?;
    if planned > normalized.max_audio_seconds * 1000. {
        return Err("WORLDLINE planned audio exceeds duration limit".into());
    }
    let work = tempfile::tempdir().map_err(|e| e.to_string())?;
    let sources = decode_sources(&entries, work.path(), deadline, cancel)?;
    let (job, pairs) = phrase_job(&notes, &entries, &sources, &normalized)?;
    let bytes = serde_json::to_vec(&job).map_err(|e| e.to_string())?;
    fs::write(work.path().join("job.json"), bytes).map_err(|e| e.to_string())?;
    cancel.check()?;
    let remaining = deadline.saturating_duration_since(Instant::now());
    let mut command = guest_command(&normalized, &library, work.path(), remaining)?;
    let proof =
        tts_engines::worldline::run_worldline_isolated(&mut command, remaining, cancel.engine())
            .map_err(|e| e.to_string())?;
    if proof["phrase_render_verified"] != true
        || proof["library_sha256"] != digest
        || proof["native_architecture"] != architecture
        || proof["source_commit"] != tts_engines::worldline::SOURCE_COMMIT
        || proof["source_version"] != tts_engines::worldline::SOURCE_VERSION
    {
        return Err("WORLDLINE helper did not verify requested pinned phrase".into());
    }
    cancel.check()?;
    let output = work.path().join("output.wav");
    let size = fs::metadata(&output).map_err(|e| e.to_string())?.len();
    if size <= 44 || size > request.max_audio_bytes as u64 {
        return Err("WORLDLINE output is missing, empty or oversized".into());
    }
    let audio = fs::read(output).map_err(|e| e.to_string())?;
    let pcm = crate::renderer::decode_wav(&audio)?;
    if pcm.is_empty()
        || pcm.iter().all(|&s| s == 0)
        || pcm.len() > ((request.max_audio_seconds * SAMPLE_RATE as f64).floor() as usize + 1)
        || proof["frames"].as_u64() != Some(pcm.len() as u64)
    {
        return Err("WORLDLINE returned silence, truncated WAV or incorrect duration proof".into());
    }
    let missing: Vec<String> = notes
        .iter()
        .zip(&entries)
        .filter(|(_, e)| e.is_none())
        .map(|(n, _)| {
            n.mora
                .candidates
                .first()
                .cloned()
                .unwrap_or_else(|| "?".into())
        })
        .take(12)
        .collect();
    let nuclei: Vec<f64> = notes
        .iter()
        .filter(|n| n.mora.role == "nucleus")
        .map(|n| n.mora.duration_ms)
        .collect();
    let mean = nuclei.iter().sum::<f64>() / nuclei.len().max(1) as f64;
    let stddev = (nuclei.iter().map(|n| (n - mean).powi(2)).sum::<f64>()
        / nuclei.len().max(1) as f64)
        .sqrt();
    let alias_cost = notes.iter().map(|n| n.mora.planner_cost).sum::<f64>();
    let mut metadata = json!({"audio_format":"wav","backend":"worldline-r","voicebank":index.name,"voice_id":request.voice_id,"voicebank_fingerprint":index.fingerprint,"renderer_version":"rust-worldline-phrase-v1","phonemizer_version":"rust-phonetics-v1","reading_language":request.reading_language,"library_sha256":digest,"native_phrase_render_verified":true,"portuguese_speech_verified":false,"speech_rate":request.prosody.speech_rate,"pitch_offset_semitones":request.prosody.pitch_offset_semitones,"aliases":index.entries.len(),"voicebank_profile":request.voicebank_profile,"rendered_phonemes":job.requests.len(),"missing_phonemes":missing});
    for extra in [
        json!({"phonetic_units":notes.iter().map(|n|n.mora.source_phonemes.len()).sum::<usize>(),"epenthetic_phonemes":notes.iter().filter(|n|n.mora.role=="epenthetic").count(),"auxiliary_phonemes":notes.iter().filter(|n|n.mora.role!="nucleus").count(),"cvvc_direct":notes.iter().filter(|n|n.mora.coverage=="cvvc-direct").count(),"cvvc_transitions":notes.iter().filter(|n|n.mora.coverage=="cvvc-transition").count(),"cluster_hits":notes.iter().filter(|n|n.mora.coverage=="cluster-hit").count(),"approximated_phonemes":notes.iter().filter(|n|n.mora.coverage=="approximation").count(),"coverage_percent":python_round(1000.*entries.iter().filter(|e|e.is_some()).count()as f64/entries.len().max(1)as f64)/10.,"alias_path_cost":python_round(alias_cost*100.)/100.,"mean_nucleus_ms":python_round(mean*100.)/100.,"nucleus_duration_stddev_ms":python_round(stddev*100.)/100.}),
        json!({"pitch_alignment_mode":"absolute-timeline","pitch_boundary_metric":"not-measured","pitch_boundary_max_cents":null,"f0_control_mode":"phrase-shared-absolute-timeline","envelope_mode":ENVELOPE_MODE,"crossfade_pairs":pairs,"pitchbend_fallbacks":0,"legacy_fragment_hits":0,"timeline_mode":"worldline-phrase-continuous","timeline_planned_ms":job.duration_ms,"timeline_audio_ms":pcm.len()as f64*1000./SAMPLE_RATE as f64,"timeline_serialized_ms":notes.iter().map(|n|n.mora.duration_ms+if n.mora.pause_after_ms>=24.{n.mora.pause_after_ms}else{0.}).sum::<f64>(),"timeline_aux_overlays":notes.iter().filter(|n|n.mora.role!="nucleus").count(),"timeline_max_preutterance_ms":notes.iter().zip(&entries).filter_map(|(n,e)|e.as_ref().map(|e|timeline::lead_ms(e,n,request.velocity,request.voicebank_profile=="english-cvvc"))).fold(0.,f64::max),"continuity_repairs":0,"energy_boundary_max_db":null,"energy_boundary_metric":"not-measured","worker_synth_ms":started.elapsed().as_secs_f64()*1000.,"sha256":format!("{:x}",Sha256::digest(&audio)),"sample_rate":SAMPLE_RATE,"channels":1,"sample_width_bytes":2,"duration_seconds":pcm.len()as f64/SAMPLE_RATE as f64}),
    ] {
        metadata
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
    }
    Ok(RenderOutput { audio, metadata })
}
pub fn render_worldline(
    request: &RenderRequest,
    cancel: &Cancellation,
) -> Result<RenderOutput, String> {
    if request.score.is_some() {
        return Err("canto MIDI exige o motor ESPER; WORLDLINE usa a API de frase".into());
    }
    if let Some(segments) = &request.segments {
        prosody::validate_performance(&request.text, &None, &Some(segments.clone()))?;
        let values = segments.as_array().ok_or("trechos WORLDLINE inválidos")?;
        if values.iter().any(|s| s["hold"] == true) {
            return Err(
                "prolongar vogais exige o motor ESPER; WORLDLINE aceita fades e pausas".into(),
            );
        }
        let started = Instant::now();
        let mut combined = Vec::new();
        let mut last = json!({});
        for segment in values {
            cancel.check()?;
            let text = segment["text"].as_str().unwrap_or("");
            if matches!(text.trim(), "|" | "~") {
                continue;
            }
            let mut plain = request.clone();
            plain.text = text.into();
            plain.segments = None;
            plain.timeout_seconds = request.timeout_seconds - started.elapsed().as_secs_f64();
            if plain.timeout_seconds <= 0. {
                return Err("efeitos TTV excederam o timeout".into());
            }
            let rendered = render_plain(&plain, cancel)?;
            let mut pcm = crate::renderer::decode_wav(&rendered.audio)?;
            let n = pcm.len();
            dsp::performance_envelope(
                &mut pcm,
                0,
                n as isize,
                segment["fade_in"] == true,
                segment["fade_out"] == true,
                0,
                n,
            );
            combined.extend(pcm);
            let pause = segment["pause_after_ms"].as_f64().unwrap_or(0.);
            let silence = python_round(SAMPLE_RATE as f64 * pause / 1000.).max(0.) as usize;
            if combined.len() + silence > SAMPLE_RATE as usize * 20 {
                return Err("efeitos TTV excederam 20 segundos".into());
            }
            combined.resize(combined.len() + silence, 0);
            last = rendered.metadata;
        }
        let audio = crate::renderer::encode_wav(&combined)?;
        if combined.is_empty() || audio.len() > request.max_audio_bytes {
            return Err("áudio com efeitos TTV inválido ou grande demais".into());
        }
        last["ttv_performance_hash"] = json!(prosody::performance_hash(
            &request.text,
            &None,
            &Some(segments.clone())
        )?);
        last["performance_version"] = json!("rust-ttv-performance-v1");
        last["performance_mode"] = json!("speech-effects");
        last["sha256"] = json!(format!("{:x}", Sha256::digest(&audio)));
        last["worker_synth_ms"] = json!(started.elapsed().as_secs_f64() * 1000.);
        last["duration_seconds"] = json!(combined.len() as f64 / SAMPLE_RATE as f64);
        return Ok(RenderOutput {
            audio,
            metadata: last,
        });
    }
    render_plain(request, cancel)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn compare(actual: &Value, expected: &Value, path: &str) {
        if let (Some(a), Some(b)) = (actual.as_f64(), expected.as_f64()) {
            assert!((a - b).abs() < 1e-9, "{path}: {a} != {b}");
            return;
        }
        match (actual, expected) {
            (Value::Array(a), Value::Array(b)) => {
                assert_eq!(a.len(), b.len(), "{path}");
                for (i, (a, b)) in a.iter().zip(b).enumerate() {
                    compare(a, b, &format!("{path}[{i}]"));
                }
            }
            (Value::Object(a), Value::Object(b)) => {
                assert_eq!(a.len(), b.len(), "{path}");
                for (key, a) in a {
                    compare(a, &b[key], &format!("{path}.{key}"));
                }
            }
            _ => assert_eq!(actual, expected, "{path}"),
        }
    }
    #[test]
    fn phrase_jobs_match_original_python_placements_curves_and_fades() {
        let contracts: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/worldline-phrase-contracts.json"
        ))
        .unwrap();
        for case in contracts["cases"].as_array().unwrap() {
            let notes: Vec<RenderNote> = serde_json::from_value(case["notes"].clone()).unwrap();
            let entries: Vec<Option<OtoEntry>> =
                serde_json::from_value(case["entries"].clone()).unwrap();
            let sources: HashMap<PathBuf, String> =
                serde_json::from_value(case["sources"].clone()).unwrap();
            let request = RenderRequest {
                voicebank_profile: case["profile"].as_str().unwrap().into(),
                max_audio_seconds: 5.,
                ..Default::default()
            };
            let (job, pairs) = phrase_job(&notes, &entries, &sources, &request).unwrap();
            let expected: WorldlineJob = serde_json::from_value(case["job"].clone()).unwrap();
            compare(
                &serde_json::to_value(job).unwrap(),
                &serde_json::to_value(expected).unwrap(),
                case["name"].as_str().unwrap(),
            );
            assert_eq!(
                pairs as u64,
                case["job"]["crossfade_pairs"].as_u64().unwrap()
            );
        }
    }
    #[test]
    fn midi_and_holds_fail_without_starting_a_motor() {
        let request = RenderRequest {
            score: Some(json!({"version":1,"preset":"ttv-demo-v1"})),
            ..Default::default()
        };
        assert!(render_worldline(&request, &Cancellation::default())
            .unwrap_err()
            .contains("MIDI"));
        let request = RenderRequest {
            text: "a".into(),
            segments: Some(
                json!([{"text":"a","hold":true,"fade_in":false,"fade_out":false,"pause_after_ms":0}]),
            ),
            ..Default::default()
        };
        assert!(render_worldline(&request, &Cancellation::default())
            .unwrap_err()
            .contains("ESPER"));
    }
}
