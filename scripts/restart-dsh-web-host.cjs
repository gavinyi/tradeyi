'use strict';
/*
 * Restart the DSH web host so host-side plugins reload (task-board
 * pinSessionId patch in node_modules/.../lib/index.js).
 *
 * Usage:
 *   node scripts/restart-dsh-web-host.cjs            # restart + wait for ready
 *   node scripts/restart-dsh-web-host.cjs --check    # status only
 *
 * The current agent turn is interrupted; session data is on disk and
 * recovers on reconnect. Spawn the relaunch detached so it survives this
 * process being killed mid-restart.
 */
const { spawn, execSync } = require('node:child_process');
const net = require('node:net');

const PORT = 43120;
const APP = 'C:/Program Files/DSH Desktop/resources/app/index.js';
const HOME = process.env.DSH_HOME || 'C:/Users/Administrator/.dsh';

function portOpen() {
  return new Promise((resolve) => {
    const s = net.connect(PORT, '127.0.0.1');
    s.once('connect', () => { s.destroy(); resolve(true); });
    s.once('error', () => resolve(false));
    setTimeout(() => { try { s.destroy(); } catch {} resolve(false); }, 1500);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitUntil(fn, timeoutMs, what) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await fn()) return true;
    await sleep(500);
  }
  throw new Error(`timeout waiting for ${what}`);
}

async function main() {
  const checkOnly = process.argv.includes('--check');
  const open = await portOpen();
  console.log(`port ${PORT}: ${open ? 'open' : 'closed'}`);
  if (checkOnly) process.exit(0);

  // Kill the whole DSH Desktop app (main process owns the node utility that
  // serves the port), then relaunch it. The current agent turn is
  // interrupted but session state is on disk and recovers on reconnect.
  if (open) {
    const out = execSync(`tasklist /FI "IMAGENAME eq DSH Desktop.exe" /FO CSV /NH`, { encoding: 'utf8' });
    const pids = [...new Set(out.split('\n').map((line) => {
      const m = line.match(/^[^,]+,"(\d+)"/);
      return m ? parseInt(m[1], 10) : 0;
      // match: "DSH Desktop.exe","12724","Console","1","44,000 K"
    }).filter((n) => n > 0))];
    if (pids.length === 0) {
      console.log('could not identify DSH Desktop pids; skipping kill');
    } else {
      for (const pid of pids) {
        try {
          execSync(`taskkill /PID ${pid} /T /F`, { encoding: 'utf8' });
          console.log(`killed DSH Desktop pid ${pid}`);
        } catch (e) {
          console.log(`taskkill pid ${pid} failed: ${String(e.message || e).split('\n')[0]}`);
        }
      }
      await waitUntil(async () => !(await portOpen()), 20000, 'port close');
      console.log('port released');
    }
  } else {
    console.log('port already closed; just starting host');
  }

  const child = spawn('C:/Program Files/DSH Desktop/DSH Desktop.exe', [], {
    cwd: HOME,
    detached: true,
    stdio: 'ignore',
  });
  child.unref();
  console.log(`started DSH Desktop (pid ${child.pid}); waiting for port ${PORT}...`);
  await waitUntil(async () => await portOpen(), 45000, 'port open');
  console.log('host is up; plugins reloaded.');
}

main().catch((e) => { console.error(e.message || e); process.exit(1); });
