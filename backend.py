"""Thread-safe stdlib SQLite task store. Never expose this module or data over HTTP."""
import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from collections import deque
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from course_schedule import Schedule

VERSION = 1
TIMEZONE = 'Asia/Shanghai'
REMINDERS = {-1, 0, 1440, 2880, 4320, 7200, 10080, 20160, 43200}
TEXT = {'id':100,'title':200,'category':100,'due':16,'notes':10000,'source':1000,
        'sourceNotes':3000,'sourceTiming':2000,'kind':40,'firedKey':100,'createdAt':40,'updatedAt':40}
FIELDS = set(TEXT) | {'priority','priorityDefault','remind','duration','completed'}
LOCKED = {'id','createdAt','updatedAt','source','sourceNotes','sourceTiming','kind'}

class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code

def require(condition, code='invalid_request', status=400):
    if not condition:
        raise APIError(status, code)

def text(value, maximum, nonempty=False):
    require(isinstance(value,str) and len(value)<=maximum and (not nonempty or bool(value.strip())))
    require(not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]',value))
    return value

def stamp(value):
    text(value,40,True)
    require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})',value)))
    try:
        return datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise APIError(400,'invalid_timestamp')

def now_after(previous=None):
    value = datetime.now(timezone.utc)
    if previous:
        value = max(value,stamp(previous)+timedelta(microseconds=1))
    return value.isoformat(timespec='microseconds').replace('+00:00','Z')

def validate_task(value):
    require(type(value) is dict and set(value)==FIELDS,'invalid_task_fields')
    t = value.copy()
    for key, maximum in TEXT.items():
        text(t[key],maximum,key in {'id','title','category','kind','createdAt','updatedAt'})
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]+',t['id'])),'invalid_id')
    for key, low, high in [('priority',1,4),('duration',1,1440)]:
        require(type(t[key]) is int and low<=t[key]<=high,'invalid_'+key)
    require(type(t['completed']) is bool and type(t['priorityDefault']) is bool)
    require(type(t['remind']) is int,'invalid_remind')
    if t['remind'] in {5,15,30,60}:
        t['remind'], t['firedKey'] = 1440, ''
    require(t['remind'] in REMINDERS,'invalid_remind')
    require(t['kind'] in {'assignment','milestone','suggestion','manual'},'invalid_kind')
    if t['due']:
        require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',t['due'])),'invalid_due')
        try:
            due = datetime.strptime(t['due'],'%Y-%m-%dT%H:%M')
            require(1900<=due.year<=9998,'invalid_due')
        except ValueError:
            raise APIError(400,'invalid_due')
    stamp(t['createdAt']); stamp(t['updatedAt'])
    require(not t['firedKey'] or (bool(t['due']) and t['firedKey']==f"{t['due']}|{t['remind']}"),'invalid_fired_key')
    return t

def validate_tasks(values):
    require(type(values) is list and len(values)<=5000,'task_limit')
    tasks = [validate_task(t) for t in values]
    require(len({t['id'] for t in tasks})==len(tasks),'duplicate_id')
    return tasks

def semantic_key(task, path=None):
    # Source line ranges change when transcripts change; dedupe on source file instead.
    source = (path or task['source'].split(' · ')[0]).replace('\\','/')
    parts = [source,task['category'],task['title'],task['kind']]
    normalized = [' '.join(unicodedata.normalize('NFKC',s).casefold().split()) for s in parts]
    return hashlib.sha256(json.dumps(normalized,ensure_ascii=False).encode()).hexdigest()

class Store:
    def __init__(self, data_dir, seed_path=None):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True,exist_ok=True)
        self.log_dir = self.data_dir.parent/'logs'
        self.log_dir.mkdir(parents=True,exist_ok=True)
        self.lock = threading.RLock()
        self.schedule = Schedule(self.data_dir)
        self.db = sqlite3.connect(self.data_dir/'tasks.sqlite3',check_same_thread=False,isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,status TEXT NOT NULL,batch_id TEXT NOT NULL,error TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ingestion(semantic_key TEXT PRIMARY KEY,task_id TEXT NOT NULL,path TEXT NOT NULL);
        INSERT OR IGNORE INTO meta VALUES('revision',0);''')
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
                tasks = validate_tasks(json.loads(Path(seed_path).read_text(encoding='utf-8'))['tasks']) if seed_path else []
                for t in tasks:
                    self.insert(t)
                    self.db.execute('INSERT OR IGNORE INTO ingestion VALUES(?,?,?)',(semantic_key(t),t['id'],t['source'].split(' · ')[0]))
                self.db.execute("INSERT INTO meta VALUES('initialized',1)")
                if tasks:
                    self.bump()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def revision(self):
        return self.db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]

    def bump(self):
        self.db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
        return self.revision()

    def insert(self,t):
        self.db.execute('INSERT INTO tasks VALUES(?,?)',(t['id'],json.dumps(t,ensure_ascii=False)))

    def snapshot(self):
        with self.lock:
            return {'version':VERSION,'revision':self.revision(),'tasks':[json.loads(r[0]) for r in self.db.execute('SELECT body FROM tasks ORDER BY rowid')]}

    def snapshot_tasks(self):
        return self.snapshot()['tasks']

    def get(self,id):
        row = self.db.execute('SELECT body FROM tasks WHERE id=?',(id,)).fetchone()
        require(row is not None,'not_found',404)
        return json.loads(row[0])

    def create(self,value):
        t = validate_task(value)
        t['createdAt'] = t['updatedAt'] = now_after()
        with self.transaction():
            require(not self.db.execute('SELECT 1 FROM tasks WHERE id=?',(t['id'],)).fetchone(),'duplicate_id',409)
            require(self.db.execute('SELECT count(*) FROM tasks').fetchone()[0]<5000,'task_limit')
            self.insert(t)
            rev = self.bump()
        self.audit('create',{'taskid':t['id']})
        return {'task':t,'revision':rev}

    def change(self,id,body,delete=False):
        require(type(body) is dict and set(body)==({'expectedUpdatedAt'} if delete else {'changes','expectedUpdatedAt'}))
        stamp(body['expectedUpdatedAt'])
        if not delete:
            require(type(body['changes']) is dict and bool(body['changes']))
            require(not(set(body['changes']) & LOCKED),'immutable_source_or_identity')
            require(set(body['changes'])<=FIELDS,'invalid_task_fields')
        with self.transaction():
            t = self.get(id)
            require(t['updatedAt']==body['expectedUpdatedAt'],'task_conflict',409)
            if delete:
                self.db.execute('DELETE FROM tasks WHERE id=?',(id,))
            else:
                changes = body['changes']
                candidate = {**t,**changes}
                if any(k in changes and changes[k]!=t[k] for k in ('due','remind')):
                    candidate['firedKey']=''
                candidate['updatedAt']=now_after(t['updatedAt'])
                t = validate_task(candidate)
                self.db.execute('UPDATE tasks SET body=? WHERE id=?',(json.dumps(t,ensure_ascii=False),id))
            rev = self.bump()
        self.audit('delete' if delete else 'patch',{'taskid':id})
        return {'revision':rev,'deleted':id} if delete else {'task':t,'revision':rev}

    def replace(self,body):
        require(type(body) is dict and set(body)=={'version','tasks','expectedRevision'})
        require(type(body['version']) is int and body['version']==1)
        require(type(body['expectedRevision']) is int)
        tasks = validate_tasks(body['tasks'])
        with self.lock:
            require(body['expectedRevision']==self.revision(),'revision_conflict',409)
            backups = self.data_dir/'backups'
            backups.mkdir(exist_ok=True)
            dest = backups/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite3')
            with closing(sqlite3.connect(dest)) as backup:
                self.db.backup(backup)
            with self.transaction():
                self.db.execute('DELETE FROM tasks')
                for t in tasks:
                    self.insert(t)
                rev = self.bump()
        self.audit('replace',{'count':len(tasks)})
        return {'version':1,'revision':rev,'tasks':tasks}

    def imports(self):
        with self.lock:
            return {'files':[dict(zip(('path','sha256','status','batchId','error','updatedAt'),r)) for r in self.db.execute('SELECT * FROM imports ORDER BY path')]}

    def import_batch(self,body):
        require(type(body) is dict and {'batchId','files'}<=set(body) and set(body)<={'batchId','files'})
        batch = text(body['batchId'],100,True)
        require(bool(re.fullmatch(r'[A-Za-z0-9_-]+',batch)))
        require(type(body['files']) is list and len(body['files'])<=5000)
        files, paths = [], set()
        total = 0
        for item in body['files']:
            require(type(item) is dict and {'path','sha256','status','tasks'}<=set(item) and set(item)<={'path','sha256','status','tasks','error'})
            p = text(item['path'],1000,True).replace('\\','/')
            require(not p.startswith('/') and not re.match(r'^[A-Za-z]:',p) and all(x not in {'','..','.'} for x in p.split('/')),'invalid_path')
            require(p not in paths,'duplicate_path'); paths.add(p)
            require(isinstance(item['sha256'],str) and bool(re.fullmatch(r'[a-fA-F0-9]{64}',item['sha256'])),'invalid_hash')
            require(item['status'] in ('processed','no_tasks','needs_review','failed'),'invalid_status')
            error = item.get('error','')
            require(isinstance(error,str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{0,80}',error)),'invalid_error_code')
            tasks = validate_tasks(item['tasks'])
            require(item['status']=='processed' or not tasks,'pending_tasks_not_allowed')
            require(item['status']!='processed' or bool(tasks),'use_no_tasks')
            total += len(tasks); require(total<=5000,'task_limit')
            files.append((p,item['sha256'].lower(),item['status'],error,tasks))
        added = skipped = 0
        with self.transaction():
            for p,digest,status,error,tasks in files:
                old = self.db.execute('SELECT sha256,status FROM imports WHERE path=?',(p,)).fetchone()
                if old and old[0]==digest and old[1] in ('processed','no_tasks'):
                    skipped += len(tasks)
                    continue
                for t in tasks:
                    t = validate_task(self.schedule.infer_task(t))
                    key = semantic_key(t,p)
                    if self.db.execute('SELECT 1 FROM tasks WHERE id=?',(t['id'],)).fetchone() or self.db.execute('SELECT 1 FROM ingestion WHERE semantic_key=?',(key,)).fetchone():
                        self.db.execute('INSERT OR IGNORE INTO ingestion VALUES(?,?,?)',(key,t['id'],p))
                        skipped += 1
                        continue
                    require(self.db.execute('SELECT count(*) FROM tasks').fetchone()[0]<5000,'task_limit')
                    self.insert(t)
                    self.db.execute('INSERT INTO ingestion VALUES(?,?,?)',(key,t['id'],p))
                    added += 1
                self.db.execute('INSERT OR REPLACE INTO imports VALUES(?,?,?,?,?,?)',(p,digest,status,batch,error,now_after()))
            rev = self.bump() if added else self.revision()
        self.audit('import',{'count':added,'skipped':skipped,'fileCount':len(files)})
        return {'added':added,'skipped':skipped,'fileCount':len(files),'revision':rev}

    def audit(self,event,fields=None,**kwargs):
        values = dict(fields or {}); values.update(kwargs)
        record = {'timestamp':now_after(),'action':event if isinstance(event,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,60}',event) else 'event'}
        for k in ('taskid','status','count','skipped','fileCount'):
            v = values.get(k)
            if type(v) is int or (isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',v)):
                record[k]=v
        with self.lock:
            try:
                with (self.log_dir/'events.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(record)+'\n')
            except OSError:
                pass  # Never turn a successfully committed write into an apparent failure.

    def events(self,limit):
        require(type(limit) is int and 1<=limit<=200,'invalid_limit')
        with self.lock:
            try:
                with (self.log_dir/'events.jsonl').open(encoding='utf-8') as f:
                    rows = list(deque(f,maxlen=limit))
                return {'events':[json.loads(r) for r in rows]}
            except FileNotFoundError:
                return {'events':[]}

    def close(self):
        with self.lock:
            self.db.close()
