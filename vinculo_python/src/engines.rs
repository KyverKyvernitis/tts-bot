//! Python transport adapter only; native motor execution and ownership are Rust.
use pyo3::{
    exceptions::{PyOSError, PyRuntimeError, PyTimeoutError, PyValueError},
    prelude::*,
};
use std::{collections::HashMap, process::Command, sync::Mutex, time::Duration};
use tts_engines::{esper::EsperResident, Cancellation, EngineError};

fn error(e: EngineError) -> PyErr {
    match e {
        EngineError::Invalid(_) => PyValueError::new_err(e.to_string()),
        EngineError::Timeout => PyTimeoutError::new_err(e.to_string()),
        EngineError::Io(_) => PyOSError::new_err(e.to_string()),
        _ => PyRuntimeError::new_err(e.to_string()),
    }
}
fn timeout(ms: u64) -> PyResult<Duration> {
    if !(1..=120_000).contains(&ms) {
        return Err(PyValueError::new_err(
            "native engine timeout must be 1 to 120000 ms",
        ));
    }
    Ok(Duration::from_millis(ms))
}
fn command(values: Vec<String>, environment: Option<HashMap<String, String>>) -> PyResult<Command> {
    if values.is_empty()
        || values.len() > 128
        || values
            .iter()
            .any(|s| s.is_empty() || s.contains('\0') || s.len() > 16_384)
    {
        return Err(PyValueError::new_err("invalid native engine command"));
    }
    for value in &values {
        let name = std::path::Path::new(value)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if name.ends_with(".py")
            || name == "python"
            || name.starts_with("python3")
            || name.starts_with("pypy")
        {
            return Err(PyValueError::new_err(
                "native engines cannot execute a Python motor",
            ));
        }
    }
    let mut c = Command::new(&values[0]);
    c.args(&values[1..]);
    if let Some(environment) = environment {
        c.envs(environment);
    }
    Ok(c)
}
#[pyclass(
    name = "NativeEngineCancellation",
    module = "tts_fluxo_experimental._nucleo_fluxo_tts"
)]
pub struct PythonCancellation {
    inner: Cancellation,
}
#[pymethods]
impl PythonCancellation {
    #[new]
    fn new() -> Self {
        Self {
            inner: Cancellation::new(),
        }
    }
    fn cancel(&self) {
        self.inner.cancel();
    }
    #[getter]
    fn cancelled(&self) -> bool {
        self.inner.is_cancelled()
    }
}
#[pyclass(
    name = "NativeEsperResident",
    module = "tts_fluxo_experimental._nucleo_fluxo_tts"
)]
pub struct PythonEsperResident {
    inner: Mutex<Option<EsperResident>>,
    cancelled: Cancellation,
}
#[pymethods]
impl PythonEsperResident {
    #[new]
    #[pyo3(signature=(command,environment=None,timeout_ms=20_000))]
    fn new(
        py: Python<'_>,
        command: Vec<String>,
        environment: Option<HashMap<String, String>>,
        timeout_ms: u64,
    ) -> PyResult<Self> {
        let mut command = self::command(command, environment)?;
        let wait = timeout(timeout_ms)?;
        let cancelled = Cancellation::new();
        let token = cancelled.clone();
        let session = py
            .detach(move || EsperResident::spawn(&mut command, wait, &token))
            .map_err(error)?;
        Ok(Self {
            inner: Mutex::new(Some(session)),
            cancelled,
        })
    }
    /// Invoke in a bounded worker thread. GIL is released through physical I/O.
    #[pyo3(signature=(args,timeout_ms=20_000,headroom=true))]
    fn render(
        &self,
        py: Python<'_>,
        args: Vec<String>,
        timeout_ms: u64,
        headroom: bool,
    ) -> PyResult<String> {
        let wait = timeout(timeout_ms)?;
        let token = self.cancelled.clone();
        let report = py
            .detach(|| {
                let mut guard = self
                    .inner
                    .lock()
                    .map_err(|_| EngineError::Native("ESPER session lock poisoned".into()))?;
                let session = guard.as_mut().ok_or(EngineError::Exited)?;
                if headroom {
                    session.render_utau(&args, wait, &token)
                } else {
                    session.render(&args, wait, &token)
                }
            })
            .map_err(error)?;
        Ok(report.to_string())
    }
    fn cancel(&self) {
        self.cancelled.cancel();
    }
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.cancelled.cancel();
        py.detach(|| {
            let mut guard = self
                .inner
                .lock()
                .map_err(|_| EngineError::Native("ESPER session lock poisoned".into()))?;
            guard.take();
            Ok::<_, EngineError>(())
        })
        .map_err(error)
    }
    #[getter]
    fn process_id(&self, py: Python<'_>) -> PyResult<Option<u32>> {
        py.detach(|| {
            Ok(self
                .inner
                .lock()
                .map_err(|_| PyRuntimeError::new_err("ESPER session lock poisoned"))?
                .as_ref()
                .and_then(EsperResident::process_id))
        })
    }
}

#[pyfunction]
#[pyo3(signature=(command,timeout_ms=20_000,cancellation=None,environment=None))]
fn native_worldline(
    py: Python<'_>,
    command: Vec<String>,
    timeout_ms: u64,
    cancellation: Option<&PythonCancellation>,
    environment: Option<HashMap<String, String>>,
) -> PyResult<String> {
    let mut command = self::command(command, environment)?;
    let wait = timeout(timeout_ms)?;
    let cancelled = cancellation.map(|c| c.inner.clone()).unwrap_or_default();
    let report = py
        .detach(move || {
            tts_engines::worldline::run_worldline_isolated(&mut command, wait, &cancelled)
        })
        .map_err(error)?;
    Ok(report.to_string())
}
#[pyfunction]
#[pyo3(signature=(command,args,timeout_ms=20_000,cancellation=None,environment=None))]
fn native_resampler(
    py: Python<'_>,
    command: Vec<String>,
    args: Vec<String>,
    timeout_ms: u64,
    cancellation: Option<&PythonCancellation>,
    environment: Option<HashMap<String, String>>,
) -> PyResult<()> {
    let mut command = self::command(command, environment)?;
    let wait = timeout(timeout_ms)?;
    let cancelled = cancellation.map(|c| c.inner.clone()).unwrap_or_default();
    py.detach(move || tts_engines::process::run_resampler(&mut command, &args, wait, &cancelled))
        .map_err(error)
}
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("NATIVE_ENGINES_PROTOCOL_VERSION", 1)?;
    m.add_class::<PythonCancellation>()?;
    m.add_class::<PythonEsperResident>()?;
    m.add_function(wrap_pyfunction!(native_worldline, m)?)?;
    m.add_function(wrap_pyfunction!(native_resampler, m)?)?;
    Ok(())
}
