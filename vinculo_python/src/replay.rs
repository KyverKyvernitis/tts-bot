//! Python ownership boundary for bounded native replay storage.
use std::sync::Arc;
use std::path::Path;
use nucleo_fluxo_tts::replay::{MemoryBudget, ReplayBuffer, ReplayError};
use pyo3::prelude::*;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::types::{PyBytes, PyDict};

fn python_error(error: ReplayError) -> PyErr {
    match error {
        ReplayError::InvalidLimit | ReplayError::CapacityExceeded => PyValueError::new_err(error.to_string()),
        ReplayError::Io(error) => PyOSError::new_err(error.to_string()),
        _ => PyRuntimeError::new_err(error.to_string()),
    }
}

#[pyclass(name = "ReplayMemoryBudget", module = "tts_fluxo_experimental._nucleo_fluxo_tts")]
pub struct PythonMemoryBudget { inner: Arc<MemoryBudget> }

#[pymethods]
impl PythonMemoryBudget {
    #[new]
    fn new(limit: usize) -> Self { Self { inner: Arc::new(MemoryBudget::new(limit)) } }
    #[getter]
    fn limit(&self) -> usize { self.inner.limit() }
    #[getter]
    fn used(&self) -> usize { self.inner.used() }
    fn reserve(&self, size: usize) -> bool { self.inner.reserve(size) }
    fn release(&self, size: usize) { self.inner.release(size) }
}

#[pyclass(name = "NativeReplayBuffer", module = "tts_fluxo_experimental._nucleo_fluxo_tts")]
pub struct PythonReplayBuffer { pub(crate) inner: Arc<ReplayBuffer> }

#[pymethods]
impl PythonReplayBuffer {
    #[new]
    fn new(directory: String, budget: &PythonMemoryBudget, memory_limit: usize, max_bytes: usize) -> PyResult<Self> {
        Ok(Self { inner: Arc::new(ReplayBuffer::new(directory, budget.inner.clone(), memory_limit, max_bytes).map_err(python_error)?) })
    }

    fn append(&self, py: Python<'_>, data: &Bound<'_, PyBytes>) -> PyResult<()> {
        let data = data.as_bytes().to_vec(); let inner = self.inner.clone();
        py.detach(move || inner.append(&data)).map_err(python_error)
    }

    fn try_append_in_memory(&self, data: &Bound<'_, PyBytes>) -> PyResult<bool> {
        self.inner.try_append_in_memory(data.as_bytes()).map_err(python_error)
    }

    fn try_read_in_memory<'py>(&self, py: Python<'py>, offset: usize, length: usize) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let data = self.inner.try_read_in_memory(offset, length).map_err(python_error)?;
        Ok(data.map(|data| PyBytes::new(py, &data)))
    }

    fn read_at<'py>(&self, py: Python<'py>, offset: usize, length: usize) -> PyResult<Bound<'py, PyBytes>> {
        let inner = self.inner.clone();
        let data = py.detach(move || inner.read_at(offset, length)).map_err(python_error)?;
        Ok(PyBytes::new(py, &data))
    }

    #[pyo3(signature = (error=None))]
    fn finish(&self, py: Python<'_>, error: Option<String>) {
        let inner = self.inner.clone(); py.detach(move || inner.finish(error));
    }

    fn copy_to(&self, py: Python<'_>, path: String) -> PyResult<()> {
        let inner = self.inner.clone();
        py.detach(move || inner.copy_to(Path::new(&path))).map_err(python_error)
    }

    fn close(&self, py: Python<'_>) {
        let inner = self.inner.clone(); py.detach(move || inner.close());
    }

    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let inner = self.inner.clone(); let state = py.detach(move || inner.snapshot());
        let result = PyDict::new(py);
        result.set_item("size", state.size)?;
        result.set_item("memory_bytes", state.memory_bytes)?;
        result.set_item("spilled", state.spilled)?;
        result.set_item("ended", state.ended)?;
        result.set_item("closed", state.closed)?;
        result.set_item("error", state.error)?;
        Ok(result)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("REPLAY_PROTOCOL_VERSION", 1)?;
    m.add_class::<PythonMemoryBudget>()?;
    m.add_class::<PythonReplayBuffer>()?;
    Ok(())
}
