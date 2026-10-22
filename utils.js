/* Shared pure logic. No framework, network or DOM dependency. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TaskUtils = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const VERSION = 1, REMINDERS = [-1, 0, 1440, 2880, 4320, 7200, 10080, 20160, 43200];
  const LEGACY_REMINDERS = [5, 15, 30, 60];
  function normalizeReminder(value) { return LEGACY_REMINDERS.includes(value) ? 1440 : value; }
  function reminderLabel(value) { const n=normalizeReminder(value); return n<0?'不提醒':n===0?'当天提醒':`提前 ${n/1440} 天提醒`; }
  function escalationLabels(value) {
    const n=normalizeReminder(value); if(n<0)return [];
    return [...new Set([n,4320,1440,0].filter(x=>x<=n))].map(reminderLabel).concat(['逾期 1 天','逾期 3 天','逾期 7 天']);
  }
  function qqBots(status) {
    const incoming=Array.isArray(status?.bots)?status.bots:[];
    return ['bot-1','bot-2'].map((slot,i)=>{const bot=incoming.find(b=>b&&b.slot===slot)||{};return {slot,label:typeof bot.label==='string'?bot.label.slice(0,80):`机器人${i+1}`,configured:bot.configured===true,bound:bot.bound===true,gatewayReady:bot.gatewayReady===true,enabled:bot.enabled===true,deliveryStatus:typeof bot.deliveryStatus==='string'?bot.deliveryStatus:'not_configured'};});
  }
  function qqTestResult(result,slot) {
    const part=Array.isArray(result?.results)?result.results.find(r=>r.slot===slot):result;
    return part?.ok===true&&['sent','accepted'].includes(part.status)?'平台已受理，请在该机器人 QQ 会话中核对实际到达。':'该机器人测试未确认受理，请检查绑定、网关和平台限制。';
  }
  const pad = n => String(n).padStart(2, '0');
  function dayKey(d) { return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`; }
  function parseDue(s) { return new Date(s+'+08:00'); }
  function chinaNow(now=new Date()) { const d=new Date(+now+8*3600000); return new Date(d.getUTCFullYear(),d.getUTCMonth(),d.getUTCDate(),d.getUTCHours(),d.getUTCMinutes(),d.getUTCSeconds()); }
  function chinaDayKey(now=new Date()) { const d=new Date(+now+8*3600000); return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())}`; }
  function validDue(s) {
    if (s === '') return true;
    if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(s)) return false;
    const d = new Date(s+'Z');
    return Number.isFinite(+d) && d.getUTCFullYear() >= 1900 && d.getUTCFullYear() <= 9998 && `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` === s;
  }
  function text(v, field, max, empty = true) {
    if (typeof v !== 'string' || v.length > max || (!empty && !v.trim()) || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(v)) throw new Error(`${field}不是有效文本或长度超限`);
    return v;
  }
  function scheduleKey(t) { return t.due ? `${t.due}|${t.remind}` : ''; }
  function validateStore(value) {
    if (!value || value.version !== VERSION || !Array.isArray(value.tasks) || value.tasks.length > 5000) throw new Error('备份版本不兼容或任务数超限（最多 5000 条）');
    const ids = new Set();
    const tasks = value.tasks.map((t, i) => {
      if (!t || typeof t !== 'object') throw new Error(`第 ${i+1} 条任务格式错误`);
      const id = text(t.id, 'ID', 100, false);
      if (!/^[a-zA-Z0-9_-]+$/.test(id) || ids.has(id)) throw new Error('任务 ID 无效或重复');
      ids.add(id);
      if (![1,2,3,4].includes(t.priority) || typeof t.completed !== 'boolean' || !REMINDERS.includes(normalizeReminder(t.remind)) || !Number.isInteger(t.duration) || t.duration < 1 || t.duration > 1440 || !validDue(t.due)) throw new Error(`第 ${i+1} 条任务的日期、优先级或提醒设置无效`);
      if (typeof t.priorityDefault !== 'boolean') throw new Error('默认优先级标记无效');
      const clean = {id, title:text(t.title,'标题',200,false), category:text(t.category,'分类',100,false), priority:t.priority, priorityDefault:t.priorityDefault, due:t.due, remind:normalizeReminder(t.remind), duration:t.duration, completed:t.completed, notes:text(t.notes,'备注',10000), source:text(t.source,'来源',1000), sourceNotes:text(t.sourceNotes,'来源说明',3000), sourceTiming:text(t.sourceTiming,'时间线索',2000), kind:text(t.kind,'任务类型',40,false), firedKey:text(t.firedKey,'提醒状态',100), createdAt:text(t.createdAt,'创建时间',40,false), updatedAt:text(t.updatedAt,'更新时间',40,false)};
      if (!['assignment','milestone','suggestion','manual'].includes(clean.kind) || !Number.isFinite(Date.parse(clean.createdAt)) || !Number.isFinite(Date.parse(clean.updatedAt))) throw new Error('任务类型或修改时间无效');
      if (clean.firedKey && clean.firedKey !== scheduleKey(t)) throw new Error('提醒记录与日程不一致');
      if (clean.remind !== t.remind) clean.firedKey = '';
      return clean;
    });
    return {version:VERSION, tasks};
  }
  function filterTasks(tasks, f = {}, now = new Date()) {
    const today = chinaDayKey(now), until = new Date(+now+7*86400000);
    const query = (f.query || '').trim().toLocaleLowerCase();
    return tasks.filter(t => {
      const when = t.due ? parseDue(t.due) : null;
      if (f.category && t.category !== f.category) return false;
      if (f.priority && t.priority !== Number(f.priority)) return false;
      if (query && ![t.title,t.notes,t.category,t.source,t.sourceTiming,t.sourceNotes].join(' ').toLocaleLowerCase().includes(query)) return false;
      if (f.day && (!t.due || t.due.slice(0,10) !== f.day)) return false;
      switch (f.scope) {
        case 'today': return !t.completed && t.due.slice(0,10) === today;
        case 'upcoming': return !t.completed && when && when >= now && when <= until;
        case 'undated': return !t.completed && !t.due;
        case 'completed': return t.completed;
        case 'overdue': return !t.completed && when && when < now;
        case 'all': return true;
        default: return !t.completed;
      }
    });
  }
  function sortTasks(tasks, mode) {
    const due = t => t.due ? +parseDue(t.due) : Infinity;
    return [...tasks].sort((a,b) => {
      if (mode === 'priority') return a.priority-b.priority || due(a)-due(b) || a.title.localeCompare(b.title,'zh-CN');
      if (mode === 'newest') return Date.parse(b.createdAt)-Date.parse(a.createdAt);
      if (mode === 'title') return a.title.localeCompare(b.title,'zh-CN');
      return Number(a.completed)-Number(b.completed) || due(a)-due(b) || a.priority-b.priority || a.title.localeCompare(b.title,'zh-CN');
    });
  }
  function stats(tasks, now = new Date()) {
    return {overdue:filterTasks(tasks,{scope:'overdue'},now).length, upcoming:filterTasks(tasks,{scope:'upcoming'},now).length, undated:filterTasks(tasks,{scope:'undated'},now).length, completed:tasks.filter(t=>t.completed).length, total:tasks.length};
  }
  function escapeICS(s) { return String(s).replace(/\\/g,'\\\\').replace(/\r\n|\r|\n/g,'\\n').replace(/;/g,'\\;').replace(/,/g,'\\,'); }
  function foldICS(line) {
    const encoder = new TextEncoder(); let out = '', part = '', bytes = 0;
    for (const char of line) {
      const size = encoder.encode(char).length;
      if (bytes + size > 75) { out += part+'\r\n'; part=' '; bytes=1; }
      part += char; bytes += size;
    }
    return out+part;
  }
  function utc(d) { return d.toISOString().replace(/[-:]/g,'').replace(/\.\d{3}Z$/,'Z'); }
  function makeICS(tasks, now = new Date()) {
    const events = tasks.filter(t=>!t.completed && t.due && validDue(t.due));
    const lines = ['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Local Planner//Task Calendar ZH//EN','CALSCALE:GREGORIAN'];
    for (const t of events) {
      const d = parseDue(t.due);
      lines.push('BEGIN:VEVENT',`UID:${t.id}@local-task-calendar`,`DTSTAMP:${utc(now)}`,`LAST-MODIFIED:${utc(new Date(t.updatedAt))}`,`DTSTART:${utc(d)}`,`DTEND:${utc(new Date(+d+t.duration*60000))}`,`SUMMARY:${escapeICS(t.title)}`,`CATEGORIES:${escapeICS(t.category)}`,`DESCRIPTION:${escapeICS([t.notes, t.source && '来源：'+t.source,t.sourceNotes,t.sourceTiming && '原始时间线索：'+t.sourceTiming].filter(Boolean).join('\n'))}`,`PRIORITY:${({1:1,2:3,3:5,4:9})[t.priority]}`,'STATUS:CONFIRMED');
      if (t.remind >= 0) lines.push('BEGIN:VALARM',`TRIGGER:${t.remind===0?'PT0M':'-PT'+t.remind+'M'}`,'ACTION:DISPLAY',`DESCRIPTION:${escapeICS(t.title)}`,'END:VALARM');
      lines.push('END:VEVENT');
    }
    lines.push('END:VCALENDAR');
    return lines.map(foldICS).join('\r\n')+'\r\n';
  }
  function reminderReady(t, now = new Date()) {
    return !t.completed && t.due && t.remind >= 0 && t.firedKey !== scheduleKey(t) && +now >= +parseDue(t.due)-t.remind*60000 && +now <= +parseDue(t.due)+t.duration*60000;
  }
  return {VERSION, REMINDERS, qqBots, qqTestResult, normalizeReminder, reminderLabel, escalationLabels, dayKey, parseDue, chinaNow, chinaDayKey, validDue, scheduleKey, validateStore, filterTasks, sortTasks, stats, escapeICS, foldICS, makeICS, reminderReady};
});
