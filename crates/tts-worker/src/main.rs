use std::{io::Read,path::PathBuf};
use tokio_util::sync::CancellationToken;
use tts_worker::{Config,Service};

#[tokio::main(worker_threads = 2)]
async fn main()->Result<(),Box<dyn std::error::Error>>{
    let mut args=std::env::args().skip(1);let mut socket=None;let mut cache=None;
    let mut owner=false;let mut version=None;
    while let Some(arg)=args.next(){match arg.as_str(){
        "--check"|"--version-json"=>{println!("{}",serde_json::json!({"ok":true,"service":"tts-worker","version":env!("CARGO_PKG_VERSION"),"protocol":1,"target_arch":std::env::consts::ARCH,"target_os":std::env::consts::OS}));return Ok(());},
        "--socket"=>socket=args.next().map(PathBuf::from),
        "--cache-dir"=>cache=args.next().map(PathBuf::from),
        "--worker-version"=>version=args.next(),
        "--owner-stdin"=>owner=true,
        _=>return Err(format!("unknown argument: {arg}").into()),
    }}
    let socket=socket.ok_or("--socket required")?;let cache=cache.ok_or("--cache-dir required")?;
    let mut config=Config::new(socket,cache);if let Some(version)=version{config.worker_version=version;}
    if let Ok(value)=std::env::var("PHONE_WORKER_TTS_AGENT_MAX_TEXT_LENGTH"){config.max_text_length=value.parse()?;}
    if let Ok(value)=std::env::var("PHONE_WORKER_MAX_OUTPUT_MB"){config.max_audio_bytes=value.parse::<usize>()?.checked_mul(1024*1024).ok_or("invalid max output")?;}
    if let Ok(value)=std::env::var("PHONE_WORKER_TTS_CACHE_MAX_MB"){config.max_cache_bytes=value.parse::<u64>()?.checked_mul(1024*1024).ok_or("invalid cache size")?;}
    if let Ok(value)=std::env::var("PHONE_WORKER_TTS_CACHE_MAX_FILES"){config.max_cache_files=value.parse()?;}
    let stop=CancellationToken::new();
    if owner {let token=stop.clone();std::thread::spawn(move||{let mut byte=[0u8;1];loop{match std::io::stdin().read(&mut byte){Ok(0)|Err(_)=>{token.cancel();break;},_=>{}}}});}
    let signal=stop.clone();tokio::spawn(async move{
        let mut term=tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM");
        tokio::select!{_ = term.recv()=>{},_ = tokio::signal::ctrl_c()=>{}};signal.cancel();
    });
    Service::new(config)?.run(stop).await?;Ok(())
}
