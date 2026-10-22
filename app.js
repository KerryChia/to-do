/* All user and imported text is rendered with textContent, never HTML. */
(() => {
  'use strict';
  const U = window.TaskUtils, $ = id => document.getElementById(id);
  const KEY = 'semester-task-calendar:v1';
  const priorities = {1:'紧急',2:'重要',3:'普通',4:'低优先级'};
  const kinds = {assignment:'资料任务',milestone:'已宣布的课程规划',suggestion:'建议任务（根据文件生成）',manual:'手动添加'};
  const scopeMeta = [['active','待办清单'],['today','今天'],['upcoming','未来七天'],['undated','待定日期'],['completed','已完成'],['all','全部任务'],['overdue','已逾期']];
  // Page router: one HTML shell, section per pathname. Server maps / /tasks /calendar /qq /data to this file.
  const PAGES = ['home','tasks','calendar','qq','data'];
  let currentPage = (PAGES.find(p=>location.pathname==='/'+p)||(location.pathname==='/'?'home':'home'));
  const scopeFromQuery = () => { const m=new URLSearchParams(location.search).get('scope'); return scopeMeta.some(([id])=>id===m)?m:'active'; };
  let store={version:1,tasks:[]}, revision=0, token='', connected=false, loaded=false, qqState=null, busy=false, syncing=false, editingId=null, editRevision='', importBusy=false;
  // Remove the obsolete plaintext-password key once; passwords are never stored client-side.
  try{globalThis.localStorage?.removeItem('semester-task-calendar:access');}catch(err){/* storage disabled */}
  let view = 'list', scope = scopeFromQuery(), category = new URLSearchParams(location.search).get('category')||'', query = '', priority = '', sort = 'due', selectedDay = U.chinaDayKey();
  let courseData={events:[],courses:[],resources:{}};
  function mondayOf(date){const d=new Date(date.getFullYear(),date.getMonth(),date.getDate());d.setDate(d.getDate()-((d.getDay()+6)%7));return d;}
  let weekStart = mondayOf(U.chinaNow());
  function el(tag, className, text) { const n=document.createElement(tag); if(className)n.className=className; if(text!==undefined)n.textContent=text; return n; }
  function button(text, action, cls='') { const n=el('button',cls,text); n.type='button'; n.addEventListener('click',action); return n; }
  function announce(text) {
    const target = $('page-message');
    if (target) { target.textContent = text; if(typeof target.scrollIntoView==='function')target.scrollIntoView({block:'nearest'}); }
    if ($('live-message')) $('live-message').textContent = text;
  }
  function pageError(slotId, message) { const slot=$(slotId); if(!slot)return; slot.replaceChildren(); if(!message)return; const banner=el('div','banner error'); banner.setAttribute('role','alert'); banner.textContent=message; slot.append(banner); }
  function showLogin(message='请登录后查看日历。'){
    token='';loginRequired=true;setConnection(false,message);
    $('login-page').hidden=false;$('planner-page').hidden=true;
    $('login-error').textContent=message;
    if($('task-dialog').open){suspendedDraft=true;$('task-dialog').close();}
    if($('confirm-dialog').open)$('confirm-dialog').close();
  }
  let loginRequired=false, loginBusy=false, suspendedDraft=false;
  const qqMessages={}, qqMessageVersions={};
  function setConnection(ok, message='') {
    connected=ok;
    for(const id of ['home-error-slot','tasks-error-slot','calendar-error-slot','qq-error-slot','data-error-slot'])pageError(id, ok?'':(message||'后端暂不可用，已暂停修改。'));
    $('storage-error').hidden=ok;
    $('storage-error').textContent=message || '后端暂不可用，已暂停修改。';
    $('local-status').textContent=ok?'后端已连接 · 统一保存':'后端未连接 · 只读';
    $('sync-status').textContent=ok?`数据刷新时间：${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`:'数据刷新失败 · 修改已暂停';
    updateWriteControls();
  }
  function updateWriteControls() {
    document.querySelectorAll('[data-write]').forEach(n=>{n.disabled=!connected||busy;});
    $('sync-retry').disabled=syncing||busy;
    document.querySelectorAll('[data-qq-slot]').forEach(n=>{const bot=U.qqBots(qqState).find(b=>b.slot===n.dataset.qqSlot);n.disabled=!connected||busy||!bot.configured||!bot.gatewayReady||(n.dataset.qqAction==='pairing'?bot.bound:!bot.bound);});
  }
  async function api(path, method='GET', body, refreshed=false) {
    if(location.protocol==='file:')throw new Error('双击模式不支持后端编辑，请先运行 python server.py，再打开 http://127.0.0.1:8766/。');
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),12000);
    try {
      const headers={Accept:'application/json'};
      if(method!=='GET'){headers['Content-Type']='application/json';if(path!=='/session')headers['X-Planner-Token']=token;}
      const response=await fetch('/api/v1'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal,cache:'no-store',credentials:'same-origin'});
      const data=await response.json().catch(()=>null);
      if(!response.ok){
        if(response.status===401){showLogin(path==='/session'&&method==='POST'?'账号或口令不正确。':'登录已过期，请重新登录。');}
        if(response.status===403&&data?.error==='invalid_token'&&!refreshed){
          token=(await api('/session')).token;
          return api(path,method,body,true);
        }
        const message=response.status===409?'任务已被其他来源更新，请保留你的输入，关闭编辑后同步并重新编辑。':response.status===401?'请登录后重试。':response.status===403?'请求被安全校验拒绝，请刷新页面后重试。':response.status===429?'登录尝试过多，请一分钟后再试。':response.status>=500?'后端暂时故障，请同步核对后再试。':'请求未成功，请检查输入后重试。';
        throw Object.assign(new Error(message),{status:response.status});
      }
      if(!data||typeof data!=='object')throw new Error('后端返回格式无效。');
      return data;
    } finally {clearTimeout(timer);}
  }
  function receiveSnapshot(data) {
    if(!Number.isInteger(data.revision)||data.revision<0)throw new Error('后端版本号无效');
    const next=U.validateStore(data);
    store=next;revision=data.revision;loaded=true;
    if($('task-dialog').open&&editingId){const current=store.tasks.find(t=>t.id===editingId);if(!current||current.updatedAt!==editRevision)$('form-error').textContent='此任务已更新或删除。当前输入已保留，请复制需要的内容，关闭后重新编辑最新版本。';}
  }
  async function sync(manual=false) {
    if(syncing||busy||loginRequired)return;
    syncing=true;updateWriteControls();
    try {
      if(!token){
        const session=await api('/session');
        if(typeof session.token!=='string'||!session.token)throw new Error('后端会话无效');
        token=session.token;
      }
      // Cookie session accepted: this device is logged in, so the shell must be
      // unhidden here too — the login form is the only other place that does it.
      $('login-page').hidden=true;$('planner-page').hidden=false;
      receiveSnapshot(await api('/tasks'));
      try{const data=await api('/schedule');courseData={events:Array.isArray(data.events)?data.events:[],courses:Array.isArray(data.courses)?data.courses:[],resources:data.resources||{}};}catch(err){if(err.status===401)throw err;announce('课表暂时无法加载，任务数据仍可使用。');}
      setConnection(true);render();await refreshQQ();
      if(manual)announce('已从后端同步，未改动正在编辑的输入。');
    } catch(err){
      if(err.status!==401)setConnection(false,err.message+' 已保留当前页面数据。');
    }
    finally {syncing=false;updateWriteControls();}
  }
  function mayWrite(){if(!connected||busy||syncing){announce('后端未就绪或正在同步，请稍后点击“重试 / 同步”。');return false;}return true;}
  async function write(path,method,body,onSuccess) {
    if(!mayWrite())return false;
    busy=true;updateWriteControls();
    try {const result=await api(path,method,body);await onSuccess(result);setConnection(true);render();return true;}
    catch(err){if(!err.status||err.status===401||err.status===403||err.status>=500)setConnection(false,'写入未获成功确认。请先同步核对结果，避免重复新建。当前表单输入仍保留。');const text=err.message;if($('task-dialog').open)$('form-error').textContent=text;else announce(text);return false;}
    finally {busy=false;updateWriteControls();}
  }
  function receiveTask(result) {
    if(!Number.isInteger(result.revision))throw new Error('后端版本号无效');
    const task=U.validateStore({version:1,tasks:[result.task]}).tasks[0];
    store.tasks=store.tasks.some(t=>t.id===task.id)?store.tasks.map(t=>t.id===task.id?task:t):[...store.tasks,task];revision=result.revision;
  }
  function allCategories() { return [...new Set([...['未分类'],...courseData.courses,...store.tasks.map(t=>t.category)])]; }
  function filtered(day='') { return U.sortTasks(U.filterTasks(store.tasks,{scope,category,query,priority,day}),sort); }
  function renderNavCounts() {
    const s=U.stats(store.tasks);
    const countTargets={tasks:'home-task-count'};
    const activeCount=U.filterTasks(store.tasks,{scope:'active'}).length;
    const datedCount=store.tasks.filter(t=>t.due&&!t.completed).length;
    for(const [page,count] of Object.entries({tasks:activeCount,calendar:datedCount})){const n=document.querySelector(`.page-link[data-page="${page}"] .nav-count`);if(n)n.textContent=String(count);}
    $('category-total').textContent=allCategories().length;
    const qq=$('home-qq-summary');
    if(qq&&qqState){const bots=U.qqBots(qqState);qq.textContent=`已绑定 ${bots.filter(b=>b.bound).length} / 2 台机器人`;}
    const data=$('home-data-summary');if(data)data.textContent=`共 ${s.total} 件任务 · 版本 ${revision}`;
    const cats=$('categories');cats.replaceChildren();
    const navCategories=allCategories();
    $('category-total').textContent=navCategories.length;
    for(const name of ['',...navCategories]) {
      const a=el('a','page-link category-link');a.href='/tasks'+(name?'?category='+encodeURIComponent(name):'');
      if(currentPage==='tasks'&&category===name)a.setAttribute('aria-current','true');
      const label=el('span','nav-name');label.append(el('span','category-dot'),el('span','',name||'所有课程与项目'));
      a.append(label,el('span','nav-count',store.tasks.filter(t=>!t.completed&&(!name||t.category===name)).length));cats.append(a);
    }
  }
  function renderStats(target) {
    const s=U.stats(store.tasks);target.replaceChildren();
    for(const [id,label,count,help,symbol] of [['overdue','已逾期',s.overdue,'未完成 · 已过确认时间','↘'],['upcoming','未来七天',s.upcoming,'未完成 · 从现在起七天','↗'],['undated','待定日期',s.undated,'先核对，再设定提醒','○'],['completed','已完成',s.completed,`${s.total?Math.round(s.completed/s.total*100):0}% 完成率 · 共 ${s.total} 件`,'✓']]) {
      const a=el('a','stat '+id);a.href='/tasks?scope='+id;
      const labelNode=el('span','stat-label',label);labelNode.append(el('span','stat-symbol',symbol));
      a.append(labelNode,el('strong','',String(count).padStart(2,'0')),el('small','',help));target.append(a);
    }
  }
  function dateLabel(t) {
    if(!t.due)return '待定日期 · 待确认';
    return `${t.due.slice(0,4)}年${Number(t.due.slice(5,7))}月${Number(t.due.slice(8,10))}日 ${t.due.slice(11)} 北京时间${!t.completed&&U.parseDue(t.due)<new Date()?' · 已逾期':''}`;
  }
  function taskCard(t) {
    const article=el('article','task-card'+(t.completed?' is-completed':''));
    const check=button(t.completed?'✓':'',()=>toggleTask(t.id),'check-task');check.setAttribute('aria-label',(t.completed?'重新打开：':'标为完成：')+t.title);check.setAttribute('aria-pressed',String(t.completed));check.dataset.write='';
    const content=el('div','task-content'),top=el('div','task-top');
    top.append(el('h3','task-title',t.title),el('span','priority p'+t.priority,priorities[t.priority]+(t.priorityDefault?' · 默认':'')));
    const meta=el('div','task-meta');meta.append(el('span','category-tag',t.category),el('span','due-label'+(!t.due?' unknown':(!t.completed&&U.parseDue(t.due)<new Date()?' late':'')),dateLabel(t)),el('span','kind-label',kinds[t.kind]));
    if(t.due&&t.remind>=0)meta.append(el('span','',U.reminderLabel(t.remind)));
    content.append(top,meta);
    if(t.notes)content.append(el('p','task-note',t.notes));
    const bottom=el('div','task-bottom'),source=el('details','task-source');
    const summary=el('summary','',t.sourceTiming&&!t.due?'查看来源与待确认时间':'查看来源记录');source.append(summary);
    if(t.sourceTiming)source.append(el('p','timing','时间线索：'+t.sourceTiming));
    source.append(el('p','',t.source||'手动添加，无外部文件来源'));
    if(t.sourceNotes)source.append(el('p','',t.sourceNotes));
    const actions=el('div','task-actions');
    if(t.completed){const reopen=button('撤回完成',()=>toggleTask(t.id));reopen.dataset.write='';reopen.setAttribute('aria-label','撤回完成：'+t.title);actions.append(reopen);}
    const edit=button('编辑',()=>openTask(t.id));edit.setAttribute('aria-label','编辑：'+t.title);
    const del=button('删除',()=>deleteTask(t.id));del.dataset.action='delete';del.setAttribute('aria-label','删除：'+t.title);
    edit.dataset.write='';del.dataset.write='';actions.append(edit,del);bottom.append(source,actions);content.append(bottom);article.append(check,content);return article;
  }
  function renderTaskList(items, target, emptyTitle, emptyHint, emptyAction) {
    target.replaceChildren();
    if(!items.length) {
      const empty=el('div','empty');empty.append(el('span','empty-mark','✓'),el('h3','',emptyTitle),el('p','',emptyHint),button(emptyAction,()=>openTask(),'primary'));target.append(empty);return;
    }
    for(const t of items)target.append(taskCard(t));
  }
  function renderWeekGrid(items, grid, start=weekStart) {
    const end=new Date(start);end.setDate(end.getDate()+6);
    const titleEl=grid.id==='calendar-page-grid'?$('calendar-month-title'):$('month-title');
    titleEl.textContent=`${start.getFullYear()}年${start.getMonth()+1}月${start.getDate()}日 — ${end.getMonth()+1}月${end.getDate()}日`;
    grid.replaceChildren();const today=U.chinaDayKey();
    for(let i=0;i<7;i++){
      const date=new Date(start);date.setDate(start.getDate()+i);const key=U.dayKey(date);
      const entries=items.filter(t=>t.due&&t.due.slice(0,10)===key).sort((a,b)=>a.due.localeCompare(b.due));
      const column=el('section','week-day'+(key===today?' today':''));
      const head=el('header','week-day-head');head.append(el('strong','',`${date.getMonth()+1}/${date.getDate()}`),el('span','',`周${'一二三四五六日'[i]}`));column.append(head);
      const body=el('div','week-day-body');
      for(const item of entries){
        if(item.isCourse){const event=el('a','week-event course-event');event.href='/tasks?category='+encodeURIComponent(item.category);event.append(el('time','',item.start.slice(11)+'–'+item.end.slice(11)),el('strong','',item.title),el('span','',item.location));body.append(event);}
        else{const event=el('button','week-event task-event'+(item.completed?' done':''));event.append(el('time','',item.due.slice(11)),el('strong','',item.title),el('span','',item.category));event.addEventListener('click',()=>openTask(item.id));event.setAttribute('aria-label',(item.completed?'已完成任务：':'截止任务：')+item.title);body.append(event);}
      }
      if(!entries.length)body.append(el('span','week-empty','无安排'));
      const add=button('＋ 添加任务',()=>openTask(null,key),'week-add');body.append(add);column.append(body);grid.append(column);
    }
  }
  function render() {
    for(const section of document.querySelectorAll('.page'))section.hidden=section.dataset.pageName!==currentPage;
    for(const link of document.querySelectorAll('.page-link[data-page]'))link.setAttribute('aria-current',String(link.dataset.page===currentPage));
    document.title = ({home:'待办日历 · 把重要的事，安排好。',tasks:'任务清单 · 待办日历',calendar:'周历 · 待办日历',qq:'QQ 提醒设置 · 待办日历',data:'数据与导入 · 待办日历'})[currentPage]||'待办日历';
    $('today-label').textContent=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long',timeZone:'Asia/Shanghai'}).format(new Date());
    if(currentPage==='home') {
      renderStats($('stats'));
      $('home-task-count').textContent=store.tasks.length?`${U.filterTasks(store.tasks,{scope:'active'}).length} 件待办`:'—';
      $('home-dated-count').textContent=loaded?`${store.tasks.filter(t=>t.due&&!t.completed).length} 件已定日期`:'—';
    }
    if(currentPage==='tasks') {
      const items=filtered();
      const label=(scopeMeta.find(([id])=>id===scope)||['','待办清单'])[1];
      $('view-title').textContent=category||label;
      $('scope-filter').value=scope;
      renderCourseInfo();
      $('active-scope-label').textContent=`${items.length} 件${scope==='completed'?'已完成':'匹配任务'}${view==='calendar'?` · ${items.filter(t=>t.due).length} 件已定日期`:''}`;
      $('active-filter').textContent=[category&&'分类：'+category,scope!=='active'&&'范围：'+label,priority&&'优先级：'+priorities[priority],query&&'搜索：'+query].filter(Boolean).join(' / ');
      $('view-list').setAttribute('aria-pressed',String(view==='list'));$('view-calendar').setAttribute('aria-pressed',String(view==='calendar'));$('calendar-area').hidden=view!=='calendar';
      if(view==='calendar') {
        renderWeekGrid(items,$('calendar-grid'));
        $('day-heading').replaceChildren();$('task-list').replaceChildren();
      } else {
        renderTaskList(items,$('task-list'),'这里暂时没有任务','试试调整筛选，或者把下一件重要的事记下来。未安排日期的任务在“待定日期”中。','＋ 新建任务');
      }
    }
    if(currentPage==='calendar') {
      const classes=courseData.events.map(e=>({...e,due:e.start,isCourse:true,completed:false}));
      const items=[...store.tasks.filter(t=>t.due),...classes];
      const end=new Date(weekStart);end.setDate(end.getDate()+6);
      const inWeek=x=>x.due.slice(0,10)>=U.dayKey(weekStart)&&x.due.slice(0,10)<=U.dayKey(end);
      $('calendar-count').textContent=`本周 ${classes.filter(inWeek).length} 节课 · ${store.tasks.filter(t=>t.due&&inWeek(t)).length} 件截止任务`;
      renderWeekGrid(items,$('calendar-page-grid'));
    }
    if(currentPage==='qq')renderQQ();
    $('export-json').disabled=!loaded;$('export-ics').disabled=!loaded;updateWriteControls();
    renderNavCounts();
  }
  function renderCourseInfo(){
    const info=$('course-info'),resources=courseData.resources[category]||[];
    info.hidden=!category||!resources.length;if(info.hidden)return;
    const links=$('course-resources');links.replaceChildren();
    for(const resource of resources){try{const url=new URL(resource.url);if(!['http:','https:'].includes(url.protocol))continue;const a=el('a','resource-link',resource.label);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';links.append(a);}catch(err){}}
  }
  function syncFilters() { if($('scope-filter'))$('scope-filter').value=scope;if($('search'))$('search').value=query;if($('priority-filter'))$('priority-filter').value=priority;if($('sort'))$('sort').value=sort; }
  function confirmAction(title,text) {
    const d=$('confirm-dialog');$('confirm-title').textContent=title;$('confirm-text').textContent=text;d.returnValue='';
    return new Promise(resolve=>{d.addEventListener('close',()=>resolve(d.returnValue==='yes'),{once:true});d.showModal();});
  }
  async function toggleTask(id) {
    if(!mayWrite())return;const t=store.tasks.find(t=>t.id===id);if(!t)return;
    await write('/tasks/'+encodeURIComponent(id),'PATCH',{changes:{completed:!t.completed},expectedUpdatedAt:t.updatedAt},result=>{receiveTask(result);announce(t.completed?'任务已重新打开，已保存到后端。':'已完成，后续 QQ 阶段提醒将停止。');});
  }
  async function deleteTask(id) {
    if(!mayWrite())return;const t=store.tasks.find(t=>t.id===id);if(!t)return;
    if(await confirmAction('删除这件任务？',`“${t.title}”将从后端移除，原始资料不变。建议先导出备份。`))await write('/tasks/'+encodeURIComponent(id),'DELETE',{expectedUpdatedAt:t.updatedAt},result=>{if(!Number.isInteger(result.revision))throw new Error('后端版本号无效');store.tasks=store.tasks.filter(t=>t.id!==id);revision=result.revision;announce('任务已从后端删除。');});
  }
  function openTask(id=null,day='') {
    if($('task-dialog').open||!mayWrite())return;
    const t=id?store.tasks.find(x=>x.id===id):null;if(id&&!t)return;
    editingId=id;editRevision=t?t.updatedAt:'';
    const f=$('task-form');f.reset();$('form-error').textContent='';
    const select=$('task-category');select.replaceChildren();for(const cat of allCategories()) {const o=el('option','',cat);o.value=cat;select.append(o);}
    const values=t||{title:'',category:category||'未分类',priority:3,due:day?day+'T09:00':'',remind:4320,duration:30,notes:''};
    for(const key of ['title','category','priority','due','remind','duration','notes'])f.elements.namedItem(key).value=values[key];
    $('dialog-title').textContent=t?'编辑任务':'新建任务';
    $('form-source').textContent=t?[kinds[t.kind],t.source||'手动添加',t.sourceNotes,t.sourceTiming&&'原始时间线索：'+t.sourceTiming,'来源只读；确认日期和优先级不会更改原始记录。'].filter(Boolean).join('\n'):'手动添加，无外部文件来源。你填写的日期和优先级由自己确认。';
    reminderPreview();$('task-dialog').showModal();f.elements.namedItem('title').focus();
  }
  function closeTask(){ if(!busy)$('task-dialog').close(); }
  $('task-form').addEventListener('submit',async e=>{
    e.preventDefault();const f=e.currentTarget;if(!mayWrite()||!f.reportValidity())return;
    const old=editingId?store.tasks.find(t=>t.id===editingId):null;
    if(editingId&&(!old||old.updatedAt!==editRevision)){$('form-error').textContent='此任务已在其他窗口修改或移除，请关闭后重新编辑。';return;}
    const value=name=>f.elements.namedItem(name).value;
    const now=new Date().toISOString();
    const t={...(old||{id:'task-'+(globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)),completed:false,source:'手动添加',sourceNotes:'由用户创建，无外部文件来源。',sourceTiming:'',kind:'manual',createdAt:now,firedKey:''}),title:value('title').trim(),category:value('customCategory').trim()||value('category'),priority:Number(value('priority')),due:value('due'),remind:Number(value('remind')),duration:Number(value('duration')),notes:value('notes').trim(),updatedAt:now};
    t.priorityDefault=Boolean(old&&old.priorityDefault&&old.priority===t.priority);
    if(!old||U.scheduleKey(old)!==U.scheduleKey(t))t.firedKey='';
    try{U.validateStore({version:1,tasks:[t]});}catch(err){$('form-error').textContent=err.message;return;}
    if(!old&&store.tasks.length>=5000){$('form-error').textContent='任务已达到 5000 条上限';return;}
    const editable=['title','category','priority','priorityDefault','due','remind','duration','notes'];
    const changes=Object.fromEntries(editable.map(key=>[key,t[key]]));
    const body=old?{changes,expectedUpdatedAt:editRevision}:t;
    await write(old?'/tasks/'+encodeURIComponent(old.id):'/tasks',old?'PATCH':'POST',body,result=>{receiveTask(result);$('task-dialog').close();announce(old?'任务已保存到后端。':'新任务已保存到后端。');});
  });
  $('task-dialog').addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  function reminderPreview(){const n=Number($('task-form').elements.namedItem('remind').value);$('reminder-preview').textContent=n<0?'该任务不参与 QQ 阶段提醒。':'QQ 提醒阶段：'+U.escalationLabels(n).join(' → ')+'。仅在未完成时继续，后台执行并限频。';}
  $('task-form').elements.namedItem('remind').addEventListener('change',reminderPreview);
  function download(data,name,type) {
    const url=URL.createObjectURL(new Blob([data],{type}));const a=el('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  $('export-json').addEventListener('click',()=>{download(JSON.stringify({...store,exportedAt:new Date().toISOString()},null,2),`待办日历-备份-${U.chinaDayKey()}.json`,'application/json;charset=utf-8');announce('备份已生成，请在浏览器下载记录中确认文件保存成功。');});
  $('export-legacy').addEventListener('click',()=>{
    try {const raw=localStorage.getItem(KEY);if(!raw){announce('此浏览器当前地址没有旧版数据。');return;}JSON.parse(raw);download(raw,`待办日历-旧浏览器备份-${U.chinaDayKey()}.json`,'application/json;charset=utf-8');announce('仅导出了旧浏览器数据，未上传或修改后端；请人工核对后再导入。');}catch(err){announce('旧浏览器数据不可读取或格式损坏，未修改任何数据。');}
  });
  $('import-json').addEventListener('click',()=>{if(mayWrite()&&!importBusy)$('import-file').click();});
  $('import-file').addEventListener('change',async e=>{
    const file=e.target.files[0];e.target.value='';if(!file||importBusy||!mayWrite())return;importBusy=true;
    try {
      if(file.size>12*1024*1024)throw new Error('备份超过 12 MB 限制');
      const next=U.validateStore(JSON.parse((await file.text()).replace(/^\uFEFF/,''))),expectedRevision=revision;
      if(await confirmAction('用备份替换后端任务？',`验证通过：${next.tasks.length} 件。将整体替换后端 ${store.tasks.length} 件任务。请先导出当前后端备份。旧分钟提醒已转换为提前 1 天。其他来源若同时更新，替换会被拒绝。`))await write('/tasks','PUT',{...next,expectedRevision},async result=>{if(result.tasks)receiveSnapshot(result);else receiveSnapshot(await api('/tasks'));scope='active';category='';query='';priority='';selectedDay=U.chinaDayKey();syncFilters();announce('备份已确认写入后端。');});
    } catch(err){announce('未导入：'+(err instanceof SyntaxError?'不是有效 JSON':err.message)+'。当前任务未被前端替换。');}
    finally{importBusy=false;}
  });
  $('import-schedule').addEventListener('click',async()=>{
    const file=$('schedule-file').files[0];
    if(!file){$('schedule-import-result').textContent='请先选择 .ics 课表文件。';return;}
    if(!mayWrite())return;
    if(file.size>2*1024*1024){$('schedule-import-result').textContent='课表文件超过 2 MB 限制。';return;}
    try{
      const content=(await file.text()).replace(/^\uFEFF/,'');
      await write('/schedule','POST',{ics:content},async result=>{courseData=await api('/schedule');$('schedule-import-result').textContent=`已导入 ${result.count} 节课${result.unchanged?'（与现有课表一致）':''}。`;render();});
    }catch(err){$('schedule-import-result').textContent='课表导入失败：'+err.message;}
  });
  $('export-ics').addEventListener('click',async()=>{
    const tasks=store.tasks.filter(t=>!t.completed&&t.due);
    if(!tasks.length){announce('还没有可导出的日程。请先编辑任务，确认完整日期和时间；待定日期不会被猜测或导出。');return;}
    if(await confirmAction('导出到系统日历',`将导出全部 ${tasks.length} 件已定日期的未完成任务，包含时长和提醒。按北京时间（UTC+8）转换为 UTC。重复导入可能产生重复事件；网页修改不会自动同步。`)){download(U.makeICS(tasks),`待办日历-${U.chinaDayKey()}.ics`,'text/calendar;charset=utf-8');announce('日历文件已生成。打开 .ics 导入系统日历，核对日期、时区及提醒设置。');}
  });
  const qqDeliveryLabels={not_configured:'未配置',starting:'启动中',ready:'就绪',not_bound:'未绑定',gateway_unavailable:'网关不可用',sent:'平台已受理',rate_limited:'平台限流',rejected:'平台拒绝',uncertain:'结果不确定',partial_failure:'部分失败'};
  function renderQQ(){
    const bots=U.qqBots(qqState),container=$('qq-bots');container.replaceChildren();
    if(qqState)$('qq-status').textContent=`已绑定 ${bots.filter(b=>b.bound).length} / 2 · 网关就绪 ${bots.filter(b=>b.gatewayReady).length} / 2 · 北京时间（UTC+8）`;
    for(const bot of bots){
      const card=el('section','qq-bot-card');card.append(el('h3','',bot.label));
      card.append(el('p','qq-bot-status',[bot.configured?'已配置':'未配置',bot.bound?'已绑定':'未绑定',bot.gatewayReady?'网关就绪':'网关未就绪',bot.enabled?'提醒已开启':'提醒未开启','投递：'+(qqDeliveryLabels[bot.deliveryStatus]||'待确认')].join(' · ')));
      const actions=el('div','utility-actions');
      for(const [action,label] of [['test','发送测试'],['pairing','生成配对码']]){const n=button(label,()=>qqAction(bot.slot,action));n.dataset.write='';n.dataset.qqSlot=bot.slot;n.dataset.qqAction=action;n.setAttribute('aria-label',bot.label+' · '+label);actions.append(n);}
      const message=el('p','qq-bot-result',qqMessages[bot.slot]||'');message.setAttribute('role','status');card.append(actions,message);container.append(card);
    }
    updateWriteControls();
  }
  async function refreshQQ(){
    if(currentPage!=='qq'&&currentPage!=='home')return;
    try {qqState=await api('/qq/status');if(currentPage==='qq')renderQQ();renderNavCounts();}
    catch(err){qqState=null;$('qq-status').textContent='暂时无法读取 QQ 状态，不能据此确认投递可用。';renderQQ();}
  }
  async function qqAction(slot,action){
    if(!mayWrite())return;const bot=U.qqBots(qqState).find(b=>b.slot===slot);
    if(!bot||!bot.configured||!bot.gatewayReady||(action==='pairing'?bot.bound:!bot.bound)){announce('该机器人尚不满足操作条件，请同步状态后重试。');return;}
    const pairing=action==='pairing';
    if(!await confirmAction(pairing?`为${bot.label}生成私有配对码？`:`向${bot.label}发送测试？`,pairing?'配对码只用于这个机器人，请勿截图、外传或发送到另一机器人会话。':'将通过这个已绑定机器人实际发送一条测试消息。平台受理不等于已到达。'))return;
    await write('/qq/'+action,'POST',{bot:slot},result=>{
      if(!pairing){qqMessages[slot]=bot.label+'：'+U.qqTestResult(result,slot);renderQQ();return;}
      if(!result.ok||typeof result.code!=='string'||!result.code||(result.slot&&result.slot!==slot)){qqMessages[slot]=bot.label+'：未返回匹配的有效配对码，请同步后检查配置。';renderQQ();return;}
      qqMessages[slot]=`仅用于${bot.label}：请私聊此机器人发送 /bind ${result.code}。有效期：${result.expiresAt||'以后端为准'}。请勿发送到其他机器人，页面 60 秒后隐藏。`;
      const version=(qqMessageVersions[slot]||0)+1;qqMessageVersions[slot]=version;renderQQ();
      setTimeout(()=>{if(qqMessageVersions[slot]===version){qqMessages[slot]=bot.label+'：配对码已从页面隐藏，有效期以后端为准。';renderQQ();}},60000);
    });
  }
  for(const id of ['add-task','add-task-tasks','add-task-calendar']){const n=$(id);if(n)n.addEventListener('click',()=>openTask());}
  $('close-dialog').addEventListener('click',closeTask);$('cancel-dialog').addEventListener('click',closeTask);
  $('view-list').addEventListener('click',()=>{view='list';render();});
  $('view-calendar').addEventListener('click',()=>{view='calendar';weekStart=mondayOf(U.chinaNow());render();});
  $('scope-filter').addEventListener('change',e=>{scope=e.target.value;const params=new URLSearchParams(location.search);params.set('scope',scope);if(category)params.set('category',category);history.replaceState(null,'','/tasks?'+params.toString());render();});
  $('search').addEventListener('input',e=>{query=e.target.value;render();});
  $('priority-filter').addEventListener('change',e=>{priority=e.target.value;render();});
  $('sort').addEventListener('change',e=>{sort=e.target.value;render();});
  $('clear-filters').addEventListener('click',()=>{scope='active';category='';query='';priority='';sort='due';selectedDay=U.chinaDayKey();syncFilters();history.replaceState(null,'','/tasks');render();});
  function moveWeek(delta){const next=new Date(weekStart);next.setDate(next.getDate()+delta*7);if(next.getFullYear()<1900||next.getFullYear()>9998)return;weekStart=next;render();}
  $('month-prev').addEventListener('click',()=>moveWeek(-1));$('month-next').addEventListener('click',()=>moveWeek(1));
  $('month-today').addEventListener('click',()=>{weekStart=mondayOf(U.chinaNow());render();});
  $('cal-prev').addEventListener('click',()=>moveWeek(-1));$('cal-next').addEventListener('click',()=>moveWeek(1));
  $('cal-today').addEventListener('click',()=>{weekStart=mondayOf(U.chinaNow());render();});
  $('sync-retry').addEventListener('click',()=>sync(true));
  $('export-json').disabled=true;$('export-ics').disabled=true;
  $('login-form').addEventListener('submit',async e=>{
    e.preventDefault();if(loginBusy||!e.currentTarget.reportValidity())return;
    loginBusy=true;$('login-submit').disabled=true;$('login-error').textContent='正在登录…';
    try{
      const session=await api('/session','POST',{username:$('login-username').value,accessPassword:$('login-password').value});
      token=session.token;loginRequired=false;
      if(suspendedDraft){
        suspendedDraft=false;$('login-password').value='';
        $('login-page').hidden=true;$('planner-page').hidden=false;
        await sync(true);$('task-dialog').showModal();
      }else{location.replace(location.pathname==='/session'?'/' : location.pathname+location.search);}
    }catch(err){$('login-error').textContent=err.status===401?'账号或口令不正确。':err.message;}
    finally{loginBusy=false;$('login-submit').disabled=false;}
  });
  $('access-logout').addEventListener('click',async()=>{
    if(busy||syncing)return;
    try{
      await api('/logout','POST',{});
      store={version:1,tasks:[]};revision=0;loaded=false;qqState=null;
      for(const key of Object.keys(qqMessages))delete qqMessages[key];
      editingId=null;editRevision='';suspendedDraft=false;$('task-dialog').close();$('task-form').reset();render();renderQQ();
      showLogin('已退出登录。');$('login-password').value='';
    }catch(err){announce('退出未获确认：'+err.message);}
  });
  // login-form keeps action="/api/v1/session": a no-JS fallback POST would land on
  // the JSON API, so it is rewritten to stay on this page when JS is available.
  $('login-form').setAttribute('action','');
  render();sync();
  setInterval(()=>sync(),30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)sync();});
})();
