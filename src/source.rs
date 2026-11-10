//! Native replay -> FFmpeg -> Discord PCM bridge. Compressed bytes never
//! return to Python. Only complete 20 ms PCM frames cross the voice boundary.
use crate::replay::{ReplayBuffer, ReplayError};
use std::collections::VecDeque;
use std::fmt;
use std::io::{self, Read, Write};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const PCM_FRAME_BYTES: usize = 3840;

#[derive(Debug)]
pub enum SourceError { Io(io::Error), Cancelled, Failed(String) }
impl fmt::Display for SourceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => error.fmt(f),
            Self::Cancelled => write!(f, "source TTS cancelada"),
            Self::Failed(error) => write!(f, "{error}"),
        }
    }
}
impl std::error::Error for SourceError {}
impl From<io::Error> for SourceError { fn from(error: io::Error) -> Self { Self::Io(error) } }

struct Shared {
    child: Mutex<Option<Child>>,
    stop: AtomicBool,
    reaped: AtomicBool,
    error: Mutex<Option<String>>,
    stderr: Mutex<VecDeque<u8>>,
    input_bytes: AtomicU64,
    frames: AtomicU64,
}
impl Shared {
    fn fail(&self, error: String) {
        let mut current = self.error.lock().unwrap_or_else(|e| e.into_inner());
        if current.is_none() { *current = Some(error.chars().take(4096).collect()); }
    }
    fn error(&self) -> Option<String> { self.error.lock().unwrap_or_else(|e| e.into_inner()).clone() }
    fn kill_and_reap(&self) {
        let mut child = self.child.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut process) = child.take() {
            let _ = process.kill();
            if process.wait().is_ok() { self.reaped.store(true, Ordering::Release); }
        }
    }
}

pub struct SourceHandle {
    replay: Arc<ReplayBuffer>,
    shared: Arc<Shared>,
    stdout: Mutex<ChildStdout>,
    workers: Mutex<Vec<JoinHandle<()>>>,
    eof: AtomicBool,
    cancel_requested: AtomicBool,
}

#[derive(Debug, Clone)]
pub struct SourceSnapshot {
    pub cancelled: bool,
    pub reaped: bool,
    pub input_bytes: u64,
    pub frames: u64,
    pub error: Option<String>,
}

impl SourceHandle {
    pub fn spawn(replay: Arc<ReplayBuffer>, executable: &str, before_args: &[String],
                 output_args: &[String]) -> Result<Self, SourceError> {
        let mut process = Command::new(executable)
            .args(["-hide_banner", "-nostdin"])
            .args(before_args).args(["-i", "pipe:0"])
            .args(output_args)
            .args(["-vn", "-f", "s16le", "-ar", "48000", "-ac", "2", "-acodec", "pcm_s16le", "pipe:1"])
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
        let mut stdin = process.stdin.take().unwrap();
        let stdout = process.stdout.take().unwrap();
        let mut stderr = process.stderr.take().unwrap();
        let shared = Arc::new(Shared { child: Mutex::new(Some(process)), stop: AtomicBool::new(false),
            reaped: AtomicBool::new(false), error: Mutex::new(None), stderr: Mutex::new(VecDeque::new()),
            input_bytes: AtomicU64::new(0), frames: AtomicU64::new(0) });
        let source = Self { replay: replay.clone(), shared: shared.clone(), stdout: Mutex::new(stdout),
            workers: Mutex::new(Vec::new()), eof: AtomicBool::new(false), cancel_requested: AtomicBool::new(false) };
        let stderr_state = shared.clone();
        let stderr_worker = thread::Builder::new().name("tts-ffmpeg-stderr".into()).stack_size(256 * 1024).spawn(move || {
            let mut bytes = [0; 2048];
            loop {
                match stderr.read(&mut bytes) {
                    Ok(0) => break,
                    Ok(count) => {
                        let mut tail = stderr_state.stderr.lock().unwrap_or_else(|e| e.into_inner());
                        tail.extend(&bytes[..count]);
                        while tail.len() > 8192 { tail.pop_front(); }
                    }
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    Err(_) => break,
                }
            }
        })?;
        source.workers.lock().unwrap_or_else(|e| e.into_inner()).push(stderr_worker);
        let input_state = shared;
        let input_worker = thread::Builder::new().name("tts-replay-ffmpeg".into()).stack_size(256 * 1024).spawn(move || {
            let mut offset = 0;
            loop {
                match replay.read_next_at(offset, 16384, &input_state.stop) {
                    Ok(Some(bytes)) => {
                        if let Err(error) = stdin.write_all(&bytes) {
                            if !input_state.stop.load(Ordering::Acquire) { input_state.fail(format!("FFmpeg stdin: {error}")); }
                            break;
                        }
                        offset += bytes.len();
                        input_state.input_bytes.fetch_add(bytes.len() as u64, Ordering::Relaxed);
                    }
                    Ok(None) | Err(ReplayError::Cancelled) => break,
                    Err(error) => { input_state.fail(format!("replay TTS: {error}")); break; }
                }
            }
            // Closing stdin is the only successful EOF. Cancellation instead
            // kills FFmpeg and its pipes, waking a blocked writer or PCM read.
            drop(stdin);
        })?;
        source.workers.lock().unwrap_or_else(|e| e.into_inner()).push(input_worker);
        Ok(source)
    }

    fn join_workers(&self) {
        let workers = std::mem::take(&mut *self.workers.lock().unwrap_or_else(|e| e.into_inner()));
        for worker in workers { let _ = worker.join(); }
    }

    fn complete(&self) {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let result = {
                let mut child = self.shared.child.lock().unwrap_or_else(|e| e.into_inner());
                match child.as_mut() {
                    None => break,
                    Some(process) => match process.try_wait() {
                        Ok(Some(status)) => { child.take(); Some(Ok(status)) }
                        Ok(None) => None,
                        Err(error) => Some(Err(error)),
                    },
                }
            };
            if let Some(result) = result {
                match result {
                    Ok(status) => {
                        self.shared.reaped.store(true, Ordering::Release);
                        if !status.success() { self.shared.fail(format!("FFmpeg encerrou com {status}")); }
                    }
                    Err(error) => { self.shared.fail(format!("FFmpeg wait: {error}")); self.shared.kill_and_reap(); }
                }
                break;
            }
            if Instant::now() >= deadline {
                self.shared.fail("FFmpeg não encerrou após EOF".into());
                self.shared.kill_and_reap(); break;
            }
            thread::park_timeout(Duration::from_millis(5));
        }
        // An early decoder exit must also wake a feeder waiting for future
        // replay bytes, without terminating the shared network producer.
        self.shared.stop.store(true, Ordering::Release);
        self.replay.wake_readers();
        self.join_workers();
    }

    fn terminal_result(&self) -> Result<Vec<u8>, SourceError> {
        if let Some(error) = self.shared.error() { return Err(SourceError::Failed(error)); }
        // A caller-requested cancellation is distinguishable from successful
        // EOF; complete() also stops the feeder after normal EOF.
        if self.cancelled() { return Err(SourceError::Cancelled); }
        Ok(Vec::new())
    }

    pub fn read_frame(&self) -> Result<Vec<u8>, SourceError> {
        if self.eof.load(Ordering::Acquire) { return self.terminal_result(); }
        let mut stdout = self.stdout.lock().unwrap_or_else(|e| e.into_inner());
        let mut frame = vec![0; PCM_FRAME_BYTES];
        let mut filled = 0;
        while filled < PCM_FRAME_BYTES {
            match stdout.read(&mut frame[filled..]) {
                Ok(0) => {
                    self.eof.store(true, Ordering::Release);
                    self.complete();
                    if filled == 0 { return self.terminal_result(); }
                    break;
                }
                Ok(count) => filled += count,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => { self.shared.fail(format!("FFmpeg PCM: {error}")); self.cancel(); return Err(SourceError::Io(error)); }
            }
        }
        self.shared.frames.fetch_add(1, Ordering::Relaxed);
        Ok(frame)
    }

    pub fn cancel(&self) {
        self.cancel_requested.store(true, Ordering::Release);
        self.shared.stop.store(true, Ordering::Release);
        self.replay.wake_readers();
        self.shared.kill_and_reap();
        self.join_workers();
    }

    pub fn cancelled(&self) -> bool { self.cancel_requested.load(Ordering::Acquire) }

    pub fn snapshot(&self) -> SourceSnapshot {
        SourceSnapshot { cancelled: self.cancelled(), reaped: self.shared.reaped.load(Ordering::Acquire),
            input_bytes: self.shared.input_bytes.load(Ordering::Relaxed), frames: self.shared.frames.load(Ordering::Relaxed),
            error: self.shared.error() }
    }
}

impl Drop for SourceHandle { fn drop(&mut self) { self.cancel(); } }

#[cfg(test)]
mod tests {
    use super::*;
    use crate::replay::MemoryBudget;
    use std::sync::mpsc;

    fn wav() -> Vec<u8> {
        let data_size = 2640u32 * 4; // 55 ms: two frames plus one short tail.
        let mut bytes = Vec::new();
        bytes.extend(b"RIFF"); bytes.extend((36 + data_size).to_le_bytes()); bytes.extend(b"WAVEfmt ");
        bytes.extend(16u32.to_le_bytes()); bytes.extend(1u16.to_le_bytes()); bytes.extend(2u16.to_le_bytes());
        bytes.extend(48000u32.to_le_bytes()); bytes.extend(192000u32.to_le_bytes());
        bytes.extend(4u16.to_le_bytes()); bytes.extend(16u16.to_le_bytes()); bytes.extend(b"data");
        bytes.extend(data_size.to_le_bytes());
        for _ in 0..data_size / 2 { bytes.extend(1000i16.to_le_bytes()); }
        bytes
    }

    fn replay() -> Arc<ReplayBuffer> {
        Arc::new(ReplayBuffer::new(std::env::temp_dir(), Arc::new(MemoryBudget::new(0)), 0, 1024 * 1024).unwrap())
    }

    fn ffmpeg_available() -> bool {
        Command::new("ffmpeg").arg("-version").stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false)
    }

    fn args() -> Vec<String> { ["-f", "wav", "-probesize", "32", "-analyzeduration", "0"].into_iter().map(str::to_owned).collect() }

    #[test]
    fn real_ffmpeg_decodes_native_spool_and_pads_only_final_frame() {
        if !ffmpeg_available() { return; }
        let replay = replay(); replay.append(&wav()).unwrap(); replay.finish(None);
        let source = SourceHandle::spawn(replay, "ffmpeg", &args(), &["-loglevel".into(), "error".into()]).unwrap();
        let mut frames = Vec::new();
        loop { let frame = source.read_frame().unwrap(); if frame.is_empty() { break; } frames.push(frame); }
        assert_eq!(frames.len(), 3); assert!(frames.iter().all(|f| f.len() == PCM_FRAME_BYTES));
        assert_eq!(&frames[0][..4], &[0xe8, 3, 0xe8, 3]);
        assert!(frames[2][2880..].iter().all(|b| *b == 0));
        assert!(source.snapshot().reaped); assert_eq!(source.snapshot().frames, 3);
    }

    #[test]
    fn configured_ffmpeg_filter_is_preserved() {
        if !ffmpeg_available() { return; }
        let replay = replay(); replay.append(&wav()).unwrap(); replay.finish(None);
        let source = SourceHandle::spawn(replay, "ffmpeg", &args(), &["-af".into(), "volume=0".into(), "-loglevel".into(), "error".into()]).unwrap();
        let mut frames = 0;
        loop { let frame = source.read_frame().unwrap(); if frame.is_empty() { break; } frames += 1; assert!(frame.iter().all(|b| *b == 0)); }
        assert_eq!(frames, 3); assert!(source.snapshot().reaped);
    }

    #[test]
    fn cancel_unblocks_pcm_read_and_preserves_shared_replay() {
        if !ffmpeg_available() { return; }
        let replay = replay();
        let source = Arc::new(SourceHandle::spawn(replay.clone(), "ffmpeg", &args(), &[]).unwrap());
        let reader = source.clone(); let (send, receive) = mpsc::channel();
        let thread = thread::spawn(move || { send.send(reader.read_frame()).unwrap(); });
        source.cancel();
        assert!(matches!(receive.recv_timeout(Duration::from_secs(2)).unwrap(), Err(SourceError::Cancelled)));
        thread.join().unwrap(); assert!(source.snapshot().reaped); assert!(!replay.snapshot().closed);
        replay.append(b"another reader remains alive").unwrap();
    }

    #[test]
    fn upstream_failure_and_decoder_failure_are_not_successful_eof() {
        if !ffmpeg_available() { return; }
        let replay = replay(); replay.append(&wav()).unwrap(); replay.finish(Some("provider test failed".into()));
        let source = SourceHandle::spawn(replay, "ffmpeg", &args(), &[]).unwrap();
        loop { match source.read_frame() {
            Ok(frame) => assert!(!frame.is_empty(), "upstream failure became successful EOF"),
            Err(error) => { assert!(error.to_string().contains("provider test failed")); break; }
        } }
        assert!(source.snapshot().reaped);
        let replay = self::replay(); replay.append(b"invalid audio").unwrap(); replay.finish(None);
        let invalid = SourceHandle::spawn(replay, "ffmpeg", &args(), &[]).unwrap();
        assert!(invalid.read_frame().is_err()); assert!(invalid.snapshot().reaped);
    }
}
