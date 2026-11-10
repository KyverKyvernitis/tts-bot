//! Prepare the existing pinned ARM64 ESPER installation without executing its
//! Python installer/bridge. Only the private analysis cache is guest-mounted.
use crate::{Cancellation, EngineError, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use sha2::{Digest, Sha256};
#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    collections::HashMap,
    fs::{self, File, Metadata, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex, OnceLock,
    },
    time::{Duration, Instant},
};

pub const ENGINE_SHA256: &str = "e2cb6dc113593cb3b788debb51996f591a5f9d47bbd2a54df57bc1ecd843602f";
pub const CONFIG_SHA256: &str = "21951154b9bafdebde0b3a1d6533bb1fde549113b63b99a573b49ea68fadbe83";
pub const HOOK_SHA256: &str = "9a7a67253fafdd9e2a25aa265f40ab1a8789cfb7825bfa03b0abaa49ee3d9da4";
const HOOK_BASE64: &str = include_str!("../../data/esper-startup-hook.base64");
static NEXT_FILE: AtomicU64 = AtomicU64::new(0);
static VERIFIED: OnceLock<Mutex<HashMap<PathBuf, (String, Metadata)>>> = OnceLock::new();

fn bad<T>(message: &str) -> Result<T> {
    Err(EngineError::Invalid(message.into()))
}
fn check(deadline: Instant, cancel: &Cancellation) -> Result<()> {
    if cancel.is_cancelled() {
        return Err(EngineError::Cancelled);
    }
    if Instant::now() >= deadline {
        return Err(EngineError::Timeout);
    }
    Ok(())
}
fn same_file(a: &Metadata, b: &Metadata) -> bool {
    #[cfg(unix)]
    {
        a.dev() == b.dev()
            && a.ino() == b.ino()
            && a.len() == b.len()
            && a.mtime() == b.mtime()
            && a.mtime_nsec() == b.mtime_nsec()
            && a.ctime() == b.ctime()
            && a.ctime_nsec() == b.ctime_nsec()
    }
    #[cfg(not(unix))]
    {
        a.len() == b.len() && a.modified().ok() == b.modified().ok()
    }
}
fn real_directory(path: &Path, create: bool) -> Result<()> {
    if !path.is_absolute() || path.to_string_lossy().contains(':') {
        return bad("ESPER paths must be absolute and compatible with proot bind");
    }
    if create {
        fs::create_dir_all(path)?;
    }
    for directory in path.ancestors() {
        let m = fs::symlink_metadata(directory)?;
        if !m.is_dir() || m.file_type().is_symlink() {
            return bad("ESPER directories cannot contain symlinks");
        }
    }
    Ok(())
}
fn verify_file(
    path: &Path,
    bytes: u64,
    digest: &str,
    arm64_executable: bool,
    deadline: Instant,
    cancel: &Cancellation,
) -> Result<()> {
    check(deadline, cancel)?;
    let before = fs::symlink_metadata(path)?;
    if !before.is_file() || before.len() != bytes {
        return bad("pinned ESPER file is missing, symlinked or has a wrong size");
    }
    #[cfg(unix)]
    if arm64_executable && before.mode() & 0o100 == 0 {
        return bad("ESPER engine is not executable");
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    options.custom_flags(libc::O_NOFOLLOW);
    let mut file = options.open(path)?;
    if !same_file(&before, &file.metadata()?) {
        return bad("ESPER file changed before verification");
    }
    if VERIFIED
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| EngineError::Native("ESPER verification cache poisoned".into()))?
        .get(path)
        .is_some_and(|(saved, stamp)| saved == digest && same_file(stamp, &before))
    {
        return Ok(());
    }
    let mut sha = Sha256::new();
    let mut block = vec![0u8; 1024 * 1024];
    let mut first = true;
    let mut total = 0u64;
    loop {
        check(deadline, cancel)?;
        let n = file.read(&mut block)?;
        if n == 0 {
            break;
        }
        if first
            && arm64_executable
            && (n < 64
                || &block[..6] != b"\x7fELF\x02\x01"
                || u16::from_le_bytes([block[18], block[19]]) != 183)
        {
            return bad("ESPER engine must be the pinned Linux ARM64 ELF");
        }
        first = false;
        total += n as u64;
        if total > bytes {
            return bad("ESPER file grew during verification");
        }
        sha.update(&block[..n]);
    }
    if total != bytes
        || format!("{:x}", sha.finalize()) != digest
        || !same_file(&before, &file.metadata()?)
        || !same_file(&before, &fs::symlink_metadata(path)?)
    {
        return bad(
            "ESPER artifact differs from its pinned SHA-256 or changed during verification",
        );
    }
    VERIFIED
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| EngineError::Native("ESPER verification cache poisoned".into()))?
        .insert(path.to_owned(), (digest.to_owned(), before));
    Ok(())
}
fn install_hook(cache: &Path, deadline: Instant, cancel: &Cancellation) -> Result<PathBuf> {
    let data = STANDARD.decode(HOOK_BASE64.trim()).map_err(|_| {
        EngineError::Native("embedded ESPER startup hook has invalid base64".into())
    })?;
    if !(1024..=65536).contains(&data.len())
        || format!("{:x}", Sha256::digest(&data)) != HOOK_SHA256
    {
        return bad("embedded ESPER startup hook hash mismatch");
    }
    let directory = cache.join("resident-hook").join(HOOK_SHA256);
    real_directory(&directory, true)?;
    #[cfg(unix)]
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
    let target = directory.join("Esper.StartupHook.dll");
    if fs::symlink_metadata(&target).is_ok() {
        verify_file(
            &target,
            data.len() as u64,
            HOOK_SHA256,
            false,
            deadline,
            cancel,
        )?;
        return Ok(target);
    }
    let temporary = directory.join(format!(
        ".hook-{}-{}",
        std::process::id(),
        NEXT_FILE.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let mut file = options.open(&temporary)?;
        file.write_all(&data)?;
        file.sync_all()?;
        fs::rename(&temporary, &target)?;
        File::open(&directory)?.sync_all()?;
        Ok::<_, EngineError>(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result?;
    check(deadline, cancel)?;
    Ok(target)
}

/// Return a native resident command compatible with current ESPER installs.
/// All resampler sources and output jobs must be below `cache_root`; their
/// absolute names are preserved inside the guest, without exposing voicebanks.
/// Engine/config/hook hashes are verified before any guest process is spawned.
pub fn installed_resident_command(
    environment: &HashMap<String, String>,
    cache_root: &Path,
    timeout: Duration,
    cancel: &Cancellation,
) -> Result<Vec<String>> {
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| EngineError::Invalid("timeout overflow".into()))?;
    let root = if let Some(root) = environment
        .get("PHONE_WORKER_ESPER_ROOT")
        .filter(|s| !s.trim().is_empty())
    {
        PathBuf::from(root)
    } else {
        PathBuf::from(
            environment
                .get("HOME")
                .cloned()
                .or_else(|| std::env::var("HOME").ok())
                .ok_or_else(|| EngineError::Invalid("ESPER root or HOME is required".into()))?,
        )
        .join(".esper-utau")
    };
    let container = environment
        .get("PHONE_WORKER_ESPER_CONTAINER")
        .filter(|s| !s.is_empty())
        .map(String::as_str)
        .unwrap_or("voicepeak-arm64");
    if container.len() > 64
        || !container
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        || !container
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
    {
        return bad("invalid ESPER proot container");
    }
    let release = root.join("releases/v2.5.0");
    real_directory(&release, false)?;
    real_directory(cache_root, true)?;
    verify_file(
        &release.join("ESPER-Utau"),
        107_335_629,
        ENGINE_SHA256,
        true,
        deadline,
        cancel,
    )?;
    verify_file(
        &release.join("esper-config.ini"),
        762,
        CONFIG_SHA256,
        false,
        deadline,
        cancel,
    )?;
    let hook = install_hook(cache_root, deadline, cancel)?;
    real_directory(&cache_root.join("dotnet"), true)?;
    real_directory(&cache_root.join("jobs"), true)?;
    real_directory(&cache_root.join("sources"), true)?;
    let cache = cache_root.to_string_lossy();
    let extract = cache_root.join("dotnet");
    Ok(vec![
        "proot-distro".into(),
        "login".into(),
        container.into(),
        "--bind".into(),
        format!("{}:/opt/esper-engine", release.display()),
        "--bind".into(),
        format!("{cache}:{cache}"),
        "--".into(),
        "/usr/bin/env".into(),
        "LANG=C".into(),
        "LC_ALL=C".into(),
        "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1".into(),
        format!("DOTNET_BUNDLE_EXTRACT_BASE_DIR={}", extract.display()),
        "DOTNET_GCHeapHardLimit=40000000".into(),
        "DOTNET_gcServer=0".into(),
        "OMP_NUM_THREADS=2".into(),
        "OPENBLAS_NUM_THREADS=2".into(),
        format!("DOTNET_STARTUP_HOOKS={}", hook.display()),
        "PHONE_WORKER_ESPER_HOOK_PROTOCOL=1".into(),
        "/opt/esper-engine/ESPER-Utau".into(),
        "--_resident".into(),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn embedded_hook_is_exact_original_and_not_executable_python() {
        let data = STANDARD.decode(HOOK_BASE64.trim()).unwrap();
        assert_eq!(format!("{:x}", Sha256::digest(data)), HOOK_SHA256);
    }
    #[test]
    fn native_hook_installation_is_atomic_and_detects_tampering() {
        let directory = std::env::temp_dir().join(format!(
            "esper-hook-{}-{}",
            std::process::id(),
            NEXT_FILE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&directory).unwrap();
        let deadline = Instant::now() + Duration::from_secs(1);
        let cancel = Cancellation::new();
        let hook = install_hook(&directory, deadline, &cancel).unwrap();
        assert_eq!(install_hook(&directory, deadline, &cancel).unwrap(), hook);
        fs::write(&hook, b"modified").unwrap();
        assert!(install_hook(&directory, deadline, &cancel).is_err());
        let _ = fs::remove_dir_all(directory);
    }
    #[test]
    fn file_verification_rejects_change_and_cancellation() {
        let path = std::env::temp_dir().join(format!("esper-artifact-{}", std::process::id()));
        fs::write(&path, b"motor").unwrap();
        let digest = format!("{:x}", Sha256::digest(b"motor"));
        let cancel = Cancellation::new();
        let deadline = Instant::now() + Duration::from_secs(1);
        verify_file(&path, 5, &digest, false, deadline, &cancel).unwrap();
        assert!(verify_file(&path, 5, HOOK_SHA256, false, deadline, &cancel).is_err());
        cancel.cancel();
        assert!(matches!(
            verify_file(&path, 5, &digest, false, deadline, &cancel),
            Err(EngineError::Cancelled)
        ));
        let _ = fs::remove_file(path);
    }
}
