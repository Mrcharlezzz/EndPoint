const SHUTDOWN_POLL_MS = 200;
const SHUTDOWN_TIMEOUT_MS = 10_000;

async function globalTeardown(): Promise<void> {
  // Stop preview first so nothing new hits the daemon while we're killing it.
  await shutdown(process.env.__PREVIEW_PID, 'pnpm preview');
  await shutdown(process.env.__DAEMON_PID, 'daemon bootstrap');
}

async function shutdown(pidStr: string | undefined, label: string): Promise<void> {
  if (!pidStr) return;
  const pid = Number(pidStr);
  if (!Number.isInteger(pid) || pid <= 0) return;

  if (!isAlive(pid)) return;

  try {
    process.kill(pid, 'SIGTERM');
  } catch {
    return;
  }

  const deadline = Date.now() + SHUTDOWN_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!isAlive(pid)) return;
    await sleep(SHUTDOWN_POLL_MS);
  }

  console.warn(`[globalTeardown] ${label} (pid=${pid}) ignored SIGTERM; sending SIGKILL`);
  try {
    process.kill(pid, 'SIGKILL');
  } catch {
    /* already gone */
  }
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default globalTeardown;
