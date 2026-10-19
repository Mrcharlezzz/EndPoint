import { spawn, type ChildProcess, type StdioOptions } from 'node:child_process';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ADMIN_PASSWORD } from './fixtures/daemon';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INGESTION_ROOT = path.resolve(__dirname, '..', '..', '..');
const DASHBOARD_ROOT = path.resolve(__dirname, '..', '..');

const BOOT_TIMEOUT_MS = 120_000;
const BUILD_TIMEOUT_MS = 180_000;
const PREVIEW_READY_TIMEOUT_MS = 30_000;
const PREVIEW_PORT = 4173;

interface BootstrapHandle {
  pid: number;
  daemonUrl: string;
}

async function globalSetup(): Promise<void> {
  const bootstrap = await startBootstrap();
  process.env.__DAEMON_PID = String(bootstrap.pid);
  process.env.VITE_DAEMON_URL = bootstrap.daemonUrl;

  let previewPid: number | null = null;
  try {
    await runProcessorSeed();
    await runBuild(bootstrap.daemonUrl);
    previewPid = await startPreview();
    process.env.__PREVIEW_PID = String(previewPid);
  } catch (err) {
    // If build or preview fails, reap the bootstrap we already started —
    // globalTeardown won't run if globalSetup throws.
    if (previewPid !== null) killPid(previewPid);
    killPid(bootstrap.pid);
    throw err;
  }
}

function killPid(pid: number): void {
  try {
    process.kill(pid, 'SIGTERM');
  } catch {
    /* already gone */
  }
}

function startBootstrap(): Promise<BootstrapHandle> {
  const child = spawn(
    'uv',
    ['run', 'python', 'scripts/e2e_dashboard_bootstrap.py', '--keep-alive'],
    {
      cwd: INGESTION_ROOT,
      stdio: ['ignore', 'pipe', 'pipe'] as StdioOptions,
      env: { ...process.env, ENDPOINT_ADMIN_PASSWORD: ADMIN_PASSWORD },
      detached: false,
    },
  );

  const stdoutLines: string[] = [];
  const stderrLines: string[] = [];
  let daemonUrl: string | null = null;

  return new Promise<BootstrapHandle>((resolve, reject) => {
    const bail = (err: Error): void => {
      try {
        child.kill('SIGKILL');
      } catch {
        /* ignore */
      }
      reject(err);
    };

    const timer = setTimeout(() => {
      bail(
        new Error(
          `bootstrap did not emit READY within ${BOOT_TIMEOUT_MS}ms\n` +
            `last stdout:\n${stdoutLines.slice(-50).join('\n')}\n\n` +
            `last stderr:\n${stderrLines.slice(-50).join('\n')}`,
        ),
      );
    }, BOOT_TIMEOUT_MS);

    const daemonUrlPattern = /^DAEMON_URL=(.+)$/;
    readLines(child.stdout, (line) => {
      stdoutLines.push(line);
      const match = daemonUrlPattern.exec(line);
      if (match) daemonUrl = match[1] ?? null;
      if (line === 'READY' && daemonUrl && child.pid !== undefined) {
        clearTimeout(timer);
        resolve({ pid: child.pid, daemonUrl });
      }
    });

    readLines(child.stderr, (line) => {
      stderrLines.push(line);
    });

    child.on('exit', (code, signal) => {
      clearTimeout(timer);
      bail(
        new Error(
          `bootstrap exited before READY (code=${code}, signal=${signal})\n` +
            `last stderr:\n${stderrLines.slice(-50).join('\n')}`,
        ),
      );
    });

    child.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });

    // Decouple so the bootstrap survives after globalSetup resolves.
    child.unref();
  });
}

function runBuild(daemonUrl: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const child = spawn('pnpm', ['build'], {
      cwd: DASHBOARD_ROOT,
      stdio: 'inherit',
      env: { ...process.env, VITE_DAEMON_URL: daemonUrl },
    });
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`pnpm build exceeded ${BUILD_TIMEOUT_MS}ms`));
    }, BUILD_TIMEOUT_MS);
    child.on('exit', (code) => {
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error(`pnpm build exited with code ${code}`));
    });
    child.on('error', reject);
  });
}

function runProcessorSeed(): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const child = spawn(
      'uv',
      ['run', 'python', 'tests/e2e/seed_dashboard_processor_observability.py'],
      {
        cwd: INGESTION_ROOT,
        stdio: 'inherit',
        env: { ...process.env },
      },
    );
    child.on('exit', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`processor observability seed exited with code ${code}`));
    });
    child.on('error', reject);
  });
}

async function startPreview(): Promise<number> {
  const child: ChildProcess = spawn(
    'pnpm',
    ['preview', '--port', String(PREVIEW_PORT), '--strictPort'],
    {
      cwd: DASHBOARD_ROOT,
      // Inherit stdio so vite preview's "Local:" banner + any errors surface
      // in the Playwright runner's console without buffering risk.
      stdio: 'inherit',
      env: { ...process.env },
    },
  );

  if (child.pid === undefined) {
    throw new Error('pnpm preview failed to spawn (no pid)');
  }

  const deadline = Date.now() + PREVIEW_READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await isListening('127.0.0.1', PREVIEW_PORT)) return child.pid;
    if (child.exitCode !== null) {
      throw new Error(`pnpm preview exited before listening (code=${child.exitCode})`);
    }
    await sleep(200);
  }

  try {
    child.kill('SIGKILL');
  } catch {
    /* ignore */
  }
  throw new Error(`pnpm preview did not listen on :${PREVIEW_PORT} within ${PREVIEW_READY_TIMEOUT_MS}ms`);
}

function readLines(
  stream: NodeJS.ReadableStream | null,
  onLine: (line: string) => void,
): void {
  if (!stream) return;
  let buffer = '';
  stream.setEncoding('utf-8');
  stream.on('data', (chunk: string) => {
    buffer += chunk;
    let idx = buffer.indexOf('\n');
    while (idx !== -1) {
      const line = buffer.slice(0, idx).replace(/\r$/, '');
      buffer = buffer.slice(idx + 1);
      onLine(line);
      idx = buffer.indexOf('\n');
    }
  });
}

function isListening(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    const done = (ok: boolean): void => {
      socket.destroy();
      resolve(ok);
    };
    socket.once('connect', () => done(true));
    socket.once('error', () => done(false));
    socket.setTimeout(500, () => done(false));
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default globalSetup;
