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

function MicIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}

type Phase ="idle" | "listening" | "transcribing";

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
    const why = "Voice input needs Chrome, Edge or Safari";
    return (
      <button type="button" className="btn btn-icon mic" aria-disabled="true" aria-label={why} title={why}>
        <MicIcon />
      </button>
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
        className={`btn btn-icon mic${phase !== "idle" ? " mic-on" : ""}`}
        aria-pressed={phase !== "idle"}
        aria-label={LABEL[phase]}
        title={
          source === "elevenlabs"
            ? "Speech to text by ElevenLabs"
            : "Speech to text by this browser"
        }
        onClick={toggle}
      >
        <MicIcon />
      </button>
      {error && (
        <span role="status" className="mic-error">
          {error}
        </span>
      )}
    </>
  );
}
