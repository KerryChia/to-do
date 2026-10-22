"""Offline tests only: no credentials, network, or real QQ delivery."""
import copy
import json
import os
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import unittest
from qq_reminders import CHINA, ReminderService, MultiReminderService, schedule

class Fake:
    def __init__(self):
        self.sent = []
        self.results = []
    def start(self): pass
    def stop(self): pass
    def status(self): return {'bound':True,'gatewayReady':True}
    def send(self, text):
        self.sent.append(text)
        return self.results.pop(0) if self.results else {'ok':True,'status':'sent'}

def stamp(text): return datetime.fromisoformat(text).replace(tzinfo=CHINA).timestamp()

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tasks = [{'id':'a','title':'Task','due':'2026-09-20T09:00','remind':20160,'completed':False}]
        self.now = stamp('2026-09-06T09:00')
        self.fake = Fake()
        self.service = self.make()
    def make(self):
        service = ReminderService(lambda:copy.deepcopy(self.tasks), Path(self.temp.name), lambda *a,**k:None, transport=self.fake, clock=lambda:self.now)
        self.addCleanup(service.db.close)
        return service
    def test_offsets_and_timezone(self):
        fp,due,stages = schedule(self.tasks[0])
        self.assertEqual([m//1440 for m,_ in stages], [14,3,1,0,-1,-3,-7])
        self.assertEqual(due.utcoffset().total_seconds(),28800)
        for minutes, expected in [(4320,[3,1,0,-1,-3,-7]),(1440,[1,0,-1,-3,-7]),(0,[0,-1,-3,-7])]:
            self.tasks[0]['remind']=minutes
            self.assertEqual([m//1440 for m,_ in schedule(self.tasks[0])[2]],expected)
    def test_escalation_no_priority_mutation(self):
        for date in ['06','17','19','20','21','23','27']:
            self.now=stamp(f'2026-09-{date}T09:00'); self.service.tick()
        self.assertEqual(len(self.fake.sent),7)
        self.assertEqual(len({text.split('】')[0] for text in self.fake.sent}),7)
        self.assertIn('温和',self.fake.sent[0]); self.assertIn('重要',self.fake.sent[1]); self.assertIn('紧急',self.fake.sent[2]); self.assertIn('逾期',self.fake.sent[-1])
        self.assertNotIn('priority',self.tasks[0])
    def test_coalesce_and_final(self):
        self.now=stamp('2026-11-20T09:00'); self.service.tick()
        self.assertEqual(len(self.fake.sent),1)
        self.now+=86400; self.service.tick()
        self.assertEqual(len(self.fake.sent),1)
    def test_duplicates_restart_reopen(self):
        self.service.tick(); self.service.tick()
        other=self.make(); other.tick()
        self.tasks[0]['completed']=True; other.tick()
        self.tasks[0]['completed']=False; other.tick()
        self.assertEqual(len(self.fake.sent),1)
    def test_disabled_deleted_missing_due(self):
        for change in [{'completed':True},{'due':''},{'remind':-1}]:
            old=copy.deepcopy(self.tasks); self.tasks[0].update(change); self.service.tick(); self.tasks=old
        self.tasks=[]; self.service.tick()
        self.assertEqual(self.fake.sent,[])
    def test_late_completion_and_schedule_change(self):
        for change in [{'completed':True},{'due':'2026-10-20T09:00'}]:
            self.tasks[0].update(id=str(change),completed=False,due='2026-09-20T09:00')
            count=[0]
            def provider():
                count[0]+=1
                tasks=copy.deepcopy(self.tasks)
                if count[0]>1: tasks[0].update(change)
                return tasks
            self.service.task_provider=provider
            self.service.tick(); self.now+=61
        self.assertEqual(self.fake.sent,[])
    def test_changed_schedule_daily_cap(self):
        self.service.tick()
        self.tasks[0]['due']='2026-09-19T09:00'
        self.now+=61; self.service.tick()
        self.assertEqual(len(self.fake.sent),1)
        self.now+=86400; self.service.tick()
        self.assertEqual(len(self.fake.sent),2)
    def test_rate_limit_retry_cap(self):
        self.fake.results=[{'ok':False,'status':'rate_limited','retryAfter':120}]*5
        self.service.tick(); self.now+=119; self.service.tick()
        self.assertEqual(len(self.fake.sent),1)
        for _ in range(6): self.now+=500; self.service.tick()
        self.assertEqual(len(self.fake.sent),4)
    def test_uncertain_never_replayed(self):
        self.fake.results=[{'ok':False,'status':'uncertain'}]
        self.service.tick(); self.now+=500; self.make().tick()
        self.assertEqual(len(self.fake.sent),1)
    def test_lease_and_crash_recovery(self):
        self.service.tick()
        self.service.db.execute("UPDATE stages SET state='sending',lease=? WHERE state='sent'",(self.now+120,))
        self.service.db.execute('DELETE FROM sends')
        self.service.db.commit()
        self.tasks.append(dict(self.tasks[0],id='b'))
        self.now+=61; self.make().tick()
        self.assertEqual(len(self.fake.sent),1)
        self.now+=60; self.make().tick()
        self.assertEqual(len(self.fake.sent),2)
        state=self.service.db.execute("SELECT state FROM stages WHERE task='a' AND stage=20160").fetchone()[0]
        self.assertEqual(state,'uncertain')
    def test_reopen_future_stages(self):
        self.service.tick()
        self.tasks[0]['completed']=True; self.now+=60; self.service.tick()
        self.tasks[0]['completed']=False; self.now=stamp('2026-09-17T09:00'); self.service.tick()
        self.assertEqual(len(self.fake.sent),2)
    def test_gateway_restart_cooldown(self):
        count=[0]
        self.fake.status=lambda:{'bound':False,'gatewayReady':False}
        def failed_start():
            count[0]+=1
            raise OSError('offline')
        self.fake.start=failed_start
        self.service.tick(); self.service.tick()
        self.assertEqual(count[0],1)
        self.now+=5; self.service.tick()
        self.assertEqual(count[0],2)
        self.now+=9; self.service.tick()
        self.assertEqual(count[0],2)
        self.now+=1; self.service.tick()
        self.assertEqual(count[0],3)
    def test_global_cap(self):
        self.tasks.append(dict(self.tasks[0],id='b'))
        self.service.tick(); self.service.tick()
        self.assertEqual(len(self.fake.sent),1)
        self.now+=60; self.service.tick()
        self.assertEqual(len(self.fake.sent),2)
    def test_safe_status_test(self):
        self.assertNotIn('id',self.service.test())
        self.assertNotIn('owner',self.service.status())
        self.assertEqual(self.service.test()['status'],'rate_limited')
    def test_pairing_validation_node(self):
        script = """import {pairingMatches,stageFrame,bindOwner} from './qq_gateway.mjs';
import crypto from 'node:crypto'; import assert from 'node:assert/strict';
const code='random-code-at-least-16'; const record={hash:crypto.createHash('sha256').update(code).digest('hex'),expiresAt:1000};
assert(pairingMatches(record,code,900)); assert(!pairingMatches(record,'stranger',900));
assert(!pairingMatches(record,code,1000)); assert(!pairingMatches({...record,consumed:true},code,900));
assert.equal(stageFrame({op:0,t:'READY',d:{t:'bad'}}),'READY');
assert.equal(bindOwner({pairing:record},'fake','hello',900),null);
const bound=bindOwner({pairing:record},'fake','/bind '+code,900); assert.equal(bound.owner,'fake');
const restored=JSON.parse(JSON.stringify(bound)); assert.equal(bindOwner(restored,'another','/bind '+code,900),null);
assert.equal(bindOwner({pairing:restored.pairing},'fake','/bind '+code,900),null);
"""
        result=subprocess.run(['node','--input-type=module','-e',script],cwd=Path(__file__).parent,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

class MultiBotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.first,self.second=Fake(),Fake()
        tasks=[{'id':'a','title':'Fake task','due':'2026-09-20T09:00','remind':1440,'completed':False}]
        self.multi=MultiReminderService(lambda:tasks,Path(self.temp.name),lambda *a,**k:None,bots=[{'slot':'bot-1'},{'slot':'bot-2'}],transports={'bot-1':self.first,'bot-2':self.second},clock=lambda:stamp('2026-09-20T09:00'))
        for service in self.multi.services.values(): self.addCleanup(service.db.close)
    def test_independent_failure_and_database(self):
        self.first.results=[{'ok':False,'status':'rejected'}]
        for service in self.multi.services.values(): service.tick(); service.tick()
        self.assertEqual(len(self.first.sent),1); self.assertEqual(len(self.second.sent),1)
        self.assertEqual(self.multi.status()['deliveryStatus'],'partial_failure')
        self.assertEqual(len(self.multi.status()['bots']),2)
        self.assertNotEqual(self.multi.services['bot-1'].db.execute('PRAGMA database_list').fetchone()[2],self.multi.services['bot-2'].db.execute('PRAGMA database_list').fetchone()[2])
    def test_parallel_explicit_test_and_selection(self):
        self.first.results=[{'ok':False,'status':'rejected'}]
        result=self.multi.test()
        self.assertFalse(result['ok']); self.assertEqual(result['status'],'partial_failure')
        self.assertTrue(result['results'][1]['ok'])
        self.assertEqual(self.multi.pairing()['status'],'invalid_selection')
        self.assertEqual(self.multi.pairing('invalid')['status'],'invalid_selection')
        self.assertEqual(self.multi.pairing('bot-2')['slot'],'bot-2')
        self.assertEqual(self.multi.test('invalid')['results'],[])

class GatewayProtocolTests(unittest.TestCase):
    def test_real_gateway_loop_with_offline_provider(self):
        # Execute the actual gateway, injecting an offline provider; never contact QQ.
        script = r"""import assert from 'node:assert/strict';
import {main} from './qq_gateway.mjs';
let tokens=0, gateways=0, sends=0, identified=0;
const output=process.stdout.write.bind(process.stdout);
process.stdout.write=(line)=>{
  const value=JSON.parse(line);
  if(value.event==='health' && value.gatewayReady) {
    assert.equal(tokens,2); assert.equal(identified,1);
    process.stdin.emit('data',JSON.stringify({id:'fixture',op:'send',text:'Offline fixture'})+'\n');
  }
  if(value.id==='fixture') {
    assert.equal(value.status,'sent'); assert.equal(sends,1);
    output('offline_gateway_passed\n'); process.exit(0);
  }
  return true;
};
globalThis.fetch=async(url,options)=>{
  assert.equal(options.redirect,'error');
  if(url==='https://bots.qq.com/app/getAppAccessToken') {
    tokens++; return new Response(JSON.stringify({access_token:'fixture-'+tokens,expires_in:7200}));
  }
  if(url==='https://api.sgroup.qq.com/gateway') {
    gateways++;
    if(gateways===1) return new Response('{}',{status:401});
    assert.equal(options.headers.Authorization,'QQBot fixture-2');
    return new Response(JSON.stringify({url:'wss://fixture.qq.com'}));
  }
  assert.equal(url,'https://api.sgroup.qq.com/v2/users/offline-owner/messages');
  assert.equal(JSON.parse(options.body).content,'Offline fixture'); sends++;
  return new Response(JSON.stringify({id:'offline-message'}));
};
globalThis.WebSocket=class extends EventTarget {
  static OPEN=1; readyState=1;
  constructor(){super();setTimeout(()=>this.frame({op:10,d:{heartbeat_interval:30000}}),0);}
  frame(frame){this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(frame)}));}
  send(raw){const frame=JSON.parse(raw); if(frame.op===2){identified++;assert.equal(frame.d.token,'QQBot fixture-2');assert.equal(frame.d.intents,1<<25);setTimeout(()=>this.frame({op:0,t:'READY',s:1,d:{}}),0);}}
  close(){}
};
setTimeout(()=>process.exit(2),12000);
await main();
"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'owner-state.json').write_text(json.dumps({'owner':'offline-owner'}), encoding='utf-8')
            env = dict(os.environ, QQBOT_APP_ID='offline-app', QQBOT_APP_SECRET='offline-secret', QQBOT_PRIVATE_DIR=tmp, QQBOT_BOTS='[]')
            proc = subprocess.Popen(['node','--input-type=module','-e',script],cwd=Path(__file__).parent,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                proc.wait(timeout=18)
                stdout, stderr = proc.communicate()
                self.assertEqual(proc.returncode,0,stderr)
                self.assertIn('offline_gateway_passed',stdout)
            finally:
                if proc.poll() is None: proc.kill(); proc.communicate()

if __name__=='__main__': unittest.main()
