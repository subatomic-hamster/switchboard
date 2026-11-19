import { useEffect, useRef, useState } from "react";

import { canListen, listen, type Listening, useSpeechSource } from "../speech";

export interface MicButtonProps {
  /** Interim text while the owner is still speaking. */
  onText: (text: string) => void;
  /** The finished sentence. */
  onFinal: (text: string) => void;
}

const ERRORS: Record<string, string> = {
  "not-allowed": "Microphone access was refused.",
  "service-not-allowed": "Microphone access was refused.",
  "no-speech": "I did not hear anything.",
  "audio-capture": "No microphone was found.",
  network: "Speech recognition could not reach its service.",
};

type Phase = "idle" | "listening" | "transcribing";

const LABEL: Record<Phase, string> = {
  idle: "Speak",
  listening: "Listening…",
  transcribing: "Transcribing…",
};

/** Speak the request instead of typing it. Hidden entirely where the browser cannot listen. */
export function MicButton({ onText, onFinal }: MicButtonProps) {
  const source = useSpeechSource();
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const session = useRef<Listening | null>(null);

  useEffect(() => () => session.current?.stop(), []);

  if (!canListen()) {
    return (
      <span className="muted mic-unavailable" title="This browser has no speech recognition">
        Voice input needs Chrome, Edge or Safari
      </span>
    );
  }

  function toggle() {
    if (phase !== "idle") {
      session.current?.stop();
      return;
    }
    setError("");
    session.current = listen({
      onText,
      onFinal,
      onTranscribing: () => setPhase("transcribing"),
      onEnd: (reason) => {
        setPhase("idle");
        session.current = null;
        if (reason && reason !== "aborted") setError(ERRORS[reason] ?? "Voice input stopped.");
      },
    });
    setPhase(session.current !== null ? "listening" : "idle");
  }

  return (
    <>
      <button
        type="button"
        className={`mic${phase !== "idle" ? " mic-on" : ""}`}
        aria-pressed={phase !== "idle"}
        title={
          source === "elevenlabs"
            ? "Speech to text by ElevenLabs"
            : "Speech to text by this browser"
        }
        onClick={toggle}
      >
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <rect x="5.5" y="1.5" width="5" height="8" rx="2.5" fill="currentColor" />
          <path
            d="M3 7.5a5 5 0 0 0 10 0M8 12.5V15"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
        </svg>
        {LABEL[phase]}
      </button>
      {error && (
        <span role="status" className="mic-error">
          {error}
        </span>
      )}
    </>
  );
}
