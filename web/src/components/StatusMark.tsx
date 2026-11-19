import type { Tone } from "../format";

const GLYPH: Record<Tone, string> = {
  good: "M3.5 8.5l3 3 6-7",
  warning: "M8 3v6M8 12v.5",
  critical: "M4 4l8 8M12 4l-8 8",
  neutral: "M4 8h8",
};

/** A status mark: a shape plus a label, so state never depends on color alone. */
export function StatusMark({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`status status-${tone}`}>
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <path
          d={GLYPH[tone]}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="status-label">{label}</span>
    </span>
  );
}
