//! Operation-level asyncio bridge. Network audio can be appended directly into
//! native replay, with only progress counters crossing back to Python.
use std::{sync::{Arc, OnceLock}, time::Instant};
use pyo3::{prelude::*, exceptions::{PyRuntimeError, PyTimeoutError, PyValueError}, types::{PyBytes, PyDict}};
use tokio::sync::Mutex;
use tts_providers::{NativeStream, ProviderError, ProviderPool, ProviderRequest};
use crate::replay::PythonReplayBuffer;

fn error(error: ProviderError) -> PyErr {
    let exception = match error.kind {
        "timeout" => PyTimeoutError::new_err(error.to_string()),
        "validation" | "limit" => PyValueError::new_err(error.to_string()),
        _ => PyRuntimeError::new_err(error.to_string()),
    };
    Python::attach(|py| {
        let value = exception.value(py);
        let _ = value.setattr("kind", error.kind);
        if let Some(status) = error.status { let _ = value.setattr("status", status); }
    });
    exception
}
fn pool() -> PyResult<ProviderPool> {
    static POOL: OnceLock<Result<ProviderPool, ProviderError>> = OnceLock::new();
    let slots = |key: &str| std::env::var(key).ok().and_then(|v| v.parse::<usize>().ok()).unwrap_or(4).clamp(1, 64);
    POOL.get_or_init(|| ProviderPool::new(slots("TTS_SYNTH_CONCURRENCY"), slots("TTS_GTTS_CONCURRENCY")))
        .clone().map_err(error)
}

#[pyclass(name="NativeProviderStream", module="tts_fluxo_experimental._nucleo_fluxo_tts")]
pub struct PythonProviderStream {
    inner: Arc<Mutex<NativeStream>>,
    stop: tokio_util::sync::CancellationToken,
    started: Instant,
}
#[pymethods]
impl PythonProviderStream {
    fn next_chunk<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let data = inner.lock().await.next().await.map_err(error)?;
            Python::attach(|py| Ok(data.map(|data| PyBytes::new(py, &data).unbind())))
        })
    }
    fn pump_chunk<'py>(&self, py: Python<'py>, replay: &PythonReplayBuffer) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let target = replay.inner.clone();
        let started = self.started;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let data = inner.lock().await.next().await.map_err(error)?;
            let Some(data) = data else { return Ok(None); };
            let received_ms = started.elapsed().as_secs_f64() * 1000.0;
            let count = data.len();
            // This operation owns target until physical disk I/O is complete.
            let total = if target.try_append_in_memory(&data).map_err(|e| PyRuntimeError::new_err(e.to_string()))? {
                target.snapshot().size
            } else {
                tokio::task::spawn_blocking(move || {
                    target.append(&data).map_err(|e| e.to_string())?;
                    Ok::<_, String>(target.snapshot().size)
                }).await.map_err(|e| PyRuntimeError::new_err(e.to_string()))?
                    .map_err(PyRuntimeError::new_err)?
            };
            Ok(Some((total, count, received_ms)))
        })
    }
    fn cancel(&self) { self.stop.cancel(); }
    fn metadata<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let data = serde_json::to_string(&inner.lock().await.metadata()).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
            Ok(data)
        })
    }
}
impl Drop for PythonProviderStream { fn drop(&mut self) { self.stop.cancel(); } }

#[pyfunction]
fn provider_start<'py>(py: Python<'py>, request_json: String) -> PyResult<Bound<'py, PyAny>> {
    let request: ProviderRequest = serde_json::from_str(&request_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let pool = pool()?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let started = Instant::now();
        let stream = pool.start(request).await.map_err(error)?;
        let stop = stream.cancellation();
        Python::attach(|py| Py::new(py, PythonProviderStream { inner: Arc::new(Mutex::new(stream)), stop, started }))
    })
}

#[pyfunction]
fn provider_save<'py>(py: Python<'py>, request_json: String, path: String) -> PyResult<Bound<'py, PyAny>> {
    let request: ProviderRequest = serde_json::from_str(&request_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let pool = pool()?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let metadata = pool.save(request, std::path::Path::new(&path)).await.map_err(error)?;
        serde_json::to_string(&metadata).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    })
}

#[pyfunction]
fn provider_languages_json() -> &'static str {
    include_str!("../../shared/gtts-languages.json")
}

#[pyfunction]
fn provider_voices<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    let pool = pool()?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        serde_json::to_string(&pool.voices().await.map_err(error)?).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    })
}
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("NATIVE_PROVIDERS_PROTOCOL_VERSION", 1)?;
    m.add_class::<PythonProviderStream>()?;
    m.add_function(wrap_pyfunction!(provider_start, m)?)?;
    m.add_function(wrap_pyfunction!(provider_save, m)?)?;
    m.add_function(wrap_pyfunction!(provider_voices, m)?)?;
    m.add_function(wrap_pyfunction!(provider_languages_json, m)?)?;
    // Synchronous diagnostics do not perform network I/O or start a provider.
    let info = PyDict::new(m.py());
    info.set_item("protocol", 1)?;
    info.set_item("engines", ["edge", "gtts", "android_native"])?;
    m.add("NATIVE_PROVIDERS", info)?;
    Ok(())
}
