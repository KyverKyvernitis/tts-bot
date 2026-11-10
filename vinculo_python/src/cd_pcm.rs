//! Operation-level PCM composition binding; no per-sample Python callbacks.
use pyo3::prelude::*;
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::types::PyBytes;
use tts_render::{cd_pcm, dsp};

fn error(error: String) -> PyErr {
    if error == "pcm_int16_overflow" { PyOverflowError::new_err("signed short integer is greater than maximum") }
    else { PyValueError::new_err(error) }
}

#[pyfunction]
fn cd_scale_pcm16<'py>(py: Python<'py>, pcm: &Bound<'_, PyBytes>, gain: f64) -> PyResult<Bound<'py, PyBytes>> {
    let pcm = pcm.as_bytes().to_vec();
    let result = py.detach(move || {
        let samples = dsp::decode_pcm16(&pcm)?;
        dsp::scale_pcm16(&samples, gain).map(|samples| dsp::encode_pcm16(&samples))
    }).map_err(error)?;
    Ok(PyBytes::new(py, &result))
}

#[pyfunction]
fn cd_stereo_mix<'py>(py: Python<'py>, items: Vec<(Vec<u8>, f64, f64)>, frames: usize,
                     numpy_wrap: bool) -> PyResult<Bound<'py, PyBytes>> {
    let result = py.detach(move || {
        let decoded = items.into_iter().map(|(pcm, left, right)|
            Ok((dsp::decode_pcm16(&pcm)?, left, right))).collect::<Result<Vec<_>, String>>()?;
        let refs = decoded.iter().map(|(samples, left, right)| (samples.as_slice(), *left, *right)).collect::<Vec<_>>();
        cd_pcm::stereo_mix(&refs, frames, numpy_wrap).map(|samples| dsp::encode_pcm16(&samples))
    }).map_err(error)?;
    Ok(PyBytes::new(py, &result))
}

#[pyfunction]
fn cd_linear_overlap<'py>(py: Python<'py>, old: &Bound<'_, PyBytes>, incoming: &Bound<'_, PyBytes>,
                          relative: usize, total_overlap: usize) -> PyResult<Bound<'py, PyBytes>> {
    let old = old.as_bytes().to_vec(); let incoming = incoming.as_bytes().to_vec();
    let result = py.detach(move || {
        cd_pcm::linear_overlap(&dsp::decode_pcm16(&old)?, &dsp::decode_pcm16(&incoming)?, relative, total_overlap)
            .map(|samples| dsp::encode_pcm16(&samples))
    }).map_err(error)?;
    Ok(PyBytes::new(py, &result))
}

#[pyfunction]
fn cd_first_nonzero_frame(py: Python<'_>, pcm: &Bound<'_, PyBytes>) -> PyResult<Option<usize>> {
    let pcm = pcm.as_bytes().to_vec();
    py.detach(move || cd_pcm::first_nonzero_stereo_frame(&dsp::decode_pcm16(&pcm)?)).map_err(error)
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("CD_PCM_PROTOCOL_VERSION", 1)?;
    module.add_function(wrap_pyfunction!(cd_scale_pcm16, module)?)?;
    module.add_function(wrap_pyfunction!(cd_stereo_mix, module)?)?;
    module.add_function(wrap_pyfunction!(cd_linear_overlap, module)?)?;
    module.add_function(wrap_pyfunction!(cd_first_nonzero_frame, module)?)?;
    Ok(())
}
