/**
 * Relative + absolute date formatting respecting a 12h/24h preference.
 *
 * Per the design spec:
 *   Past:    "2 hours ago" / "Yesterday" / "3 days ago" / "Apr 22" beyond 7 days
 *   Future:  "Today, 14:30" / "Tomorrow" / "In 3 days, Apr 28" / "Apr 28" beyond 7 days
 */

export type TimeFormat = "12h" | "24h";

const MS_MIN = 60_000;
const MS_HR = 60 * MS_MIN;
const MS_DAY = 24 * MS_HR;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function pad(n: number): string {
  return n < 10 ? `0${n}` : `${n}`;
}

export function formatTime(d: Date, timeFmt: TimeFormat): string {
  if (timeFmt === "24h") return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const h12 = d.getHours() % 12 || 12;
  const ampm = d.getHours() < 12 ? "AM" : "PM";
  return `${h12}:${pad(d.getMinutes())} ${ampm}`;
}

export function formatShortDate(d: Date): string {
  return `${MONTHS[d.getMonth()] ?? ""} ${d.getDate()}`;
}

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

export function formatPublished(iso: string, now: Date = new Date()): string {
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  const delta = now.getTime() - dt.getTime();

  if (delta < MS_HR) {
    const m = Math.max(1, Math.round(delta / MS_MIN));
    return `${m} min ago`;
  }
  if (delta < MS_DAY) {
    const h = Math.round(delta / MS_HR);
    return `${h} hour${h === 1 ? "" : "s"} ago`;
  }
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (isSameDay(dt, yesterday)) return "Yesterday";
  if (delta < 7 * MS_DAY) {
    const d = Math.round(delta / MS_DAY);
    return `${d} days ago`;
  }
  return formatShortDate(dt);
}

/**
 * Format an event's time-and-place row.
 *  Both start + end:  "Apr 28, 14:00 – 16:00"
 *  Start only:        "Apr 28, 14:00"
 *  Date-only start:   "Apr 28"
 *  Past:              "Ended 2 days ago"
 */
export function formatEventTime(
  startTime: string | null,
  endTime: string | null,
  timeFmt: TimeFormat = "24h",
  now: Date = new Date(),
): { text: string; past: boolean } {
  if (!startTime) return { text: "", past: false };
  const start = new Date(startTime);
  if (Number.isNaN(start.getTime())) return { text: "", past: false };
  const end = endTime ? new Date(endTime) : null;
  const referenceEnd = end && !Number.isNaN(end.getTime()) ? end : start;
  const past = referenceEnd.getTime() < now.getTime();

  if (past) {
    const delta = now.getTime() - referenceEnd.getTime();
    if (delta < MS_DAY) {
      const h = Math.round(delta / MS_HR);
      return { text: `Ended ${h} hour${h === 1 ? "" : "s"} ago`, past: true };
    }
    const d = Math.round(delta / MS_DAY);
    return { text: `Ended ${d} day${d === 1 ? "" : "s"} ago`, past: true };
  }

  const head = formatShortDate(start);
  const startTxt = formatTime(start, timeFmt);
  if (end && !Number.isNaN(end.getTime())) {
    const endTxt = formatTime(end, timeFmt);
    return { text: `${head}, ${startTxt} – ${endTxt}`, past: false };
  }
  // No end time — but only show the time if it isn't midnight (likely date-only).
  if (start.getHours() === 0 && start.getMinutes() === 0) {
    return { text: head, past: false };
  }
  return { text: `${head}, ${startTxt}`, past: false };
}
