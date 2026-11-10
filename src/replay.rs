//! Bounded append-only audio storage. Every reader owns its own byte offset.
//!
//! Disk I/O and close share one mutex: a descriptor cannot be closed while a
//! read, append or cache publication still owns it. No Python objects are held.
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, TryLockError};

static NEXT_FILE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug)]
pub enum ReplayError {
    InvalidLimit,
    CapacityExceeded,
    Closed,
    Cancelled,
    Incomplete,
    Failed(String),
    Io(io::Error),
}

impl fmt::Display for ReplayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidLimit => write!(f, "limite de replay inválido"),
            Self::CapacityExceeded => write!(f, "áudio TTS excedeu o orçamento de bytes"),
            Self::Closed => write!(f, "buffer TTS fechado"),
            Self::Cancelled => write!(f, "leitura TTS cancelada"),
            Self::Incomplete => write!(f, "áudio incompleto não pode entrar no cache"),
            Self::Failed(error) => write!(f, "{error}"),
            Self::Io(error) => error.fmt(f),
        }
    }
}

impl std::error::Error for ReplayError {}
impl From<io::Error> for ReplayError {
    fn from(value: io::Error) -> Self { Self::Io(value) }
}

/// Shared across replay buffers; reservation and release are atomic together.
#[derive(Debug)]
pub struct MemoryBudget {
    limit: usize,
    used: Mutex<usize>,
}

impl MemoryBudget {
    pub fn new(limit: usize) -> Self { Self { limit, used: Mutex::new(0) } }
    pub fn limit(&self) -> usize { self.limit }
    pub fn used(&self) -> usize { *self.used.lock().unwrap_or_else(|e| e.into_inner()) }
    pub fn reserve(&self, size: usize) -> bool {
        let mut used = self.used.lock().unwrap_or_else(|e| e.into_inner());
        if size > self.limit.saturating_sub(*used) { return false; }
        *used += size;
        true
    }
    fn try_reserve(&self, size: usize) -> Option<bool> {
        let mut used = match self.used.try_lock() {
            Ok(used) => used,
            Err(TryLockError::Poisoned(error)) => error.into_inner(),
            Err(TryLockError::WouldBlock) => return None,
        };
        if size > self.limit.saturating_sub(*used) { return Some(false); }
        *used += size;
        Some(true)
    }
    pub fn release(&self, size: usize) {
        let mut used = self.used.lock().unwrap_or_else(|e| e.into_inner());
        *used = used.saturating_sub(size);
    }
}

#[derive(Debug)]
struct Spool { file: File, path: Option<PathBuf> }

impl Drop for Spool {
    fn drop(&mut self) {
        if let Some(path) = self.path.take() { let _ = fs::remove_file(path); }
    }
}

#[derive(Debug)]
enum Storage { Memory(Vec<u8>), Disk(Spool), Closed }

#[derive(Debug)]
struct Interior {
    storage: Storage,
    size: usize,
    ended: bool,
    error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplaySnapshot {
    pub size: usize,
    pub memory_bytes: usize,
    pub spilled: bool,
    pub ended: bool,
    pub closed: bool,
    pub error: Option<String>,
}

#[derive(Debug)]
pub struct ReplayBuffer {
    directory: PathBuf,
    budget: Arc<MemoryBudget>,
    memory_limit: usize,
    max_bytes: usize,
    inner: Mutex<Interior>,
    changed: Condvar,
}

fn temporary(directory: &Path, prefix: &str) -> io::Result<(File, PathBuf)> {
    for _ in 0..128 {
        let sequence = NEXT_FILE.fetch_add(1, Ordering::Relaxed);
        let path = directory.join(format!("{prefix}{}-{sequence}.part", std::process::id()));
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&path) {
            Ok(file) => return Ok((file, path)),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
    Err(io::Error::new(io::ErrorKind::AlreadyExists, "nomes temporários esgotados"))
}

impl ReplayBuffer {
    pub fn new(directory: impl Into<PathBuf>, budget: Arc<MemoryBudget>, memory_limit: usize,
               max_bytes: usize) -> Result<Self, ReplayError> {
        if max_bytes == 0 { return Err(ReplayError::InvalidLimit); }
        Ok(Self { directory: directory.into(), budget, memory_limit, max_bytes,
            inner: Mutex::new(Interior { storage: Storage::Memory(Vec::new()), size: 0,
                ended: false, error: None }), changed: Condvar::new() })
    }

    fn lock(&self) -> MutexGuard<'_, Interior> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Event-loop fast path: try-lock only, bounded RAM operations, no disk.
    /// False hands ownership to the ordinary blocking-I/O worker path.
    pub fn try_append_in_memory(&self, data: &[u8]) -> Result<bool, ReplayError> {
        if data.len() > 65536 { return Ok(false); }
        let mut inner = match self.inner.try_lock() {
            Ok(inner) => inner,
            Err(TryLockError::Poisoned(error)) => error.into_inner(),
            Err(TryLockError::WouldBlock) => return Ok(false),
        };
        if data.is_empty() || inner.ended || matches!(inner.storage, Storage::Closed) { return Ok(true); }
        if data.len() > self.max_bytes.saturating_sub(inner.size) { return Err(ReplayError::CapacityExceeded); }
        let Storage::Memory(memory) = &mut inner.storage else { return Ok(false); };
        // Bound potential realloc/memcpy on the loop. Larger in-memory buffers
        // continue through the worker, as do every spill and disk operation.
        if memory.len().saturating_add(data.len()) > self.memory_limit.min(256 * 1024) {
            return Ok(false);
        }
        if self.budget.try_reserve(data.len()) != Some(true) { return Ok(false); }
        memory.reserve_exact(data.len());
        memory.extend_from_slice(data);
        inner.size += data.len();
        drop(inner);
        self.changed.notify_all();
        Ok(true)
    }

    /// None means contention/disk/large copy; Some always comes from RAM and
    /// requires no blocking lock, filesystem call, or background thread.
    pub fn try_read_in_memory(&self, offset: usize, length: usize) -> Result<Option<Vec<u8>>, ReplayError> {
        if length == 0 { return Err(ReplayError::InvalidLimit); }
        if length > 65536 { return Ok(None); }
        let inner = match self.inner.try_lock() {
            Ok(inner) => inner,
            Err(TryLockError::Poisoned(error)) => error.into_inner(),
            Err(TryLockError::WouldBlock) => return Ok(None),
        };
        match &inner.storage {
            Storage::Closed => Err(ReplayError::Closed),
            Storage::Disk(_) => Ok(None),
            Storage::Memory(memory) => {
                let count = length.min(inner.size.saturating_sub(offset));
                if count == 0 { return Ok(Some(Vec::new())); }
                Ok(Some(memory[offset..offset + count].to_vec()))
            }
        }
    }

    /// Closed/finished buffers ignore new input, matching the existing bridge.
    pub fn append(&self, data: &[u8]) -> Result<(), ReplayError> {
        let mut inner = self.lock();
        if data.is_empty() || inner.ended || matches!(inner.storage, Storage::Closed) { return Ok(()); }
        if data.len() > self.max_bytes.saturating_sub(inner.size) {
            return Err(ReplayError::CapacityExceeded);
        }
        if let Storage::Memory(memory) = &mut inner.storage {
            if data.len() <= self.memory_limit.saturating_sub(memory.len()) && self.budget.reserve(data.len()) {
                // Exact reservation prevents Vec's geometric growth from hiding RAM.
                memory.reserve_exact(data.len());
                memory.extend_from_slice(data);
                inner.size += data.len();
                drop(inner);
                self.changed.notify_all();
                return Ok(());
            }
            let (file, path) = temporary(&self.directory, "tts-spool-")?;
            let mut spool = Spool { file, path: Some(path) };
            // The Python/host cleanup uses flock before unlinking named files.
            // Keep the same lease if the platform cannot unlink an open spool.
            spool.file.lock_shared()?;
            // Unlink immediately on Unix, just like tempfile.TemporaryFile.
            #[cfg(unix)]
            if fs::remove_file(spool.path.as_ref().unwrap()).is_ok() { spool.path = None; }
            spool.file.write_all(memory)?;
            spool.file.write_all(data)?;
            spool.file.flush()?;
            let released = memory.len();
            inner.storage = Storage::Disk(spool);
            self.budget.release(released);
        } else if matches!(inner.storage, Storage::Disk(_)) {
            // A failed previous write may have appended a partial suffix. Only
            // committed bytes count, so truncate before retrying another append.
            let committed = inner.size;
            if let Storage::Disk(spool) = &mut inner.storage {
                spool.file.set_len(committed as u64)?;
                spool.file.seek(SeekFrom::Start(committed as u64))?;
                spool.file.write_all(data)?;
                spool.file.flush()?;
            }
        }
        inner.size += data.len();
        drop(inner);
        self.changed.notify_all();
        Ok(())
    }

    /// Independent offsets allow replay and simultaneous slow/fast consumers.
    /// Empty means no bytes currently available; snapshot distinguishes EOF.
    pub fn read_at(&self, offset: usize, length: usize) -> Result<Vec<u8>, ReplayError> {
        if length == 0 { return Err(ReplayError::InvalidLimit); }
        let mut inner = self.lock();
        if matches!(inner.storage, Storage::Closed) { return Err(ReplayError::Closed); }
        let count = length.min(inner.size.saturating_sub(offset));
        if count == 0 { return Ok(Vec::new()); }
        match &mut inner.storage {
            Storage::Memory(memory) => Ok(memory[offset..offset + count].to_vec()),
            Storage::Disk(spool) => {
                spool.file.seek(SeekFrom::Start(offset as u64))?;
                let mut data = vec![0; count];
                spool.file.read_exact(&mut data)?;
                Ok(data)
            }
            Storage::Closed => Err(ReplayError::Closed),
        }
    }

    pub fn finish(&self, error: Option<String>) {
        let mut inner = self.lock();
        inner.ended = true;
        inner.error = error;
        drop(inner);
        self.changed.notify_all();
    }

    /// Blocking native consumer path. Cancellation belongs to this reader,
    /// leaving other replay consumers and the shared producer unaffected.
    pub fn read_next_at(&self, offset: usize, length: usize, cancelled: &AtomicBool)
        -> Result<Option<Vec<u8>>, ReplayError> {
        if length == 0 { return Err(ReplayError::InvalidLimit); }
        let mut inner = self.lock();
        loop {
            if cancelled.load(Ordering::Acquire) { return Err(ReplayError::Cancelled); }
            if matches!(inner.storage, Storage::Closed) { return Err(ReplayError::Closed); }
            if offset < inner.size {
                drop(inner);
                return self.read_at(offset, length).map(Some);
            }
            if inner.ended {
                return match &inner.error {
                    Some(error) => Err(ReplayError::Failed(error.clone())),
                    None => Ok(None),
                };
            }
            inner = self.changed.wait(inner).unwrap_or_else(|error| error.into_inner());
        }
    }

    pub fn wake_readers(&self) {
        // Locking pairs cancellation's notification with the predicate check
        // above, so notify cannot race between that check and Condvar::wait.
        let _guard = self.lock();
        self.changed.notify_all();
    }

    /// Publish only a fully copied, synced file. Failure never truncates the
    /// previous cache entry; temporary files are removed on every error path.
    pub fn copy_to(&self, path: &Path) -> Result<(), ReplayError> {
        let mut inner = self.lock();
        if matches!(inner.storage, Storage::Closed) { return Err(ReplayError::Closed); }
        if !inner.ended || inner.error.is_some() { return Err(ReplayError::Incomplete); }
        let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or_else(|| Path::new("."));
        let (mut output, temporary_path) = temporary(parent, ".tts-cache-")?;
        let result = (|| -> io::Result<()> {
            match &mut inner.storage {
                Storage::Memory(memory) => output.write_all(memory)?,
                Storage::Disk(spool) => {
                    spool.file.seek(SeekFrom::Start(0))?;
                    io::copy(&mut spool.file, &mut output)?;
                }
                Storage::Closed => unreachable!(),
            }
            // A partial failed append is not part of the committed stream.
            output.set_len(inner.size as u64)?;
            output.sync_all()?;
            drop(output);
            fs::rename(&temporary_path, path)?;
            Ok(())
        })();
        if result.is_err() { let _ = fs::remove_file(&temporary_path); }
        result.map_err(ReplayError::Io)
    }

    pub fn snapshot(&self) -> ReplaySnapshot {
        let inner = self.lock();
        ReplaySnapshot { size: inner.size, memory_bytes: match &inner.storage {
                Storage::Memory(memory) => memory.len(), _ => 0 },
            spilled: matches!(inner.storage, Storage::Disk(_)), ended: inner.ended,
            closed: matches!(inner.storage, Storage::Closed), error: inner.error.clone() }
    }

    pub fn close(&self) {
        let mut inner = self.lock();
        if let Storage::Memory(memory) = &inner.storage { self.budget.release(memory.len()); }
        inner.storage = Storage::Closed;
        drop(inner);
        self.changed.notify_all();
    }
}

impl Drop for ReplayBuffer {
    fn drop(&mut self) { self.close(); }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    fn directory() -> PathBuf {
        let path = std::env::temp_dir().join(format!("tts-replay-test-{}-{}", std::process::id(), NEXT_FILE.fetch_add(1, Ordering::Relaxed)));
        fs::create_dir(&path).unwrap(); path
    }

    #[test]
    fn shared_budget_spill_and_independent_readers() {
        let dir = directory(); let budget = Arc::new(MemoryBudget::new(8));
        let first = ReplayBuffer::new(&dir, budget.clone(), 8, 1024).unwrap();
        let second = ReplayBuffer::new(&dir, budget.clone(), 8, 1024).unwrap();
        first.append(b"abcdef").unwrap(); second.append(b"1234").unwrap();
        assert!(second.snapshot().spilled); assert_eq!(budget.used(), 6);
        assert_eq!(first.read_at(0, 2).unwrap(), b"ab");
        first.append(b"ghijk").unwrap(); assert_eq!(budget.used(), 0);
        assert_eq!(first.read_at(2, 100).unwrap(), b"cdefghijk");
        assert_eq!(first.read_at(0, 100).unwrap(), b"abcdefghijk");
        assert_eq!(second.read_at(0, 100).unwrap(), b"1234");
        first.close(); first.close(); second.close(); assert_eq!(budget.used(), 0);
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 0); fs::remove_dir(dir).unwrap();
    }

    #[test]
    fn failed_spill_keeps_committed_memory_and_budget() {
        let dir = directory(); let budget = Arc::new(MemoryBudget::new(4));
        let buffer = ReplayBuffer::new(dir.join("missing"), budget.clone(), 4, 16).unwrap();
        buffer.append(b"abcd").unwrap(); assert!(buffer.append(b"e").is_err());
        assert_eq!(buffer.read_at(0, 10).unwrap(), b"abcd"); assert_eq!(budget.used(), 4);
        assert_eq!(buffer.snapshot().size, 4); drop(buffer); assert_eq!(budget.used(), 0);
        fs::remove_dir(dir).unwrap();
    }

    #[test]
    fn cache_publication_is_complete_and_failure_keeps_previous_entry() {
        let dir = directory(); let path = dir.join("cached.mp3"); fs::write(&path, b"old").unwrap();
        let buffer = ReplayBuffer::new(&dir, Arc::new(MemoryBudget::new(0)), 0, 1024).unwrap();
        buffer.append(b"complete").unwrap(); assert!(matches!(buffer.copy_to(&path), Err(ReplayError::Incomplete)));
        assert_eq!(fs::read(&path).unwrap(), b"old"); buffer.finish(Some("failed".into()));
        assert!(buffer.copy_to(&path).is_err()); assert_eq!(fs::read(&path).unwrap(), b"old");
        buffer.finish(None); buffer.copy_to(&path).unwrap(); assert_eq!(fs::read(&path).unwrap(), b"complete");
        assert!(buffer.copy_to(&dir).is_err()); assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
        buffer.close(); fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn concurrent_readers_observe_same_stream_and_close_releases_budget() {
        let dir = directory(); let budget = Arc::new(MemoryBudget::new(1024));
        let buffer = Arc::new(ReplayBuffer::new(&dir, budget.clone(), 1024, 4096).unwrap());
        buffer.append(&vec![42; 300]).unwrap();
        let readers: Vec<_> = (0..8).map(|_| { let buffer = buffer.clone(); thread::spawn(move || {
            (0..300).step_by(13).flat_map(|offset| buffer.read_at(offset, 13).unwrap()).collect::<Vec<_>>()
        }) }).collect();
        for reader in readers { assert_eq!(reader.join().unwrap(), vec![42; 300]); }
        assert!(matches!(buffer.append(&vec![0; 4096]), Err(ReplayError::CapacityExceeded)));
        buffer.finish(None); buffer.append(b"ignored").unwrap(); assert_eq!(buffer.snapshot().size, 300);
        buffer.close(); assert_eq!(budget.used(), 0); assert!(matches!(buffer.read_at(0, 1), Err(ReplayError::Closed)));
        fs::remove_dir(dir).unwrap();
    }

    #[test]
    fn ram_fast_paths_never_wait_or_touch_the_filesystem() {
        let dir = directory(); let budget = Arc::new(MemoryBudget::new(8));
        let buffer = ReplayBuffer::new(dir.join("missing"), budget.clone(), 8, 16).unwrap();
        assert!(buffer.try_append_in_memory(b"abcd").unwrap());
        assert_eq!(buffer.try_read_in_memory(0, 4).unwrap(), Some(b"abcd".to_vec()));
        assert!(!buffer.try_append_in_memory(b"12345").unwrap());
        assert_eq!(buffer.snapshot().size, 4); assert_eq!(budget.used(), 4);
        let inner = buffer.lock();
        assert!(!buffer.try_append_in_memory(b"a").unwrap());
        assert_eq!(buffer.try_read_in_memory(0, 4).unwrap(), None);
        drop(inner);
        let reservation = budget.used.lock().unwrap();
        assert!(!buffer.try_append_in_memory(b"a").unwrap());
        drop(reservation);
        assert!(matches!(buffer.try_append_in_memory(&[0; 13]), Err(ReplayError::CapacityExceeded)));
        assert!(buffer.append(b"12345").is_err());
        assert_eq!(buffer.read_at(0, 100).unwrap(), b"abcd");
        buffer.close(); fs::remove_dir(dir).unwrap();
    }
}
