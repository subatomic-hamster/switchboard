import type { Tone } from "../format";

const GLYPH: Record<Tone, string> = {
  good: "M3.5 8.5l3 3 6-7",
  warning: "M8 3v6M8 12v.5",
  critical: "M4 4l8 8M12 4l-8 8",
  neutral: "M4 8h8",
};

/** The shape for a tone, so state never depends on color alone. */
export function ToneIcon({ tone, size = 14 }: { tone: Tone; size?: number }) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} aria-hidden="true">
      <path
        d={GLYPH[tone]}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** A status mark: a shape plus a label. */
export function StatusMark({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`status status-${tone}`}>
      <ToneIcon tone={tone} />
      <span className="status-label">{label}</span>
    </span>
  );
}
