//! Python boundary for pure policy and the native queue coordinator.
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde_json::Value;
use std::sync::Mutex;
use tts_policy::pending::PendingQueue;
use tts_policy::QueueCoordinator;

#[pyclass(name = "NativePendingQueue")]
struct NativePendingQueue {
    inner: Mutex<PendingQueue>,
}

#[pymethods]
impl NativePendingQueue {
    #[new]
    fn new(maxsize: usize) -> PyResult<Self> {
        Ok(Self {
            inner: Mutex::new(PendingQueue::new(maxsize).map_err(PyValueError::new_err)?),
        })
    }
    fn admit(&self, entries_json: &str) -> PyResult<String> {
        let entries =
            serde_json::from_str(entries_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .admit(entries)
            .map(|v| v.to_string())
            .map_err(PyValueError::new_err)
    }
    fn put(&self, token: u64, group: String, signature: String) -> PyResult<()> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .put(tts_policy::pending::Entry {
                token,
                group,
                signature,
            })
            .map_err(PyValueError::new_err)
    }
    fn pop(&self) -> PyResult<Option<u64>> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .pop())
    }
    fn qsize(&self) -> PyResult<usize> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .len())
    }
    fn tokens(&self) -> PyResult<Vec<u64>> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .tokens())
    }
    #[getter]
    fn generation(&self) -> PyResult<u64> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .generation())
    }
    #[setter]
    fn set_generation(&self, value: u64) -> PyResult<()> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .set_generation(value)
            .map_err(PyValueError::new_err)
    }
    #[getter]
    fn accepting(&self) -> PyResult<bool> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .accepting())
    }
    #[setter]
    fn set_accepting(&self, value: bool) -> PyResult<()> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .set_accepting(value);
        Ok(())
    }
    fn signatures(&self) -> PyResult<String> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .signatures()
            .to_string())
    }
    fn clear_signatures(&self) -> PyResult<()> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("queue lock poisoned"))?
            .clear_signatures();
        Ok(())
    }
}

#[pyfunction]
fn policy_call(py: Python<'_>, operation: String, payload_json: String) -> PyResult<String> {
    let payload: Value =
        serde_json::from_str(&payload_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    py.detach(move || tts_policy::dispatch(&operation, &payload))
        .map(|v| v.to_string())
        .map_err(PyValueError::new_err)
}

#[pyclass(name = "NativeQueueCoordinator")]
struct NativeQueueCoordinator {
    inner: Mutex<QueueCoordinator>,
}

#[pymethods]
impl NativeQueueCoordinator {
    #[new]
    #[pyo3(signature = (max_items, max_bytes, dedup_limit=4096))]
    fn new(max_items: usize, max_bytes: usize, dedup_limit: usize) -> PyResult<Self> {
        Ok(Self {
            inner: Mutex::new(
                QueueCoordinator::new(max_items, max_bytes, dedup_limit)
                    .map_err(PyValueError::new_err)?,
            ),
        })
    }
    fn admit(&self, guild_id: u64, message_id: u64, items_json: &str) -> PyResult<Vec<String>> {
        let items =
            serde_json::from_str(items_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TTS queue lock poisoned"))?
            .admit(guild_id, message_id, items)
            .map_err(PyValueError::new_err)
    }
    fn dequeue(&self, guild_id: u64) -> PyResult<Option<String>> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TTS queue lock poisoned"))?
            .dequeue(guild_id)
            .map(|v| v.to_string()))
    }
    fn complete(&self, guild_id: u64, request_id: &str) -> PyResult<bool> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TTS queue lock poisoned"))?
            .complete(guild_id, request_id))
    }
    fn promote(&self, guild_id: u64, request_id: &str) -> PyResult<bool> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TTS queue lock poisoned"))?
            .promote(guild_id, request_id)
            .map_err(PyValueError::new_err)
    }
    fn cancel_request(&self, guild_id: u64, request_id: &str) -> PyResult<bool> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TTS queue lock poisoned"))?
            .cancel_request(guild_id, request_id))
    }
    fn cancel_guild(&self, guild_id: u64) -> PyResult<u64> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TTS queue lock poisoned"))?
            .cancel_guild(guild_id))
    }
    fn snapshot(&self, guild_id: u64) -> PyResult<String> {
        Ok(self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("TTS queue lock poisoned"))?
            .snapshot(guild_id)
            .to_string())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(policy_call, m)?)?;
    m.add_class::<NativeQueueCoordinator>()?;
    m.add_class::<NativePendingQueue>()?;
    Ok(())
}
