#!/usr/bin/env node
// Send a proactive text message to the user's WeChat via the ilink bot API.
// Mirrors @xmanrui/dsh-im weixin-api.mjs sendText (endpoint: ilink/bot/sendmessage).
//
// Usage: node weixin-send.mjs "message text"
//        node weixin-send.mjs --file path/to/file.txt
//
// Reads:
//   - C:\Users\Administrator\.dsh\integrations\dsh-weixin\config.json (baseUrl)
//   - C:\Users\Administrator\.dsh\.credentials.yaml (bot token)
//   - C:\Users\Administrator\.dsh\integrations\dsh-weixin\accounts\<botId>\state.json (contextToken)

import { readFileSync } from 'node:fs';
import { randomUUID, randomBytes } from 'node:crypto';

const DSH_HOME = 'C:/Users/Administrator/.dsh';

// --- load config.json for baseUrl ---
const config = JSON.parse(readFileSync(`${DSH_HOME}/integrations/dsh-weixin/config.json`, 'utf8'));
const account = config.accounts[0];
if (!account) throw new Error('no weixin account configured');
const baseUrl = account.baseUrl.replace(/\/+$/, '');

// --- load token from .credentials.yaml (value may be "accountId:secret" or bare) ---
const credentials = readFileSync(`${DSH_HOME}/.credentials.yaml`, 'utf8');
const ref = account.tokenRef;
const lines = credentials.split(/\r?\n/);
const refLine = lines.find((l) => l.includes(ref));
if (!refLine) throw new Error(`tokenRef ${ref} not found in .credentials.yaml`);
// The value after "key:" is "accountId:secret" — the secret is the Bearer token.
const value = refLine.slice(refLine.indexOf(ref) + ref.length).replace(/^[:\s]+/, '').trim();
// Credential entries are commonly stored as accountId:secret; the account
// id itself contains @im.bot, so always use the portion after the final colon.
const token = value.includes(':') ? value.split(':').pop().trim() : value;

// --- load contextToken (optional, improves delivery reliability) ---
let contextToken;
try {
  const state = JSON.parse(readFileSync(
    `${DSH_HOME}/integrations/dsh-weixin/accounts/${account.botId}/state.json`, 'utf8'));
  const ctx = state.contextTokens?.users?.[account.ownerUserId];
  if (ctx?.token) contextToken = ctx.token;
} catch { /* no state yet, fine */ }

// --- message text ---
const args = process.argv.slice(2);
let text;
if (args[0] === '--file') {
  text = readFileSync(args[1], 'utf8');
} else {
  text = args.join(' ');
}
if (!text.trim()) {
  console.error('empty message');
  process.exit(1);
}

// Weixin limits single text messages; keep it under 1800 chars, truncate with note.
const MAX = 1800;
if (text.length > MAX) {
  text = text.slice(0, MAX - 40) + '\n\n（内容过长已截断，完整内容见飞书文档）';
}

const clientId = `dsh-weixin-${randomUUID()}`;
const ILINK_APP_ID = 'bot';
const ILINK_CLIENT_VERSION = (2 << 16) | (4 << 8) | 6;

const body = {
  msg: {
    from_user_id: '',
    to_user_id: account.ownerUserId,
    client_id: clientId,
    message_type: 2,
    message_state: 2,
    item_list: [{ type: 1, text_item: { text } }],
    ...(contextToken ? { context_token: contextToken } : {}),
  },
  base_info: {
    channel_version: '2.4.6',
    bot_agent: 'DeepSeekHarness/1.1.0',
  },
};

const url = `${baseUrl}/ilink/bot/sendmessage`;
const uin = Buffer.from(String(randomBytes(4).readUInt32BE(0)), 'utf8').toString('base64');

const request = async (messageBody) => {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'iLink-App-Id': ILINK_APP_ID,
      'iLink-App-ClientVersion': String(ILINK_CLIENT_VERSION),
      'content-type': 'application/json',
      'AuthorizationType': 'ilink_bot_token',
      'X-WECHAT-UIN': uin,
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(messageBody),
    signal: AbortSignal.timeout(15_000),
  });
  return response.json();
};

let json = await request(body);
// A persisted context token can expire or become invalid after the native
// integration reconnects. Retry once without it; the server can still route
// by to_user_id and will issue a fresh context on the next inbound message.
if (json.ret !== undefined && json.ret !== 0 && contextToken) {
  const retryBody = { ...body.msg };
  delete retryBody.context_token;
  json = await request({ ...body, msg: retryBody });
}
const rejection = (json.ret !== undefined && json.ret !== 0)
  || (json.errcode !== undefined && json.errcode !== 0);
if (rejection) {
  console.error('WeChat rejected the message:', JSON.stringify(json));
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, clientId, resp: json }));
