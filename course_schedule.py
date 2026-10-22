"""Course calendar import and source-aware deadline inference in China time."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import re

CHINA = timezone(timedelta(hours=8))
# Optional title-to-category aliases and course resource links. Start empty;
# imported titles are used as categories unless an alias is configured here.
COURSES = {}
RESOURCES = {}

def parse_ics(content):
    if not isinstance(content,str) or len(content)>2_000_000:
        raise ValueError('invalid_calendar_size')
    lines = re.sub(r'\r?\n[ \t]', '', content).splitlines()
    events=[]; current=None; alarm=False
    def unescape(s):
        return re.sub(r'\\([nN,;\\])', lambda m:'\n' if m[1] in 'nN' else m[1], s)
    def date(raw):
        value, params=raw
        if params and 'TZID=Asia/Shanghai' not in params:
            raise ValueError('unsupported_calendar_timezone')
        if value.endswith('Z'):
            return datetime.strptime(value,'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(CHINA).strftime('%Y-%m-%dT%H:%M')
        return datetime.strptime(value,'%Y%m%dT%H%M%S').strftime('%Y-%m-%dT%H:%M')
    for line in lines:
        if line=='BEGIN:VEVENT': current={}
        elif line=='BEGIN:VALARM': alarm=True
        elif line=='END:VALARM': alarm=False
        elif line=='END:VEVENT':
            if current is None: raise ValueError('invalid_calendar_event')
            if any(k in current for k in ['RRULE','RDATE','EXDATE']): raise ValueError('expand_recurrence_before_import')
            title=unescape(current.get('SUMMARY',('', ''))[0]); start=date(current['DTSTART']); end=date(current['DTEND'])
            if not title or end<=start: raise ValueError('invalid_calendar_event')
            uid=current.get('UID',(title+start,''))[0]
            events.append({'id':'class-'+hashlib.sha256((uid+'|'+start).encode()).hexdigest()[:24], 'title':title[:200], 'category':COURSES.get(title,title), 'start':start,'end':end,'location':unescape(current.get('LOCATION',('',''))[0])[:500]})
            current=None
        elif current is not None and not alarm and ':' in line:
            key,value=line.split(':',1); name,_,params=key.partition(';');current[name]=(value,params)
    if not events or len(events)>3000: raise ValueError('invalid_calendar_count')
    unique={e['id']:e for e in events}
    return sorted(unique.values(),key=lambda e:(e['start'],e['id']))

class Schedule:
    def __init__(self,data_dir): self.path=Path(data_dir)/'course-schedule.json'
    def snapshot(self):
        data=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'events':[],'source':'','sha256':''}
        events=data['events']
        courses=set(COURSES.values())|set(e['category'] for e in events)
        return {**data,'events':events,'timezone':'Asia/Shanghai','resources':RESOURCES,'courses':sorted(courses)}
    def import_ics(self,content):
        events=parse_ics(content)
        data={'events':events,'source':'schedule.ics','sha256':hashlib.sha256(content.encode()).hexdigest()}
        saved=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
        if saved==data: return {'ok':True,'count':len(events),'unchanged':True}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');temp.replace(self.path)
        return {'ok':True,'count':len(events),'unchanged':False}
    def resolve(self,category,phrase,source_date=None,now=None):
        now=now or datetime.now(CHINA)
        events=[e for e in self.snapshot()['events'] if e['category']==category]
        if not events: return {'due':'','basis':'没有匹配的课程课表','inferred':False}
        phrase=str(phrase)[:4000]
        anchor=datetime.fromisoformat(source_date) if source_date else now.replace(tzinfo=None)
        if anchor.tzinfo: anchor=anchor.astimezone(CHINA).replace(tzinfo=None)
        basis=('资料日期 '+anchor.isoformat(timespec='minutes')) if source_date else ('未提供资料日期，按导入所在周 '+anchor.date().isoformat()+' 推算')
        startweek=anchor.date()-timedelta(days=anchor.weekday())
        dates=re.search(r'(?:(20\d{2})[年/-])?(\d{1,2})[月/-](\d{1,2})日?',phrase)
        if not dates:
            en=re.search(r'September\s+(\d{1,2})',phrase,re.I)
            if en: dates=re.search(r'(9)月('+en[1]+')', '9月'+en[1])
        selected=[]; rule=''
        if dates:
            groups=dates.groups()
            year,month,day=groups if len(groups)==3 else (None,*groups)
            years={int(e['start'][:4]) for e in events}
            if year: years={int(year)}
            elif len(years)!=1:return {'due':'','basis':'课表跨年，日期年份需确认','inferred':False}
            target=datetime(years.pop(),int(month),int(day)).date().isoformat()
            selected=[e for e in events if e['start'].startswith(target)];rule='按课表学期年份及指定日期上课时间'
        elif re.search(r'下下周|下周|next\s+week',phrase,re.I):
            offset=2 if '下下周' in phrase else 1
            begin=startweek+timedelta(days=7*offset); end=begin+timedelta(days=7)
            selected=[e for e in events if begin.isoformat()<=e['start'][:10]<end.isoformat()]
            if source_date:
                same=[e for e in selected if datetime.fromisoformat(e['start']).weekday()==anchor.weekday()]
                if same:selected=same
            rule='下一周该课程上课开始时间（同星期优先，否则该周首节）'
        elif re.search(r'下次|下节|next\s+(?:time|class)',phrase,re.I):
            selected=[e for e in events if datetime.fromisoformat(e['start'])>anchor];rule='资料时间之后该课程的下一节课'
        elif re.search(r'第\s*([0-9]+|[一二三四五六七八九十]+)\s*周',phrase):
            m=re.search(r'第\s*([0-9]+|[一二三四五六七八九十]+)\s*周',phrase)
            nums={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
            week=int(m[1]) if m[1].isdigit() else nums.get(m[1],0)
            if week<1:return {'due':'','basis':'周次需确认','inferred':False}
            first=datetime.fromisoformat(self.snapshot()['events'][0]['start']).date();first-=timedelta(days=first.weekday());begin=first+timedelta(days=7*(week-1));end=begin+timedelta(days=7)
            selected=[e for e in events if begin.isoformat()<=e['start'][:10]<end.isoformat()];rule='按课表首周和指定教学周的首节课安排'
        if not selected:return {'due':'','basis':basis+'；该时间范围没有匹配课次，不擅自移到其他周','inferred':False}
        if re.search(r'实验|上机|\bLab\b',phrase,re.I):
            labs=[e for e in selected if e['start'][11:]>='17:00']
            if labs:selected=labs;rule+='；实验/上机优先晚间课次'
        event=selected[0]
        return {'due':event['start'],'basis':basis+'；'+rule+'；地点：'+event['location'],'inferred':True,'eventId':event['id']}
    def infer_task(self,task,source_date=None):
        if task['due'] or task['completed'] or task['kind']=='suggestion':return task
        result=self.resolve(task['category'],task['sourceTiming']+' '+task['title'],source_date)
        if not result['due']:return task
        return {**task,'due':result['due'],'firedKey':'','notes':(task['notes']+'\n[课表推算] '+result['basis'])[-10000:]}
