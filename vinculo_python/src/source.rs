//! Discord reads one PCM frame; replay/FFmpeg supervision remain native.
use std::sync::Arc;
use nucleo_fluxo_tts::source::SourceHandle;
use pyo3::prelude::*;
use pyo3::exceptions::PyRuntimeError;
use pyo3::types::{PyBytes, PyDict};
use crate::replay::PythonReplayBuffer;

#[pyclass(name = "NativeSourceHandle", module = "tts_fluxo_experimental._nucleo_fluxo_tts")]
struct PythonSourceHandle { inner: Arc<SourceHandle> }

#[pymethods]
impl PythonSourceHandle {
    #[new]
    #[pyo3(signature = (replay, before_args, output_args, executable="ffmpeg"))]
    fn new(py: Python<'_>, replay: &PythonReplayBuffer, before_args: Vec<String>,
           output_args: Vec<String>, executable: &str) -> PyResult<Self> {
        let replay = replay.inner.clone(); let executable = executable.to_owned();
        let source = py.detach(move || SourceHandle::spawn(replay, &executable, &before_args, &output_args))
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        Ok(Self { inner: Arc::new(source) })
    }

    fn read_frame<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let inner = self.inner.clone();
        let data = py.detach(move || inner.read_frame())
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        Ok(PyBytes::new(py, &data))
    }

    fn cancel(&self, py: Python<'_>) {
        let inner = self.inner.clone(); py.detach(move || inner.cancel());
    }

    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let state = self.inner.snapshot(); let result = PyDict::new(py);
        result.set_item("cancelled", state.cancelled)?;
        result.set_item("reaped", state.reaped)?;
        result.set_item("input_bytes", state.input_bytes)?;
        result.set_item("frames", state.frames)?;
        result.set_item("error", state.error)?;
        Ok(result)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("SOURCE_PROTOCOL_VERSION", 1)?;
    m.add_class::<PythonSourceHandle>()?;
    Ok(())
}
