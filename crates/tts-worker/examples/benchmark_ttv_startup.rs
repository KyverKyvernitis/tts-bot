//! Reproducible local service benchmark: generated PCM/OTO, real Unix HTTP,
//! request configuration, fingerprint, render plan and PCM composition. The
//! fixture resampler copies generated PCM; it is not a real voice engine or
//! a Discord/audible-latency benchmark. This example uses the baseline public
//! API so the identical file can be built against both Git revisions.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, fs, path::Path, time::{Duration, Instant}};
use tokio::{io::{AsyncReadExt, AsyncWriteExt}, net::UnixStream};
use tokio_util::sync::CancellationToken;
use tts_render::{phonetics::{self, PhonemizeOptions}, renderer::encode_wav};
use tts_worker::{Config, Service};

const TEXT: &str = "Olá mundo, este é um teste de fala em português.";

fn fixture(root: &Path, aliases: usize) {
    fs::create_dir_all(root.join("samples")).unwrap();
    let pcm: Vec<i16> = (0..22050).map(|n| ((n * 97) % 20001) as i16 - 10000).collect();
    fs::write(root.join("source.wav"), encode_wav(&pcm).unwrap()).unwrap();
    let options = PhonemizeOptions { max_moras: 1025, voicebank_profile: "standard".into(),
        reading_language: "pt-BR".into(), inventory_profile: String::new() };
    let mut used = BTreeSet::new();
    for mora in phonetics::phonemize(TEXT, &options, None).unwrap() {
        used.extend(mora.candidates);
    }
    let mut oto = used.iter().map(|a|format!("source.wav={a},0,45,-400,30,10\n")).collect::<String>();
    // Distinct files exercise canonicalization/stat work, as in installed banks.
    let tiny_wav = encode_wav(&[123]).unwrap();
    for n in 0..aliases {
        let path = format!("samples/filler-{n:05}.wav");
        fs::write(root.join(&path), &tiny_wav).unwrap();
        oto.push_str(&format!("{path}=fixture_unused_{n:05},0,45,-400,30,10\n"));
    }
    fs::write(root.join("oto.ini"), oto).unwrap();
    fs::write(root.join("character.txt"), "name=Generated benchmark fixture\n").unwrap();
}

async fn sample(socket: &Path, mode: &str) -> Value {
    let body = serde_json::to_vec(&json!({"engine":"teto","text":TEXT,"ttv_voice_id":"kasane-teto",
        "cache_mode":mode,"timeout_seconds":30})).unwrap();
    let request = format!("POST /tts-agent/synthesize.raw HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",body.len());
    let started = Instant::now();
    let mut stream = UnixStream::connect(socket).await.unwrap();
    stream.write_all(request.as_bytes()).await.unwrap();
    stream.write_all(&body).await.unwrap();
    let mut response = Vec::new();
    let mut headers_at = None;
    let mut first_audio_at = None;
    let mut header_end = None;
    loop {
        let mut chunk = [0u8; 32768];
        let count = tokio::time::timeout(Duration::from_secs(35), stream.read(&mut chunk)).await.unwrap().unwrap();
        if count == 0 { break; }
        response.extend_from_slice(&chunk[..count]);
        if header_end.is_none() {
            if let Some(index) = response.windows(4).position(|s| s == b"\r\n\r\n") {
                header_end = Some(index + 4);
                headers_at = Some(started.elapsed().as_secs_f64() * 1000.0);
            }
        }
        if header_end.is_some_and(|end| response.len() > end) && first_audio_at.is_none() {
            first_audio_at = Some(started.elapsed().as_secs_f64() * 1000.0);
        }
        assert!(response.len() <= 8 * 1024 * 1024 + 32768);
    }
    let total_ms = started.elapsed().as_secs_f64() * 1000.0;
    let end = header_end.expect("HTTP headers");
    let headers = std::str::from_utf8(&response[..end]).unwrap();
    assert!(headers.starts_with("HTTP/1.1 200 "), "{headers} {}",String::from_utf8_lossy(&response[end..]));
    let audio = &response[end..];
    assert_eq!(&audio[..4], b"RIFF");
    let hash = format!("{:x}",Sha256::digest(audio));
    assert!(headers.to_lowercase().contains(&format!("x-core-worker-sha256: {hash}")));
    json!({"headers_ms":headers_at.unwrap(),"first_wav_byte_ms":first_audio_at.unwrap(),
        "total_ms":total_ms,"audio_bytes":audio.len(),"audio_sha256":hash,
        "cache_hit":headers.to_lowercase().contains("x-core-worker-cache-hit: true")})
}

fn distribution(samples: &[Value], key: &str) -> Value {
    let mut sorted = samples.iter().map(|s|s[key].as_f64().unwrap()).collect::<Vec<_>>();
    sorted.sort_by(f64::total_cmp);
    let percentile = |p: f64| {
        let index = p * (sorted.len() - 1) as f64;
        let low = index.floor() as usize;
        let high = index.ceil() as usize;
        sorted[low] + (sorted[high]-sorted[low]) * (index-low as f64)
    };
    json!({"p50":percentile(0.5),"p95":percentile(0.95),"min":sorted[0],"max":sorted[sorted.len()-1]})
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let args = std::env::args().collect::<Vec<_>>();
    if args.get(1).map(String::as_str) == Some("--fixture-resampler") {
        fs::copy(&args[2],&args[3]).unwrap();
        return;
    }
    let runs = args.get(1).map(|s|s.parse::<usize>().unwrap()).unwrap_or(30).clamp(1,1000);
    let aliases = args.get(2).map(|s|s.parse::<usize>().unwrap()).unwrap_or(5000).clamp(1,20000);
    let root = tempfile::tempdir().unwrap();
    let bank = root.path().join("bank");
    fixture(&bank, aliases);
    // Isolate only the benchmark process's voice configuration. Keep HOME,
    // PATH and other system settings intact; all writable paths are explicit.
    for (key, _) in std::env::vars().filter(|(key,_)| {
        ["PHONE_WORKER_TETO_","PHONE_WORKER_MIKU_","PHONE_WORKER_ANNA_",
         "PHONE_WORKER_PURURU_","PHONE_WORKER_ESPER_","PHONE_WORKER_WORLDLINE_"]
            .iter().any(|prefix|key.starts_with(prefix))
            || key == "TTS_NATIVE_HEAVY_LEASE_SOCKET"
    }) { std::env::remove_var(key); }
    for (key,value) in [
        ("PHONE_WORKER_TTS_AGENT_ENABLED","true".into()),
        ("PHONE_WORKER_TETO_ENABLED","true".into()),
        ("PHONE_WORKER_TETO_VOICEBANK_MODE","standard".into()),
        ("PHONE_WORKER_TETO_VOICEBANK_DIR",bank.to_string_lossy().into_owned()),
        ("PHONE_WORKER_TETO_MIN_ALIASES","1".into()),
        ("PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR",root.path().join("fragments").to_string_lossy().into_owned()),
        ("PHONE_WORKER_TETO_NATIVE_RESAMPLER_COMMAND",format!("'{}' --fixture-resampler",std::env::current_exe().unwrap().display())),
    ] { std::env::set_var(key,value); }
    let socket = root.path().join("tts.sock");
    let service = Service::new(Config::new(socket.clone(),root.path().join("response-cache"))).unwrap();
    let stop = CancellationToken::new();
    let job = tokio::spawn(service.run(stop.clone()));
    while !socket.exists() { tokio::time::sleep(Duration::from_millis(1)).await; }
    let mut cases = serde_json::Map::new();
    let mut output_hash = None;
    for mode in ["bypass", "prefer"] {
        for _ in 0..2 { sample(&socket,mode).await; }
        let mut samples = Vec::new();
        for _ in 0..runs {
            let value = sample(&socket,mode).await;
            if let Some(hash) = &output_hash { assert_eq!(hash,&value["audio_sha256"]); }
            output_hash = Some(value["audio_sha256"].clone());
            assert_eq!(value["cache_hit"],mode=="prefer");
            samples.push(value);
        }
        cases.insert(mode.into(),json!({"runs":runs,"first_wav_byte_ms":distribution(&samples,"first_wav_byte_ms"),
            "total_ms":distribution(&samples,"total_ms"),"samples":samples}));
    }
    stop.cancel();
    job.await.unwrap().unwrap();
    println!("{}",serde_json::to_string_pretty(&json!({"benchmark":"native-worker-ttv-startup-v1", "aliases_generated":aliases,
        "text":TEXT,"cases":cases,"audio_sha256":output_hash,
        "limitations":["Generated voicebank and PCM-copy fixture resampler; not a real voice engine.",
            "Real Unix HTTP/configuration/fingerprint/plan/PCM/cache path; no network provider or Discord.",
            "Both cases use warm OS/fragment caches. bypass excludes full-response cache; prefer measures full-response cache hits.",
            "First WAV byte for raw Teto follows full phrase rendering; it is not a played/audible frame."]})).unwrap());
}
