// Speech and sound for the dashboard.
//
// Two engines. When the server has an ElevenLabs key, the microphone recording goes to
// ElevenLabs speech-to-text and the operator's lines are played with ElevenLabs
// text-to-speech (both through the local server, which holds the key). Otherwise, and
// whenever ElevenLabs fails, the browser's own speech recognition and synthesis are used.
// Everything here degrades to "not available" rather than throwing.
import { useSyncExternalStore } from "react";

import type { Sound } from "./ring";

export type SpeechSource = "elevenlabs" | "browser";

/** The server refuses longer lines; those are read by the browser instead. */
const MAX_SPOKEN_CHARS = 1500;
const MAX_RECORDING_MS = 15_000;
/** Peak level (of 128) that counts as speech, and how long a pause ends the sentence. */
const SPEECH_LEVEL = 12;
const SILENCE_MS = 1300;

// --------------------------------------------------------------------------- which engine

let elevenLabs = false;
let sourcePromise: Promise<boolean> | null = null;
const sourceListeners = new Set<() => void>();

/** Ask the server once whether ElevenLabs is configured. Never rejects. */
export function loadSpeechSource(): Promise<boolean> {
  sourcePromise ??= fetch("/api/speech")
    .then((response) => (response.ok ? response.json() : { elevenlabs: false }))
    .then((body: { elevenlabs?: boolean }) => {
      elevenLabs = body.elevenlabs === true;
      sourceListeners.forEach((listener) => listener());
      return elevenLabs;
    })
    .catch(() => false);
  return sourcePromise;
}

function subscribeToSource(listener: () => void): () => void {
  sourceListeners.add(listener);
  void loadSpeechSource();
  return () => sourceListeners.delete(listener);
}

/** Which engine the dashboard's speech is using. Re-renders when the server has answered. */
export function useSpeechSource(): SpeechSource {
  return useSyncExternalStore(subscribeToSource, () => (elevenLabs ? "elevenlabs" : "browser"));
}

/** Tests only: forget what the server said. */
export function resetSpeechSource(): void {
  elevenLabs = false;
  sourcePromise = null;
}

// --------------------------------------------------------------------------- listening

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

function canRecord(): boolean {
  return (
    typeof MediaRecorder !== "undefined" &&
    typeof navigator !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function"
  );
}

export function canListen(): boolean {
  return (elevenLabs && canRecord()) || recognitionConstructor() !== null;
}

export interface Listening {
  stop(): void;
}

export interface ListenHandlers {
  /** Interim text while the owner is still speaking (browser recognition only). */
  onText: (text: string) => void;
  /** The finished sentence, once. */
  onFinal: (text: string) => void;
  onEnd: (error?: string) => void;
  /** The recording is finished and is being turned into text (ElevenLabs only). */
  onTranscribing?: () => void;
}

/**
 * Listen for one sentence. Returns null when there is no way to listen in this browser.
 */
export function listen(handlers: ListenHandlers): Listening | null {
  if (elevenLabs && canRecord()) return recordForElevenLabs(handlers);
  return listenWithBrowser(handlers);
}

function listenWithBrowser(handlers: ListenHandlers): Listening | null {
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

/** Stop the recording once the speaker has spoken and then paused. Returns a cleanup. */
function stopOnSilence(stream: MediaStream, stop: () => void): () => void {
  if (typeof AudioContext === "undefined") return () => undefined;
  try {
    const context = new AudioContext();
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    context.createMediaStreamSource(stream).connect(analyser);
    const samples = new Uint8Array(analyser.fftSize);
    let spoke = false;
    let quietSince = 0;
    const timer = setInterval(() => {
      analyser.getByteTimeDomainData(samples);
      let peak = 0;
      for (const sample of samples) peak = Math.max(peak, Math.abs(sample - 128));
      if (peak > SPEECH_LEVEL) {
        spoke = true;
        quietSince = 0;
      } else if (spoke) {
        quietSince ||= Date.now();
        if (Date.now() - quietSince > SILENCE_MS) stop();
      }
    }, 100);
    return () => {
      clearInterval(timer);
      void context.close();
    };
  } catch {
    return () => undefined;
  }
}

function recordForElevenLabs(handlers: ListenHandlers): Listening {
  let recorder: MediaRecorder | null = null;
  let stream: MediaStream | null = null;
  let stopRequested = false;
  let ended = false;
  let limit: ReturnType<typeof setTimeout> | undefined;
  let stopWatching: () => void = () => undefined;
  const chunks: Blob[] = [];

  const finish = (error?: string) => {
    if (ended) return;
    ended = true;
    clearTimeout(limit);
    stopWatching();
    stream?.getTracks().forEach((track) => track.stop());
    handlers.onEnd(error);
  };
  const stop = () => {
    stopRequested = true;
    if (recorder && recorder.state !== "inactive") recorder.stop();
  };

  navigator.mediaDevices
    .getUserMedia({ audio: true })
    .then((media) => {
      stream = media;
      if (stopRequested) {
        finish("aborted");
        return;
      }
      const active = new MediaRecorder(media);
      recorder = active;
      active.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.push(event.data);
      };
      active.onstop = () => {
        const type = active.mimeType || "audio/webm";
        const recording = new Blob(chunks, { type });
        stream?.getTracks().forEach((track) => track.stop());
        if (recording.size === 0) {
          finish("no-speech");
          return;
        }
        handlers.onTranscribing?.();
        fetch("/api/speech/hear", {
          method: "POST",
          headers: { "Content-Type": type },
          body: recording,
        })
          .then((response) =>
            response.ok ? response.json() : Promise.reject(new Error(String(response.status))),
          )
          .then((body: { text?: string }) => {
            const text = (body.text ?? "").trim();
            if (!text) {
              finish("no-speech");
              return;
            }
            handlers.onText(text);
            handlers.onFinal(text);
            finish();
          })
          .catch(() => finish("network"));
      };
      active.start();
      limit = setTimeout(stop, MAX_RECORDING_MS);
      stopWatching = stopOnSilence(media, stop);
    })
    .catch((error: unknown) => {
      const refused = error instanceof Error && error.name === "NotAllowedError";
      finish(refused ? "not-allowed" : "audio-capture");
    });
  return { stop };
}

// --------------------------------------------------------------------------- speaking

function browserCanSpeak(): boolean {
  return typeof speechSynthesis !== "undefined" && typeof SpeechSynthesisUtterance !== "undefined";
}

export function canSpeak(): boolean {
  return (elevenLabs && typeof Audio !== "undefined") || browserCanSpeak();
}

let playing: HTMLAudioElement | null = null;
let speakTicket = 0;

function speakWithBrowser(text: string): void {
  if (!browserCanSpeak()) return;
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.05;
  speechSynthesis.speak(utterance);
}

/** True when ElevenLabs produced the audio (even if a newer line has since replaced it). */
async function speakWithElevenLabs(text: string, ticket: number): Promise<boolean> {
  try {
    const response = await fetch("/api/speech/say", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) return false;
    const audio = await response.blob();
    if (ticket !== speakTicket) return true;
    const url = URL.createObjectURL(audio);
    const player = new Audio(url);
    const release = () => URL.revokeObjectURL(url);
    player.onended = release;
    player.onerror = release;
    playing = player;
    await player.play();
    return true;
  } catch {
    return false;
  }
}

/** Read text aloud (the operator reading the quote back). Replaces anything being spoken. */
export function speak(text: string): void {
  if (!text) return;
  stopSpeaking();
  const ticket = speakTicket;
  void loadSpeechSource().then(async (viaElevenLabs) => {
    if (ticket !== speakTicket) return;
    if (viaElevenLabs && text.length <= MAX_SPOKEN_CHARS && typeof Audio !== "undefined") {
      if (await speakWithElevenLabs(text, ticket)) return;
    }
    if (ticket === speakTicket) speakWithBrowser(text);
  });
}

export function stopSpeaking(): void {
  speakTicket += 1;
  if (playing) {
    playing.pause();
    playing = null;
  }
  if (browserCanSpeak()) speechSynthesis.cancel();
}

// --------------------------------------------------------------------------- tones

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
