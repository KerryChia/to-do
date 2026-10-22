const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const U = require('./utils.js');
let count = 0;
function test(name, fn) { try { fn(); count++; console.log('PASS', name); } catch (e) { console.error('FAIL', name); throw e; } }
// Synthetic records are test-only and never loaded by the application.
const base = {id:'test-a',title:'测试任务',category:'Sample Course',priority:3,
  priorityDefault:false,due:'2026-09-09T12:00',remind:4320,duration:30,completed:false,
  notes:'Synthetic assignment',source:'sample/notice.txt',sourceNotes:'line:1 Fixture evidence',
  sourceTiming:'Date to confirm',kind:'assignment',firedKey:'',
  createdAt:'2026-09-09T00:00:00Z',updatedAt:'2026-09-09T00:00:00Z'};
function validate(tasks) {return U.validateStore({version:1,tasks});}
const now = new Date('2026-09-09T10:00+08:00');
const samples = [base,{...base,id:'past',due:'2026-09-09T09:00'}, {...base,id:'later',due:'2026-09-11T12:00'}, {...base,id:'none',due:''}, {...base,id:'done',completed:true}];
test('empty store is valid without bundled tasks',()=>{assert.deepEqual(validate([]).tasks,[]);validate(samples);});
test('synthetic undated defaults and task kinds remain valid',()=>{const tasks=['assignment','milestone','suggestion','manual'].map((kind,n)=>({...base,id:'fixture-'+n,kind,due:'',priorityDefault:true}));assert(validate(tasks).tasks.every(t=>!t.due&&t.priority===3&&t.priorityDefault));});
test('UI describes user imports without preloaded semester claims',()=>{const html=fs.readFileSync(path.join(__dirname,'index.html'),'utf8');assert(html.includes('任务与课程工作台'));assert(html.includes('导入自己的课表'));assert(!/已导入\s*20\d{2}|MY SEMESTER/.test(html));const app=fs.readFileSync(path.join(__dirname,'app.js'),'utf8');assert(!app.includes('taskOnlyCourses'));});
test('valid dates and leap day',()=>{assert(U.validDue(''));assert(U.validDue('2028-02-29T12:30'));assert(!U.validDue('2026-02-29T12:30'));assert(!U.validDue('2026-04-31T12:30'));assert(!U.validDue('2026-09-09'));assert(!U.validDue('2026-09-09T25:00'));});
test('today includes overdue today but not completed',()=>assert.deepEqual(U.filterTasks(samples,{scope:'today'},now).map(t=>t.id),['test-a','past']));
test('seven days excludes overdue',()=>assert.deepEqual(U.filterTasks(samples,{scope:'upcoming'},now).map(t=>t.id),['test-a','later']));
test('undated and completed filters',()=>{assert.equal(U.filterTasks(samples,{scope:'undated'},now)[0].id,'none');assert.equal(U.filterTasks(samples,{scope:'completed'},now)[0].id,'done');});
test('combined date category priority and source search',()=>{assert.equal(U.filterTasks(samples,{scope:'all',category:base.category,priority:'3',query:'unmatched query',day:'2026-09-09'},now).length,0);assert.equal(U.filterTasks(samples,{scope:'all',category:base.category,priority:'3',query:'notice.txt',day:'2026-09-09'},now).length,3);assert.equal(U.filterTasks(samples,{scope:'all',priority:'1',query:'notice.txt'},now).length,0);});
test('global stats meaningful',()=>assert.deepEqual(U.stats(samples,now),{overdue:1,upcoming:2,undated:1,completed:1,total:5}));
test('date sorting undated last; priority sort',()=>{assert.equal(U.sortTasks(samples.filter(t=>!t.completed),'due').at(-1).id,'none');assert.equal(U.sortTasks([{...base,priority:4},{...base,id:'urgent',priority:1}],'priority')[0].id,'urgent');});
test('strict store version duplicate and missing fields',()=>{assert.throws(()=>U.validateStore({version:2,tasks:[]}));assert.throws(()=>validate([base,base]));assert.throws(()=>validate([{id:'x'}]));assert.throws(()=>validate([{...base,id:'<img>'}]));});
test('reject malformed enums booleans dates and duration',()=>{for(const patch of [{priority:0},{completed:'false'},{remind:7},{duration:0},{duration:1.5},{due:'2026-02-30T10:00'},{kind:'evil'},{priorityDefault:'yes'}])assert.throws(()=>validate([{...base,...patch}]));});
test('reject oversized fields, invalid fired record and control injection',()=>{for(const patch of [{title:'x'.repeat(201)},{title:' '},{notes:'bad\u0000data'},{firedKey:'invalid'}])assert.throws(()=>validate([{...base,...patch}]));});
test('malicious title stays data and unknown fields stripped',()=>{const title='<img src=x onerror=alert(1)><script>alert(2)</script>';const t=validate([{...base,title,evil:'<script>'}]).tasks[0];assert.equal(t.title,title);assert(!('evil' in t));});
test('no prototype pollution on import',()=>{const obj=JSON.parse(JSON.stringify(base).replace('"title":"测试任务"','"title":"测试任务","__proto__":{"polluted":true}'));const t=validate([obj]).tasks[0];assert.equal(t.polluted,undefined);assert.equal({}.polluted,undefined);});
test('reminder boundary and persisted dedup',()=>{const t={...base,remind:30};assert(!U.reminderReady(t,new Date('2026-09-09T11:29+08:00')));assert(U.reminderReady(t,new Date('2026-09-09T11:30+08:00')));assert(!U.reminderReady({...t,firedKey:U.scheduleKey(t)},new Date('2026-09-09T11:31+08:00')));assert(!U.reminderReady({...t,completed:true},new Date('2026-09-09T11:31+08:00')));});
test('no old catchup after event end or disabled reminder',()=>{assert(!U.reminderReady(base,new Date('2026-09-09T12:31+08:00')));assert(!U.reminderReady({...base,remind:-1},new Date('2026-09-09T12:00+08:00')));assert(!U.reminderReady({...base,due:''},now));});
test('ICS excludes undated and completed, includes alarms',()=>{const ics=U.makeICS(samples,now);assert.equal((ics.match(/BEGIN:VEVENT/g)||[]).length,3);assert.equal((ics.match(/BEGIN:VALARM/g)||[]).length,3);assert(ics.includes('TRIGGER:-PT4320M'));assert(ics.endsWith('END:VCALENDAR\r\n'));});
test('ICS escaping cannot inject new calendar properties',()=>{const t={...base,title:'中文,分号;斜线\\\r\nBEGIN:VEVENT'};const ics=U.makeICS([t],now);assert.equal((ics.match(/\r\nBEGIN:VEVENT/g)||[]).length,1);assert(ics.includes('SUMMARY:中文\\,分号\\;斜线\\\\\\nBEGIN:VEVENT'));});
test('UTF8 line folding <=75 bytes preserves Unicode and CRLF',()=>{const str='SUMMARY:'+('多字节中文😀,;'.repeat(50));const folded=U.foldICS(str);for(const line of folded.split('\r\n'))assert(Buffer.byteLength(line,'utf8')<=75);assert.equal(folded.replace(/\r\n /g,''),str);});
test('ICS duration UTC stable UID and disabled alarm',()=>{const a=U.makeICS([{...base,remind:-1,duration:45}],now);assert(!a.includes('BEGIN:VALARM'));assert(a.includes('UID:test-a@local-task-calendar'));const lines=a.split('\r\n');const parse=s=>Date.parse(s.slice(0,4)+'-'+s.slice(4,6)+'-'+s.slice(6,8)+'T'+s.slice(9,11)+':'+s.slice(11,13)+':'+s.slice(13,15)+'Z');assert.equal(parse(lines.find(s=>s.startsWith('DTEND:')).slice(6))-parse(lines.find(s=>s.startsWith('DTSTART:')).slice(8)),45*60000);});
test('zero-minute alarm and long escaped source descriptions',()=>{const ics=U.makeICS([{...base,remind:0,sourceNotes:'资料来源'.repeat(100)}],now);assert(ics.includes('TRIGGER:PT0M'));for(const line of ics.split('\r\n'))assert(Buffer.byteLength(line)<=75);});
test('app uses safe text DOM APIs, no HTML injection sinks or remote URLs',()=>{const app=fs.readFileSync(path.join(__dirname,'app.js'),'utf8');assert(!/innerHTML|outerHTML|insertAdjacentHTML|\beval\s*\(/.test(app));const html=fs.readFileSync(path.join(__dirname,'index.html'),'utf8');assert(!/<(?:script|link)[^>]*(?:src|href)="https?:/.test(html));});
test('day reminder options and explicit legacy conversion',()=>{assert.deepEqual(U.REMINDERS,[-1,0,1440,2880,4320,7200,10080,20160,43200]);for(const n of [5,15,30,60])assert.equal(validate([{...base,remind:n}]).tasks[0].remind,1440);assert.equal(U.reminderLabel(4320),'提前 3 天提醒');});
test('bounded escalation labels skip stages before selected lead time',()=>{assert.deepEqual(U.escalationLabels(-1),[]);assert.equal(U.escalationLabels(1440).length,5);assert.equal(U.escalationLabels(4320).length,6);assert.equal(U.escalationLabels(43200).length,7);assert.equal(U.escalationLabels(0)[0],'当天提醒');});
test('backend API-first and no silent local writes or browser notifications',()=>{const app=fs.readFileSync(path.join(__dirname,'app.js'),'utf8');assert(!app.includes('localStorage.setItem'));assert(!app.includes('new Notification'));assert(app.includes("headers['X-Planner-Token']=token"));assert(app.includes('expectedUpdatedAt'));assert(app.includes('expectedRevision'));assert(app.includes("location.protocol==='file:'"));assert(app.includes('setInterval(()=>sync(),30000)'));});
test('Beijing timezone fixed for comparisons and ICS regardless of device TZ',()=>{assert.equal(U.parseDue('2026-09-09T12:00').toISOString(),'2026-09-09T04:00:00.000Z');assert.equal(U.chinaDayKey(new Date('2026-09-08T17:00:00Z')),'2026-09-09');assert(U.makeICS([base],now).includes('DTSTART:20260909T040000Z'));assert(U.validDue('2026-03-08T02:30'));});
test('two QQ bots normalize independently and ignore aggregate unavailable flags',()=>{const bots=U.qqBots({bound:false,gatewayReady:false,bots:[{slot:'bot-1',configured:true,bound:false,gatewayReady:true},{slot:'bot-2',configured:true,bound:false,gatewayReady:false}]});assert.equal(bots.length,2);assert(bots[0].configured&&bots[0].gatewayReady&&!bots[0].bound);assert(!bots[1].gatewayReady);assert.equal(U.qqBots(null)[1].slot,'bot-2');});
test('QQ test results label selected bot and distinguish partial failure',()=>{const result={ok:false,status:'partial_failure',results:[{slot:'bot-1',ok:true,status:'sent'},{slot:'bot-2',ok:false,status:'rejected'}]};assert(U.qqTestResult(result,'bot-1').includes('平台已受理'));assert(U.qqTestResult(result,'bot-2').includes('未确认'));assert(U.qqTestResult({ok:true,status:'sent'},'bot-1').includes('平台已受理'));});
console.log(`\n${count} tests passed.`);

// Minimal DOM test double exercises API ordering without opening a browser.
async function apiContractTests() {
  class Node {
    constructor(){this.children=[];this.dataset={};this.listeners={};this.value='';this.open=false;this.disabled=false;this.textContent='';}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){this.children=[...nodes];}
    setAttribute(){}
    addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
    focus(){}
    reset(){for(const n of Object.values(fields))n.value='';}
    reportValidity(){return true;}
    showModal(){this.open=true;}
    close(){this.open=false;}
    async fire(type){for(const fn of this.listeners[type]||[])await fn({currentTarget:this,target:this,preventDefault(){}});}
  }
  let nodes={},fields={};
  function buildDom(){
    nodes={};fields={};
    const html=fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
    for(const m of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)){
      const n=nodes[m[1]]=new Node();n.id=m[1];
      n.hidden=/\bhidden(?:\s|>)/.test(m[0]);
      const page=m[0].match(/data-page-name="([^"]+)"/);
      if(page)n.dataset.pageName=page[1];
    }
    for(const name of ['title','category','customCategory','priority','due','remind','duration','notes'])fields[name]=new Node();
    nodes['task-form'].elements={namedItem:name=>fields[name]};
  }
  let db={version:1,revision:1,tasks:[{...base,remind:4320}]}, calls=[],pending=null,fail=false,interval;
  function resetState(){
    db={version:1,revision:1,tasks:[{...base,remind:4320}]};calls=[];pending=null;fail=false;interval=undefined;
  }
  const response=data=>({ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(data))});
  const ACCESS_GOOD='contract-access-pass';
  const storage={};
  const fetchBase=async(url,options)=>{
    if(url.endsWith('/health'))return response({ok:true,accessRequired:false});
    if(url.endsWith('/session'))return response({token:'test-token-memory-only'});
    if(url.endsWith('/schedule'))return response({events:[{id:'class-test',title:'Sample Course',category:base.category,start:U.chinaDayKey()+'T14:00',end:U.chinaDayKey()+'T16:30',location:'Sample Room'}],courses:[base.category],resources:{[base.category]:[{label:'课程平台',url:'https://example.test/course'}]}});
    if(url.endsWith('/qq/status'))return response({configured:true,bound:false,gatewayReady:false,bots:[{slot:'bot-1',label:'机器人1',configured:true,bound:false,gatewayReady:true},{slot:'bot-2',label:'机器人2',configured:true,bound:false,gatewayReady:false}]});
    if(url.endsWith('/qq/pairing')){assert.equal(JSON.parse(options.body).bot,'bot-1');assert.equal(options.headers['X-Planner-Token'],'test-token-memory-only');return response({ok:true,slot:'bot-1',code:'TEST-ONLY-CODE',expiresAt:'2026-09-09T12:00:00+08:00'});}
    if(options.method==='GET')return response(db);
    assert.equal(options.headers['X-Planner-Token'],'test-token-memory-only');
    if(fail==='network')throw Error('connection lost');
    if(fail)return {ok:false,status:typeof fail==='number'?fail:409,json:async()=>({})};
    return new Promise(resolve=>{pending=()=>{const task=JSON.parse(options.body);db={version:1,revision:2,tasks:[...db.tasks,task]};resolve(response({task,revision:2}));};});
  };
  // Cookie-gated test double mirrors the real HTTP tests in tests_api.py.
  let serverPassword=ACCESS_GOOD, cookiePassword='',navigated=false, staleToken=false;
  const fetchGated=async(url,options)=>{
    assert.equal(options.credentials,'same-origin');
    assert.equal(options.headers['X-Planner-Access'],undefined);
    if(url.endsWith('/session')&&options.method==='POST'){
      assert.equal(options.headers['X-Planner-Token'],undefined);
      if(JSON.parse(options.body).accessPassword!==serverPassword)return {ok:false,status:401,json:async()=>({error:'invalid_credentials'})};
      cookiePassword=serverPassword;return response({token:'test-token-memory-only'});
    }
    if(cookiePassword!==serverPassword)return {ok:false,status:401,json:async()=>({error:'access_required'})};
    if(url.endsWith('/logout')){cookiePassword='';return response({ok:true});}
    if(staleToken&&options.method!=='GET'){staleToken=false;return {ok:false,status:403,json:async()=>({error:'invalid_token'})};}
    return fetchBase(url,options);
  };
  function makeContext(fetchImpl,promptImpl,pathname='/tasks') {
    const record=async(url,options)=>{calls.push({url,...options});return fetchImpl(url,options);};
    const context={window:{TaskUtils:U},document:{getElementById:id=>nodes[id],createElement:()=>new Node(),querySelectorAll:selector=>selector==='.page'?Object.values(nodes).filter(n=>n.dataset.pageName):[],querySelector:()=>null,addEventListener(){}},location:{protocol:'http:',pathname,search:'',replace:()=>{navigated=true;}},URLSearchParams,localStorage:{getItem:k=>{if(k==='legacy-guard')throw Error('Unexpected legacy read');return storage[k]??null;},setItem:(k,v)=>{storage[k]=String(v);},removeItem:k=>{delete storage[k];}},Date,Intl,console,AbortController,setTimeout:(fn,ms)=>ms===60000?0:setTimeout(fn,ms),clearTimeout,setInterval:fn=>{interval=fn;},crypto:{randomUUID:()=> 'contract-test-id'},fetch:record,prompt:promptImpl};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'app.js'),'utf8'),context);
    return context;
  }
  const settle=()=>new Promise(resolve=>setImmediate(resolve));
  for(const [route,page] of [['/','home'],['/tasks','tasks'],['/calendar','calendar'],['/qq','qq'],['/data','data']]){
    buildDom();resetState();cookiePassword=serverPassword;
    assert.equal(nodes['planner-page'].hidden,true,'HTML starts with protected shell hidden');
    makeContext(fetchGated,()=>{throw Error('no password prompt with valid cookie');},route);
    await settle();await settle();
    assert.equal(nodes['planner-page'].hidden,false,route+' restores visible shell');
    assert.equal(nodes['login-page'].hidden,true,route+' hides login after session recovery');
    assert.deepEqual(Object.values(nodes).filter(n=>n.dataset.pageName&&!n.hidden).map(n=>n.dataset.pageName),[page]);
    if(page==='tasks')assert.equal(nodes['task-list'].children.length,1);
    if(page==='calendar'){assert.equal(nodes['calendar-page-grid'].children.length,7);assert(nodes['calendar-count'].textContent.includes('1 节课'));}
    if(page==='qq')assert.equal(nodes['qq-bots'].children.length,2);
  }
  cookiePassword='';
  console.log('PASS blank-screen regression: all five routes restore visible content from existing cookie');
  buildDom();resetState();
  const importedOnlyCourse='Sample Course Without Tasks';
  makeContext(async(url,options)=>{
    if(url.endsWith('/tasks'))return response({version:1,revision:0,tasks:[]});
    if(url.endsWith('/schedule'))return response({events:[],courses:[importedOnlyCourse],resources:{}});
    return fetchBase(url,options);
  },()=>{throw Error('no prompt');},'/tasks');
  await settle();await settle();
  const texts=n=>[n.textContent,...n.children.filter(x=>x&&typeof x==='object').flatMap(texts)];
  assert(texts(nodes.categories).includes(importedOnlyCourse),'imported courses remain available without tasks');
  assert.equal(nodes['category-total'].textContent,2);
  assert(texts(nodes['task-list']).includes('这里暂时没有任务'),'empty release has a usable empty state');
  assert.equal(nodes['course-info'].hidden,true,'no initial course resources');
  await nodes['add-task'].fire('click');assert(nodes['task-dialog'].open);
  assert(nodes['task-category'].children.some(n=>n.value===importedOnlyCourse));
  console.log('PASS synthetic empty startup and imported courses without tasks');
  // QQ bot cards only render on the /qq page: use that pathname for the contract test.
  buildDom();resetState();
  makeContext(fetchBase,()=>{throw Error('prompt must not run in local mode');},'/qq');
  await settle();await settle();
  assert.equal(nodes['qq-bots'].children.length,2);
  const walk=n=>[n,...n.children.filter(x=>x&&typeof x==='object').flatMap(walk)];
  const pair=walk(nodes['qq-bots']).find(n=>n.dataset.qqSlot==='bot-1'&&n.dataset.qqAction==='pairing');
  const pairing=pair.fire('click');await settle();nodes['confirm-dialog'].returnValue='yes';await nodes['confirm-dialog'].fire('close');await pairing;
  const pairText=walk(nodes['qq-bots']).map(n=>n.textContent).join(' ');assert(pairText.includes('仅用于机器人1'));assert(pairText.includes('/bind TEST-ONLY-CODE'));
  await nodes['add-task'].fire('click');
  assert(nodes['task-dialog'].open);
  assert.equal(fields.remind.value,4320);
  fields.title.value='API pending write';fields.customCategory.value='';fields.notes.value='';
  const saving=nodes['task-form'].fire('submit');await settle();
  // On /qq the task list is not rendered; the dialog staying open is the optimistic-append guard.
  assert(nodes['task-dialog'].open,'must not optimistically close before write confirmation');
  assert(nodes['task-dialog'].open,'must not close before write confirmation');
  pending();await saving;
  assert(!nodes['task-dialog'].open,'closes after write confirmation');
  await nodes['add-task'].fire('click');fields.title.value='Keep my unsaved draft';
  db={...db,revision:3};await interval();
  assert.equal(fields.title.value,'Keep my unsaved draft','poll must preserve draft');
  fail=true;await nodes['task-form'].fire('submit');
  assert(nodes['task-dialog'].open);
  assert(nodes['form-error'].textContent.includes('更新'));
  assert.equal(calls.filter(c=>c.url.endsWith('/tasks')&&c.method==='POST').length,2);
  for(const failure of [503,'network']){
    fail=false;await interval();fail=failure;
    const before=calls.filter(c=>c.url.endsWith('/tasks')&&c.method==='POST').length;
    await nodes['task-form'].fire('submit');
    assert.equal(calls.filter(c=>c.url.endsWith('/tasks')&&c.method==='POST').length,before+1,'uncertain writes never automatically replay');
    assert.equal(fields.title.value,'Keep my unsaved draft');
  }
  console.log('PASS API contract: load, token, pending-write ordering, draft preservation, conflict, independent bot-1 pairing');

  buildDom();resetState();storage['semester-task-calendar:access']='obsolete-secret';
  makeContext(fetchGated,()=>{throw Error('prompt is forbidden');},'/tasks');
  await settle();await settle();
  assert.equal(storage['semester-task-calendar:access'],undefined,'legacy password removed');
  assert.equal(nodes['login-page'].hidden,false);assert.equal(nodes['planner-page'].hidden,true);
  const before=calls.length;await interval();await interval();
  assert.equal(calls.length,before,'logged-out polling does not request credentials');
  nodes['login-username'].value='planner';nodes['login-password'].value='wrong';
  await nodes['login-form'].fire('submit');assert(nodes['login-error'].textContent.includes('不正确'));
  nodes['login-password'].value=ACCESS_GOOD;
  await nodes['login-form'].fire('submit');assert(navigated,'successful login navigates');
  assert.equal(storage['semester-task-calendar:access'],undefined);
  await interval();assert.equal(nodes['task-list'].children.length,1);
  await nodes['add-task'].fire('click');fields.title.value='Draft across reauthentication';
  serverPassword='rotated-pass';await interval();assert.equal(nodes['login-page'].hidden,false);
  assert.equal(fields.title.value,'Draft across reauthentication');
  nodes['login-password'].value=serverPassword;await nodes['login-form'].fire('submit');
  assert(nodes['task-dialog'].open);assert.equal(fields.title.value,'Draft across reauthentication');
  staleToken=true;fail=true;
  const writes=calls.filter(c=>c.method==='POST'&&c.url.endsWith('/tasks')).length;
  await nodes['task-form'].fire('submit');
  assert.equal(calls.filter(c=>c.method==='POST'&&c.url.endsWith('/tasks')).length,writes+2,'only explicit invalid_token retries once');
  assert.equal(fields.title.value,'Draft across reauthentication');
  await nodes['access-logout'].fire('click');assert.equal(nodes['login-page'].hidden,false);assert.equal(cookiePassword,'');
  console.log('PASS cookie login: form, no prompt/storage, navigation, polling pause, rotation, draft, CSRF retry and logout');
  const html=fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
  assert(html.includes('autocomplete="username"'));assert(html.includes('autocomplete="current-password"'));
  assert(!html.includes('seed-data.js'));
  assert(!fs.readFileSync(path.join(__dirname,'app.js'),'utf8').includes('prompt'));

}
apiContractTests().catch(error=>{console.error(error);process.exitCode=1;});
