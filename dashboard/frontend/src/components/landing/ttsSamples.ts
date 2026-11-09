export const TTS_SAMPLE_TRANSCRIPT = "Tô sem microfone hoje. Vocês me ouvem por aqui?";

export interface TtsSample {
  id: string;
  label: string;
  description: string;
  src: string;
  transcript: string;
  command?: string;
}

/** Prerecorded with the same TTS providers supported by the Discord bot. */
export const TTS_SAMPLES: readonly TtsSample[] = [
  {
    id: "francisca",
    label: "Voz padrão",
    description: "Francisca · Português do Brasil",
    src: "/assets/tts/francisca.mp3?v=20261103-call",
    transcript: TTS_SAMPLE_TRANSCRIPT,
  },
  {
    id: "texttovocaloid",
    label: "TextToVocaloid",
    description: "Young Girl A · Reverb 2",
    src: "/assets/tts/texttovocaloid-young-girl-a.mp3?v=20261104",
    transcript: TTS_SAMPLE_TRANSCRIPT,
    command: `'“${TTS_SAMPLE_TRANSCRIPT}”~~`,
  },
  {
    id: "google-pt",
    label: "Google TTS",
    description: "Google TTS · Português",
    src: "/assets/tts/google-pt.mp3?v=20261103-call",
    transcript: TTS_SAMPLE_TRANSCRIPT,
  },
];
