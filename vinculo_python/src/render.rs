//! Transport-only Python bindings for native TTV domain and renderer.
use pyo3::{prelude::*,exceptions::{PyRuntimeError,PyValueError},types::PyBytes};
use std::collections::HashMap;
use tts_render::{phonetics,prosody,renderer::{self,RenderRequest,Cancellation}};
use serde_json::{json,Value};

fn invalid(e:impl ToString)->PyErr{PyValueError::new_err(e.to_string())}
fn failure(e:impl ToString)->PyErr{PyRuntimeError::new_err(e.to_string())}
#[pyclass(name="NativeRenderCancellation",module="tts_fluxo_experimental._nucleo_fluxo_tts")]
pub struct PythonCancellation{inner:Cancellation}
#[pymethods]impl PythonCancellation{
 #[new]fn new()->Self{Self{inner:Default::default()}}
 fn cancel(&self){self.inner.cancel();}
 #[getter]fn cancelled(&self)->bool{self.inner.is_cancelled()}
}
#[pyfunction]
fn ttv_plan_json(py:Python<'_>,request:&str)->PyResult<String>{let request:RenderRequest=serde_json::from_str(request).map_err(invalid)?;py.detach(move||renderer::plan(&request)).and_then(|p|serde_json::to_string(&p).map_err(|e|e.to_string())).map_err(failure)}
#[pyfunction]
fn ttv_worker_request_json(py:Python<'_>,body:&str,environment:&str)->PyResult<String>{let body:Value=serde_json::from_str(body).map_err(invalid)?;let environment:HashMap<String,String>=serde_json::from_str(environment).map_err(invalid)?;py.detach(move||renderer::request_from_worker(&body,&environment)).and_then(|p|serde_json::to_string(&p).map_err(|e|e.to_string())).map_err(failure)}
#[pyfunction]
#[pyo3(signature=(request,cancellation=None))]
fn ttv_render_json<'py>(py:Python<'py>,request:&str,cancellation:Option<&PythonCancellation>)->PyResult<(Bound<'py,PyBytes>,String)>{let request:RenderRequest=serde_json::from_str(request).map_err(invalid)?;let token=cancellation.map(|c|c.inner.clone()).unwrap_or_default();let output=py.detach(move||renderer::render_command(&request,&token)).map_err(failure)?;Ok((PyBytes::new(py,&output.audio),output.metadata.to_string()))}
#[pyfunction]
fn ttv_domain_json(py:Python<'_>,operation:&str,payload:&str)->PyResult<String>{
 let payload:Value=serde_json::from_str(payload).map_err(invalid)?;let operation=operation.to_owned();
 let output:Result<Value,String>=py.detach(move||{
  let text=payload.get("text").and_then(Value::as_str).unwrap_or("");let score=payload.get("score").filter(|v|!v.is_null()).cloned();let segments=payload.get("segments").filter(|v|!v.is_null()).cloned();
  match operation.as_str(){
   "g2p"=>{let deaccented=payload["deaccented"].as_bool().unwrap_or(false);let out=if payload["language"].as_str()==Some("en-US"){phonetics::g2p_english_word(text,deaccented)}else{phonetics::g2p_word(text,deaccented)};serde_json::to_value(out).map_err(|e|e.to_string())},
   "phonemize"=>{let options:phonetics::PhonemizeOptions=serde_json::from_value(payload.get("options").cloned().unwrap_or(json!({}))).map_err(|e|e.to_string())?;let aliases:HashMap<String,phonetics::AliasResolution>=serde_json::from_value(payload.get("aliases").cloned().unwrap_or(json!({}))).map_err(|e|e.to_string())?;let resolver=|c:&[String]|c.iter().find_map(|c|aliases.get(c).cloned());let out=phonetics::phonemize(text,&options,if payload.get("aliases").is_some(){Some(&resolver)}else{None})?;serde_json::to_value(out).map_err(|e|e.to_string())},
   "build_notes"=>{let moras:Vec<phonetics::Mora>=serde_json::from_value(payload["moras"].clone()).map_err(|e|e.to_string())?;let settings:prosody::ProsodySettings=serde_json::from_value(payload.get("settings").cloned().unwrap_or(json!({}))).map_err(|e|e.to_string())?;serde_json::to_value(prosody::build_notes(&moras,&settings)?).map_err(|e|e.to_string())},
   "validate_performance"=>{prosody::validate_performance(text,&score,&segments)?;Ok(json!(true))},
   "performance_hash"=>Ok(json!(prosody::performance_hash(text,&score,&segments)?)),
   "default_melody_score"=>{let count=payload["count"].as_u64().ok_or("count must be integer")?as usize;let boundaries:Vec<usize>=serde_json::from_value(payload.get("boundaries").cloned().unwrap_or(json!([]))).map_err(|e|e.to_string())?;prosody::default_melody_score(count,&boundaries)},
   _=>Err(format!("unsupported TTV operation {operation}"))
  }
 });output.map(|v|v.to_string()).map_err(failure)
}
#[pyfunction]
fn ttv_pcm_scale<'py>(py:Python<'py>,pcm:&[u8],gain:f64)->PyResult<Bound<'py,PyBytes>>{let samples=tts_render::dsp::decode_pcm16(pcm).map_err(invalid)?;let pcm=py.detach(move||tts_render::dsp::scale_pcm16(&samples,gain)).map_err(invalid)?;Ok(PyBytes::new(py,&tts_render::dsp::encode_pcm16(&pcm)))}
#[pyfunction]
fn ttv_close_residents(py:Python<'_>){py.detach(renderer::close_residents);}
#[pyfunction]
#[pyo3(signature=(data,lyrics="",track_index=None,inspect_only=false))]
fn ttv_midi_json(py:Python<'_>,data:&[u8],lyrics:&str,track_index:Option<usize>,inspect_only:bool)->String{
 let data=data.to_vec();let lyrics=lyrics.to_owned();
 py.detach(move||{
  let result=if inspect_only{tts_render::midi::inspect_midi_tracks(&data).map(|v|json!(v))}else{tts_render::midi::parse_midi(&data,&lyrics,track_index)};
  match result{Ok(value)=>json!({"ok":true,"value":value}),Err(error)=>json!({"ok":false,"error":error})}.to_string()
 })
}
pub fn register(m:&Bound<'_,PyModule>)->PyResult<()>{
 m.add("NATIVE_RENDER_PROTOCOL_VERSION",1)?;
 m.add_class::<PythonCancellation>()?;
 m.add_function(wrap_pyfunction!(ttv_plan_json,m)?)?;
 m.add_function(wrap_pyfunction!(ttv_worker_request_json,m)?)?;
 m.add_function(wrap_pyfunction!(ttv_render_json,m)?)?;
 m.add_function(wrap_pyfunction!(ttv_domain_json,m)?)?;
 m.add_function(wrap_pyfunction!(ttv_pcm_scale,m)?)?;
 m.add_function(wrap_pyfunction!(ttv_close_residents,m)?)?;
 m.add_function(wrap_pyfunction!(ttv_midi_json,m)?)?;
 Ok(())
}
