// Browser speech and sound. Everything here degrades to "not available" rather than throwing:
// speech recognition exists in Chromium and Safari, not in every browser.
import type { Sound } from "./ring";

interface RecognitionAlternative {
  transcript: string;
}
interface RecognitionResult {
  isFinal: boolean;
  0: RecognitionAlternative;
}
interface RecognitionEvent {
  resultIndex: number;
  results: ArrayLike<RecognitionResult>;
}
export interface Recognition {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
}
type RecognitionConstructor = new () => Recognition;

function recognitionConstructor(): RecognitionConstructor | null {
  const scope = globalThis as unknown as {
    SpeechRecognition?: RecognitionConstructor;
    webkitSpeechRecognition?: RecognitionConstructor;
  };
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null;
}

export function canListen(): boolean {
  return recognitionConstructor() !== null;
}

export interface Listening {
  stop(): void;
}

/**
 * Listen for one sentence. `onText` gets interim text as it is heard; `onFinal` gets the
 * finished sentence once. Returns null when the browser has no speech recognition.
 */
export function listen(handlers: {
  onText: (text: string) => void;
  onFinal: (text: string) => void;
  onEnd: (error?: string) => void;
}): Listening | null {
  const Constructor = recognitionConstructor();
  if (!Constructor) return null;
  const recognition = new Constructor();
  recognition.lang = "en-US";
  recognition.continuous = false;
  recognition.interimResults = true;
  let finished = false;
  let error: string | undefined;
  recognition.onresult = (event) => {
    let text = "";
    let final = false;
    for (let index = 0; index < event.results.length; index += 1) {
      const result = event.results[index];
      if (!result) continue;
      text += result[0].transcript;
      final = final || result.isFinal;
    }
    text = text.trim();
    handlers.onText(text);
    if (final && !finished && text) {
      finished = true;
      handlers.onFinal(text);
    }
  };
  recognition.onerror = (event) => {
    error = event.error;
  };
  recognition.onend = () => handlers.onEnd(error);
  recognition.start();
  return { stop: () => recognition.stop() };
}

export function canSpeak(): boolean {
  return typeof speechSynthesis !== "undefined" && typeof SpeechSynthesisUtterance !== "undefined";
}

/** Read text aloud (the operator reading the quote back). Replaces anything being spoken. */
export function speak(text: string): void {
  if (!canSpeak() || !text) return;
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.05;
  speechSynthesis.speak(utterance);
}

export function stopSpeaking(): void {
  if (canSpeak()) speechSynthesis.cancel();
}

let audio: AudioContext | null = null;

const TONES: Record<Exclude<Sound, null>, { frequency: number[]; duration: number; type: OscillatorType }> = {
  pass: { frequency: [660], duration: 0.09, type: "sine" },
  ask: { frequency: [520, 660], duration: 0.16, type: "triangle" },
  block: { frequency: [196, 147], duration: 0.22, type: "square" },
};

/** A short tone for the ring: soft for a pass, two rising notes for "ask", low for a block. */
export function playTone(sound: Sound): void {
  if (!sound || typeof AudioContext === "undefined") return;
  try {
    audio ??= new AudioContext();
    if (audio.state === "suspended") void audio.resume();
    const tone = TONES[sound];
    tone.frequency.forEach((frequency, index) => {
      const context = audio!;
      const start = context.currentTime + index * tone.duration;
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = tone.type;
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(sound === "block" ? 0.08 : 0.12, start + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + tone.duration);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start(start);
      oscillator.stop(start + tone.duration + 0.02);
    });
  } catch {
    // Sound is a nicety; a browser that refuses audio must not break the view.
  }
}
