//! Client for the existing verified ESPER .NET resident engine.
//! Both upstream JSON lines and startup-hook base64/TSV are supported.
use crate::{process::EngineProcess, Cancellation, EngineError, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use std::{
    process::Command,
    time::{Duration, Instant},
};
mod installed;
pub use installed::installed_resident_command;

pub const SOURCE_COMMIT: &str = "2ff1a4692fa88fb1b60933408125632d0c5d8356";
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Transport {
    Json,
    Base64Tsv,
}

pub struct EsperResident {
    process: EngineProcess,
    transport: Transport,
    next_id: u64,
}
impl EsperResident {
    /// The command must launch the verified .NET host directly, or through
    /// proot. Installation and artifact hashes are owned by the release manager.
    pub fn spawn(
        command: &mut Command,
        timeout: Duration,
        cancelled: &Cancellation,
    ) -> Result<Self> {
        if cancelled.is_cancelled() {
            return Err(EngineError::Cancelled);
        }
        if timeout.is_zero() {
            return Err(EngineError::Timeout);
        }
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or_else(|| EngineError::Invalid("timeout overflow".into()))?;
        let mut process = EngineProcess::spawn(command)?;
        let ready = process.receive_json(deadline, cancelled)?;
        if ready["protocol"] != 1
            || ready["ready"] != true
            || ready["native_resident"] != true
            || ready["source_commit"] != SOURCE_COMMIT
            || ready["engine"] != "ESPER-Utau"
        {
            return Err(EngineError::Protocol(
                "unverified ESPER resident handshake".into(),
            ));
        }
        let transport = match ready
            .get("transport")
            .and_then(|v| v.as_str())
            .unwrap_or_else(|| {
                if ready["request_encoding"] == "tsv-base64-v1" {
                    "base64-tsv-v1"
                } else {
                    "json-v1"
                }
            }) {
            "json-v1" => Transport::Json,
            "base64-tsv-v1" => Transport::Base64Tsv,
            _ => return Err(EngineError::Protocol("unsupported ESPER transport".into())),
        };
        Ok(Self {
            process,
            transport,
            next_id: 0,
        })
    }
    pub fn transport(&self) -> Transport {
        self.transport
    }
    pub fn process_id(&self) -> Option<u32> {
        self.process.id()
    }
    pub fn close(&mut self) {
        self.process.close();
    }
    /// User-volume UTAU entrypoint preserving the existing 6 dB PCM headroom.
    /// Input WAV must live in the caller's leased private analysis cache: ESPER
    /// writes .esp and _wav.frq next to it. Never pass original voicebank files.
    pub fn render_utau(
        &mut self,
        args: &[String],
        timeout: Duration,
        cancelled: &Cancellation,
    ) -> Result<serde_json::Value> {
        let mut args = validate_arguments(args)?;
        let requested = args[9]
            .parse::<f64>()
            .map_err(|_| EngineError::Invalid("invalid volume".into()))?;
        let native = (requested * 0.5).min(50.);
        args[9] = native.to_string();
        let mut report = self.render(&args, timeout, cancelled)?;
        report["requested_volume_percent"] = serde_json::json!(requested);
        report["native_volume_percent"] = serde_json::json!(native);
        Ok(report)
    }
    pub fn render(
        &mut self,
        args: &[String],
        timeout: Duration,
        cancelled: &Cancellation,
    ) -> Result<serde_json::Value> {
        let args = validate_arguments(args)?;
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| EngineError::Protocol("request id exhausted".into()))?;
        let id = self.next_id.to_string();
        let mut data = match self.transport {
            Transport::Json => serde_json::to_vec(&serde_json::json!({ "id": id, "args": args }))
                .map_err(|e| EngineError::Protocol(e.to_string()))?,
            Transport::Base64Tsv => {
                let mut line = id.clone();
                for arg in &args {
                    line.push('\t');
                    line.push_str(&STANDARD.encode(arg.as_bytes()));
                }
                line.into_bytes()
            }
        };
        data.push(b'\n');
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or_else(|| EngineError::Invalid("timeout overflow".into()))?;
        self.process.send_line(&data, deadline, cancelled)?;
        let response = self.process.receive_json(deadline, cancelled)?;
        if response["protocol"] != 1
            || response["id"].as_str() != Some(&id)
            || response["ok"] != true
        {
            self.close();
            return Err(EngineError::Protocol(
                "ESPER did not confirm this request; session stopped".into(),
            ));
        }
        Ok(response)
    }
}

/// Preserve the Python bridge's 13-argument UTAU validation and integer LENGTH.
pub fn validate_arguments(args: &[String]) -> Result<Vec<String>> {
    fn bad(s: &str) -> EngineError {
        EngineError::Invalid(s.into())
    }
    if args.len() != 13 || args.iter().any(|s| s.len() > 16_384 || s.contains('\0')) {
        return Err(bad("ESPER requires 13 bounded, NUL-free UTAU arguments"));
    }
    let note = args[2].as_bytes();
    if note.len() < 2 || !matches!(note[0], b'A'..=b'G') {
        return Err(bad("invalid UTAU note"));
    }
    let mut n = 1;
    if note.get(n).is_some_and(|b| matches!(b, b'#' | b'b')) {
        n += 1;
    }
    if note.get(n) == Some(&b'-') {
        n += 1;
    }
    if !(1..=2).contains(&(note.len() - n)) || !note[n..].iter().all(u8::is_ascii_digit) {
        return Err(bad("invalid UTAU octave"));
    }
    // Existing Pururu profiles separate flags with spaces ("g5 B50"). The
    // native parser reads one argv string and skips separators between flags.
    if args[4].len() > 256
        || !args[4]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'.' | b'-' | b' '))
    {
        return Err(bad("invalid UTAU flags"));
    }
    let mut numbers = [0.; 13];
    for (index, lower, upper) in [
        (3, 1., 200.),
        (5, 0., 180_000.),
        (6, 1., 20_000.),
        (7, 0., 180_000.),
        (8, -180_000., 180_000.),
        (9, f64::MIN_POSITIVE, 200.),
        (10, 0., 100.),
    ] {
        let v = args[index]
            .parse::<f64>()
            .map_err(|_| bad("invalid numeric UTAU argument"))?;
        if !v.is_finite() || v < lower || v > upper {
            return Err(bad("UTAU argument outside engine bounds"));
        }
        numbers[index] = v;
    }
    let tempo = args[11]
        .strip_prefix('!')
        .ok_or_else(|| bad("UTAU tempo must use !60 to !240"))?;
    if tempo.is_empty()
        || !tempo.bytes().all(|b| b.is_ascii_digit() || b == b'.')
        || tempo.bytes().filter(|&b| b == b'.').count() > 1
        || tempo.starts_with('.')
        || tempo.ends_with('.')
    {
        return Err(bad("invalid UTAU tempo"));
    }
    let tempo = tempo
        .parse::<f64>()
        .map_err(|_| bad("invalid UTAU tempo"))?;
    if !(60. ..=240.).contains(&tempo) {
        return Err(bad("UTAU tempo outside !60 to !240"));
    }
    let pitch = &args[12];
    if pitch.is_empty() {
        return Err(bad("pitchbend must not be empty"));
    }
    let pieces: Vec<&str> = pitch.split('#').collect();
    let mut expanded = 0usize;
    for (i, piece) in pieces.iter().enumerate() {
        if i % 2 == 0 {
            if piece.is_empty() && i == pieces.len() - 1 {
                continue;
            }
            if piece.is_empty()
                || piece.len() % 2 != 0
                || !piece
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'/'))
            {
                return Err(bad("invalid UTAU pitchbend"));
            }
            expanded += piece.len() / 2;
        } else {
            if piece.is_empty() || piece.len() > 5 || !piece.bytes().all(|b| b.is_ascii_digit()) {
                return Err(bad("invalid UTAU pitchbend repetition"));
            }
            expanded += piece
                .parse::<usize>()
                .map_err(|_| bad("invalid UTAU pitchbend repetition"))?;
        }
    }
    if !(1..=10_000).contains(&expanded) {
        return Err(bad("UTAU pitchbend exceeds point budget"));
    }
    let mut normalized = args.to_vec();
    normalized[6] = format!("{}", numbers[6].ceil() as u64);
    Ok(normalized)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args() -> Vec<String> {
        [
            "/voice/á.wav",
            "/out/a.wav",
            "C4",
            "100",
            "B0",
            "0",
            "100.1",
            "20",
            "-100",
            "50",
            "0",
            "!120",
            "AA#2#",
        ]
        .map(String::from)
        .to_vec()
    }
    fn host(mode: &str) -> Command {
        let mut c = Command::new("sh");
        let ready = serde_json::json!({"protocol":1,"ready":true,"engine":"ESPER-Utau","source_commit":SOURCE_COMMIT,"native_resident":true,"transport":mode}).to_string();
        c.arg("-c").arg(format!("printf '%s\\n' '{ready}'; while IFS= read -r line; do printf '%s\\n' '{{\"protocol\":1,\"id\":\"1\",\"ok\":true}}'; done"));
        c
    }
    #[test]
    fn validates_unicode_and_numeric_bounds() {
        assert_eq!(validate_arguments(&args()).unwrap()[6], "101");
        for index in [3, 5, 6, 7, 8, 9, 10] {
            let mut values = args();
            values[index] = "NaN".into();
            assert!(validate_arguments(&values).is_err());
        }
        let mut values = args();
        values[12] = "AA#99999#".into();
        assert!(validate_arguments(&values).is_err());
        values = args();
        values[2] = "C".into();
        assert!(validate_arguments(&values).is_err());
    }
    #[test]
    fn native_json_and_tsv_handshakes_and_render() {
        for mode in ["json-v1", "base64-tsv-v1"] {
            let cancellation = Cancellation::new();
            let mut engine =
                EsperResident::spawn(&mut host(mode), Duration::from_secs(2), &cancellation)
                    .unwrap();
            assert_eq!(
                engine
                    .render(&args(), Duration::from_secs(2), &cancellation)
                    .unwrap()["ok"],
                true
            );
            let pid = engine.process_id().unwrap();
            engine.close();
            // SAFETY: signal zero only checks process existence; child has been reaped.
            assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
        }
    }
    #[test]
    fn encoded_requests_preserve_unicode_and_cross_request_identity() {
        for mode in ["json-v1", "base64-tsv-v1"] {
            let capture =
                std::env::temp_dir().join(format!("esper-wire-{}-{mode}", std::process::id()));
            let ready=serde_json::json!({"protocol":1,"ready":true,"engine":"ESPER-Utau","source_commit":SOURCE_COMMIT,"native_resident":true,"transport":mode}).to_string();
            let mut command = Command::new("sh");
            command.arg("-c").arg(format!("printf '%s\\n' '{ready}'; while IFS= read -r line; do printf '%s' \"$line\" > \"$1\"; printf '%s\\n' '{{\"protocol\":1,\"id\":\"1\",\"ok\":true}}'; done")).arg("fixture").arg(&capture);
            let cancellation = Cancellation::new();
            let mut engine =
                EsperResident::spawn(&mut command, Duration::from_secs(2), &cancellation).unwrap();
            engine
                .render_utau(&args(), Duration::from_secs(2), &cancellation)
                .unwrap();
            let wire = std::fs::read_to_string(&capture).unwrap();
            let received: Vec<String> = if mode == "json-v1" {
                let request: serde_json::Value = serde_json::from_str(&wire).unwrap();
                assert_eq!(request["id"], "1");
                request["args"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().to_owned())
                    .collect()
            } else {
                let fields: Vec<_> = wire.split('\t').collect();
                assert_eq!(fields.len(), 14);
                assert_eq!(fields[0], "1");
                fields[1..]
                    .iter()
                    .map(|value| String::from_utf8(STANDARD.decode(value).unwrap()).unwrap())
                    .collect()
            };
            assert_eq!(received[0], "/voice/á.wav");
            assert_eq!(received[6], "101");
            assert_eq!(received[9], "25");
            // A stale reply from a previous render must poison/close the session.
            assert!(matches!(
                engine.render(&args(), Duration::from_secs(2), &cancellation),
                Err(EngineError::Protocol(_))
            ));
            assert!(engine.process_id().is_none());
            let _ = std::fs::remove_file(capture);
        }
    }
    #[test]
    fn cancellation_physically_stops_busy_motor() {
        let mut c = Command::new("sh");
        let ready=serde_json::json!({"protocol":1,"ready":true,"engine":"ESPER-Utau","source_commit":SOURCE_COMMIT,"native_resident":true}).to_string();
        c.arg("-c")
            .arg(format!("printf '%s\\n' '{ready}'; read -r line; sleep 60"));
        let cancellation = Cancellation::new();
        let mut engine =
            EsperResident::spawn(&mut c, Duration::from_secs(2), &cancellation).unwrap();
        let cancel = cancellation.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(40));
            cancel.cancel();
        });
        let start = Instant::now();
        assert!(matches!(
            engine.render(&args(), Duration::from_secs(5), &cancellation),
            Err(EngineError::Cancelled)
        ));
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(engine.process_id().is_none());
    }
    #[test]
    fn rejects_wrong_handshake_and_timeout() {
        assert!(EsperResident::spawn(
            &mut host("unknown"),
            Duration::from_secs(2),
            &Cancellation::new()
        )
        .is_err());
        let mut c = Command::new("sleep");
        c.arg("60");
        assert!(matches!(
            EsperResident::spawn(&mut c, Duration::from_millis(30), &Cancellation::new()),
            Err(EngineError::Timeout)
        ));
    }
}
