// Patch @linxin666/dsh-client-ui-task-board so a task row may carry a
// `pinSessionId` that forces every scheduled run into one fixed session.
//
// Why: reuseSession:true only reuses the previous execution's session when
// the 5s session/list roster is known AND the session is idle. At cron
// trigger time the roster is frequently unknown, so every run mints a fresh
// session (the "7 identical sessions in the sidebar" problem).
//
// What the patch does (two edits in lib/index.js, the live plugin entry):
//   1. launch(): if task.pinSessionId is a non-empty string AND (roster
//      unknown OR the session is idle), use it as the reuseSessionId passed
//      to the runner. A stale pin falls back to the original roster-based
//      reuse rule, so the task can never be wedged.
//   2. parseLedger(): normalize `pinSessionId` (string, non-empty) so it
//      survives the host ledger round-trip; malformed values are dropped.
//
// Idempotent: each edit is applied only when its original (unpatched) text
// is still present; already-patched text is detected and skipped.
// Run after the plugin is (re)installed/upgraded, then RESTART the DSH web
// host so the plugin process reloads lib/index.js:
//   node scripts/patch-task-board-pin-session.cjs
//
// Pair with scripts/pin_task_board_session.cjs, which writes the
// pinSessionId value into the task row in ledger-v2.json.

'use strict';
const { readFileSync, writeFileSync } = require('node:fs');
const path = require('node:path');

const home = process.env.DSH_HOME || path.join(process.env.USERPROFILE || require('node:os').homedir(), '.dsh');
const pluginRelativePath = path.join('node_modules', '@linxin666', 'dsh-client-ui-task-board', 'lib', 'index.js');
const profileCandidates = ['web', 'desktop'];
const target = profileCandidates
  .map((profile) => path.join(home, 'profiles', profile, pluginRelativePath))
  .find((candidate) => require('node:fs').existsSync(candidate));
if (!target) {
  console.error('ABORT: task-board plugin not found in web or desktop profile.');
  process.exit(1);
}

const LAUNCH_FROM = [
  '\tasync launch(opened) {',
  '\t\ttry {',
  '\t\t\tconst reuseSessionId = reusableSessionId(opened.task, this.idleSessionIds);',
].join('\n');
const LAUNCH_TO = [
  '\tasync launch(opened) {',
  '\t\ttry {',
  '\t\t\tconst rawPin = typeof opened.task.pinSessionId === "string" && opened.task.pinSessionId.trim() !== "" ? opened.task.pinSessionId : void 0;',
  '\t\t\t// Pinned session override (operator): when set and the session is',
  '\t\t\t// idle (or the roster is unknown), every run continues in that',
  '\t\t\t// session. A stale pin (session gone) falls back to the',
  '\t\t\t// roster-based reuse rule, so a pin can never wedge the task.',
  '\t\t\tconst pinned = rawPin !== void 0 && (this.idleSessionIds === void 0 || this.idleSessionIds.has(rawPin)) ? rawPin : void 0;',
  '\t\t\tconst reuseSessionId = pinned !== void 0 ? pinned : reusableSessionId(opened.task, this.idleSessionIds);',
].join('\n');

const PARSE_FROM = [
  '\t\ttask.permissionConfirmedAt = typeof row.permissionConfirmedAt === "number" && Number.isFinite(row.permissionConfirmedAt) ? row.permissionConfirmedAt : void 0;',
  '\t\ttasks.push(task);',
].join('\n');
const PARSE_TO = [
  '\t\ttask.permissionConfirmedAt = typeof row.permissionConfirmedAt === "number" && Number.isFinite(row.permissionConfirmedAt) ? row.permissionConfirmedAt : void 0;',
  '\t\t// Pinned execution session (operator override); a string survives',
  '\t\t// re-parse, anything else is dropped so a malformed value never runs.',
  '\t\ttask.pinSessionId = typeof row.pinSessionId === "string" && row.pinSessionId.trim() !== "" ? row.pinSessionId : void 0;',
  '\t\ttasks.push(task);',
].join('\n');

function apply(source, from, to, label) {
  if (source.includes(to)) {
    console.log(`${label}: already patched; skipping.`);
    return [source, 0];
  }
  if (!source.includes(from)) {
    return [source, -1]; // missing original -> caller decides
  }
  return [source.replace(from, to), 1];
}

function main() {
  let source = readFileSync(target, 'utf8');
  let [next, c1] = apply(source, LAUNCH_FROM, LAUNCH_TO, 'launch()');
  if (c1 === -1) {
    console.error('ABORT: original launch() text not found (plugin version changed?); file left untouched.');
    process.exit(1);
  }
  source = next;
  let [next2, c2] = apply(source, PARSE_FROM, PARSE_TO, 'parseLedger()');
  if (c2 === -1) {
    console.error('ABORT: original parseLedger() text not found (plugin version changed?); file left untouched.');
    process.exit(1);
  }
  source = next2;
  const changes = c1 + c2;
  if (changes > 0) writeFileSync(target, source, 'utf8');
  console.log(changes > 0
    ? `done — ${changes} edit(s) applied. RESTART the DSH web host so the plugin reloads lib/index.js.`
    : 'already fully patched; nothing to do (host still needs to be restarted to pick it up).');
}

main();
