// Adapted from qqbot-task-notify's sanitized official API template.
// Node >=24 native WebSocket/fetch; private stdio only, no listening sockets.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
const API = 'https://api.sgroup.qq.com';
export function pairingMatches(record, code, now = Date.now()/1000) {
  if (!record || record.consumed || record.expiresAt <= now || typeof code !== 'string') return false;
  const expected = Buffer.from(record.hash || '', 'hex');
  const actual = crypto.createHash('sha256').update(code).digest();
  return expected.length === actual.length && crypto.timingSafeEqual(expected, actual);
}
export function bindOwner(state, owner, content, now = Date.now()/1000) {
  if (state.owner || !owner || typeof content !== 'string') return null;
  content = content.trim();
  if (!content.startsWith('/bind ') || !pairingMatches(state.pairing, content.slice(6), now)) return null;
  return {...state, owner, pairing:{...state.pairing, consumed:true}};
}
export function stageFrame(frame) {
  return frame.op === 0 ? frame.t : null;
}
export async function main() {
  if (!process.env.QQBOT_APP_ID || !process.env.QQBOT_APP_SECRET || !process.env.QQBOT_PRIVATE_DIR || Number(process.versions.node.split('.')[0]) < 24) process.exit(1);
  const dir = process.env.QQBOT_PRIVATE_DIR;
  const file = path.join(dir, 'owner-state.json');
  fs.mkdirSync(dir, {recursive:true, mode:0o700});
  let state = {};
  try { state = JSON.parse(fs.readFileSync(file, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') process.exit(1); }
  const save = () => {
    const tmp = `${file}.${process.pid}.tmp`;
    const fd = fs.openSync(tmp, 'w', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify(state)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(tmp, file);
  };
  let ready = false, token = '', expires = 0, tokenPromise;
  const output = value => process.stdout.write(JSON.stringify(value)+'\n');
  const health = () => output({event:'health', bound:!!state.owner, gatewayReady:ready});
  async function request(url, options = {}) {
    const response = await fetch(url, {...options, redirect:'error', signal:AbortSignal.timeout(15000)});
    let data = {}; try { data = await response.json(); } catch {}
    const apiCode = data.code ?? data.errcode;
    if (!response.ok || (apiCode !== undefined && apiCode !== 0)) {
      const retryHeader = response.headers.get('retry-after');
      const retry = /^\d+(\.\d+)?$/.test(retryHeader || '') ? Number(retryHeader) : Math.max(60, (Date.parse(retryHeader || '')-Date.now())/1000 || 60);
      throw Object.assign(new Error('provider_failed'), {status:response.status, retryAfter:retry});
    }
    return data;
  }
  async function accessToken() {
    if (token && Date.now() < expires-60000) return token;
    if (!tokenPromise) tokenPromise = (async () => {
      const data = await request('https://bots.qq.com/app/getAppAccessToken', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({appId:process.env.QQBOT_APP_ID,clientSecret:process.env.QQBOT_APP_SECRET})});
      if (!data.access_token) throw new Error('token_unavailable');
      token = data.access_token;
      expires = Date.now()+Number(data.expires_in || 7200)*1000;
      return token;
    })().finally(() => {tokenPromise = null;});
    return tokenPromise;
  }
  async function connect() {
    const auth = await accessToken();
    const gateway = await request(API+'/gateway', {headers:{Authorization:`QQBot ${auth}`}});
    const url = new URL(gateway.url);
    if (url.protocol !== 'wss:' || !(url.hostname === 'qq.com' || url.hostname.endsWith('.qq.com'))) throw new Error('invalid_gateway');
    await new Promise(resolve => {
      const ws = new WebSocket(url);
      let lastSeq = null, waiting = false, heartbeat, hello, watchdog, refresh, done = false;
      const finish = () => {
        if (done) return; done = true;
        clearInterval(heartbeat); clearTimeout(hello); clearTimeout(watchdog); clearTimeout(refresh);
        ready = false; health();
        try {ws.close();} catch {}
        resolve();
      };
      hello = setTimeout(finish, 15000);
      const beat = enforce => {
        if (enforce && waiting) return finish();
        if (ws.readyState !== WebSocket.OPEN) return;
        ws.send(JSON.stringify({op:1,d:lastSeq})); waiting = true;
      };
      ws.addEventListener('message', event => {
        if (done) return;
        try {
          const frame = JSON.parse(event.data);
          if (typeof frame.s === 'number') lastSeq = frame.s;
          if (frame.op === 10) {
            clearTimeout(hello);
            ws.send(JSON.stringify({op:2,d:{token:`QQBot ${auth}`,intents:1<<25,shard:[0,1]}}));
            clearTimeout(watchdog); watchdog = setTimeout(finish, 15000);
            clearInterval(heartbeat); heartbeat = setInterval(()=>beat(true), Math.max(1000,Number(frame.d?.heartbeat_interval)||30000));
          } else if (frame.op === 1) beat(false);
          else if (frame.op === 11) waiting = false;
          else if (frame.op === 7 || frame.op === 9) {token=''; finish();}
          else if (stageFrame(frame) === 'READY') {
            clearTimeout(watchdog); ready = true; health();
            refresh = setTimeout(finish, Math.max(1000, expires-Date.now()-60000));
          } else if (stageFrame(frame) === 'C2C_MESSAGE_CREATE' && !state.owner) {
            const content = String(frame.d?.content || '').trim();
            const owner = frame.d?.author?.user_openid;
            const bound = bindOwner(state, owner, content);
            if (bound) {
              // One fsynced atomic record couples consumed fingerprint and owner.
              const previous = state; state = bound;
              try {save();} catch (e) {state = previous; throw e;}
              health();
            }
          }
        } catch { finish(); }
      });
      ws.addEventListener('close', finish);
      ws.addEventListener('error', finish);
    });
  }
  let commandQueue = Promise.resolve();
  const input = readline.createInterface({input:process.stdin, crlfDelay:Infinity});
  input.on('close', () => process.exit(0));
  input.on('line', line => {
    if (line.length > 16384) return;
    commandQueue = commandQueue.then(async () => {
      let cmd;
      try { cmd = JSON.parse(line); } catch { return; }
      let result = {ok:false,status:'rejected'};
      try {
        if (cmd.op === 'pair' && !state.owner && ready && typeof cmd.code === 'string' && cmd.code.length >= 16 && cmd.expiresAt > Date.now()/1000 && cmd.expiresAt <= Date.now()/1000+601) {
          state.pairing = {hash:crypto.createHash('sha256').update(cmd.code).digest('hex'),expiresAt:cmd.expiresAt,consumed:false}; save();
          result = {ok:true,status:'pairing_created'};
        } else if (cmd.op === 'send') {
          if (!state.owner) result = {ok:false,status:'not_bound'};
          else if (!ready) result = {ok:false,status:'gateway_unavailable'};
          else if (typeof cmd.text === 'string' && cmd.text.length > 0 && cmd.text.length <= 2000) {
            const auth = await accessToken();
            const response = await request(API+'/v2/users/'+encodeURIComponent(state.owner)+'/messages', {method:'POST',headers:{Authorization:`QQBot ${auth}`,'Content-Type':'application/json'},body:JSON.stringify({msg_type:0,content:cmd.text})});
            result = {ok:!!response.id,status:response.id ? 'sent' : 'uncertain'};
          }
        }
      } catch (e) {
        if (e.status === 401) {token=''; expires=0;}
        result = {ok:false,status:e.status === 429 ? 'rate_limited' : (e.status && e.status < 500 ? 'rejected' : 'uncertain')};
        if (e.status === 429) result.retryAfter = e.retryAfter;
      }
      output({id:cmd.id,...result});
    }).catch(() => {});
  });
  health();
  let delay = 5000;
  while (true) {
    const started = Date.now();
    try {await connect();} catch (e) {if (e.status === 401) {token=''; expires=0;} ready=false; health();}
    if (Date.now()-started > 120000) delay=5000;
    await new Promise(resolve=>setTimeout(resolve,delay));
    delay=Math.min(60000,delay*2);
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch(()=>process.exit(1));
