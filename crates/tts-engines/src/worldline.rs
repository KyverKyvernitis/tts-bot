//! PhraseSynth FFI for pinned OpenUtau 0.1.565. Run this in the glibc helper
//! process: the C++ library cannot be loaded inside an Android/Bionic daemon.
use crate::{Cancellation, EngineError, Result};
use libloading::Library;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    ffi::{c_char, c_void},
    fs::{self, File, OpenOptions},
    io::Read,
    path::{Component, Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

pub const SOURCE_COMMIT: &str = "a60ca5830b9064556157245d4bf8f5920d93e5f8";
pub const SOURCE_VERSION: &str = "0.1.565";
pub const SAMPLE_RATE: u32 = 44_100;
const MAX_JOB_BYTES: u64 = 4 * 1024 * 1024;
const MAX_REQUESTS: usize = 256;
const SAMPLES_PER_FRAME: usize = 441;
const MAX_SECONDS: f64 = 120.;
fn default_max() -> f64 {
    MAX_SECONDS
}
fn hundred() -> f64 {
    100.
}
fn tempo() -> f64 {
    120.
}
fn ten() -> f64 {
    10.
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WorldlineJob {
    pub schema_version: u32,
    pub sample_rate: u32,
    #[serde(default = "default_max")]
    pub max_output_seconds: f64,
    pub requests: Vec<WorldlineRequest>,
    #[serde(default)]
    pub duration_ms: Option<f64>,
    pub curves: WorldlineCurves,
    #[serde(default)]
    pub silence_intervals_ms: Vec<[f64; 2]>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WorldlineCurves {
    pub f0: Vec<f64>,
    pub gender: Vec<f64>,
    pub tension: Vec<f64>,
    pub breathiness: Vec<f64>,
    pub voicing: Vec<f64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WorldlineRequest {
    pub sample_file: String,
    pub tone: i32,
    #[serde(default = "hundred")]
    pub con_vel: f64,
    #[serde(default = "hundred")]
    pub volume: f64,
    #[serde(default)]
    pub modulation: f64,
    #[serde(default = "tempo")]
    pub tempo: f64,
    pub offset_ms: f64,
    pub consonant_ms: f64,
    pub cutoff_ms: f64,
    pub required_length_ms: f64,
    pub position_ms: f64,
    pub length_ms: f64,
    #[serde(default)]
    pub skip_ms: f64,
    #[serde(default = "ten")]
    pub fade_in_ms: f64,
    #[serde(default = "ten")]
    pub fade_out_ms: f64,
}

/// Exact natural 64-bit OpenUtau ABI. Inputs live through PhraseSynthSynth.
#[repr(C)]
pub struct SynthRequest {
    pub sample_fs: i32,
    pub sample_length: i32,
    pub sample: *const f64,
    pub frq_length: i32,
    pub frq: *const c_void,
    pub tone: i32,
    pub con_vel: f64,
    pub offset: f64,
    pub required_length: f64,
    pub consonant: f64,
    pub cut_off: f64,
    pub volume: f64,
    pub modulation: f64,
    pub tempo: f64,
    pub pitch_bend_length: i32,
    pub pitch_bend: *const i32,
    pub flag_g: i32,
    pub flag_o: i32,
    pub flag_p: i32,
    pub flag_mt: i32,
    pub flag_mb: i32,
    pub flag_mv: i32,
}
pub fn verify_abi() -> Result<()> {
    if std::mem::size_of::<SynthRequest>() != 144
        || std::mem::offset_of!(SynthRequest, sample) != 8
        || std::mem::offset_of!(SynthRequest, pitch_bend) != 112
    {
        return Err(EngineError::Native(
            "SynthRequest does not match pinned 64-bit ABI".into(),
        ));
    }
    Ok(())
}
#[derive(Clone, Debug, Serialize)]
pub struct LibraryMetadata {
    pub native_architecture: &'static str,
    pub library_sha256: String,
    pub library_hash_verified: bool,
    pub source_commit: &'static str,
    pub source_version: &'static str,
}
pub fn inspect_library(path: &Path) -> Result<LibraryMetadata> {
    if !cfg!(all(
        target_os = "linux",
        target_env = "gnu",
        target_endian = "little",
        target_pointer_width = "64"
    )) {
        return invalid("WORLDLINE requires the 64-bit little-endian Linux glibc guest");
    }
    if !path.is_absolute() {
        return invalid("library path must be absolute");
    }
    let (architecture, machine, digest) = if cfg!(target_arch = "aarch64") {
        (
            "arm64",
            183,
            "80fb77357de4fae608e2d2fa12db867d0c48d0366263e6a280eccd90a1584dfe",
        )
    } else if cfg!(target_arch = "x86_64") {
        (
            "x64",
            62,
            "33a4123188bcf7b0b8fec4ae6c5bf882fb16b1aaa5aec6cd3391eccb5a69c490",
        )
    } else {
        return invalid("unsupported WORLDLINE architecture");
    };
    let data = read_regular(path, 8 * 1024 * 1024)?;
    if data.len() < 64
        || &data[..6] != b"\x7fELF\x02\x01"
        || u16::from_le_bytes([data[16], data[17]]) != 3
        || u16::from_le_bytes([data[18], data[19]]) != machine
    {
        return invalid("library must be a native ELF64 little-endian shared object");
    }
    let actual = format!("{:x}", Sha256::digest(&data));
    if actual != digest {
        return invalid("WORLDLINE library hash differs from pinned OpenUtau 0.1.565");
    }
    Ok(LibraryMetadata {
        native_architecture: architecture,
        library_sha256: actual,
        library_hash_verified: true,
        source_commit: SOURCE_COMMIT,
        source_version: SOURCE_VERSION,
    })
}
fn invalid<T>(s: &str) -> Result<T> {
    Err(EngineError::Invalid(s.into()))
}
fn finite(value: f64, name: &str, lo: f64, hi: f64) -> Result<f64> {
    if !value.is_finite() || value < lo || value > hi {
        return invalid(&format!("{name} must be finite in [{lo},{hi}]"));
    }
    Ok(value)
}
fn read_regular(path: &Path, maximum: u64) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.len() > maximum {
        return invalid("missing, symlinked, or oversized regular file");
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = options.open(path)?;
    if file.metadata()?.len() != metadata.len() {
        return invalid("file changed before read");
    }
    let mut data = Vec::new();
    (&mut file).take(maximum + 1).read_to_end(&mut data)?;
    if data.len() as u64 != metadata.len() || file.metadata()?.len() != metadata.len() {
        return invalid("file changed during read");
    }
    Ok(data)
}
pub fn load_job(path: &Path, output: &Path) -> Result<(WorldlineJob, PathBuf)> {
    if !path.is_absolute() {
        return invalid("job path must be absolute");
    }
    let raw = read_regular(path, MAX_JOB_BYTES)?;
    let directory = path
        .canonicalize()?
        .parent()
        .ok_or_else(|| EngineError::Invalid("job has no parent".into()))?
        .to_owned();
    validate_output(output, &directory)?;
    if output.file_name() == path.file_name() {
        return invalid("output must differ from job");
    }
    let job = serde_json::from_slice(&raw)
        .map_err(|e| EngineError::Invalid(format!("invalid phrase job: {e}")))?;
    Ok((job, directory))
}
fn validate_output(path: &Path, directory: &Path) -> Result<()> {
    if !path.is_absolute()
        || path.parent().and_then(|p| p.canonicalize().ok()).as_deref() != Some(directory)
        || path
            .extension()
            .and_then(|e| e.to_str())
            .is_none_or(|s| !s.eq_ignore_ascii_case("wav"))
        || fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
    {
        return invalid("output must be a WAV directly inside the job directory");
    }
    Ok(())
}
fn sample_path(directory: &Path, name: &str) -> Result<PathBuf> {
    let mut components = Path::new(name).components();
    if name.is_empty()
        || name.len() > 256
        || name.contains('\0')
        || !matches!(components.next(), Some(Component::Normal(_)))
        || components.next().is_some()
    {
        return invalid("sample_file must be a flat relative filename inside job directory");
    }
    let path = directory.join(name);
    if fs::symlink_metadata(&path)?.file_type().is_symlink()
        || path.canonicalize()?.parent() != Some(directory)
    {
        return invalid("sample_file must stay inside job directory without symlinks");
    }
    Ok(path)
}
fn load_wav(path: &Path) -> Result<Arc<Vec<f64>>> {
    let bytes = read_regular(path, (SAMPLE_RATE as u64 * 30 * 2) + 65536)?;
    let mut reader = hound::WavReader::new(std::io::Cursor::new(bytes))
        .map_err(|e| EngineError::Invalid(format!("invalid WAV: {e}")))?;
    let spec = reader.spec();
    if spec.channels != 1
        || spec.bits_per_sample != 16
        || spec.sample_rate != SAMPLE_RATE
        || spec.sample_format != hound::SampleFormat::Int
    {
        return invalid("source WAV must be decoded PCM16 mono 44100 Hz");
    }
    let count = reader.duration() as usize;
    if !(SAMPLE_RATE as usize / 10..=SAMPLE_RATE as usize * 30).contains(&count) {
        return invalid("source WAV must contain 100 ms to 30 seconds");
    }
    let samples: Vec<i16> = reader
        .samples::<i16>()
        .collect::<std::result::Result<_, _>>()
        .map_err(|e| EngineError::Invalid(format!("truncated WAV: {e}")))?;
    if samples.len() != count || samples.iter().all(|&s| s == 0) {
        return invalid("source WAV is truncated or silent");
    }
    Ok(Arc::new(
        samples.into_iter().map(|s| s as f64 / 32768.).collect(),
    ))
}
fn round_frame(ms: f64) -> usize {
    (ms / 10. + 0.5).floor() as usize
}
struct PreparedRequest {
    request: WorldlineRequest,
    samples: Arc<Vec<f64>>,
    end_frame: usize,
}
struct PreparedJob {
    requests: Vec<PreparedRequest>,
    output_frames: usize,
    expected_frames: usize,
    maximum_frames: usize,
    silence: Vec<[usize; 2]>,
}
fn prepare(job: &WorldlineJob, directory: &Path) -> Result<PreparedJob> {
    if job.schema_version != 1 || job.sample_rate != SAMPLE_RATE {
        return invalid("expected phrase schema 1 at 44100 Hz");
    }
    let maximum = finite(
        job.max_output_seconds,
        "max_output_seconds",
        0.01,
        MAX_SECONDS,
    )?;
    if !(1..=MAX_REQUESTS).contains(&job.requests.len()) {
        return invalid("phrase requires 1 to 256 requests");
    }
    let directory = directory.canonicalize()?;
    let mut samples = HashMap::<String, Arc<Vec<f64>>>::new();
    let mut requests = Vec::with_capacity(job.requests.len());
    let mut models = 0usize;
    for r in &job.requests {
        let sample = if let Some(sample) = samples.get(&r.sample_file) {
            sample.clone()
        } else {
            let sample = load_wav(&sample_path(&directory, &r.sample_file)?)?;
            samples.insert(r.sample_file.clone(), sample.clone());
            if samples.values().map(|v| v.len()).sum::<usize>() > SAMPLE_RATE as usize * 180 {
                return invalid("unique sources exceed 180 seconds");
            }
            sample
        };
        let duration_ms = sample.len() as f64 * 1000. / SAMPLE_RATE as f64;
        if !(0..=127).contains(&r.tone) {
            return invalid("tone must be MIDI 0 to 127");
        }
        for (v, name, lo, hi) in [
            (r.con_vel, "con_vel", 20., 300.),
            (r.volume, "volume", 0., 200.),
            (r.modulation, "modulation", 0., 100.),
            (r.tempo, "tempo", 20., 300.),
            (r.offset_ms, "offset_ms", 0., duration_ms),
            (r.consonant_ms, "consonant_ms", 0., duration_ms),
            (r.cutoff_ms, "cutoff_ms", -duration_ms, duration_ms),
            (
                r.required_length_ms,
                "required_length_ms",
                10.,
                maximum * 1000.,
            ),
            (r.position_ms, "position_ms", 0., maximum * 1000.),
            (r.length_ms, "length_ms", 10., maximum * 1000.),
            (r.skip_ms, "skip_ms", 0., maximum * 1000.),
            (r.fade_in_ms, "fade_in_ms", 0., maximum * 1000.),
            (r.fade_out_ms, "fade_out_ms", 0., maximum * 1000.),
        ] {
            finite(v, name, lo, hi)?;
        }
        let region = if r.cutoff_ms < 0. {
            -r.cutoff_ms
        } else {
            duration_ms - r.offset_ms - r.cutoff_ms
        };
        if region <= 0. || r.offset_ms + region > duration_ms + 1e-6 {
            return invalid("OTO region is empty or outside source");
        }
        let start = (r.offset_ms / 10.) as usize;
        let trimmed = ((r.offset_ms + region).ceil() / 10.) as usize - start;
        if trimmed < 2
            || (start + trimmed) * SAMPLES_PER_FRAME > sample.len()
            || r.consonant_ms > region
        {
            return invalid("OTO region requires two complete 10 ms frames and bounded consonant");
        }
        let speed = 0.5f64.powf(1. - r.con_vel / 100.);
        let mapped = (r.consonant_ms.max(1.) / speed).max(r.required_length_ms) / 10.;
        let mapping_frames = mapped.floor() as usize + 1;
        let begin = round_frame(r.position_ms);
        let end = round_frame(r.position_ms + r.length_ms);
        if end <= begin
            || r.position_ms + r.length_ms > maximum * 1000.
            || round_frame(r.skip_ms) + end - begin > mapping_frames
            || r.required_length_ms < r.skip_ms + r.length_ms
        {
            return invalid(
                "required length does not cover model frames, skip, and output duration",
            );
        }
        if r.fade_in_ms > r.length_ms || r.fade_out_ms > r.length_ms {
            return invalid("fade duration exceeds request length");
        }
        models += mapping_frames + 4;
        requests.push(PreparedRequest {
            request: r.clone(),
            samples: sample,
            end_frame: end,
        });
    }
    if models > 25280 {
        return invalid("aggregate phrase models exceed native memory budget");
    }
    requests.sort_by_key(|r| r.end_frame);
    let end_frame = requests.last().unwrap().end_frame;
    let duration_ms = finite(
        job.duration_ms.unwrap_or(end_frame as f64 * 10.),
        "duration_ms",
        end_frame as f64 * 10.,
        maximum * 1000.,
    )?;
    if (duration_ms / 10. - (duration_ms / 10.).round()).abs() > 1e-6 {
        return invalid("duration_ms must align to 10 ms grid");
    }
    let output_frames = round_frame(duration_ms) * SAMPLES_PER_FRAME + 1;
    let maximum_frames = (maximum * SAMPLE_RATE as f64).floor() as usize + 1;
    if output_frames > maximum_frames {
        return invalid("rounded output exceeds duration limit");
    }
    let mut curve_length = None;
    for (name, values, max) in [
        ("f0", &job.curves.f0, 2000.),
        ("gender", &job.curves.gender, 1.),
        ("tension", &job.curves.tension, 1.),
        ("breathiness", &job.curves.breathiness, 1.),
        ("voicing", &job.curves.voicing, 1.),
    ] {
        if values.len() < round_frame(duration_ms) + 1
            || values.len() > 12001
            || curve_length.is_some_and(|n| n != values.len())
        {
            return invalid("all five curves must have equal lengths covering the final frame");
        }
        curve_length = Some(values.len());
        for &value in values {
            finite(value, name, 0., max)?;
        }
    }
    if job.silence_intervals_ms.len() > MAX_REQUESTS + 1 {
        return invalid("too many silence intervals");
    }
    let mut silence = Vec::new();
    for &[begin, end] in &job.silence_intervals_ms {
        finite(begin, "silence start", 0., maximum * 1000.)?;
        finite(end, "silence end", begin, maximum * 1000.)?;
        silence.push([
            (begin * SAMPLE_RATE as f64 / 1000.) as usize,
            (end * SAMPLE_RATE as f64 / 1000.) as usize,
        ]);
    }
    Ok(PreparedJob {
        requests,
        output_frames,
        expected_frames: end_frame * SAMPLES_PER_FRAME + 1,
        maximum_frames,
        silence,
    })
}

type LogCallback = unsafe extern "C" fn(*const c_char);
type New = unsafe extern "C" fn() -> *mut c_void;
type Delete = unsafe extern "C" fn(*mut c_void);
type Add =
    unsafe extern "C" fn(*mut c_void, *const SynthRequest, f64, f64, f64, f64, f64, LogCallback);
type Curves = unsafe extern "C" fn(
    *mut c_void,
    *const f64,
    *const f64,
    *const f64,
    *const f64,
    *const f64,
    i32,
    LogCallback,
);
type Synth = unsafe extern "C" fn(*mut c_void, *mut *mut f32, LogCallback) -> i32;
unsafe extern "C" fn discard_log(_: *const c_char) {}
struct NativeApi {
    _library: Library,
    _cpp: Library,
    new: New,
    delete: Delete,
    add: Add,
    curves: Curves,
    synth: Synth,
    delete_array: Delete,
}
impl NativeApi {
    /// Caller verifies the ELF, architecture and hash before entering native code.
    unsafe fn load(path: &Path) -> Result<Self> {
        verify_abi()?;
        let error = |e: libloading::Error| EngineError::Native(e.to_string());
        let library = Library::new(path).map_err(error)?;
        let cpp = Library::new("libstdc++.so.6").map_err(error)?;
        let new = *library.get::<New>(b"PhraseSynthNew\0").map_err(error)?;
        let delete = *library
            .get::<Delete>(b"PhraseSynthDelete\0")
            .map_err(error)?;
        let add = *library
            .get::<Add>(b"PhraseSynthAddRequest\0")
            .map_err(error)?;
        let curves = *library
            .get::<Curves>(b"PhraseSynthSetCurves\0")
            .map_err(error)?;
        let synth = *library.get::<Synth>(b"PhraseSynthSynth\0").map_err(error)?;
        let delete_array = *cpp.get::<Delete>(b"_ZdaPv\0").map_err(error)?;
        Ok(Self {
            _library: library,
            _cpp: cpp,
            new,
            delete,
            add,
            curves,
            synth,
            delete_array,
        })
    }
}
struct Phrase<'a> {
    api: &'a NativeApi,
    pointer: *mut c_void,
}
impl Drop for Phrase<'_> {
    fn drop(&mut self) {
        unsafe {
            (self.api.delete)(self.pointer);
        }
    }
}
struct NativeOutput<'a> {
    api: &'a NativeApi,
    pointer: *mut f32,
}
impl Drop for NativeOutput<'_> {
    fn drop(&mut self) {
        if !self.pointer.is_null() {
            unsafe {
                (self.api.delete_array)(self.pointer.cast());
            }
        }
    }
}

#[derive(Debug, Serialize)]
pub struct WorldlinePcm {
    #[serde(skip)]
    pub pcm_s16le: Vec<u8>,
    pub sample_rate: u32,
    #[serde(rename = "frames")]
    pub samples: usize,
    pub peak: f64,
    pub rms: f64,
    #[serde(rename = "gain")]
    pub peak_gain: f64,
}
#[derive(Debug, Serialize)]
pub struct WorldlineReport {
    pub ok: bool,
    pub backend: &'static str,
    pub api_verified: bool,
    pub abi_verified: bool,
    pub phrase_render_verified: bool,
    pub request_count: usize,
    pub phrase_adapter_available: bool,
    pub portuguese_speech_verified: bool,
    pub voice_quality_verified: bool,
    pub channels: u32,
    pub duration_seconds: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub outputfilename: Option<String>,
    #[serde(flatten)]
    pub library: LibraryMetadata,
    #[serde(flatten)]
    pub audio: WorldlinePcm,
}
fn synthesize(api: &NativeApi, job: &WorldlineJob, prepared: &PreparedJob) -> Result<WorldlinePcm> {
    // SAFETY: NativeApi's library is held for every function pointer and guard;
    // samples/curves stay allocated until synthesis and output conversion finish.
    unsafe {
        let pointer = (api.new)();
        if pointer.is_null() {
            return Err(EngineError::Native("PhraseSynthNew returned null".into()));
        }
        let phrase = Phrase { api, pointer };
        for item in &prepared.requests {
            let r = &item.request;
            let request = SynthRequest {
                sample_fs: SAMPLE_RATE as i32,
                sample_length: item.samples.len() as i32,
                sample: item.samples.as_ptr(),
                frq_length: 0,
                frq: std::ptr::null(),
                tone: r.tone,
                con_vel: r.con_vel,
                offset: r.offset_ms,
                required_length: r.required_length_ms,
                consonant: r.consonant_ms,
                cut_off: r.cutoff_ms,
                volume: r.volume,
                modulation: r.modulation,
                tempo: r.tempo,
                pitch_bend_length: 0,
                pitch_bend: std::ptr::null(),
                flag_g: 0,
                flag_o: 0,
                flag_p: 86,
                flag_mt: 0,
                flag_mb: 0,
                flag_mv: 100,
            };
            (api.add)(
                phrase.pointer,
                &request,
                r.position_ms,
                r.skip_ms,
                r.length_ms,
                r.fade_in_ms,
                r.fade_out_ms,
                discard_log,
            );
        }
        let c = &job.curves;
        (api.curves)(
            phrase.pointer,
            c.f0.as_ptr(),
            c.gender.as_ptr(),
            c.tension.as_ptr(),
            c.breathiness.as_ptr(),
            c.voicing.as_ptr(),
            c.f0.len() as i32,
            discard_log,
        );
        let mut output = NativeOutput {
            api,
            pointer: std::ptr::null_mut(),
        };
        let count = (api.synth)(phrase.pointer, &mut output.pointer, discard_log);
        if output.pointer.is_null()
            || count <= 0
            || count as usize > prepared.maximum_frames
            || count as usize != prepared.expected_frames
        {
            return Err(EngineError::Native(format!(
                "invalid native sample count: {count}"
            )));
        }
        let values = std::slice::from_raw_parts(output.pointer, count as usize);
        let mut audio = values.iter().map(|&v| v as f64).collect::<Vec<_>>();
        if audio.iter().any(|v| !v.is_finite()) {
            return Err(EngineError::Native("non-finite output audio".into()));
        }
        audio.resize(prepared.output_frames, 0.);
        for &[begin, end] in &prepared.silence {
            let end = end.min(audio.len());
            if begin < end {
                audio[begin..end].fill(0.);
            }
        }
        let peak = audio.iter().fold(0f64, |m, v| m.max(v.abs()));
        let rms = (audio.iter().map(|v| v * v).sum::<f64>() / audio.len() as f64).sqrt();
        if peak <= 1e-8 || rms <= 1e-8 {
            return Err(EngineError::Native("native phrase produced silence".into()));
        }
        let gain = (0.98 / peak).min(1.);
        let mut pcm = Vec::with_capacity(audio.len() * 2);
        for value in &audio {
            pcm.extend_from_slice(
                &((value * gain * 32767.)
                    .round_ties_even()
                    .clamp(-32768., 32767.) as i16)
                    .to_le_bytes(),
            );
        }
        Ok(WorldlinePcm {
            pcm_s16le: pcm,
            sample_rate: SAMPLE_RATE,
            samples: audio.len(),
            peak: peak * gain,
            rms: rms * gain,
            peak_gain: gain,
        })
    }
}
pub fn render_worldline_pcm(
    library: &Path,
    job: &WorldlineJob,
    directory: &Path,
) -> Result<WorldlineReport> {
    let metadata = inspect_library(library)?;
    let prepared = prepare(job, directory)?;
    // SAFETY: the library hash and architecture were verified immediately above.
    let api = unsafe { NativeApi::load(library) }?;
    let audio = synthesize(&api, job, &prepared)?;
    Ok(WorldlineReport {
        ok: true,
        backend: "worldline-r",
        api_verified: true,
        abi_verified: true,
        phrase_render_verified: true,
        request_count: job.requests.len(),
        phrase_adapter_available: true,
        portuguese_speech_verified: false,
        voice_quality_verified: false,
        channels: 1,
        duration_seconds: audio.samples as f64 / SAMPLE_RATE as f64,
        outputfilename: None,
        library: metadata,
        audio,
    })
}
pub fn probe(library: &Path) -> Result<serde_json::Value> {
    let metadata = inspect_library(library)?;
    // SAFETY: the exact library was verified before loading symbols.
    let api = unsafe { NativeApi::load(library) }?;
    let phrase = unsafe { (api.new)() };
    if phrase.is_null() {
        return Err(EngineError::Native("PhraseSynthNew returned null".into()));
    }
    let _guard = Phrase {
        api: &api,
        pointer: phrase,
    };
    Ok(
        serde_json::json!({"ok":true,"backend":"worldline-r","api_verified":true,"abi_verified":true,"phrase_render_verified":false,"phrase_adapter_available":true,"library_sha256":metadata.library_sha256,"library_hash_verified":true,"source_commit":SOURCE_COMMIT,"source_version":SOURCE_VERSION,"native_architecture":metadata.native_architecture}),
    )
}
static NEXT_FILE: AtomicU64 = AtomicU64::new(0);
pub fn write_wav_atomic(path: &Path, audio: &WorldlinePcm) -> Result<()> {
    let directory = path
        .parent()
        .ok_or_else(|| EngineError::Invalid("output has no directory".into()))?;
    let temporary = directory.join(format!(
        ".worldline-{}-{}.wav",
        std::process::id(),
        NEXT_FILE.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: SAMPLE_RATE,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer =
            hound::WavWriter::new(file, spec).map_err(|e| EngineError::Native(e.to_string()))?;
        for chunk in audio.pcm_s16le.chunks_exact(2) {
            writer
                .write_sample(i16::from_le_bytes([chunk[0], chunk[1]]))
                .map_err(|e| EngineError::Native(e.to_string()))?;
        }
        writer
            .finalize()
            .map_err(|e| EngineError::Native(e.to_string()))?;
        File::open(&temporary)?.sync_all()?;
        fs::rename(&temporary, path)?;
        File::open(directory)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}
pub fn render_worldline(
    library: &Path,
    job: &WorldlineJob,
    directory: &Path,
    output: &Path,
) -> Result<WorldlineReport> {
    let directory = directory.canonicalize()?;
    validate_output(output, &directory)?;
    if job
        .requests
        .iter()
        .any(|r| Some(std::ffi::OsStr::new(&r.sample_file)) == output.file_name())
    {
        return invalid("output must not overwrite input WAV");
    }
    let mut report = render_worldline_pcm(library, job, &directory)?;
    write_wav_atomic(output, &report.audio)?;
    report.outputfilename = output.file_name().map(|s| s.to_string_lossy().into_owned());
    Ok(report)
}

/// Own the helper process until its native operation has physically finished.
/// For a guest, `command` includes `proot-distro login ... --` and the Rust helper.
pub fn run_worldline_isolated(
    command: &mut std::process::Command,
    timeout: Duration,
    cancelled: &Cancellation,
) -> Result<serde_json::Value> {
    if cancelled.is_cancelled() {
        return Err(EngineError::Cancelled);
    }
    if timeout.is_zero() {
        return Err(EngineError::Timeout);
    }
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| EngineError::Invalid("timeout overflow".into()))?;
    let mut child = crate::process::EngineProcess::spawn(command)?;
    let report = child.receive_json(deadline, cancelled)?;
    if report["ok"] != true {
        let detail = report["error"]
            .as_str()
            .unwrap_or("native phrase helper failed");
        return Err(EngineError::Native(detail.chars().take(1024).collect()));
    }
    if report["ok"] != true
        || report["api_verified"] != true
        || report["abi_verified"] != true
        || report["library_hash_verified"] != true
        || report["source_commit"] != SOURCE_COMMIT
    {
        return Err(EngineError::Protocol(
            "native helper did not verify operation".into(),
        ));
    }
    // Helper writes one final report; close reaps it, including guest descendants.
    child.close();
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    struct TestDirectory(PathBuf);
    impl TestDirectory {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "tts-engines-{}-{}",
                std::process::id(),
                NEXT_FILE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn fixture(directory: &Path) -> WorldlineJob {
        let sample = directory.join("source.wav");
        let mut writer = hound::WavWriter::create(
            sample,
            hound::WavSpec {
                channels: 1,
                sample_rate: SAMPLE_RATE,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            },
        )
        .unwrap();
        for i in 0..8820 {
            writer
                .write_sample(((i as f64 * 0.03).sin() * 10000.) as i16)
                .unwrap();
        }
        writer.finalize().unwrap();
        WorldlineJob {
            schema_version: 1,
            sample_rate: SAMPLE_RATE,
            max_output_seconds: 1.,
            requests: vec![WorldlineRequest {
                sample_file: "source.wav".into(),
                tone: 60,
                con_vel: 100.,
                volume: 100.,
                modulation: 0.,
                tempo: 120.,
                offset_ms: 0.,
                consonant_ms: 20.,
                cutoff_ms: -100.,
                required_length_ms: 100.,
                position_ms: 0.,
                length_ms: 100.,
                skip_ms: 0.,
                fade_in_ms: 10.,
                fade_out_ms: 10.,
            }],
            duration_ms: Some(120.),
            curves: WorldlineCurves {
                f0: vec![220.; 13],
                gender: vec![0.5; 13],
                tension: vec![0.5; 13],
                breathiness: vec![0.; 13],
                voicing: vec![1.; 13],
            },
            silence_intervals_ms: vec![[20., 30.]],
        }
    }
    #[test]
    fn abi_layout_matches_openutau_64_bit() {
        verify_abi().unwrap();
        assert_eq!(std::mem::size_of::<SynthRequest>(), 144);
    }
    #[test]
    fn preserves_job_validation_and_no_output_truncation() {
        let directory = TestDirectory::new();
        let mut job = fixture(&directory.0);
        let prepared = prepare(&job, &directory.0).unwrap();
        assert_eq!(prepared.expected_frames, 4411);
        assert_eq!(prepared.output_frames, 5293);
        job.duration_ms = Some(99.);
        assert!(prepare(&job, &directory.0).is_err());
        job.duration_ms = Some(120.);
        job.curves.gender[0] = f64::NAN;
        assert!(prepare(&job, &directory.0).is_err());
        job.curves.gender[0] = 0.5;
        job.requests[0].sample_file = "../source.wav".into();
        assert!(prepare(&job, &directory.0).is_err());
    }
    #[test]
    fn rejects_symlink_samples_and_output_overwrite() {
        let directory = TestDirectory::new();
        let mut job = fixture(&directory.0);
        std::os::unix::fs::symlink(directory.0.join("source.wav"), directory.0.join("link.wav"))
            .unwrap();
        job.requests[0].sample_file = "link.wav".into();
        assert!(prepare(&job, &directory.0).is_err());
        job.requests[0].sample_file = "source.wav".into();
        assert!(render_worldline(
            Path::new("/missing.so"),
            &job,
            &directory.0,
            &directory.0.join("source.wav")
        )
        .unwrap_err()
        .to_string()
        .contains("overwrite"));
    }
    #[test]
    fn native_ffi_retains_inputs_and_frees_phrase_on_success_and_failure() {
        let directory = TestDirectory::new();
        let job = fixture(&directory.0);
        let source = directory.0.join("motor.cpp");
        let library = directory.0.join("libfake.so");
        fs::write(&source,r#"
#include <cstdint>
#include <cmath>
#include <cstddef>
struct Request {int32_t fs,len;const double *sample;int32_t frq_len;void *frq;int32_t tone;double velocity,offset,length,consonant,cutoff,volume,modulation,tempo;int32_t bend_len;int32_t *bend;int32_t g,O,P,Mt,Mb,Mv;};
static_assert(sizeof(Request)==144);static_assert(offsetof(Request,bend)==112);
using Log=void(*)(const char*);
struct Phrase {const double *sample;int32_t len;const double *f0;};
static int deleted=0;static int invalid=0;
extern "C" {
void* PhraseSynthNew(){return new Phrase{};}
void PhraseSynthDelete(void *p){delete static_cast<Phrase*>(p);deleted++;}
void PhraseSynthAddRequest(void *p,const Request *r,double,double,double,double,double,Log){auto *q=static_cast<Phrase*>(p);q->sample=r->sample;q->len=r->len;if(r->P!=86||r->Mv!=100||r->fs!=44100)invalid++;}
void PhraseSynthSetCurves(void *p,const double *f0,const double*,const double*,const double*,const double*,int32_t n,Log){auto *q=static_cast<Phrase*>(p);q->f0=f0;if(n!=13)invalid++;}
int PhraseSynthSynth(void *p,float **out,Log){auto *q=static_cast<Phrase*>(p);if(!q->sample||q->len!=8820||q->f0[0]!=220.)invalid++;*out=new float[4411];for(int i=0;i<4411;i++)(*out)[i]=0.1f*std::sin(i*0.03f);return invalid ? -1:4411;}
int GetDeleted(){return deleted;}int GetInvalid(){return invalid;}
}
"#).unwrap();
        let status = std::process::Command::new("c++")
            .args(["-shared", "-fPIC", "-std=c++11"])
            .arg(&source)
            .arg("-o")
            .arg(&library)
            .status()
            .expect("C++ compiler is required for the FFI contract gate");
        assert!(status.success());
        // Production loader must reject an unpinned motor, including this fixture.
        assert!(inspect_library(&library).is_err());
        // SAFETY: this private test-only path loads the bounded fixture compiled above.
        let api = unsafe { NativeApi::load(&library) }.unwrap();
        let prepared = prepare(&job, &directory.0).unwrap();
        let audio = synthesize(&api, &job, &prepared).unwrap();
        assert_eq!(audio.samples, 5293);
        assert_eq!(audio.pcm_s16le.len(), 10586);
        assert!(audio.pcm_s16le[882 * 2..1323 * 2].iter().all(|&b| b == 0));
        assert!(audio.pcm_s16le[4411 * 2..].iter().all(|&b| b == 0));
        unsafe {
            let deleted = api
                ._library
                .get::<unsafe extern "C" fn() -> i32>(b"GetDeleted\0")
                .unwrap();
            assert_eq!(deleted(), 1);
            let invalid = api
                ._library
                .get::<unsafe extern "C" fn() -> i32>(b"GetInvalid\0")
                .unwrap();
            assert_eq!(invalid(), 0);
        }
        // Sample-count failure still drops native output and the phrase.
        let mut bad = prepare(&job, &directory.0).unwrap();
        bad.expected_frames = 100;
        assert!(synthesize(&api, &job, &bad).is_err());
        unsafe {
            let deleted = api
                ._library
                .get::<unsafe extern "C" fn() -> i32>(b"GetDeleted\0")
                .unwrap();
            assert_eq!(deleted(), 2);
        }
        let output = directory.0.join("render.wav");
        write_wav_atomic(&output, &audio).unwrap();
        let wav = hound::WavReader::open(output).unwrap();
        assert_eq!(wav.duration(), 5293);
        assert_eq!(wav.spec().sample_rate, 44100);
        assert!(!fs::read_dir(&directory.0).unwrap().any(|e| e
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".worldline-")));
    }
}
