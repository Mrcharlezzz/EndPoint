type LogContext = Record<string, unknown>;

type Level = "info" | "warn" | "error";

function emit(level: Level, event: string, payload?: LogContext) {
  if (typeof console === "undefined") return;
  const fn = level === "error" ? console.error : level === "warn" ? console.warn : console.info;
  if (payload) fn(`[${level}] ${event}`, payload);
  else fn(`[${level}] ${event}`);
}

export const log = {
  info(event: string, payload?: LogContext) {
    emit("info", event, payload);
  },
  warn(event: string, payload?: LogContext) {
    emit("warn", event, payload);
  },
  error(event: string, error: unknown, payload?: LogContext) {
    const message = error instanceof Error ? error.message : String(error);
    emit("error", event, { ...payload, message });
  },
};
