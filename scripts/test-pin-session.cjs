// Simulate the task-board plugin's launch() pinSessionId logic to verify
// the pin is active and a fresh run will reuse the pinned session.
//
// Reads:
//   - ledger-v2.json (task's pinSessionId + last execution session)
//   - (optionally) session roster via dsh web API if available
//
// Prints what launch() would do: reuse pinned session or fall back.
'use strict';
const { readFileSync } = require('node:fs');
const path = 'C:/Users/Administrator/.dsh/task-board/ledger-v2.json';
const ledger = JSON.parse(readFileSync(path, 'utf8'));
const task = ledger.tasks.find(t => t.title.includes('复盘') && t.schedule && t.schedule.enabled);
if (!task) { console.error('task not found'); process.exit(1); }

const rawPin = task.pinSessionId;
console.log('task:', task.title);
console.log('pinSessionId:', rawPin);
console.log('last execution session:', task.executions[task.executions.length - 1].sessionId);

// The plugin's logic (post-patch):
//   pinned = rawPin valid AND (idleSessionIds unknown OR idleSessionIds.has(rawPin))
//   We don't know idleSessionIds here (that's a live roster), so we check:
//   - rawPin is a non-empty string
//   - rawPin equals the last execution's session (sanity)
if (typeof rawPin !== 'string' || rawPin.trim() === '') {
  console.log('\nRESULT: pin is EMPTY — next run will use the original reuse rule (may mint a new session).');
  process.exit(2);
}
if (rawPin === task.executions[task.executions.length - 1].sessionId) {
  console.log('\nRESULT: pin matches last execution session. When idleSessionIds is unknown (the cron-time failure case), the patched launch() will use rawPin directly → NEXT RUN REUSES THE SAME SESSION.');
} else {
  console.log('\nRESULT: pin differs from last execution session. The pinned session will be used when idle or roster-unknown.');
}
