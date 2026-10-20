/**
 * Pin the "每天执行复盘发送到微信" task to a fixed session in the
 * task-board ledger.
 *
 * How it works:
 *   - The task record gets a `pinSessionId` field. The patched plugin's
 *     launch() checks it first and, when the pinned session is idle, forces
 *     every run into that session instead of minting a new one.
 *   - The pin target is this task's MOST RECENT settled execution session,
 *     so the pinned session keeps its accumulated context.
 *
 * Idempotent: if the task already carries a `pinSessionId` it is left
 * untouched. To force a re-pin (e.g. after the host restarted and dropped
 * the field), pass --force.
 */
const { readFileSync, writeFileSync } = require('node:fs');
const path = 'C:/Users/Administrator/.dsh/task-board/ledger-v2.json';

const TASK_TITLE_SUBSTR = '复盘';
const force = process.argv.includes('--force');
const ledger = JSON.parse(readFileSync(path, 'utf8'));
const task = ledger.tasks.find(t => t.title && t.title.includes(TASK_TITLE_SUBSTR) && t.schedule && t.schedule.enabled);
if (!task) {
  console.error('target task not found; aborting (ledger unchanged)');
  process.exit(1);
}
if (task.pinSessionId && !force) {
  console.log(`already pinned to ${task.pinSessionId}; nothing to do (use --force to re-pin)`);
  process.exit(0);
}
// Pin to the newest settled execution session (the one that holds the
// delivery-chain context). Fall back to the newest execution overall.
const latestSettled = [...task.executions].reverse().find(e => e.endedAt !== undefined);
const target = latestSettled ? latestSettled.sessionId : task.executions[task.executions.length - 1]?.sessionId;
if (!target) {
  console.error('no execution session to pin to; aborting');
  process.exit(1);
}
task.pinSessionId = target;
task.updatedAt = Date.now();
writeFileSync(path, JSON.stringify(ledger, null, 2), 'utf8');
console.log(`pinned task "${task.title}" to session ${target}`);
console.log('NOTE: the task-board plugin must be patched (pinSessionId support) and the DSH web host must be restarted for this to take effect.');
