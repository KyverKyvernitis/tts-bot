export const TTS_SAMPLE_TRANSCRIPT = "Oi, gente! Eu sou a Osaka. Você escreve, eu falo na call.";

export interface TtsSample {
  id: string;
  label: string;
  description: string;
  src: string;
  transcript: string;
}

/** Prerecorded with the same TTS providers supported by the Discord bot. */
export const TTS_SAMPLES: readonly TtsSample[] = [
  {
    id: "francisca",
    label: "Voz padrão",
    description: "Francisca · Português do Brasil",
    src: "/assets/tts/francisca.mp3",
    transcript: TTS_SAMPLE_TRANSCRIPT,
  },
  {
    id: "francisca-fast",
    label: "Mais rápida",
    description: "Francisca · Velocidade +20%",
    src: "/assets/tts/francisca-fast.mp3",
    transcript: TTS_SAMPLE_TRANSCRIPT,
  },
  {
    id: "google-pt",
    label: "Google TTS",
    description: "Google TTS · Português",
    src: "/assets/tts/google-pt.mp3",
    transcript: TTS_SAMPLE_TRANSCRIPT,
  },
];
