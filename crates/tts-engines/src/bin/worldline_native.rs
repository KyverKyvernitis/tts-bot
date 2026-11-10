use std::path::PathBuf;
use tts_engines::worldline;

fn run() -> tts_engines::Result<serde_json::Value> {
    let original: Vec<_> = std::env::args_os().skip(1).collect();
    let mut library = None;
    let mut job = None;
    let mut output = None;
    let mut probe = false;
    let mut child = false;
    let mut timeout = 20f64;
    let mut args = std::env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--library") => library=args.next().map(PathBuf::from),
            Some("--job") => job=args.next().map(PathBuf::from),
            Some("--output") => output=args.next().map(PathBuf::from),
            Some("--probe") => probe=true,
            Some("--_child") => child=true,
            Some("--timeout") => timeout=args.next().and_then(|s|s.to_str().and_then(|s|s.parse().ok())).ok_or_else(||tts_engines::EngineError::Invalid("invalid --timeout".into()))?,
            _ => return Err(tts_engines::EngineError::Invalid("usage: tts-worldline-native --library ABSOLUTE.so [--probe | --job ABSOLUTE.json --output ABSOLUTE.wav]".into())),
        }
    }
    if !timeout.is_finite() || !(0.001..=120.).contains(&timeout) {
        return Err(tts_engines::EngineError::Invalid(
            "timeout must be 0.001 to 120 seconds".into(),
        ));
    }
    let library =
        library.ok_or_else(|| tts_engines::EngineError::Invalid("--library is required".into()))?;
    #[cfg(target_os = "linux")]
    if child {
        // A supervising daemon can kill our CLI parent while native synthesis
        // runs in this isolated child. Never leave that render orphaned.
        let parent = unsafe { libc::getppid() };
        if parent == 1
            || unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) } < 0
            || unsafe { libc::getppid() } != parent
        {
            return Err(tts_engines::EngineError::Exited);
        }
    }
    if !child {
        let mut command = std::process::Command::new(std::env::current_exe()?);
        command.args(original).arg("--_child");
        let report = worldline::run_worldline_isolated(
            &mut command,
            std::time::Duration::from_secs_f64(timeout),
            &tts_engines::Cancellation::new(),
        )?;
        if !probe && report["phrase_render_verified"] != true {
            return Err(tts_engines::EngineError::Protocol(
                "native helper did not render the requested phrase".into(),
            ));
        }
        return Ok(report);
    }
    if probe {
        if job.is_some() || output.is_some() {
            return Err(tts_engines::EngineError::Invalid(
                "--probe cannot be combined with job/output".into(),
            ));
        }
        worldline::probe(&library)
    } else {
        let job =
            job.ok_or_else(|| tts_engines::EngineError::Invalid("--job is required".into()))?;
        let output = output
            .ok_or_else(|| tts_engines::EngineError::Invalid("--output is required".into()))?;
        let (job, directory) = worldline::load_job(&job, &output)?;
        let report = worldline::render_worldline(&library, &job, &directory, &output)?;
        serde_json::to_value(report).map_err(|e| tts_engines::EngineError::Protocol(e.to_string()))
    }
}
fn main() {
    match run() {
        Ok(report) => println!("{report}"),
        Err(error) => {
            println!(
                "{}",
                serde_json::json!({"ok":false,"backend":"worldline-r","error":error.to_string()})
            );
            std::process::exit(1);
        }
    }
}
