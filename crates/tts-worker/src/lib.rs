//! Private HTTP/1 transport. Only the platform bridge may reach this socket.
//! Responses send the final HTTP chunk exclusively after a provider confirms EOF.
use base64::Engine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::HashMap, io, path::{Path, PathBuf}, sync::Arc, time::Duration};
use tokio::{fs, io::{AsyncReadExt, AsyncWriteExt}, net::{UnixListener, UnixStream}, sync::Semaphore};
use tokio_util::sync::CancellationToken;
use tts_providers::{NativeStream, ProviderPool, ProviderRequest};

const MAX_HEADERS: usize = 16 * 1024;
const MAX_REQUEST_BYTES: usize = 32 * 1024 * 1024;
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone)]
pub struct Config {
    pub socket: PathBuf,
    pub cache: PathBuf,
    pub worker_id: String,
    pub worker_version: String,
    pub max_audio_bytes: usize,
    pub max_text_length: usize,
    pub max_connections: usize,
    pub max_cache_bytes: u64,
    pub max_cache_files: usize,
}
impl Config {
    pub fn new(socket: PathBuf, cache: PathBuf) -> Self {
        Self { socket, cache, worker_id: std::env::var("CORE_WORKER_ID").unwrap_or_default(),
            worker_version: env!("CARGO_PKG_VERSION").into(), max_audio_bytes: 8 * 1024 * 1024,
            max_text_length: 1200, max_connections: 8, max_cache_bytes: 4096 * 1024 * 1024,
            max_cache_files: 20_000 }
    }
}
#[derive(Clone)]
pub struct Service {
    config: Arc<Config>, pool: ProviderPool,
    environment: Arc<HashMap<String,String>>, ttv_status: Arc<Value>,
    render_slots: Arc<Semaphore>,
    render_background: Arc<std::sync::Mutex<Option<tts_render::renderer::Cancellation>>>,
}
impl Service {
    pub fn new(config: Config) -> io::Result<Self> {
        let environment=std::env::vars().collect::<HashMap<_,_>>();
        let ttv_status=voice_status(&environment);
        Ok(Self { config: Arc::new(config), pool: ProviderPool::new(4, 4).map_err(io::Error::other)?,
            environment: Arc::new(environment),ttv_status:Arc::new(ttv_status),render_slots:Arc::new(Semaphore::new(1)),
            render_background:Arc::new(std::sync::Mutex::new(None)) })
    }
    /// A pre-existing socket is never removed: a live worker retains ownership.
    pub async fn run(self, stop: CancellationToken) -> io::Result<()> {
        if let Some(parent) = self.config.socket.parent() { fs::create_dir_all(parent).await?; }
        fs::create_dir_all(&self.config.cache).await?;
        let listener = UnixListener::bind(&self.config.socket)?;
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&self.config.socket, std::fs::Permissions::from_mode(0o600)).await?;
        let slots = Arc::new(Semaphore::new(self.config.max_connections));
        let mut tasks = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                biased;
                _ = stop.cancelled() => break,
                Some(_) = tasks.join_next(), if !tasks.is_empty() => {},
                accepted = listener.accept() => {
                    let (mut stream, _) = accepted?;
                    let Ok(permit) = slots.clone().try_acquire_owned() else {
                        let _ = json_response(&mut stream, 503, &json!({"ok":false,"error":"busy"})).await;
                        continue;
                    };
                    let service = self.clone();
                    let closing = stop.clone();
                    tasks.spawn(async move {
                        let _permit = permit;
                        tokio::select! { biased; _ = closing.cancelled() => {}, _ = service.handle(&mut stream) => {} }
                    });
                }
            }
        }
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
        // Aborting connection futures drops their render cancellation guards.
        // Session closure waits for the physical native calls to release leases.
        let _=tokio::task::spawn_blocking(tts_render::renderer::close_residents).await;
        drop(listener);
        let _ = fs::remove_file(&self.config.socket).await;
        Ok(())
    }
    pub async fn handle(&self, stream: &mut UnixStream) -> io::Result<()> {
        let request = match tokio::time::timeout(Duration::from_secs(20), read_request(stream,self.config.max_audio_bytes.max(1024*1024))).await {
            Ok(Ok(request)) => request,
            Ok(Err(error)) => return json_response(stream, 400, &json!({"ok":false,"error":error.to_string()})).await,
            Err(_) => return json_response(stream, 408, &json!({"ok":false,"error":"request timeout"})).await,
        };
        match (request.method.as_str(), request.path.as_str()) {
            ("GET", "/health") => json_response(stream, 200, &self.status()).await,
            ("GET", "/tts-agent/health" | "/tts-agent/status") => {
                let mut status=self.status();status["tts_agent"]=self.status();
                json_response(stream, 200, &status).await
            },
            ("GET", "/tts-agent/configuration") => json_response(stream, 200, &json!({
                "version":self.config.worker_version, "native_version":env!("CARGO_PKG_VERSION"),
                "pid":std::process::id(), "native":true, "protocol":1,
                "renderer_configuration_sha256":self.configuration_hash(),
                "tts_agent":self.status() })).await,
            ("POST", "/tts-agent/cache.raw") => self.store_cache(stream, request).await,
            ("POST", "/tts-agent/synthesize.stream" | "/tts-agent/synthesize.raw" | "/tts-agent/singing.pcm" | "/task") => {
                let body: Value = match serde_json::from_slice(&request.body) {
                    Ok(value @ Value::Object(_)) => value,
                    _ => return json_response(stream, 400, &json!({"ok":false,"error":"JSON precisa ser objeto"})).await,
                };
                if request.path == "/task" {
                    match body["task"].as_str().unwrap_or("") {
                        "tts_agent_status" => return json_response(stream, 200, &self.status()).await,
                        "tts_cache_lookup" => return self.cache_lookup(stream, &body).await,
                        "tts_cache_store" => return self.store_json_cache(stream, &body).await,
                        "tts_android_voices" | "tts_atts_voices" | "android_tts_voices" => return self.android_voices(stream,&body).await,
                        "tts_agent_synthesize" | "tts_synthesize_piper" | "tts_synthesize_benchmark" => {},
                        _ => return json_response(stream, 400, &json!({"ok":false,"error":"tarefa TTS desconhecida"})).await,
                    }
                }
                self.synthesize(stream, &request.path, body).await
            }
            _ => json_response(stream, 404, &json!({"ok":false,"error":"rota não encontrada"})).await,
        }
    }
    pub fn status(&self) -> Value {
        let mut engines=vec!["edge","gtts","android_native"];
        if self.ttv_status["ready"]==true {engines.insert(0,"teto");}
        json!({"ok":true,"available":true,"synth_ready":true,"native":true,
            "state":"ready","runtime_kind":"rust","protocol":1,
            "version":self.config.worker_version,"worker_version":self.config.worker_version,
            "native_version":env!("CARGO_PKG_VERSION"),"pid":std::process::id(),
            "worker_id":self.config.worker_id,"available_engines":engines,
            "preferred_engine":"gtts","selected_engine":"gtts","stream_protocol":2,
            "teto":self.ttv_status.as_ref(),
            "ttv_performance_protocol":self.ttv_status["ttv_performance_protocol"],
            "ttv_pcm_stream_protocol":self.ttv_status["ttv_pcm_stream_protocol"],
            "ttv_background_protocol":self.ttv_status["ttv_background_protocol"],
            "deps":{"edge_tts":true,"gtts":true,"android_native_tts":true,"teto_tts":self.ttv_status["ready"]==true},
            "limits_enforced":false})
    }
    fn configuration_hash(&self)->String {
        environment_hash(&self.environment)
    }
    async fn synthesize(&self, socket: &mut UnixStream, path: &str, body: Value) -> io::Result<()> {
        let started = std::time::Instant::now();
        if std::env::var("PHONE_WORKER_TTS_AGENT_ENABLED").is_ok_and(|s|matches!(s.to_lowercase().as_str(),"0"|"false"|"no"|"off")) {
            return json_response(socket,503,&json!({"ok":false,"error":"PHONE_WORKER_TTS_AGENT_ENABLED=false"})).await;
        }
        if tts_policy::normalize_engine(&body["engine"],"gtts")=="teto" {
            return self.render_ttv(socket,path,&body).await;
        }
        if path=="/tts-agent/singing.pcm" {
            return json_response(socket,400,&json!({"ok":false,"error":"stream PCM exige uma partitura TTV"})).await;
        }
        if body["task"]=="tts_synthesize_piper" {
            return json_response(socket,422,&json!({"ok":false,"error":"Piper CLI não foi migrado para este serviço; nenhuma voz foi substituída"})).await;
        }
        let mut request: ProviderRequest = match serde_json::from_value(body.clone()) {
            Ok(value) => value,
            Err(error) => return json_response(socket, 400, &json!({"ok":false,"error":error.to_string()})).await,
        };
        request.engine = tts_policy::normalize_engine(&body["engine"], "gtts");
        request.text = request.text.trim().into();
        if request.text.is_empty() || request.text.chars().count() > self.config.max_text_length {
            return json_response(socket, 400, &json!({"ok":false,"error":"tamanho de texto inválido"})).await;
        }
        if !matches!(request.engine.as_str(), "edge" | "gtts" | "android_native") {
            return json_response(socket, 422, &json!({"ok":false,"error":"motor indisponível no serviço nativo","engine":request.engine})).await;
        }
        if let Some(voice) = body["voice"].as_str().or(body["fallback_voice"].as_str()) { request.voice = voice.into(); }
        let language = body.get("language").or(body.get("fallback_language")).unwrap_or(&Value::Null);
        if request.engine == "gtts" { request.language = tts_policy::normalize_gtts(language); }
        if request.engine == "edge" {
            request.rate = tts_policy::normalize_edge(&body["rate"], false);
            request.pitch = tts_policy::normalize_edge(&body["pitch"], true);
        }
        request.max_audio_bytes = request.max_audio_bytes.min(self.config.max_audio_bytes);
        if !request.timeout_seconds.is_finite() || !(0.001..=3600.0).contains(&request.timeout_seconds) || request.max_audio_bytes == 0 {
            return json_response(socket,400,&json!({"ok":false,"error":"prazo ou orçamento inválido"})).await;
        }
        if request.engine == "android_native" {
            request.android_base_url = self.environment.get("PHONE_WORKER_ANDROID_TTS_URL")
                .cloned().unwrap_or_else(|| "http://127.0.0.1:8877".into());
            request.voice=body["voice"].as_str().unwrap_or("").into();
            request.language=body["locale"].as_str().or(body["language"].as_str()).or(body["fallback_language"].as_str()).unwrap_or("pt-BR").into();
            request.rate=body["rate"].as_str().unwrap_or("1.0").into();
            request.pitch=body["pitch"].as_str().unwrap_or("1.0").into();
        }
        let mut format = if request.engine == "android_native" { "wav" } else { "mp3" }.to_string();
        let limit = request.max_audio_bytes;
        let mode = body["cache_mode"].as_str().unwrap_or("prefer").trim().to_lowercase();
        let cache_only = matches!(mode.as_str(), "only" | "cache_only") || body["cache_only"] == true
            || body["cache_only"].as_str().is_some_and(|v| matches!(v.to_lowercase().as_str(), "1"|"true"|"yes"|"on"));
        let cache_enabled = !matches!(mode.as_str(), "0"|"false"|"off"|"disabled"|"none"|"bypass");
        let key = tts_policy::standard_cache_key(&body, &request.engine, "unavailable", "C4", 1.0, None)
            .map_err(io::Error::other)?;
        let mut cache_path = self.config.cache.join(format!("{key}.{format}"));
        let mut cached = if cache_enabled && mode != "refresh" { fs::File::open(&cache_path).await.ok() } else { None };
        if cached.is_none()&&cache_enabled&&mode!="refresh"&&request.engine=="android_native" {
            for candidate in ["mp3","ogg"] {
                let path=self.config.cache.join(format!("{key}.{candidate}"));
                if let Ok(file)=fs::File::open(&path).await {cached=Some(file);format=candidate.into();cache_path=path;break;}
            }
        }
        let mut cached_size=None;
        if let Some(file) = &cached {
            let metadata = file.metadata().await?;
            if metadata.len() == 0 || metadata.len() > limit as u64 { cached = None; }
            else {cached_size=Some(metadata.len());}
        }
        if cache_only && cached.is_none() {
            return json_response(socket, 409, &json!({"ok":false,"error":"cache_miss","cache_only":true,
                "cache_hit":false,"engine":request.engine})).await;
        }
        let cache_hit = cached.is_some();
        let engine = request.engine.clone();
        let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(request.timeout_seconds.clamp(0.001, 3600.0));
        let mut source = if cache_hit { None } else {
            match self.pool.start(request).await {
                Ok(source) => Some(source),
                Err(error) => return json_response(socket, if error.kind == "busy" {503} else {400}, &json!({"ok":false,"error":error.to_string()})).await,
            }
        };
        let streaming = path.ends_with(".stream");
        let store = cache_enabled && !cache_hit && !matches!(mode.as_str(), "no_store"|"only"|"cache_only");
        let mut staging = if store { Some(Staging::new(&cache_path).await?) } else { None };
        let mut output = Vec::new();
        let mut size = 0usize;
        let mut digest = Sha256::new();
        let mut headers_sent = false;
        loop {
            let next = tokio::select! {
                biased;
                // Any new byte after the complete request is a peer close or an
                // unsupported pipelined request. Either cancels physical work.
                peer = socket.readable() => {
                    peer?;
                    let mut byte = [0u8; 1];
                    match socket.try_read(&mut byte) {
                        Ok(_) => return Err(io::Error::new(io::ErrorKind::ConnectionAborted, "consumidor encerrado")),
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => continue,
                        Err(e) => return Err(e),
                    }
                },
                value = tokio::time::timeout_at(deadline, next_audio(&mut cached, &mut source)) => value,
            };
            let chunk = match next {
                Ok(Ok(Some(data))) if !data.is_empty() => data,
                Ok(Ok(Some(_))) => continue,
                Ok(Ok(None)) => break,
                failure => {
                    if headers_sent { return Err(io::Error::other("stream nativo interrompido")); }
                    let message = match failure { Ok(Err(error)) => error.to_string(), _ => "prazo total do TTS esgotado".into() };
                    return json_response(socket, 502, &json!({"ok":false,"error":message})).await;
                }
            };
            size = size.checked_add(chunk.len()).filter(|n| *n <= limit)
                .ok_or_else(|| io::Error::other("áudio excedeu limite"))?;
            digest.update(&chunk);
            if streaming {
                if !headers_sent {
                    if let Some(source)=&source {
                        let metadata=source.metadata();
                        if !metadata.audio_format.is_empty(){format=normalize_format(&metadata.audio_format).into();}
                    }
                    write_headers(socket, 200, &self.audio_headers(&engine, &format, cache_hit, true,
                        started.elapsed().as_secs_f64() * 1000.0)).await?;
                    headers_sent = true;
                }
                let frame = format!("{:x}\r\n", chunk.len());
                write_bounded(socket, frame.as_bytes()).await?;
                write_bounded(socket, &chunk).await?;
                write_bounded(socket, b"\r\n").await?;
            } else { output.extend_from_slice(&chunk); }
            if let Some(part) = &mut staging { part.file.write_all(&chunk).await?; }
        }
        if size == 0 { return json_response(socket, 502, &json!({"ok":false,"error":"engine não enviou áudio"})).await; }
        if let Some(file)=&cached {
            if cached_size!=Some(size as u64)||file.metadata().await?.len()!=size as u64 {
                return Err(io::Error::other("cache incompleto durante o stream"));
            }
        }
        if let Some(source)=&source {
            let metadata=source.metadata();
            if !metadata.audio_format.is_empty(){format=normalize_format(&metadata.audio_format).into();}
        }
        cache_path.set_extension(&format);
        let sha256 = format!("{:x}", digest.finalize());
        if let Some(part) = staging.take() { part.commit(&cache_path).await?; self.prune(&cache_path).await; }
        if streaming { write_bounded(socket, b"0\r\n\r\n").await?; }
        else if path == "/task" {
            json_response(socket, 200, &json!({"ok":true,"engine":engine,"audio_format":format,
                "data_b64":base64::engine::general_purpose::STANDARD.encode(&output),
                "size":size,"sha256":sha256,"cache_hit":cache_hit,"cache_stored":store,
                "worker_id":self.config.worker_id,"worker_version":self.config.worker_version,
                "worker_total_ms":started.elapsed().as_secs_f64()*1000.0})).await?;
        } else {
            let mut headers = self.audio_headers(&engine, &format, cache_hit, false, started.elapsed().as_secs_f64()*1000.0);
            if let Some(source)=&source {
                let metadata=source.metadata();
                for (key,value) in [("X-Core-Worker-Voice",metadata.voice),("X-Core-Worker-Locale",metadata.locale),
                    ("X-Core-Worker-Android-Synth-Ms",metadata.android_synth_ms)] {
                    if !value.is_empty(){headers.push((key.into(),value));}
                }
                if !metadata.content_type.is_empty(){if let Some(header)=headers.iter_mut().find(|(key,_)|key=="Content-Type"){header.1=metadata.content_type;}}
            }
            headers.push(("Content-Length".into(), size.to_string()));
            headers.push(("X-Core-Worker-Sha256".into(), sha256));
            write_headers(socket, 200, &headers).await?;
            write_bounded(socket, &output).await?;
        }
        Ok(())
    }
    fn audio_headers(&self, engine: &str, format: &str, cached: bool, streaming: bool, first: f64) -> Vec<(String,String)> {
        let mut headers = vec![
            ("Content-Type".into(), if format == "wav" {"audio/wav"} else {"audio/mpeg"}.into()),
            ("Cache-Control".into(), "no-store".into()),
            ("X-Core-Worker-Audio-Format".into(),format.into()),
            ("X-Core-Worker-Engine".into(),engine.into()),
            ("X-Core-Worker-Cache-Hit".into(),cached.to_string()),
            ("X-Core-Worker-First-Audio-Ms".into(),format!("{first:.2}")),
            ("X-Core-Worker-Id".into(),self.config.worker_id.clone()),
            ("X-Core-Worker-Version".into(),self.config.worker_version.clone())];
        if streaming {
            headers.push(("Transfer-Encoding".into(),"chunked".into()));
            headers.push(("X-Core-Worker-Stream-Protocol".into(),"2".into()));
        }
        headers
    }
    async fn store_cache(&self, stream: &mut UnixStream, request: Request) -> io::Result<()> {
        let key = match tts_policy::sanitize_cache_key(&json!(request.headers.get("x-core-cache-key"))) {
            Ok(key) => key, Err(error) => return json_response(stream, 400, &json!({"ok":false,"error":error})).await,
        };
        let format = normalize_format(request.headers.get("x-core-audio-format").map(String::as_str).unwrap_or("mp3"));
        let expected = request.headers.get("x-core-sha256").map(String::as_str).unwrap_or("");
        if request.body.is_empty() || request.body.len() > self.config.max_audio_bytes
            || expected.len() != 64 || !expected.bytes().all(|b| b.is_ascii_hexdigit())
            || format!("{:x}",Sha256::digest(&request.body)) != expected.to_lowercase() {
            return json_response(stream, 400, &json!({"ok":false,"error":"tamanho ou SHA-256 inválido"})).await;
        }
        let path = self.config.cache.join(format!("{key}.{format}"));
        let mut staging = Staging::new(&path).await?;
        staging.file.write_all(&request.body).await?;
        staging.commit(&path).await?;
        self.prune(&path).await;
        json_response(stream, 200, &json!({"ok":true,"cache_stored":true,"size":request.body.len()})).await
    }
    async fn render_ttv(&self,socket:&mut UnixStream,path:&str,body:&Value)->io::Result<()> {
        use tts_render::renderer::{self,Cancellation};
        let started=std::time::Instant::now();
        let mut request=match renderer::request_from_worker(body,&self.environment) {
            Ok(request)=>request,
            Err(error)=>return json_response(socket,400,&json!({"ok":false,"engine":"teto","error":error})).await,
        };
        request.max_audio_bytes=request.max_audio_bytes.min(self.config.max_audio_bytes);
        if !request.timeout_seconds.is_finite()||!(0.001..=120.0).contains(&request.timeout_seconds) {
            return json_response(socket,400,&json!({"ok":false,"error":"prazo TTV inválido"})).await;
        }
        if path.ends_with(".stream") {
            return json_response(socket,400,&json!({"ok":false,"error":"streaming v2 aceita Edge ou gTTS; canto usa singing.pcm"})).await;
        }
        let singing=path=="/tts-agent/singing.pcm";
        let background=body["priority"]=="background";
        if (singing||background)&&request.score.is_none() {
            return json_response(socket,400,&json!({"ok":false,"error":"stream PCM ou aquecimento exige partitura TTV"})).await;
        }
        let performance=if request.score.is_some()||request.segments.is_some(){
            Some(tts_render::prosody::performance_hash(&request.text,&request.score,&request.segments).map_err(io::Error::other)?)
        }else{None};
        if singing&&body["ttv_performance_hash"].as_str()!=performance.as_deref() {
            return json_response(socket,400,&json!({"ok":false,"error":"stream PCM exige a identidade da performance"})).await;
        }
        let fingerprint=request_fingerprint(&request,&self.configuration_hash()).map_err(io::Error::other)?;
        let key=tts_policy::standard_cache_key(body,"teto",&fingerprint,&request.prosody.base_pitch,
            request.prosody.speech_rate,performance.as_deref()).map_err(io::Error::other)?;
        let cache_path=self.config.cache.join(format!("{key}.wav"));
        let mode=body["cache_mode"].as_str().unwrap_or("prefer").to_lowercase();
        let cache_only=body["cache_only"]==true||matches!(mode.as_str(),"only"|"cache_only");
        let cache_enabled=!singing&&!matches!(mode.as_str(),"0"|"false"|"off"|"disabled"|"none"|"bypass");
        if cache_enabled&&mode!="refresh" {
            if let Ok(metadata)=fs::metadata(&cache_path).await {
                if metadata.len()>0&&metadata.len()<=request.max_audio_bytes as u64 {
                    let data=fs::read(&cache_path).await?;
                    let mut metadata=json!({"voice_id":request.voice_id,"reading_language":request.reading_language,
                        "pitch_offset_semitones":request.prosody.pitch_offset_semitones,"speech_rate":request.prosody.speech_rate});
                    metadata["ttv_performance_hash"]=json!(performance);
                    return self.send_ttv(socket,path,&request,data,metadata,true,started).await;
                }
            }
        }
        if cache_only {return json_response(socket,409,&json!({"ok":false,"cache_hit":false,"cache_only":true,"error":"cache_miss","engine":"teto"})).await;}
        let cancellation=Cancellation::default();
        let permit=match self.render_slots.clone().try_acquire_owned() {
            Ok(permit)=>permit,
            Err(_)=>{
                let previous=self.render_background.lock().unwrap_or_else(|e|e.into_inner()).clone();
                if background||previous.is_none(){return json_response(socket,503,&json!({"ok":false,"error":"recurso TTV ocupado"})).await;}
                previous.unwrap().cancel();
                match tokio::time::timeout(Duration::from_secs(3),self.render_slots.clone().acquire_owned()).await {
                    Ok(Ok(permit))=>permit,
                    _=>return json_response(socket,503,&json!({"ok":false,"error":"cancelamento do aquecimento TTV pendente"})).await,
                }
            }
        };
        let platform_lease=match acquire_platform_lease(&self.environment).await {
            Ok(lease)=>lease,
            Err(error)=>return json_response(socket,503,&json!({"ok":false,"error":error.to_string()})).await,
        };
        if background{*self.render_background.lock().unwrap_or_else(|e|e.into_inner())=Some(cancellation.clone());}
        let _guard=RenderGuard(cancellation.clone());
        let (sender,mut receiver)=tokio::sync::mpsc::channel::<RenderRecord>(4);
        let render_request=request.clone();let physical_stop=cancellation.clone();let background_slot=self.render_background.clone();
        let job=spawn_physical_render(permit,platform_lease,move||{
            let result=if singing {
                renderer::render_command_stream(&render_request,&physical_stop,&mut |pcm,metadata|{
                    physical_stop.check()?;
                    sender.blocking_send(if pcm.is_empty(){RenderRecord::Metadata(metadata.clone())}else{RenderRecord::Pcm(bytes::Bytes::copy_from_slice(pcm))})
                        .map_err(|_|"consumidor PCM encerrado".to_string())
                })
            }else{renderer::render_command(&render_request,&physical_stop)};
            if background{*background_slot.lock().unwrap_or_else(|e|e.into_inner())=None;}
            let _=sender.blocking_send(match result {Ok(output)=>RenderRecord::Complete(output),Err(error)=>RenderRecord::Failed(error)});
        });
        let deadline=tokio::time::Instant::now()+Duration::from_secs_f64(request.timeout_seconds);
        let mut metadata:Option<Value>=None;let mut pcm_bytes=0usize;let mut digest=Sha256::new();let mut headers=false;
        loop {
            let record=tokio::select! {
                biased;
                peer=socket.readable()=>{
                    peer?;let mut byte=[0;1];match socket.try_read(&mut byte){
                        Err(error)if error.kind()==io::ErrorKind::WouldBlock=>continue,
                        _=>return Err(io::Error::new(io::ErrorKind::ConnectionAborted,"consumidor TTV encerrado")),
                    }
                },
                record=tokio::time::timeout_at(deadline,receiver.recv())=>record,
            };
            match record {
                Ok(Some(RenderRecord::Metadata(config)))=>{
                    if metadata.is_some()||config["sample_rate"]!=44100||config["channels"]!=1||config["sample_width_bytes"]!=2
                        ||config["normalization_gain"].as_f64()!=Some(1.0)||config["ttv_performance_hash"].as_str()!=performance.as_deref()
                        ||config["voice_id"]!=request.voice_id||config["reading_language"]!=request.reading_language {
                        return Err(io::Error::other("metadados PCM incompatíveis"));
                    }
                    let expected=config["expected_frames"].as_u64().ok_or_else(||io::Error::other("duração PCM ausente"))?;
                    if expected==0||expected.saturating_mul(2).saturating_add(44)>request.max_audio_bytes as u64{return Err(io::Error::other("PCM excedeu orçamento"));}
                    let mut projected=serde_json::Map::new();
                    for key in ["voice_id","reading_language","ttv_performance_hash","sample_rate","channels","sample_width_bytes","normalization_gain","midi_preroll_ms","expected_frames","duration_seconds"] {
                        if let Some(value)=config.get(key){projected.insert(key.into(),value.clone());}
                    }
                    let projected=Value::Object(projected);
                    write_headers(socket,200,&[("Content-Type".into(),"application/x-osaka-ttv-pcm".into()),
                        ("Transfer-Encoding".into(),"chunked".into()),("Cache-Control".into(),"no-store".into()),
                        ("X-Core-Worker-TTV-PCM-Protocol".into(),"1".into())]).await?;
                    headers=true;
                    write_record(socket,b'M',&serde_json::to_vec(&projected).map_err(io::Error::other)?).await?;
                    metadata=Some(projected);
                },
                Ok(Some(RenderRecord::Pcm(bytes)))=>{
                    let config=metadata.as_ref().ok_or_else(||io::Error::other("PCM antes de metadados"))?;
                    if bytes.len()%2!=0||pcm_bytes.saturating_add(bytes.len())>config["expected_frames"].as_u64().unwrap_or(0)as usize*2{
                        return Err(io::Error::other("bloco PCM inválido"));
                    }
                    for part in bytes.chunks(32768){write_record(socket,b'P',part).await?;digest.update(part);pcm_bytes+=part.len();}
                },
                Ok(Some(RenderRecord::Complete(output)))=>{
                    if singing {
                        let config=metadata.as_ref().ok_or_else(||io::Error::other("stream PCM sem metadados"))?;
                        if pcm_bytes!=config["expected_frames"].as_u64().unwrap_or(0)as usize*2{return Err(io::Error::other("stream PCM incompleto"));}
                        let mut receipt=output.metadata.as_object().cloned().unwrap_or_default();
                        receipt.retain(|_,value|value.is_null()||value.is_boolean()||value.is_number()||value.is_string());
                        receipt.insert("ok".into(),json!(true));receipt.insert("pcm_bytes".into(),json!(pcm_bytes));
                        receipt.insert("pcm_sha256".into(),json!(format!("{:x}",digest.finalize())));
                        let raw=serde_json::to_vec(&receipt).map_err(io::Error::other)?;
                        if raw.len()>65536{return Err(io::Error::other("recibo PCM grande demais"));}
                        write_record(socket,b'E',&raw).await?;write_bounded(socket,b"0\r\n\r\n").await?;
                    }else{
                        if cache_enabled&&!matches!(mode.as_str(),"no_store"|"only"|"cache_only"){
                            let mut temporary=Staging::new(&cache_path).await?;temporary.file.write_all(&output.audio).await?;
                            temporary.commit(&cache_path).await?;self.prune(&cache_path).await;
                        }
                        self.send_ttv(socket,path,&request,output.audio,output.metadata,false,started).await?;
                    }
                    let _=job.await;return Ok(());
                },
                failure=>{
                    cancellation.cancel();
                    if headers{return Err(io::Error::other("stream TTV interrompido"));}
                    let error=match failure{Ok(Some(RenderRecord::Failed(error)))=>error,_=>"renderização TTV interrompida ou timeout".into()};
                    return json_response(socket,502,&json!({"ok":false,"engine":"teto","error":error})).await;
                }
            }
        }
    }
    async fn send_ttv(&self,socket:&mut UnixStream,path:&str,request:&tts_render::renderer::RenderRequest,audio:Vec<u8>,mut metadata:Value,cached:bool,started:std::time::Instant)->io::Result<()> {
        let sha256=format!("{:x}",Sha256::digest(&audio));
        metadata["ok"]=json!(true);metadata["engine"]=json!("teto");metadata["audio_format"]=json!("wav");
        metadata["selected_engine"]=json!("teto");
        metadata["size"]=json!(audio.len());metadata["sha256"]=json!(sha256);metadata["cache_hit"]=json!(cached);
        metadata["worker_id"]=json!(self.config.worker_id);metadata["worker_version"]=json!(self.config.worker_version);
        metadata["ttv_voice_id"]=json!(request.voice_id);metadata["ttv_language"]=json!(request.reading_language);
        metadata["ttv_pitch_semitones"]=json!(request.prosody.pitch_offset_semitones);metadata["teto_pitch_offset_semitones"]=json!(request.prosody.pitch_offset_semitones);
        metadata["ttv_speech_rate"]=json!(request.prosody.speech_rate);metadata["teto_speech_rate"]=json!(request.prosody.speech_rate);
        metadata["worker_total_ms"]=json!(started.elapsed().as_secs_f64()*1000.0);
        for (source,target) in [("renderer_version","teto_renderer_version"),("phonemizer_version","teto_phonemizer_version"),
            ("voicebank_profile","teto_voicebank_profile"),("timeline_mode","teto_timeline_mode"),("backend","teto_backend")] {
            if let Some(value)=metadata.get(source).cloned(){metadata[target]=value;}
        }
        if path=="/task" {
            metadata["data_b64"]=json!(base64::engine::general_purpose::STANDARD.encode(audio));
            return json_response(socket,200,&metadata).await;
        }
        let mut headers=self.audio_headers("teto","wav",cached,false,started.elapsed().as_secs_f64()*1000.0);
        headers.extend([("Content-Length".into(),audio.len().to_string()),("X-Core-Worker-Sha256".into(),sha256),
            ("X-Core-Worker-Selected-Engine".into(),"teto".into()),
            ("X-Core-Worker-TTV-Voice-Id".into(),request.voice_id.clone()),("X-Core-Worker-TTV-Language".into(),request.reading_language.clone()),
            ("X-Core-Worker-TTV-Pitch-Semitones".into(),format!("{:.1}",request.prosody.pitch_offset_semitones)),
            ("X-Core-Worker-TTV-Speech-Rate".into(),request.prosody.speech_rate.to_string())]);
        if let Some(hash)=metadata["ttv_performance_hash"].as_str(){headers.push(("X-Core-Worker-TTV-Performance-Hash".into(),hash.into()));}
        for (key,header) in [("renderer_version","X-Core-Worker-Teto-Renderer"),("phonemizer_version","X-Core-Worker-Teto-Phonemizer"),
            ("backend","X-Core-Worker-Teto-Backend"),("voicebank_profile","X-Core-Worker-Teto-Voicebank-Profile"),
            ("timeline_mode","X-Core-Worker-Teto-Timeline"),("normalization_gain","X-Core-Worker-TTV-Normalization-Gain"),
            ("fragment_cache_hits","X-Core-Worker-TTV-Fragment-Cache-Hits"),("fragment_cache_misses","X-Core-Worker-TTV-Fragment-Cache-Misses"),
            ("resampler_calls","X-Core-Worker-TTV-Resampler-Calls")] {
            if let Some(value)=metadata.get(key).filter(|v|v.is_number()||v.is_string()){
                headers.push((header.into(),value.as_str().map(str::to_string).unwrap_or_else(||value.to_string())));
            }
        }
        write_headers(socket,200,&headers).await?;write_bounded(socket,&audio).await
    }
    async fn store_json_cache(&self, stream: &mut UnixStream, body: &Value) -> io::Result<()> {
        let data = match base64::engine::general_purpose::STANDARD.decode(body["data_b64"].as_str().unwrap_or("")) {
            Ok(value) => value, Err(_) => return json_response(stream,400,&json!({"ok":false,"error":"base64 inválida"})).await,
        };
        let mut headers = HashMap::new();
        headers.insert("x-core-cache-key".into(),body["cache_key"].as_str().unwrap_or("").into());
        headers.insert("x-core-audio-format".into(),body["audio_format"].as_str().unwrap_or("mp3").into());
        headers.insert("x-core-sha256".into(),format!("{:x}",Sha256::digest(&data)));
        self.store_cache(stream, Request{method:"POST".into(),path:"/tts-agent/cache.raw".into(),headers,body:data}).await
    }
    async fn android_voices(&self,stream:&mut UnixStream,body:&Value)->io::Result<()> {
        let base=self.environment.get("PHONE_WORKER_ANDROID_TTS_URL").cloned().unwrap_or_else(||"http://127.0.0.1:8877".into());
        let locale=body["locale"].as_str().or(body["language"].as_str()).unwrap_or("");
        let limit=body["limit"].as_u64().unwrap_or(500).clamp(1,1000).to_string();
        let result=async {
            let response=self.pool.client().get(format!("{}/native-tts/voices",base.trim_end_matches('/')))
                .query(&[("locale",locale),("limit",limit.as_str())]).timeout(Duration::from_secs(5)).send().await
                .map_err(io::Error::other)?.error_for_status().map_err(io::Error::other)?;
            let raw=tts_providers::limited_response(response,4*1024*1024).await.map_err(io::Error::other)?;
            serde_json::from_slice::<Value>(&raw).map_err(io::Error::other)
        }.await;
        match result {Ok(value)=>json_response(stream,200,&value).await,Err(error)=>json_response(stream,502,&json!({"ok":false,"error":error.to_string()})).await}
    }
    async fn cache_lookup(&self, stream: &mut UnixStream, body: &Value) -> io::Result<()> {
        let key = match tts_policy::sanitize_cache_key(&body["cache_key"]) {
            Ok(key) => key, Err(error) => return json_response(stream,400,&json!({"ok":false,"error":error})).await,
        };
        for format in ["mp3","wav","ogg"] {
            let path = self.config.cache.join(format!("{key}.{format}"));
            let Ok(metadata) = fs::metadata(&path).await else { continue };
            let limit = body["max_audio_bytes"].as_u64().unwrap_or(self.config.max_audio_bytes as u64).min(self.config.max_audio_bytes as u64);
            if metadata.len() == 0 || metadata.len() > limit { continue; }
            let data = fs::read(path).await?;
            return json_response(stream,200,&json!({"ok":true,"cache_hit":true,"audio_format":format,
                "size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data)),
                "data_b64":base64::engine::general_purpose::STANDARD.encode(data)})).await;
        }
        json_response(stream,200,&json!({"ok":false,"cache_hit":false,"error":"worker turbo cache miss"})).await
    }
    async fn prune(&self, protected: &Path) {
        let cache = self.config.cache.clone(); let keep = protected.to_owned();
        let max_bytes = self.config.max_cache_bytes; let max_files = self.config.max_cache_files;
        // Metadata scan and unlink never run on Tokio's I/O threads.
        let _ = tokio::task::spawn_blocking(move || {
            let Ok(entries) = std::fs::read_dir(cache) else { return };
            let mut files = entries.filter_map(Result::ok).filter_map(|e| {
                let p=e.path(); let m=e.metadata().ok()?;
                if !m.is_file() || !matches!(p.extension().and_then(|s|s.to_str()),Some("mp3"|"wav"|"ogg")) {return None;}
                Some((m.modified().ok()?,m.len(),p))
            }).collect::<Vec<_>>();
            let mut total:u64=files.iter().map(|(_,n,_)|n).sum(); let mut count=files.len();
            files.sort_by_key(|(modified,_,_)|*modified);
            for (_,size,path) in files {
                if total<=max_bytes && count<=max_files {break;}
                if path!=keep && std::fs::remove_file(path).is_ok() {total=total.saturating_sub(size);count=count.saturating_sub(1);}
            }
        }).await;
    }
}

enum RenderRecord {
    Metadata(Value),Pcm(bytes::Bytes),Complete(tts_render::renderer::RenderOutput),Failed(String),
}
struct RenderGuard(tts_render::renderer::Cancellation);
impl Drop for RenderGuard{fn drop(&mut self){self.0.cancel();}}

// A dropped HTTP future only requests cancellation. Keep both resource leases
// in the physical call so builds cannot overlap a native engine still stopping.
fn spawn_physical_render<F>(
    permit: tokio::sync::OwnedSemaphorePermit,
    platform_lease: Option<PlatformLease>,
    render: F,
) -> tokio::task::JoinHandle<()>
where
    F: FnOnce() + Send + 'static,
{
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let _platform_lease = platform_lease;
        render();
    })
}

struct PlatformLease(std::os::unix::net::UnixStream);
impl Drop for PlatformLease {
    fn drop(&mut self){
        use std::io::{Read,Write};
        if self.0.write_all(b"r").is_ok(){let mut response=[0;9];let _=self.0.read_exact(&mut response);}
    }
}
async fn acquire_platform_lease(environment:&HashMap<String,String>)->io::Result<Option<PlatformLease>>{
    let Some(path)=environment.get("TTS_NATIVE_HEAVY_LEASE_SOCKET")else{return Ok(None)};
    let result=tokio::time::timeout(Duration::from_secs(2),async {
        let mut socket=UnixStream::connect(path).await?;
        socket.write_all(b"acquire\n").await?;let mut response=[0;3];socket.read_exact(&mut response).await?;
        if &response!=b"ok\n"{return Err(io::Error::other("recurso pesado ocupado por build ou manutenção"));}
        let socket=socket.into_std()?;socket.set_nonblocking(false)?;
        socket.set_read_timeout(Some(Duration::from_secs(2)))?;socket.set_write_timeout(Some(Duration::from_secs(2)))?;
        Ok(Some(PlatformLease(socket)))
    }).await;
    result.map_err(|_|io::Error::new(io::ErrorKind::TimedOut,"lease da plataforma indisponível"))?
}
fn executable_available(command:&str,environment:&HashMap<String,String>)->bool{
    let path=Path::new(command);
    if path.components().count()>1{return path.is_file();}
    environment.get("PATH").is_some_and(|paths|std::env::split_paths(paths).any(|p|p.join(command).is_file()))
}
fn environment_hash(environment:&HashMap<String,String>)->String {
    let mut pairs=environment.iter().filter(|(key,_)|["PHONE_WORKER_TETO_","PHONE_WORKER_MIKU_","PHONE_WORKER_ANNA_","PHONE_WORKER_PURURU_","PHONE_WORKER_WORLDLINE_","PHONE_WORKER_ESPER_"].iter().any(|prefix|key.starts_with(prefix))).collect::<Vec<_>>();
    pairs.sort_by_key(|(key,_)|*key);
    format!("{:x}",Sha256::digest(serde_json::to_vec(&pairs).unwrap_or_default()))
}
fn request_fingerprint(request:&tts_render::renderer::RenderRequest,configuration:&str)->Result<String,String>{
    let bank=tts_render::voicebank::VoicebankIndex::load(&request.voicebank,&request.voicebank_options)?;
    Ok(format!("{:x}",Sha256::digest(format!("rust-ttv-v1:{configuration}:{}:{}:{}",bank.fingerprint,request.voice_id,request.reading_language).as_bytes())))
}
fn voice_status(environment:&HashMap<String,String>)->Value{
    let mut voices=Vec::new();let mut any_ready=false;let mut any_singing=false;
    let configuration=environment_hash(environment);
    let mut teto_fingerprint=String::new();let mut teto_languages=Vec::<String>::new();
    let mut teto_language_fingerprints=serde_json::Map::new();
    let mut teto_backend=String::new();
    for voice in ["kasane-teto","hatsune-miku","anna-nyui","pururu-purpura"]{
        let body=json!({"engine":"teto","text":"a","ttv_voice_id":voice,"ttv_language":"pt-BR"});
        let result=tts_render::renderer::request_from_worker(&body,environment).and_then(|request|{
            let available=if request.native_backend=="esper-installed" {
                let home=environment.get("HOME").map(PathBuf::from).unwrap_or_default();
                let root=environment.get("PHONE_WORKER_ESPER_ROOT").map(PathBuf::from).unwrap_or_else(||home.join(".esper-utau"));
                executable_available("proot-distro",environment)&&root.join("releases/v2.5.0/ESPER-Utau").is_file()
            }else if request.native_backend=="worldline-r" {
                let home=environment.get("HOME").map(PathBuf::from).unwrap_or_default();
                let library=environment.get("PHONE_WORKER_WORLDLINE_LIBRARY").map(PathBuf::from).unwrap_or_else(||home.join(".worldline-r/lib/libworldline.so"));
                library.is_file()&&executable_available("proot-distro",environment)
            }else{request.resampler.first().is_some_and(|command|executable_available(command,environment))};
            if !available{return Err("motor nativo configurado não está instalado".to_string());}
            Ok(request)
        });
        match result{
            Ok(request)=>{
                any_ready=true;
                let mut languages=vec!["pt-BR".to_string()];
                let fingerprint=request_fingerprint(&request,&configuration).unwrap_or_else(|_|"unavailable".into());
                let mut language_fingerprints=serde_json::Map::new();
                language_fingerprints.insert("pt-BR".into(),json!(fingerprint));
                let mut english_body=body.clone();english_body["ttv_language"]=json!("en-US");
                if let Ok(english)=tts_render::renderer::request_from_worker(&english_body,environment){
                    if let Ok(fingerprint)=request_fingerprint(&english,&configuration){
                        languages.push("en-US".into());language_fingerprints.insert("en-US".into(),json!(fingerprint));
                    }
                }
                let mut singing_fingerprints=serde_json::Map::new();
                if request.native_backend!="worldline-r" {
                    for language in &languages {
                        let mut singing_body=body.clone();singing_body["ttv_language"]=json!(language);
                        singing_body["ttv_score"]=json!({"version":1,"preset":"ttv-demo-v1"});
                        if let Ok(singing)=tts_render::renderer::request_from_worker(&singing_body,environment){
                            if let Ok(fingerprint)=request_fingerprint(&singing,&configuration){singing_fingerprints.insert(language.clone(),json!(fingerprint));}
                        }
                    }
                }
                let singing=!singing_fingerprints.is_empty();any_singing|=singing;
                if voice=="kasane-teto" {teto_fingerprint=fingerprint.clone();teto_languages=languages.clone();teto_language_fingerprints=language_fingerprints.clone();teto_backend=request.native_backend.clone();}
                voices.push(json!({"voice_id":voice,"id":voice,"ready":true,"available":true,"enabled":true,
                    "backend":request.native_backend,"languages":languages,"renderer_version":"rust-ttv-v1",
                    "supports_personal_rate":true,"fingerprint":fingerprint,"language_fingerprints":language_fingerprints,
                    "singing_available":singing,"singing_fingerprints":singing_fingerprints,
                    "ttv_personal_settings":true,"ttv_performance_protocol":if singing{2}else{0},
                    "modes":if singing{vec!["speech","speech-effects","singing"]}else{vec!["speech"]}}));
            },
            Err(error)=>voices.push(json!({"voice_id":voice,"id":voice,"ready":false,"available":false,"last_error":error})),
        }
    }
    json!({"ok":any_ready,"ready":any_ready,"enabled":any_ready,"available":any_ready,
        "native":true,"renderer_version":"rust-ttv-v1","phonemizer_version":"rust-phonetics-v1",
        "ttv_personal_settings":true,"ttv_voices":voices,"ttv_languages":teto_languages,
        "fingerprint":teto_fingerprint,"backend":teto_backend,"ttv_language_fingerprints":teto_language_fingerprints,
        "ttv_performance_protocol":if any_singing{2}else{0},"ttv_pcm_stream_protocol":if any_singing{1}else{0},
        "ttv_background_protocol":if any_singing{1}else{0},
        "last_error":if any_ready{""}else{"nenhuma voicebank e motor nativos configurados"}})
}
async fn write_record(socket:&mut UnixStream,kind:u8,payload:&[u8])->io::Result<()> {
    let length=u32::try_from(payload.len()).map_err(io::Error::other)?;
    let frame_length=payload.len()+5;
    write_bounded(socket,format!("{frame_length:x}\r\n").as_bytes()).await?;
    write_bounded(socket,&[kind]).await?;write_bounded(socket,&length.to_be_bytes()).await?;
    write_bounded(socket,payload).await?;write_bounded(socket,b"\r\n").await
}
async fn next_audio(cached: &mut Option<fs::File>, source: &mut Option<NativeStream>) -> io::Result<Option<bytes::Bytes>> {
    if let Some(file) = cached {
        let mut data=vec![0u8;16_384]; let n=file.read(&mut data).await?;
        data.truncate(n); return Ok(if n==0 {None} else {Some(data.into())});
    }
    source.as_mut().ok_or_else(||io::Error::other("source ausente"))?.next().await.map_err(io::Error::other)
}
fn normalize_format(value:&str)->&'static str {match value.trim().trim_start_matches('.').to_lowercase().as_str(){"wav"|"wave"=>"wav","ogg"|"opus"=>"ogg",_=>"mp3"}}
struct Staging { path: PathBuf, file: fs::File }
impl Staging {
    async fn new(target:&Path)->io::Result<Self>{
        let path=target.with_extension(format!("{}.part",uuid::Uuid::new_v4()));
        let file=fs::OpenOptions::new().write(true).create_new(true).open(&path).await?;
        Ok(Self{path,file})
    }
    async fn commit(mut self,target:&Path)->io::Result<()> {
        self.file.flush().await?; self.file.sync_all().await?;
        fs::rename(&self.path,target).await?; self.path=PathBuf::new(); Ok(())
    }
}
impl Drop for Staging {fn drop(&mut self){if !self.path.as_os_str().is_empty(){let _=std::fs::remove_file(&self.path);}}}
struct Request { method:String,path:String,headers:HashMap<String,String>,body:Vec<u8> }
async fn read_request(stream:&mut UnixStream,max_body:usize)->io::Result<Request>{
    let mut data=Vec::new(); let end;
    loop {
        if let Some(i)=data.windows(4).position(|w|w==b"\r\n\r\n"){end=i+4;break;}
        if data.len()>=MAX_HEADERS{return Err(io::Error::other("headers grandes demais"));}
        let mut block=[0u8;1024];let n=stream.read(&mut block).await?;
        if n==0{return Err(io::Error::new(io::ErrorKind::UnexpectedEof,"headers incompletos"));}
        data.extend_from_slice(&block[..n]);
    }
    if end>MAX_HEADERS{return Err(io::Error::other("headers grandes demais"));}
    let head=std::str::from_utf8(&data[..end]).map_err(io::Error::other)?;
    let mut lines=head.split("\r\n");let parts=lines.next().unwrap_or("").split_whitespace().collect::<Vec<_>>();
    if parts.len()!=3||!parts[2].starts_with("HTTP/1."){return Err(io::Error::other("requisição HTTP inválida"));}
    let method=parts[0].to_owned();let path=parts[1].split('?').next().unwrap_or("").to_owned();
    let mut headers=HashMap::new();
    for line in lines.filter(|s|!s.is_empty()){
        let (key,value)=line.split_once(':').ok_or_else(||io::Error::other("header inválido"))?;
        if headers.insert(key.trim().to_lowercase(),value.trim().to_string()).is_some(){return Err(io::Error::other("header duplicado"));}
    }
    if headers.contains_key("transfer-encoding"){return Err(io::Error::other("use Content-Length"));}
    let length=headers.get("content-length").map(|s|s.parse::<usize>()).transpose().map_err(io::Error::other)?.unwrap_or(0);
    if length>max_body.min(MAX_REQUEST_BYTES){return Err(io::Error::other("corpo grande demais"));}
    let mut body=data[end..].to_vec();
    if body.len()>length{return Err(io::Error::other("pipelining não suportado"));}
    let present=body.len();body.resize(length,0);stream.read_exact(&mut body[present..]).await?;
    Ok(Request{method,path,headers,body})
}
async fn write_bounded(stream:&mut UnixStream,data:&[u8])->io::Result<()>{
    tokio::time::timeout(WRITE_TIMEOUT,stream.write_all(data)).await.map_err(|_|io::Error::new(io::ErrorKind::TimedOut,"cliente lento"))?
}
async fn write_headers(stream:&mut UnixStream,status:u16,headers:&[(String,String)])->io::Result<()>{
    let reason=match status{200=>"OK",400=>"Bad Request",404=>"Not Found",408=>"Request Timeout",409=>"Conflict",422=>"Unprocessable Content",503=>"Service Unavailable",_=>"Bad Gateway"};
    let mut raw=format!("HTTP/1.1 {status} {reason}\r\nConnection: close\r\n");
    for (key,value) in headers {
        if key.bytes().any(|c|!c.is_ascii_alphanumeric()&&c!=b'-')||value.contains(['\r','\n']){return Err(io::Error::other("header de resposta inválido"));}
        raw.push_str(key);raw.push_str(": ");raw.push_str(value);raw.push_str("\r\n");
    }
    raw.push_str("\r\n");write_bounded(stream,raw.as_bytes()).await
}
async fn json_response(stream:&mut UnixStream,status:u16,value:&Value)->io::Result<()>{
    let data=serde_json::to_vec(value).map_err(io::Error::other)?;
    write_headers(stream,status,&[("Content-Type".into(),"application/json; charset=utf-8".into()),("Content-Length".into(),data.len().to_string())]).await?;
    write_bounded(stream,&data).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn cancelled_response_retains_leases_until_blocking_native_call_returns() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("lease.sock");
        let broker = UnixListener::bind(&path).unwrap();
        let (released_tx, mut released_rx) = tokio::sync::oneshot::channel();
        let broker = tokio::spawn(async move {
            let (mut connection, _) = broker.accept().await.unwrap();
            let mut request = [0; 8];
            connection.read_exact(&mut request).await.unwrap();
            assert_eq!(&request, b"acquire\n");
            connection.write_all(b"ok\n").await.unwrap();
            let mut release = [0; 1];
            connection.read_exact(&mut release).await.unwrap();
            assert_eq!(&release, b"r");
            released_tx.send(()).unwrap();
            connection.write_all(b"released\n").await.unwrap();
        });
        let environment = HashMap::from([(
            "TTS_NATIVE_HEAVY_LEASE_SOCKET".into(),
            path.to_string_lossy().into_owned(),
        )]);
        let slots = Arc::new(Semaphore::new(1));
        let permit = slots.clone().acquire_owned().await.unwrap();
        let cancellation = tts_render::renderer::Cancellation::default();
        let physical_stop = cancellation.clone();
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (cancelled_tx, cancelled_rx) = tokio::sync::oneshot::channel();
        let (finish_tx, finish_rx) = std::sync::mpsc::sync_channel(1);
        let response = tokio::spawn(async move {
            let _guard = RenderGuard(cancellation);
            let lease = acquire_platform_lease(&environment).await.unwrap();
            spawn_physical_render(permit, lease, move || {
                started_tx.send(()).unwrap();
                while !physical_stop.is_cancelled() {
                    std::thread::sleep(Duration::from_millis(1));
                }
                cancelled_tx.send(()).unwrap();
                // Model a native call that has noticed cancellation but still
                // needs to finish stopping/reaping its physical engine.
                let _ = finish_rx.recv_timeout(Duration::from_secs(3));
            }).await.unwrap();
        });
        tokio::time::timeout(Duration::from_secs(2), started_rx).await.unwrap().unwrap();
        response.abort();
        assert!(response.await.unwrap_err().is_cancelled());
        tokio::time::timeout(Duration::from_secs(2), cancelled_rx).await.unwrap().unwrap();
        assert_eq!(slots.available_permits(), 0);
        assert!(tokio::time::timeout(Duration::from_millis(100), &mut released_rx).await.is_err());
        finish_tx.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), released_rx).await.unwrap().unwrap();
        broker.await.unwrap();
        let _permit = tokio::time::timeout(Duration::from_secs(2), slots.acquire_owned()).await.unwrap().unwrap();
    }
    async fn request(service:Service,raw:Vec<u8>)->Vec<u8>{
        let (mut client,mut server)=UnixStream::pair().unwrap();
        let task=tokio::spawn(async move{service.handle(&mut server).await});
        client.write_all(&raw).await.unwrap();let mut response=Vec::new();client.read_to_end(&mut response).await.unwrap();
        task.await.unwrap().unwrap();response
    }
    fn post(path:&str,body:&[u8],extra:&str)->Vec<u8>{
        let mut raw=format!("POST {path} HTTP/1.1\r\nHost: native\r\nContent-Length: {}\r\n{extra}\r\n",body.len()).into_bytes();raw.extend_from_slice(body);raw
    }
    #[tokio::test]
    async fn cache_only_miss_never_contacts_provider(){
        let dir=tempfile::tempdir().unwrap();let service=Service::new(Config::new(dir.path().join("s"),dir.path().to_owned())).unwrap();
        let body=br#"{"engine":"gtts","text":"teste","cache_mode":"only","endpoint":"http://invalid"}"#;
        let response=request(service,post("/tts-agent/synthesize.stream",body,"")).await;
        assert!(response.starts_with(b"HTTP/1.1 409"));assert!(String::from_utf8_lossy(&response).contains("cache_miss"));
    }
    #[tokio::test]
    async fn binary_cache_atomic_hash_and_stream_contract(){
        let dir=tempfile::tempdir().unwrap();let service=Service::new(Config::new(dir.path().join("s"),dir.path().to_owned())).unwrap();
        let audio=b"first chunk real opaque bytes";let key="abcdef0123456789abcdef0123456789";
        let headers=format!("X-Core-Cache-Key: {key}\r\nX-Core-Audio-Format: mp3\r\nX-Core-Sha256: {:x}\r\n",Sha256::digest(audio));
        let response=request(service.clone(),post("/tts-agent/cache.raw",audio,&headers)).await;assert!(response.starts_with(b"HTTP/1.1 200"));
        let body=serde_json::to_vec(&json!({"engine":"gtts","text":"test","cache_only":true,"cache_key":key})).unwrap();
        let response=request(service.clone(),post("/tts-agent/synthesize.stream",&body,"")).await;
        assert!(response.starts_with(b"HTTP/1.1 200"));assert!(response.ends_with(b"0\r\n\r\n"));
        assert!(String::from_utf8_lossy(&response).contains("X-Core-Worker-Cache-Hit: true"));
        let response=request(service,post("/tts-agent/cache.raw",b"different bytes",&headers)).await;
        assert!(response.starts_with(b"HTTP/1.1 400"));assert_eq!(std::fs::read(dir.path().join(format!("{key}.mp3"))).unwrap(),audio);
    }
    #[tokio::test]
    async fn oversized_and_ambiguous_requests_are_rejected(){
        let dir=tempfile::tempdir().unwrap();let service=Service::new(Config::new(dir.path().join("s"),dir.path().to_owned())).unwrap();
        for raw in [b"POST /task HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n".to_vec(),
            b"POST /task HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n".to_vec(),
            b"POST /task HTTP/1.1\r\nContent-Length: 999999999\r\n\r\n".to_vec()] {
            assert!(request(service.clone(),raw).await.starts_with(b"HTTP/1.1 400"));
        }
    }
    #[tokio::test]
    async fn android_metadata_and_cache_format_come_from_actual_provider(){
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address=listener.local_addr().unwrap();
        let apk=tokio::spawn(async move{
            let(mut socket,_)=listener.accept().await.unwrap();let mut raw=Vec::new();
            let end=loop{
                let mut bytes=[0;2048];let n=socket.read(&mut bytes).await.unwrap();assert!(n>0);raw.extend_from_slice(&bytes[..n]);
                if let Some(index)=raw.windows(4).position(|w|w==b"\r\n\r\n"){break index+4;}
            };
            let headers=String::from_utf8_lossy(&raw[..end]).to_lowercase();
            let length=headers.lines().find_map(|line|line.strip_prefix("content-length: ")).unwrap().trim().parse::<usize>().unwrap();
            let mut body=raw[end..].to_vec();let present=body.len();body.resize(length,0);socket.read_exact(&mut body[present..]).await.unwrap();
            let body:Value=serde_json::from_slice(&body).unwrap();
            assert_eq!(body["voice"],"");assert_eq!(body["locale"],"pt-BR");assert_eq!(body["rate"],"1.0");
            socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nContent-Type: audio/mpeg\r\nX-Core-Worker-Audio-Format: mp3\r\nX-Core-Worker-Voice: android-local\r\nConnection: close\r\n\r\nnativeMP3").await.unwrap();
        });
        let directory=tempfile::tempdir().unwrap();let mut service=Service::new(Config::new(directory.path().join("s"),directory.path().to_owned())).unwrap();
        let mut environment=HashMap::new();environment.insert("PHONE_WORKER_ANDROID_TTS_URL".into(),format!("http://{address}"));service.environment=Arc::new(environment);
        let body=br#"{"engine":"android_native","text":"teste"}"#;
        let response=request(service.clone(),post("/tts-agent/synthesize.raw",body,"")).await;
        assert!(response.starts_with(b"HTTP/1.1 200"));assert!(response.ends_with(b"nativeMP3"));
        assert!(String::from_utf8_lossy(&response).contains("X-Core-Worker-Audio-Format: mp3"));
        assert!(String::from_utf8_lossy(&response).contains("X-Core-Worker-Voice: android-local"));
        apk.await.unwrap();
        let body=br#"{"engine":"android_native","text":"teste","cache_only":true}"#;
        let response=request(service,post("/tts-agent/synthesize.raw",body,"")).await;
        assert!(response.starts_with(b"HTTP/1.1 200"));assert!(response.ends_with(b"nativeMP3"));
        assert!(String::from_utf8_lossy(&response).contains("X-Core-Worker-Cache-Hit: true"));
    }
}
