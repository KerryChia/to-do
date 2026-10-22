"""Durable, bounded QQ reminders. Credentials and owner never enter HTTP responses."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import queue
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time

CHINA = timezone(timedelta(hours=8))
NOTICE = 'QQ主动消息受会话窗口、平台权益及配额限制；接口受理不保证送达。长期未交互请先给机器人发消息。'


def schedule(task):
    try:
        if task.get('completed') or not task.get('due') or not task.get('id'):
            return None
        minutes = task.get('remind', -1)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
            return None
        due = datetime.fromisoformat(task['due'].replace('Z', '+00:00'))
        due = (due.replace(tzinfo=CHINA) if due.tzinfo is None else due.astimezone(CHINA))
        offsets = {minutes, 0, -1440, -4320, -10080}
        if minutes > 4320:
            offsets.add(4320)
        if minutes > 1440:
            offsets.add(1440)
        fingerprint = hashlib.sha256(f'{due.isoformat()}|{minutes}'.encode()).hexdigest()
        return fingerprint, due, [(m, (due - timedelta(minutes=m)).timestamp()) for m in sorted(offsets, reverse=True)]
    except (ValueError, TypeError, OverflowError):
        return None


def private_directory(override=None):
    base = Path(override or os.environ.get('QQBOT_PRIVATE_DIR') or (Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'to-do-open-source/qq-private'))
    base = base.resolve()
    if base == Path(__file__).resolve().parent or Path(__file__).resolve().parent in base.parents:
        raise ValueError('private_state_must_be_external')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=True)
        import csv
        sid = list(csv.reader(result.stdout.strip().splitlines()))[0][1]
        subprocess.run(['icacls', str(base), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F'], capture_output=True, check=True)
    else:
        base.chmod(0o700)
    return base


class NodeGateway:
    def __init__(self, *, credentials=None, private_dir=None):
        self.credentials = credentials
        self.private_dir = private_dir
        self.process = None
        self.lock = threading.Lock()
        self.responses = queue.Queue()
        self.health = {'bound': False, 'gatewayReady': False}

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.health = {'bound': False, 'gatewayReady': False}
        env = os.environ.copy()
        env.pop('QQBOT_BOTS', None)
        if self.credentials is not None:
            env['QQBOT_APP_ID'] = self.credentials.get('appId', '')
            env['QQBOT_APP_SECRET'] = self.credentials.get('appSecret', '')
        env['QQBOT_PRIVATE_DIR'] = str(private_directory(self.private_dir))
        self.process = subprocess.Popen([shutil.which('node') or 'node', str(Path(__file__).with_name('qq_gateway.mjs'))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env, shell=False)
        process = self.process
        def reader():
            try:
                for line in process.stdout:
                    data = json.loads(line)
                    if self.process is not process:
                        break
                    if data.get('event') == 'health':
                        self.health = {'bound': data.get('bound') is True, 'gatewayReady': data.get('gatewayReady') is True}
                    elif 'id' in data:
                        self.responses.put(data)
            except Exception:
                pass
            finally:
                if self.process is process:
                    self.health['gatewayReady'] = False
                process.stdout.close()
                if process.stdin:
                    process.stdin.close()
        threading.Thread(target=reader, daemon=True).start()

    def status(self):
        return dict(self.health, gatewayReady=self.health['gatewayReady'] and bool(self.process and self.process.poll() is None))

    def request(self, op, **fields):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                return {'ok': False, 'status': 'gateway_unavailable'}
            ident = secrets.token_hex(12)
            try:
                self.process.stdin.write(json.dumps(dict(id=ident, op=op, **fields), ensure_ascii=False) + '\n')
                self.process.stdin.flush()
                until = time.monotonic() + 50
                while time.monotonic() < until:
                    response = self.responses.get(timeout=max(.01, until-time.monotonic()))
                    if response.get('id') == ident:
                        return response
            except Exception:
                pass
            return {'ok': False, 'status': 'uncertain'}

    def send(self, text):
        return self.request('send', text=text)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.health['gatewayReady'] = False


class ReminderService:
    def __init__(self, task_provider, data_dir, audit, *, transport=None, clock=time.time):
        self.task_provider, self.audit, self.clock = task_provider, audit, clock
        self.configured = bool(os.environ.get('QQBOT_APP_ID') and os.environ.get('QQBOT_APP_SECRET')) if transport is None else True
        self.transport = transport or NodeGateway()
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.halt = threading.Event()
        self.thread = None
        self.gateway_retry_at = 0
        self.gateway_retry_delay = 5
        self.delivery = 'starting' if self.configured else 'not_configured'
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(data_dir) / 'reminders.sqlite3'), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS stages(task TEXT, fp TEXT, stage INTEGER, at REAL, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, next REAL DEFAULT 0, lease REAL DEFAULT 0, PRIMARY KEY(task,fp,stage));
        CREATE TABLE IF NOT EXISTS sends(task TEXT, day TEXT, at REAL, PRIMARY KEY(task,day));
        CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY, value REAL);
        ''')
        self.db.commit()

    def start(self):
        if not self.configured or (self.thread and self.thread.is_alive()):
            return
        self.halt.clear()
        try:
            self.transport.start()
        except Exception:
            self.delivery = 'gateway_unavailable'
            self.gateway_retry_at = self.clock()+5
        def run():
            while not self.halt.is_set():
                try:
                    self.tick()
                except Exception:
                    self.delivery = 'scheduler_error'
                self.halt.wait(15)
        self.thread = threading.Thread(target=run, daemon=True, name='qq-reminders')
        self.thread.start()

    def stop(self):
        self.halt.set()
        if self.thread:
            self.thread.join(timeout=55)
        self.transport.stop()

    def status(self):
        health = self.transport.status() if self.configured else {}
        ready, bound = bool(health.get('gatewayReady')), bool(health.get('bound'))
        delivery = self.delivery
        if delivery in ('starting', 'ready', 'not_bound', 'gateway_unavailable'):
            delivery = 'gateway_unavailable' if not ready else ('ready' if bound else 'not_bound')
        return {'configured': self.configured, 'bound': bound, 'gatewayReady': ready, 'enabled': self.configured and ready and bound, 'deliveryStatus': delivery, 'timezone': 'Asia/Shanghai', 'platformNotice': NOTICE}

    def pairing(self):
        health = self.status()
        if not health['configured'] or not health['gatewayReady'] or health['bound']:
            return {'ok': False, 'status': 'already_bound' if health['bound'] else health['deliveryStatus']}
        code, expires = secrets.token_urlsafe(24), int(self.clock()+600)
        result = self.transport.request('pair', code=code, expiresAt=expires)
        if not result.get('ok'):
            return {'ok': False, 'status': 'pairing_unavailable'}
        return {'ok': True, 'code': code, 'expiresAt': datetime.fromtimestamp(expires, CHINA).isoformat()}

    def _reserve_rate(self, now):
        if self.db.execute("SELECT 1 FROM stages WHERE state='sending' AND lease>? LIMIT 1", (now,)).fetchone():
            return False
        row = self.db.execute("SELECT value FROM control WHERE key='next_send'").fetchone()
        if row and row[0] > now:
            return False
        self.db.execute("INSERT OR REPLACE INTO control VALUES('next_send',?)", (now+60,))
        return True

    def test(self):
        if not self.status()['enabled']:
            return {'ok': False, 'status': self.status()['deliveryStatus']}
        with self.send_lock, self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            if not self._reserve_rate(self.clock()):
                self.db.commit()
                return {'ok': False, 'status': 'rate_limited'}
            self.db.commit()
            try:
                result = self.transport.send('待办日历：这是一条您主动请求的QQ提醒测试。')
            except Exception:
                result = {'ok': False, 'status': 'uncertain'}
            if result.get('status') == 'rate_limited':
                try:
                    delay = min(86400, max(60, float(result.get('retryAfter', 60))))
                except (ValueError, TypeError):
                    delay = 60
                self.db.execute("INSERT OR REPLACE INTO control VALUES('next_send',?)", (self.clock()+delay,))
                self.db.commit()
            return self._safe_result(result)

    def _safe_result(self, result):
        allowed = {'sent', 'rate_limited', 'rejected', 'uncertain', 'gateway_unavailable', 'not_bound'}
        self.delivery = 'sent' if result.get('ok') else (result.get('status') if result.get('status') in allowed else 'rejected')
        try:
            self.audit('qq_delivery', {'status': self.delivery})
        except Exception:
            pass
        return {'ok': bool(result.get('ok')), 'status': self.delivery}

    def tick(self):
        if not self.status()['enabled']:
            if self.configured and not self.halt.is_set() and self.clock() >= self.gateway_retry_at:
                try:
                    self.transport.start()  # Idempotent: a live gateway owns its reconnect loop.
                except Exception:
                    self.delivery = 'gateway_unavailable'
                self.gateway_retry_at = self.clock()+self.gateway_retry_delay
                self.gateway_retry_delay = min(60, self.gateway_retry_delay*2)
            return
        self.gateway_retry_delay = 5
        with self.send_lock, self.lock:
            now = self.clock()
            tasks = {str(t['id']): t for t in self.task_provider() if t.get('id')}
            self.db.execute('BEGIN IMMEDIATE')
            try:
                # A crash during an in-flight send is ambiguous: never automatically replay it.
                for abandoned in self.db.execute("SELECT task,lease FROM stages WHERE state='sending' AND lease<=?", (now,)).fetchall():
                    attempted = abandoned['lease']-120
                    attempted_day = datetime.fromtimestamp(attempted, CHINA).date().isoformat()
                    self.db.execute('INSERT OR IGNORE INTO sends VALUES(?,?,?)', (abandoned['task'], attempted_day, attempted))
                self.db.execute("UPDATE stages SET state='uncertain' WHERE state='sending' AND lease<=?", (now,))
                active = {}
                for ident, task in tasks.items():
                    plan = schedule(task)
                    if not plan:
                        continue
                    fp, due, stages = plan
                    active[ident] = fp
                    self.db.execute("UPDATE stages SET state='pending' WHERE task=? AND fp=? AND state='cancelled' AND attempts=0", (ident, fp))
                    for stage, at in stages:
                        self.db.execute('INSERT OR IGNORE INTO stages(task,fp,stage,at) VALUES(?,?,?,?)', (ident, fp, stage, at))
                    elapsed = [s for s, at in stages if at <= now]
                    if elapsed:
                        latest = elapsed[-1]
                        self.db.execute("UPDATE stages SET state='skipped' WHERE task=? AND fp=? AND stage!=? AND at<=? AND state IN ('pending','retry')", (ident, fp, latest, now))
                for row in self.db.execute("SELECT task,fp,stage FROM stages WHERE state IN ('pending','retry')").fetchall():
                    if active.get(row['task']) != row['fp']:
                        self.db.execute("UPDATE stages SET state='cancelled' WHERE task=? AND fp=? AND stage=?", tuple(row))
                day = datetime.fromtimestamp(now, CHINA).date().isoformat()
                row = self.db.execute("SELECT * FROM stages WHERE state IN ('pending','retry') AND at<=? AND next<=? AND attempts<4 AND NOT EXISTS (SELECT 1 FROM sends WHERE sends.task=stages.task AND day=?) ORDER BY at DESC LIMIT 1", (now, now, day)).fetchone()
                if row is None or not self._reserve_rate(now):
                    self.db.commit()
                    return
                key = (row['task'], row['fp'], row['stage'])
                self.db.execute("UPDATE stages SET state='sending',lease=?,attempts=attempts+1 WHERE task=? AND fp=? AND stage=?", (now+120, *key))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            # Fresh provider snapshot after durable claim, immediately before network IO.
            current = next((t for t in self.task_provider() if str(t.get('id')) == row['task']), None)
            plan = schedule(current) if current else None
            if not plan or plan[0] != row['fp'] or self.halt.is_set():
                self.db.execute("UPDATE stages SET state='cancelled' WHERE task=? AND fp=? AND stage=?", key)
                self.db.commit()
                return
            stage = row['stage']
            strength = {-10080:'逾期7天·最终强提醒', -4320:'逾期3天·升级催办', -1440:'逾期1天·高优先催办', 0:'到期·请立即处理'}.get(stage)
            if strength is None:
                strength = '紧急提醒' if stage <= 1440 else ('重要提醒' if stage <= 4320 else '温和提醒')
            remaining_days = (plan[1].timestamp()-now)/86400
            timing = f'距截止 {remaining_days:.1f} 天' if remaining_days > 0 else f'已逾期 {abs(remaining_days):.1f} 天'
            text = f"【{strength}】{str(current.get('title', '未命名任务'))[:400]}\n截止（北京时间）：{plan[1].strftime('%Y-%m-%d %H:%M')}（{timing}）\n任务仍未完成，请安排处理；完成后在日历勾选。"
            try:
                result = self.transport.send(text)
            except Exception:
                result = {'ok': False, 'status': 'uncertain'}
            safe = self._safe_result(result)
            state, next_at = ('sent' if safe['ok'] else 'failed'), 0
            if safe['status'] == 'uncertain':
                state = 'uncertain'
            elif safe['status'] == 'rate_limited':
                try:
                    retry = max(60, float(result.get('retryAfter', 60)))
                except (ValueError, TypeError):
                    retry = 60
                # Cap pathological provider values at a one-day global cooldown.
                next_at = now+max(min(retry, 86400), 60 * 2**row['attempts'])
                if retry <= 86400 and row['attempts']+1 < 4:
                    state = 'retry'
            self.db.execute('UPDATE stages SET state=?,next=?,lease=0 WHERE task=? AND fp=? AND stage=?', (state, next_at, *key))
            if state in ('sent', 'uncertain'):
                self.db.execute('INSERT OR IGNORE INTO sends VALUES(?,?,?)', (row['task'], day, now))
            if safe['status'] == 'rate_limited':
                self.db.execute("INSERT OR REPLACE INTO control VALUES('next_send',?)", (max(now+60, next_at),))
            self.db.commit()


class MultiReminderService:
    """Independent bot slots; never expose app/recipient identifiers to callers."""
    def __init__(self, task_provider, data_dir, audit, *, bots=None, transports=None, clock=time.time):
        if bots is None:
            try:
                bots = json.loads(os.environ.get('QQBOT_BOTS', 'null'))
            except (ValueError, TypeError):
                bots = []
            if bots is None:
                bots = [{'slot': 'bot-1', 'appId': os.environ.get('QQBOT_APP_ID', ''), 'appSecret': os.environ.get('QQBOT_APP_SECRET', '')}]
        if not isinstance(bots, list):
            bots = []
        configs = {b['slot']: b for b in bots if isinstance(b, dict) and b.get('slot') in ('bot-1', 'bot-2')}
        if not configs:
            configs = {'bot-1': {}}
        base = Path(os.environ.get('QQBOT_PRIVATE_DIR') or (Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'to-do-open-source/qq-private'))
        self.services = {}
        for slot, config in sorted(configs.items()):
            credentials = {key: config.get(key, '') if isinstance(config.get(key, ''), str) else '' for key in ('appId', 'appSecret')}
            fake = (transports or {}).get(slot)
            gateway = fake or NodeGateway(credentials=credentials, private_dir=base / slot)
            def scoped_audit(event, fields=None, _slot=slot, **kwargs):
                audit(event, dict(fields or {}, slot=_slot))
            service = ReminderService(task_provider, Path(data_dir) / ('qq-' + slot), scoped_audit, transport=gateway, clock=clock)
            service.configured = bool(fake or (credentials['appId'] and credentials['appSecret']))
            service.delivery = 'starting' if service.configured else 'not_configured'
            self.services[slot] = service

    def start(self):
        for service in self.services.values():
            service.start()

    def stop(self):
        for service in self.services.values():
            service.stop()

    def status(self):
        bots = [dict(service.status(), slot=slot, label='机器人' + slot[-1]) for slot, service in self.services.items()]
        aggregate = {key: all(bot[key] for bot in bots) for key in ('configured', 'bound', 'gatewayReady', 'enabled')}
        states = {bot['deliveryStatus'] for bot in bots}
        aggregate.update(deliveryStatus=next(iter(states)) if len(states) == 1 else 'partial_failure', bots=bots, timezone='Asia/Shanghai', platformNotice=NOTICE)
        return aggregate

    def pairing(self, bot=None):
        if not isinstance(bot, str) or bot not in self.services:
            return {'ok': False, 'status': 'invalid_selection'}
        return dict(self.services[bot].pairing(), slot=bot)

    def test(self, bot=None):
        if bot is not None and (not isinstance(bot, str) or bot not in self.services):
            return {'ok': False, 'status': 'invalid_selection', 'results': []}
        selected = list(self.services) if bot is None else [bot]
        from concurrent.futures import ThreadPoolExecutor
        def send(slot):
            try:
                return dict(self.services[slot].test(), slot=slot)
            except Exception:
                return {'slot': slot, 'ok': False, 'status': 'uncertain'}
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            results = list(executor.map(send, selected))
        ok = all(result['ok'] for result in results)
        return {'ok': ok, 'status': 'sent' if ok else 'partial_failure', 'results': results}
