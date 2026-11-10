//! Bounded, cancellable pipe I/O with physical process-group ownership.
use crate::{Cancellation, EngineError, Result};
#[cfg(unix)]
use std::os::{fd::AsRawFd, unix::process::CommandExt};
use std::{
    io::{Read, Write},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    time::{Duration, Instant},
};

pub const MAX_LINE_BYTES: usize = 65_536;

pub struct EngineProcess {
    child: Option<Child>,
    input: Option<ChildStdin>,
    output: Option<ChildStdout>,
    buffered: Vec<u8>,
}
impl EngineProcess {
    pub fn spawn(command: &mut Command) -> Result<Self> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(unix)]
        // SAFETY: setsid is async-signal-safe; no allocator or lock is used.
        // Resident hosts observe owner-pipe EOF. Do not install PDEATHSIG here:
        // Linux ties it to the spawning thread, which may be a retired pool
        // thread while this resident session is legitimately still alive.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
        let mut child = command.spawn()?;
        let input = child.stdin.take();
        let output = child.stdout.take();
        let mut process = Self {
            child: Some(child),
            input,
            output,
            buffered: Vec::new(),
        };
        if let Err(e) = process.nonblocking() {
            process.close();
            return Err(e);
        }
        Ok(process)
    }
    fn nonblocking(&self) -> Result<()> {
        #[cfg(unix)]
        for fd in [
            self.input.as_ref().unwrap().as_raw_fd(),
            self.output.as_ref().unwrap().as_raw_fd(),
        ] {
            // SAFETY: the descriptors remain owned by this object.
            unsafe {
                let flags = libc::fcntl(fd, libc::F_GETFL);
                if flags < 0 || libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) < 0 {
                    return Err(std::io::Error::last_os_error().into());
                }
            }
        }
        #[cfg(not(unix))]
        return Err(EngineError::Invalid(
            "native pipe engines require Unix".into(),
        ));
        #[allow(unreachable_code)]
        Ok(())
    }
    pub fn id(&self) -> Option<u32> {
        self.child.as_ref().map(Child::id)
    }
    fn check(&mut self, deadline: Instant, cancelled: &Cancellation) -> Result<()> {
        if cancelled.is_cancelled() {
            return Err(EngineError::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(EngineError::Timeout);
        }
        // Do not discard buffered final reports when the child has just exited.
        if self.child.is_none() {
            return Err(EngineError::Exited);
        }
        Ok(())
    }
    fn poll(&mut self, write: bool, deadline: Instant, cancelled: &Cancellation) -> Result<()> {
        loop {
            self.check(deadline, cancelled)?;
            #[cfg(unix)]
            {
                let fd = if write {
                    self.input.as_ref().ok_or(EngineError::Exited)?.as_raw_fd()
                } else {
                    self.output.as_ref().ok_or(EngineError::Exited)?.as_raw_fd()
                };
                let mut descriptor = libc::pollfd {
                    fd,
                    events: if write { libc::POLLOUT } else { libc::POLLIN },
                    revents: 0,
                };
                let wait = deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(25));
                // SAFETY: a valid single-element pollfd array is provided.
                let count =
                    unsafe { libc::poll(&mut descriptor, 1, wait.as_millis().max(1) as i32) };
                if count > 0 {
                    return Ok(());
                }
                if count < 0
                    && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
                {
                    return Err(std::io::Error::last_os_error().into());
                }
            }
        }
    }
    pub fn send_line(
        &mut self,
        data: &[u8],
        deadline: Instant,
        cancelled: &Cancellation,
    ) -> Result<()> {
        if data.len() > MAX_LINE_BYTES || !data.ends_with(b"\n") {
            return Err(EngineError::Invalid(
                "engine line exceeds 64 KiB or lacks newline".into(),
            ));
        }
        let result = (|| {
            let mut offset = 0;
            while offset < data.len() {
                self.poll(true, deadline, cancelled)?;
                match self
                    .input
                    .as_mut()
                    .ok_or(EngineError::Exited)?
                    .write(&data[offset..])
                {
                    Ok(0) => return Err(EngineError::Exited),
                    Ok(n) => offset += n,
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                        ) => {}
                    Err(e) => return Err(e.into()),
                }
            }
            Ok(())
        })();
        if result.is_err() {
            self.close();
        }
        result
    }
    pub fn receive_json(
        &mut self,
        deadline: Instant,
        cancelled: &Cancellation,
    ) -> Result<serde_json::Value> {
        let result = (|| loop {
            self.check(deadline, cancelled)?;
            if let Some(index) = self.buffered.iter().position(|&c| c == b'\n') {
                if index > MAX_LINE_BYTES {
                    return Err(EngineError::Protocol("response exceeds 64 KiB".into()));
                }
                let raw: Vec<u8> = self.buffered.drain(..=index).collect();
                let response: serde_json::Value = serde_json::from_slice(&raw)
                    .map_err(|_| EngineError::Protocol("response is not UTF-8 JSON".into()))?;
                if !response.is_object() {
                    return Err(EngineError::Protocol("response must be an object".into()));
                }
                return Ok(response);
            }
            if self.buffered.len() > MAX_LINE_BYTES {
                return Err(EngineError::Protocol("response exceeds 64 KiB".into()));
            }
            self.poll(false, deadline, cancelled)?;
            let mut block = [0u8; 4096];
            let remaining = (MAX_LINE_BYTES + 1 - self.buffered.len()).min(block.len());
            match self
                .output
                .as_mut()
                .ok_or(EngineError::Exited)?
                .read(&mut block[..remaining])
            {
                Ok(0) => return Err(EngineError::Exited),
                Ok(n) => self.buffered.extend_from_slice(&block[..n]),
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e.into()),
            }
        })();
        if result.is_err() {
            self.close();
        }
        result
    }
    /// Closes the owner pipe, stops all children, and reaps the direct child.
    /// The operation is idempotent; return never means just a cancelled future.
    pub fn close(&mut self) {
        self.input.take();
        if let Some(mut child) = self.child.take() {
            #[cfg(unix)]
            // SAFETY: setsid created a dedicated process group whose id is the child's pid.
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
        }
        self.output.take();
        self.buffered.clear();
    }
}
impl Drop for EngineProcess {
    fn drop(&mut self) {
        self.close();
    }
}

/// Run a one-shot native UTAU resampler with exactly 13 positional arguments.
/// Diagnostics cannot fill pipes; errors, cancellation and timeout reap the
/// entire owned process group before returning to the source-cache owner.
pub fn run_resampler(
    command: &mut Command,
    args: &[String],
    timeout: Duration,
    cancelled: &Cancellation,
) -> Result<()> {
    if args.len() != 13 || args.iter().any(|s| s.len() > 16_384 || s.contains('\0')) {
        return Err(EngineError::Invalid(
            "resampler requires 13 bounded UTAU arguments".into(),
        ));
    }
    command.args(args);
    run_command(command, timeout, cancelled)
}

/// Run an already-configured native command, retaining physical ownership of
/// its process group through timeout/cancellation and collecting the child.
pub fn run_command(
    command: &mut Command,
    timeout: Duration,
    cancelled: &Cancellation,
) -> Result<()> {
    if cancelled.is_cancelled() {
        return Err(EngineError::Cancelled);
    }
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| EngineError::Invalid("timeout overflow".into()))?;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    // SAFETY: setsid is async-signal-safe and touches no Rust-owned locks.
    unsafe {
        let owner_pid = std::process::id();
        command.pre_exec(move || {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            #[cfg(any(target_os = "linux", target_os = "android"))]
            {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getppid() as u32 != owner_pid {
                    return Err(std::io::Error::from_raw_os_error(libc::ECHILD));
                }
            }
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    let outcome = loop {
        if cancelled.is_cancelled() {
            break Err(EngineError::Cancelled);
        }
        if Instant::now() >= deadline {
            break Err(EngineError::Timeout);
        }
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break Ok(()),
            Ok(Some(status)) => {
                break Err(EngineError::Native(format!(
                    "resampler exited with {status}"
                )))
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(error) => break Err(error.into()),
        }
    };
    #[cfg(unix)]
    // Also collect descendants that survived a successful direct child.
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.kill();
    let _ = child.wait();
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn one_shot_enforces_status_and_physically_cancels() {
        let args = vec!["".to_owned(); 13];
        let mut command = Command::new("sh");
        command.args(["-c", "exit 9", "fixture"]);
        assert!(matches!(
            run_resampler(
                &mut command,
                &args,
                Duration::from_secs(1),
                &Cancellation::new()
            ),
            Err(EngineError::Native(_))
        ));
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 60", "fixture"]);
        let start = Instant::now();
        assert!(matches!(
            run_resampler(
                &mut command,
                &args,
                Duration::from_millis(30),
                &Cancellation::new()
            ),
            Err(EngineError::Timeout)
        ));
        assert!(start.elapsed() < Duration::from_secs(1));
    }
    #[test]
    fn oversized_response_is_rejected_and_reaped() {
        let mut command = Command::new("sh");
        command.args(["-c", "head -c 70000 /dev/zero | tr '\\0' x; sleep 60"]);
        let mut process = EngineProcess::spawn(&mut command).unwrap();
        assert!(matches!(
            process.receive_json(
                Instant::now() + Duration::from_secs(2),
                &Cancellation::new()
            ),
            Err(EngineError::Protocol(_))
        ));
        assert!(process.id().is_none());
    }
}
