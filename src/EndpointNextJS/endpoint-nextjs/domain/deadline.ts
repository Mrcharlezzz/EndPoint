/**
 * Deadline urgency calc + relative formatting.
 *
 * Urgency buckets (per design spec):
 *   - `overdue`  — past
 *   - `urgent`   — < 24h
 *   - `warm`     — < 72h
 *   - `neutral`  — otherwise
 *   - `none`     — no deadline
 */

export type Urgency = "overdue" | "urgent" | "warm" | "neutral" | "none";

export type FormattedDeadline = {
  text: string;
  urgency: Urgency;
};

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;

export function deadlineUrgency(
  deadline: string | null,
  now: Date = new Date(),
): Urgency {
  if (!deadline) return "none";
  const dt = new Date(deadline).getTime();
  if (Number.isNaN(dt)) return "none";
  const delta = dt - now.getTime();
  if (delta < 0) return "overdue";
  if (delta < DAY_MS) return "urgent";
  if (delta < 3 * DAY_MS) return "warm";
  return "neutral";
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : `${n}`;
}

function formatTime(d: Date, timeFmt: "12h" | "24h"): string {
  if (timeFmt === "24h") return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const h12 = d.getHours() % 12 || 12;
  const ampm = d.getHours() < 12 ? "AM" : "PM";
  return `${h12}:${pad(d.getMinutes())} ${ampm}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatShortDate(d: Date): string {
  return `${MONTHS[d.getMonth()] ?? ""} ${d.getDate()}`;
}

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/**
 * Examples (per design spec):
 *   "Due Today, 23:59"
 *   "Due Tomorrow"
 *   "Due in 3 days · Apr 28"
 *   "Due Apr 28, 23:59"
 *   "Closed · Apr 22"
 */
export function formatDeadline(
  deadline: string | null,
  timeFmt: "12h" | "24h" = "24h",
  now: Date = new Date(),
): FormattedDeadline {
  if (!deadline) return { text: "No deadline", urgency: "none" };

  const dl = new Date(deadline);
  if (Number.isNaN(dl.getTime())) {
    return { text: "No deadline", urgency: "none" };
  }
  const urgency = deadlineUrgency(deadline, now);

  if (urgency === "overdue") {
    return { text: `Closed · ${formatShortDate(dl)}`, urgency };
  }

  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const time = formatTime(dl, timeFmt);

  if (isSameDay(dl, now)) {
    return { text: `Due Today, ${time}`, urgency };
  }
  if (isSameDay(dl, tomorrow)) {
    return { text: "Due Tomorrow", urgency };
  }

  const days = Math.ceil((dl.getTime() - now.getTime()) / DAY_MS);
  if (days <= 7) {
    return { text: `Due in ${days} days · ${formatShortDate(dl)}`, urgency };
  }
  return { text: `Due ${formatShortDate(dl)}, ${time}`, urgency };
}
