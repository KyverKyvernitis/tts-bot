import { LoaderCircle, Pause, Play, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { TTS_SAMPLES } from "./ttsSamples";

type PlaybackPhase = "idle" | "loading" | "playing" | "paused" | "error";

const WAVE_HEIGHTS = [19, 32, 47, 26, 63, 79, 46, 94, 60, 37, 76, 100, 65, 39, 83, 55, 29, 69, 92, 49, 74, 35, 57, 85, 43, 64, 31, 48, 25, 16];

function formatTime(seconds: number) {
  const rounded = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
}

export function TtsSamplePlayer({ id = "ouvir-osaka" }: { id?: string }) {
  const [sampleIndex, setSampleIndex] = useState(0);
  const [phase, setPhase] = useState<PlaybackPhase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(0);
  const audioRef = useRef<HTMLAudioElement>(null);
  const requestRef = useRef(0);
  const wantsPlaybackRef = useRef(false);
  const sampleRef = useRef(TTS_SAMPLES[0]);
  const mountedRef = useRef(false);
  const sample = TTS_SAMPLES[sampleIndex];
  const isPlaying = phase === "playing";
  const isLoading = phase === "loading";
  const progress = duration > 0 ? Math.min(100, Math.max(0, (elapsed / duration) * 100)) : 0;

  useEffect(() => {
    mountedRef.current = true;
    const audio = audioRef.current;
    const handleVisibility = () => {
      if (!document.hidden) return;
      requestRef.current += 1;
      wantsPlaybackRef.current = false;
      audio?.pause();
      setPhase(current => current === "playing" || current === "loading" ? "paused" : current);
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      mountedRef.current = false;
      requestRef.current += 1;
      wantsPlaybackRef.current = false;
      document.removeEventListener("visibilitychange", handleVisibility);
      audio?.pause();
    };
  }, []);

  function isCurrentAudio(audio: HTMLAudioElement) {
    return !audio.currentSrc || audio.currentSrc === new URL(sampleRef.current.src, window.location.href).href;
  }

  function selectSample(index: number) {
    if (index === sampleIndex) return;
    requestRef.current += 1;
    wantsPlaybackRef.current = false;
    sampleRef.current = TTS_SAMPLES[index];
    const audio = audioRef.current;
    audio?.pause();
    if (audio) audio.currentTime = 0;
    setSampleIndex(index);
    setElapsed(0);
    setDuration(0);
    setPhase("idle");
  }

  async function togglePlayback() {
    const audio = audioRef.current;
    if (!audio) return;
    const request = ++requestRef.current;
    if (wantsPlaybackRef.current || !audio.paused) {
      wantsPlaybackRef.current = false;
      audio.pause();
      setPhase("paused");
      return;
    }

    // A failed network request can be retried without reloading the page.
    if (phase === "error" || audio.error) audio.load();
    wantsPlaybackRef.current = true;
    setPhase("loading");
    try {
      await audio.play();
      if (mountedRef.current && request === requestRef.current && wantsPlaybackRef.current && !audio.paused) {
        setPhase("playing");
      }
    } catch {
      if (!mountedRef.current || request !== requestRef.current || !wantsPlaybackRef.current) return;
      wantsPlaybackRef.current = false;
      setPhase("error");
    }
  }

  const playLabel = isLoading
    ? "Cancelar carregamento do áudio"
    : isPlaying
      ? `Pausar voz: ${sample.label}`
      : phase === "error"
        ? `Tentar reproduzir novamente: ${sample.label}`
        : `Ouvir voz: ${sample.label}`;

  return (
    <section id={id} className="osk-tts-player" aria-labelledby={`${id}-title`} data-playing={isPlaying || undefined} data-loading={isLoading || undefined}>
      <div className="osk-tts-player__heading">
        <h2 id={`${id}-title`} className="osk-tts-player__eyebrow">Escolha uma voz</h2>
      </div>

      <div className="osk-tts-player__choices" role="group" aria-label="Escolha uma voz">
        {TTS_SAMPLES.map((item, index) => (
          <button key={item.id} type="button" className="osk-tts-player__choice" data-selected={index === sampleIndex || undefined} aria-pressed={index === sampleIndex} onClick={() => selectSample(index)}>
            {item.label}
          </button>
        ))}
      </div>

      <div className="osk-tts-player__controls">
        <button type="button" className="osk-tts-player__play" onClick={() => void togglePlayback()} aria-label={playLabel} aria-describedby={`${id}-voice`}>
          {isLoading ? <LoaderCircle size={26} aria-hidden="true" /> : isPlaying ? <Pause size={26} fill="currentColor" aria-hidden="true" /> : phase === "error" ? <RotateCcw size={25} aria-hidden="true" /> : <Play size={26} fill="currentColor" aria-hidden="true" />}
        </button>
        <div className="osk-tts-player__waveform" aria-hidden="true">
          {WAVE_HEIGHTS.map((height, index) => (
            <span key={index} className="osk-tts-player__bar" style={{ "--bar-height": `${height}%`, "--bar-delay": `${index * -0.083}s` } as CSSProperties} />
          ))}
        </div>
        <span className="osk-tts-player__time" aria-hidden="true">{formatTime(elapsed)}<span> / {duration > 0 ? formatTime(duration) : "—"}</span></span>
      </div>

      <div className="osk-tts-player__progress" role="progressbar" aria-label="Progresso do áudio" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress)} aria-valuetext={duration > 0 ? `${formatTime(elapsed)} de ${formatTime(duration)}` : "Áudio ainda não iniciado"}>
        <span style={{ transform: `scaleX(${progress / 100})` }} />
      </div>

      <p id={`${id}-voice`} className="osk-tts-player__caption">{sample.description}</p>
      <p className="osk-tts-player__transcript">“{sample.transcript}”</p>
      {phase === "error" && <p className="osk-tts-player__error" role="alert">Não foi possível reproduzir. Toque no botão para tentar novamente.</p>}
      <span className="osk-sr-only" role="status" aria-live="polite">{phase === "loading" ? "Carregando áudio." : phase === "playing" ? `Reproduzindo ${sample.label}.` : phase === "paused" ? "Áudio pausado." : ""}</span>

      <audio
        ref={audioRef}
        src={sample.src}
        preload="none"
        onLoadedMetadata={event => {
          const audio = event.currentTarget;
          if (isCurrentAudio(audio)) setDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
        }}
        onDurationChange={event => {
          const audio = event.currentTarget;
          if (isCurrentAudio(audio)) setDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
        }}
        onTimeUpdate={event => {
          const audio = event.currentTarget;
          if (isCurrentAudio(audio)) setElapsed(audio.currentTime);
        }}
        onPlaying={event => {
          const audio = event.currentTarget;
          if (!isCurrentAudio(audio)) return;
          if (document.hidden || !wantsPlaybackRef.current) audio.pause();
          else setPhase("playing");
        }}
        onWaiting={event => {
          if (isCurrentAudio(event.currentTarget) && wantsPlaybackRef.current) setPhase("loading");
        }}
        onPause={event => {
          const audio = event.currentTarget;
          if (!isCurrentAudio(audio) || !audio.paused || audio.ended) return;
          wantsPlaybackRef.current = false;
          setPhase(current => current === "error" ? current : audio.currentTime > 0 ? "paused" : "idle");
        }}
        onEnded={event => {
          const audio = event.currentTarget;
          if (!isCurrentAudio(audio)) return;
          wantsPlaybackRef.current = false;
          audio.currentTime = 0;
          setElapsed(0);
          setPhase("idle");
        }}
        onError={event => {
          if (!isCurrentAudio(event.currentTarget)) return;
          wantsPlaybackRef.current = false;
          setPhase("error");
        }}
      />
    </section>
  );
}
