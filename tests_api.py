"""Run: python -m unittest -v tests_api. No browser, credentials or real data used."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from backend import Store
from server import LocalServer, MAX_BODY, ROOT


def task(id='test-1'):
    return dict(id=id,title='Test <script>alert(1)</script>',category='Test',priority=3,
                priorityDefault=True,due='',remind=1440,duration=30,completed=False,
                notes='',source='course/1.txt',sourceNotes='Evidence',sourceTiming='Unknown',
                kind='assignment',firedKey='',createdAt='2026-09-09T00:00:00Z',updatedAt='2026-09-09T00:00:00Z')

class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'data')
        self.server = LocalServer(('127.0.0.1',0),store=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.token = self.req('GET','/api/v1/session')[1]['token']

    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.store.close(); self.temp.cleanup()

    def req(self,method,path,body=None,headers=None,auth=True,raw=None):
        h = {'Content-Type':'application/json'}
        if auth and hasattr(self,'token'):
            h['X-Planner-Token']=self.token
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn = http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        conn.request(method,path,body=data,headers=h)
        r = conn.getresponse(); payload = r.read()
        try:
            value = json.loads(payload)
        except ValueError:
            value = payload
        self.response_headers = dict(r.getheaders())
        code=r.status; conn.close()
        return code,value

    def create(self,id='test-1'):
        status,result = self.req('POST','/api/v1/tasks',task(id))
        self.assertEqual(status,201,result)
        return result['task']

    def test_crud_conflict_and_persistence(self):
        t=self.create()
        code,result=self.req('PATCH','/api/v1/tasks/'+t['id'],{'changes':{'completed':True},'expectedUpdatedAt':t['updatedAt']})
        self.assertEqual(code,200); self.assertTrue(result['task']['completed'])
        self.assertEqual(self.req('DELETE','/api/v1/tasks/'+t['id'],{'expectedUpdatedAt':t['updatedAt']})[0],409)
        self.assertEqual(self.req('DELETE','/api/v1/tasks/'+t['id'],{'expectedUpdatedAt':result['task']['updatedAt']})[0],200)
        self.assertEqual(self.req('GET','/api/v1/tasks')[1]['revision'],3)
        self.assertEqual(self.req('DELETE','/api/v1/tasks/nope',{'expectedUpdatedAt':t['updatedAt']})[0],404)
        self.create('persistent')
        other=Store(self.store.data_dir)
        self.assertEqual(other.snapshot_tasks()[0]['id'],'persistent'); other.close()

    def test_auth_origin_host_and_put(self):
        for method,path,body in [('POST','/api/v1/tasks',task()),('PUT','/api/v1/tasks',{'version':1,'tasks':[],'expectedRevision':0}),('PATCH','/api/v1/tasks/a',{}),('DELETE','/api/v1/tasks/a',{}),('POST','/api/v1/imports',{})]:
            self.assertEqual(self.req(method,path,body,auth=False)[0],403)
            self.assertEqual(self.req(method,path,body,headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.req('GET','/api/v1/session',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.req('POST','/api/v1/tasks',task(),headers={'Origin':'null'})[0],403)
        self.assertEqual(self.req('POST','/api/v1/tasks',task(),headers={'Origin':f'http://127.0.0.1:{self.port}'})[0],201)

    def test_static_whitelist(self):
        self.assertEqual(self.req('GET','/')[0],200)
        for path in ['/server.py','/backend.py','/data/tasks.sqlite3','/logs/events.jsonl','/../app.js','/%2e%2e/app.js','/%2fapp.js','/seed-data.json','/seed-data.js','/API.md','/qq_credentials.py']:
            self.assertEqual(self.req('GET',path)[0],404,path)
        for page in ['/tasks','/calendar','/qq','/data']:
            self.assertEqual(self.req('GET',page)[0],200,page)
        self.assertEqual(self.req('GET','/course')[0],404)
        self.assertEqual(self.req('GET','/nope')[0],404)
        self.assertEqual(self.req('GET','/qq/../server.py')[0],404)

    def test_schedule_import_and_deadline_resolution(self):
        code,initial=self.req('GET','/api/v1/schedule')
        self.assertEqual(code,200)
        self.assertEqual(initial['events'],[])
        self.assertEqual(initial['courses'],[])
        self.assertEqual(initial['resources'],{})
        content=(ROOT/'fixtures'/'sample-schedule.ics').read_text(encoding='utf-8')
        for route,body in [('/api/v1/schedule',{'ics':content}),
                           ('/api/v1/deadlines/resolve',{'category':'Sample Seminar','phrase':'下周'})]:
            self.assertEqual(self.req('POST',route,body,auth=False)[0],403)
            self.assertEqual(self.req('POST',route,body,headers={'Origin':'https://evil.example'})[0],403)
        code,result=self.req('POST','/api/v1/schedule',{'ics':content})
        self.assertEqual(code,200,result);self.assertEqual(result['count'],8)
        self.assertFalse(result['unchanged'])
        self.assertTrue(self.req('POST','/api/v1/schedule',{'ics':content})[1]['unchanged'])
        code,schedule=self.req('GET','/api/v1/schedule')
        self.assertEqual(code,200);self.assertEqual(len(schedule['events']),8)
        self.assertEqual(schedule['courses'],['Sample Computing','Sample Seminar'])
        self.assertEqual(schedule['resources'],{})
        code,resolved=self.req('POST','/api/v1/deadlines/resolve',{'category':'Sample Seminar','phrase':'下周交','sourceDate':'2026-09-08T16:00'})
        self.assertEqual(code,200);self.assertEqual(resolved['due'],'2026-09-15T16:00')
        self.assertEqual(self.req('POST','/api/v1/schedule',{'ics':'bad'})[0],400)

    def test_validation(self):
        for change in [{'priority':True},{'duration':False},{'remind':True},{'due':'2026-02-30T12:00'},{'kind':'unknown'},{'id':"a'; DROP TABLE tasks;--"},{'title':'x\x00'},{'createdAt':'yesterday'},{'firedKey':'bad'},{'title':'x'*201},{'extra':1}]:
            self.assertEqual(self.req('POST','/api/v1/tasks',{**task(),**change})[0],400,change)
        self.assertEqual(self.req('POST','/api/v1/tasks',raw=b'{"id":1,"id":2}')[0],400)
        self.assertEqual(self.req('POST','/api/v1/tasks',raw=b'{"a":NaN}')[0],400)
        self.assertEqual(self.req('POST','/api/v1/tasks',task(),headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.req('POST','/api/v1/tasks',raw=b'',headers={'Content-Length':str(MAX_BODY+1)})[0],413)
        t=self.create()
        self.assertEqual(self.req('GET','/api/v1/tasks')[1]['tasks'][0]['title'],task()['title'])
        for field in ['source','sourceNotes','sourceTiming','kind','createdAt','updatedAt','id']:
            self.assertEqual(self.req('PATCH','/api/v1/tasks/test-1',{'changes':{field:'x'},'expectedUpdatedAt':t['updatedAt']})[0],400)

    def test_reminder_migration(self):
        t=task(); t.update(due='2026-09-12T12:00',remind=30,firedKey='2026-09-12T12:00|30')
        result=self.req('POST','/api/v1/tasks',t)[1]['task']
        self.assertEqual(result['remind'],1440); self.assertEqual(result['firedKey'],'')
        for offset in [-1,0,1440,2880,4320,7200,10080,20160,43200]:
            t=task('offset_'+str(abs(offset))); t['remind']=offset
            if offset==-1: t['id']='disabled'
            self.assertEqual(self.req('POST','/api/v1/tasks',t)[0],201)

    def test_atomic_replace_backup(self):
        self.create()
        payload={'version':1,'tasks':[task('replacement')],'expectedRevision':0}
        self.assertEqual(self.req('PUT','/api/v1/tasks',payload)[0],409)
        payload['expectedRevision']=1
        payload['tasks'].append(task('replacement'))
        self.assertEqual(self.req('PUT','/api/v1/tasks',payload)[0],400)
        self.assertEqual(len(self.store.snapshot_tasks()),1)
        payload['tasks'].pop()
        self.assertEqual(self.req('PUT','/api/v1/tasks',payload)[0],200)
        self.assertEqual(len(list((self.store.data_dir/'backups').glob('*.sqlite3'))),1)

    def test_import_idempotency_retry_semantic_and_human_edits(self):
        payload={'batchId':'batch-1','files':[{'path':'course/1.txt','sha256':'a'*64,'status':'needs_review','tasks':[]}]}
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[0],200)
        item=payload['files'][0]; item.update(status='processed',tasks=[task()])
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[1]['added'],1)
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[1]['added'],0)
        t=self.store.snapshot_tasks()[0]
        self.req('PATCH','/api/v1/tasks/test-1',{'changes':{'title':'Human edited'},'expectedUpdatedAt':t['updatedAt']})
        item['sha256']='b'*64
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[1]['skipped'],1)
        self.assertEqual(self.store.snapshot_tasks()[0]['title'],'Human edited')
        item['sha256']='c'*64; item['tasks'][0]['id']='different-id'
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[1]['skipped'],1)
        self.assertEqual(len(self.store.snapshot_tasks()),1)
        files=self.req('GET','/api/v1/imports')[1]['files']
        self.assertEqual(files[0]['sha256'],'c'*64)
        other=Store(self.store.data_dir)
        item['sha256']='d'*64
        self.assertEqual(other.import_batch(payload)['added'],0); other.close()

    def test_import_failure_no_tasks_and_atomic_validation(self):
        payload={'batchId':'retry','files':[{'path':'course/fail.txt','sha256':'a'*64,'status':'failed','error':'parse_error','tasks':[]}]}
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[0],200)
        payload['files'][0].update(status='no_tasks',error='')
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[0],200)
        payload['files'][0].update(status='processed',tasks=[task()])
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[1]['added'],0)
        payload['files'][0]['sha256']='b'*64
        payload['files'].append({'path':'../secret','sha256':'b'*64,'status':'no_tasks','tasks':[]})
        self.assertEqual(self.req('POST','/api/v1/imports',payload)[0],400)
        self.assertEqual(self.store.snapshot_tasks(),[])

    def test_concurrent_optimistic_updates(self):
        t=self.create()
        def change(i):
            return self.req('PATCH','/api/v1/tasks/test-1',{'changes':{'notes':str(i)},'expectedUpdatedAt':t['updatedAt']})[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes=list(pool.map(change,range(8)))
        self.assertEqual(codes.count(200),1); self.assertEqual(codes.count(409),7)

    def test_events_health_qq_and_sanitization(self):
        self.create()
        self.store.audit('test',{'token':'secret','body':'sensitive','taskid':'test-1','status':'ok','count':1})
        events=self.req('GET','/api/v1/events?limit=1')[1]['events']
        self.assertEqual(len(events),1)
        self.assertNotIn('secret',json.dumps(events)); self.assertNotIn('sensitive',json.dumps(events))
        self.assertEqual(self.req('GET','/api/v1/events?limit=201')[0],400)
        self.assertEqual(self.req('GET','/api/v1/health')[1]['timezone'],'Asia/Shanghai')
        self.assertEqual(self.req('GET','/api/v1/qq/status')[1]['deliveryStatus'],'not_configured')
        self.assertEqual(self.req('POST','/api/v1/qq/test',{})[1]['ok'],False)
        self.assertEqual(self.req('POST','/api/v1/qq/pairing',{},auth=False)[0],403)

    def test_qq_multi_bot_routes(self):
        calls=[]
        class FakeMulti:
            def status(self):
                return {'configured':True,'timezone':'Asia/Shanghai','bots':[{'slot':'bot-1','configured':True},{'slot':'bot-2','configured':True}]}
            def pairing(self,slot):
                calls.append(('pairing',slot))
                return {'ok':True,'bot':slot,'code':'fake-pair-code','expiresAt':'2026-09-09T00:00:00Z'}
            def test(self,slot=None):
                calls.append(('test',slot))
                return {'ok':True,'status':'sent','bot':slot}
        self.server.reminders=FakeMulti()
        self.assertEqual(len(self.req('GET','/api/v1/qq/status')[1]['bots']),2)
        for slot in ('bot-1','bot-2'):
            self.assertEqual(self.req('POST','/api/v1/qq/pairing',{'bot':slot})[0],200)
            self.assertEqual(calls[-1],('pairing',slot))
            self.assertEqual(self.req('POST','/api/v1/qq/test',{'bot':slot})[0],200)
            self.assertEqual(calls[-1],('test',slot))
        self.assertEqual(self.req('POST','/api/v1/qq/test',{})[0],200)
        self.assertEqual(calls[-1],('test',None))
        before=len(calls)
        self.assertEqual(self.req('POST','/api/v1/qq/pairing',{})[0],400)
        for route in ('test','pairing'):
            for body in [{'bot':'bot-3'},{'bot':None},{'bot':[]},{'bot':True},{'bot':'bot-1','secret':'bad'},[]]:
                self.assertEqual(self.req('POST','/api/v1/qq/'+route,body)[0],400)
            self.assertEqual(self.req('POST','/api/v1/qq/'+route,{'bot':'bot-1'},auth=False)[0],403)
            self.assertEqual(self.req('POST','/api/v1/qq/'+route,{'bot':'bot-1'},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(len(calls),before)
        self.assertNotIn('fake-pair-code',json.dumps(self.req('GET','/api/v1/events')[1]))

    def test_empty_initialization_and_optional_synthetic_bootstrap_only_once(self):
        self.assertEqual(self.store.snapshot_tasks(),[])
        with tempfile.TemporaryDirectory() as temp:
            fixture=Path(temp)/'initial-tasks.json'
            fixture.write_text(json.dumps({'version':1,'tasks':[{**task('synthetic-initial'),'remind':30}]}),encoding='utf-8')
            store=Store(Path(temp)/'data',fixture)
            self.assertEqual([t['id'] for t in store.snapshot_tasks()],['synthetic-initial'])
            self.assertTrue(all(t['remind']==1440 for t in store.snapshot_tasks()))
            store.replace({'version':1,'expectedRevision':1,'tasks':[]}); store.close()
            store=Store(Path(temp)/'data',fixture)
            self.assertEqual(store.snapshot_tasks(),[]); store.close()

class CookieAuthTests(unittest.TestCase):
    """Real sockets and SQLite, not a fetch mock. All credentials are test-only."""
    req = APITests.req
    tearDown = APITests.tearDown

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'data')
        self.start()

    def start(self,password='test-only-password',proxy=False):
        self.server = LocalServer(('127.0.0.1',0),store=self.store,access_password=password,
                                  trust_proxy=proxy,public_origin='https://planner.example' if proxy else None)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def restart(self,password='test-only-password'):
        self.server.shutdown();self.thread.join();self.server.server_close()
        self.start(password)

    def login(self,headers=None):
        code,data = self.req('POST','/api/v1/session',{'accessPassword':'test-only-password'},headers=headers,auth=False)
        self.assertEqual(code,200,data)
        self.token = data['token']
        value = self.response_headers['Set-Cookie']
        self.assertIn('HttpOnly',value);self.assertIn('SameSite=Lax',value);self.assertIn('Max-Age=2592000',value)
        return value.split(';')[0]

    def test_cookie_crud_conflict_csrf_logout(self):
        for route in ['/api/v1/session','/api/v1/tasks','/%61pi/v1/tasks','/api/v1/health']:
            self.assertEqual(self.req('GET',route,auth=False)[0],401)
        self.assertEqual(self.req('POST','/api/v1/tasks',task(),auth=False)[0],401)
        self.assertEqual(self.req('POST','/api/v1/session',{'accessPassword':'wrong'},auth=False)[0],401)
        cookie = self.login({'Origin':f'http://127.0.0.1:{self.port}'})
        self.assertNotIn('Secure',self.response_headers['Set-Cookie'])
        h = {'Cookie':cookie}
        self.assertEqual(self.req('GET','/api/v1/session',headers=h,auth=False)[1]['token'],self.token)
        self.assertEqual(self.req('GET','/api/v1/tasks',headers=h)[0],200)
        self.assertEqual(self.req('POST','/api/v1/tasks',task(),headers=h,auth=False)[0],403)
        self.assertEqual(self.req('POST','/api/v1/tasks',task(),headers={**h,'Origin':'https://evil.example'})[0],403)
        code,result = self.req('POST','/api/v1/tasks',task(),headers=h)
        self.assertEqual(code,201,result)
        body = {'changes':{'completed':True},'expectedUpdatedAt':result['task']['updatedAt']}
        self.assertEqual(self.req('PATCH','/api/v1/tasks/test-1',body,headers=h)[0],200)
        self.assertEqual(self.req('PATCH','/api/v1/tasks/test-1',body,headers=h)[0],409)
        self.assertEqual(self.req('POST','/api/v1/logout',{},headers=h,auth=False)[0],403)
        self.assertEqual(self.req('POST','/api/v1/logout',{},headers=h)[0],200)
        self.assertIn('Max-Age=0',self.response_headers['Set-Cookie'])
        self.assertEqual(self.req('GET','/api/v1/tasks',headers=h)[0],401,'logout revokes copied cookie too')

    def test_persistent_cookie_rotation_tamper_and_agent_header(self):
        cookie = self.login()
        self.restart()
        self.assertEqual(self.req('GET','/api/v1/session',headers={'Cookie':cookie})[0],200)
        self.assertEqual(self.req('GET','/api/v1/tasks',headers={'Cookie':cookie+'x'})[0],401)
        self.assertEqual(self.req('GET','/api/v1/session',headers={'X-Planner-Access':'test-only-password'})[0],200)
        self.restart('rotated-test-only-password')
        self.assertEqual(self.req('GET','/api/v1/session',headers={'Cookie':cookie})[0],401)
        self.assertEqual(self.req('GET','/api/v1/session',headers={'X-Planner-Access':'test-only-password'})[0],401)
        self.assertEqual(self.req('GET','/api/v1/session',headers={'X-Planner-Access':'rotated-test-only-password'})[0],200)

    def test_login_origin_rate_limit_and_static_privacy(self):
        payload = {'username':'planner','accessPassword':'test-only-password'}
        for headers in [{'Origin':'https://evil.example'},{'Origin':'null'},{'Sec-Fetch-Site':'cross-site'}]:
            self.assertEqual(self.req('POST','/api/v1/session',payload,headers=headers,auth=False)[0],403)
        self.assertEqual(self.req('POST','/api/v1/session',payload,auth=False)[0],200)
        for _ in range(9):
            self.assertEqual(self.req('POST','/api/v1/session',{'accessPassword':'bad'},auth=False)[0],401)
        self.assertEqual(self.req('POST','/api/v1/session',payload,auth=False)[0],429)
        self.assertLessEqual(len(self.server.login_attempts),1024)
        for route in ['/seed-data.js','/seed-data.json','/data/session-secret']:
            self.assertEqual(self.req('GET',route,auth=False)[0],404)
        code,html = self.req('GET','/',auth=False)
        self.assertEqual(code,200)
        for markup in [b'autocomplete="username"',b'autocomplete="current-password"',b'id="login-form"',b'method="post"']:
            self.assertIn(markup,html)
        self.assertNotIn(b'seed-data.js',html)
        for item in self.store.snapshot_tasks():
            self.assertNotIn(item['title'].encode(),html)

    def test_https_cookie_and_pinned_public_origin(self):
        self.server.shutdown();self.thread.join();self.server.server_close()
        self.start(proxy=True)
        h = {'Host':'planner.example','X-Forwarded-Proto':'https','Origin':'https://planner.example'}
        cookie = self.login(h)
        self.assertIn('; Secure',self.response_headers['Set-Cookie'])
        self.assertEqual(self.req('GET','/api/v1/session',headers={**h,'Cookie':cookie})[0],200)
        for patch in [{'Host':'evil.example'},{'Origin':'https://evil.example'},{'X-Forwarded-Proto':'http'}]:
            self.assertEqual(self.req('POST','/api/v1/session',{'accessPassword':'test-only-password'},headers={**h,**patch},auth=False)[0],403)

if __name__=='__main__':
    unittest.main(verbosity=2)
