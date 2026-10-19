import type { Platform } from "@/domain/entities";

const LABELS: Record<Platform, string> = {
  moodle: "Moodle",
  teams: "Microsoft Teams",
  telegram: "Telegram",
};

type Props = {
  platform: Platform;
  size?: number;
};

/**
 * Original miniature platform glyphs (NOT the brand logos) — matches the
 * reference design's stylized abstract marks.
 */
export function PlatformIcon({ platform, size = 14 }: Props) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 16 16",
    fill: "none" as const,
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };

  let glyph: React.ReactNode = null;
  if (platform === "moodle") {
    glyph = (
      <path d="M2 12V6a2 2 0 0 1 4 0v6M6 9a2 2 0 0 1 4 0v3M10 9a2 2 0 0 1 4 0v3" />
    );
  } else if (platform === "teams") {
    glyph = (
      <>
        <circle cx="6" cy="6" r="2" />
        <path d="M2.5 13a3.5 3.5 0 0 1 7 0" />
        <circle cx="11" cy="7" r="1.5" />
        <path d="M9 13.5a3 3 0 0 1 5 0" />
      </>
    );
  } else if (platform === "telegram") {
    glyph = (
      <>
        <path d="m2 8 12-5-3 11-3.5-3z" />
        <path d="m7.5 11 6.5-8" />
      </>
    );
  }

  return (
    <span
      className="platform-icon"
      data-platform={platform}
      title={LABELS[platform]}
      aria-label={LABELS[platform]}
    >
      <svg {...common}>{glyph}</svg>
    </span>
  );
}
