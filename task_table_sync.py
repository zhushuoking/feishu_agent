"""普通任务→正式表；只镜像已有个人日历，不创建日程。默认不启用。"""
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from datetime import datetime


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def recovery_filter(key,fields):
    if 'Agent任务ID' in fields:
        from workbench_profile import recovery_filter as workbench_filter
        return workbench_filter(fields)
    return {'conjunction':'and','conditions':[
        {'field_name':'日程ID','operator':'is','value':[fields['日程ID']]},
        {'field_name':'日历ID','operator':'is','value':[fields['日历ID']]},
        {'field_name':'原始消息ID','operator':'is','value':[fields['原始消息ID']]},
        {'field_name':'来源群','operator':'is','value':[fields['来源群']]}]}


def identity_text(value):
    if isinstance(value,str):return value
    if isinstance(value,list) and value and all(isinstance(x,dict) and x.get('type')=='text' and isinstance(x.get('text'),str) for x in value):
        return ''.join(x['text'] for x in value)
    return None


def short_title(title):
    """Conservative display-only cleanup; never guess or truncate task meaning."""
    title=re.sub(r'\s+',' ',title).strip().rstrip('。；;')
    title=re.sub(r'^(?:待办|进行中|已完成|待验收|已取消|已阻塞)｜','',title)
    match=re.fullmatch(r'完成(.+?)并提交(.+)',title)
    if match:
        action,deliverable=match.groups()
        if action.endswith('测试') and deliverable.startswith('测试'):
            deliverable=deliverable[2:]
        return action+'及'+deliverable+'提交'
    return title


def fields_for(row):
    row=dict(row)
    if row['synced_version'] != row['version'] or row['cal_status'] != 'created':
        return None
    if not row['event_id'] or not row['calendar_id'] or not row['message_id']:
        return None
    body = json.loads(row['request_body'] or '{}')
    if not row['open_id'].startswith('ou_'):
        return None
    if row.get('title') and row.get('deadline_at'):
        from calendar_sync import _lead_minutes
        from state_machine import STATUS_LABELS
        from zoneinfo import ZoneInfo
        deadline=datetime.fromisoformat(row['deadline_at'])
        if deadline.tzinfo is None:deadline=deadline.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        end=int(deadline.timestamp())
        body['start_time']={'timestamp':str(end-_lead_minutes()*60)}
        body['end_time']={'timestamp':str(end)}
        label=STATUS_LABELS.get(row['status'],row['status'])
        body['summary']=row['title'] if row['status']=='TODO' else label+'｜'+row['title']
    state = '已完成' if row['status'] == 'DONE' else '已进日历'
    fields = {'议题': short_title(row.get('title') or body['summary']), '开始时间': int(body['start_time']['timestamp'])*1000,
              '结束时间': int(body['end_time']['timestamp'])*1000,
              '负责人': [{'id': row['open_id']}], '来源群': row['chat_id'],
              '原始消息ID': row['message_id'], '状态': state,
              '日程ID': row['event_id'], '日历ID': row['calendar_id']}
    if fields['结束时间'] <= fields['开始时间']:
        return None
    return fields


class Mirror:
    def __init__(self, policy):
        self.a = policy
        self.path = Path(policy['task_table_db'])
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        binding = packed({k: policy[k] for k in ('app_id','tenant','chat','task_table_app','task_table_table','task_table_cutoff')})
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS target(binding TEXT NOT NULL)')
            old = c.execute('SELECT binding FROM target').fetchone()
            if old and old[0] != binding:
                raise RuntimeError('task table ledger target mismatch')
            if not old: c.execute('INSERT INTO target VALUES (?)', (binding,))
            c.execute('CREATE TABLE IF NOT EXISTS request_window(at REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS mirror(key TEXT PRIMARY KEY, token TEXT UNIQUE, record TEXT, desired TEXT, applied TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT)')
            columns={r[1] for r in c.execute('PRAGMA table_info(mirror)')}
            if 'destination' not in columns:
                c.execute("ALTER TABLE mirror ADD COLUMN destination TEXT NOT NULL DEFAULT 'legacy'")
            if policy.get('workbench'):
                wb=packed({k:policy[k] for k in ('workbench_app','workbench_table','workbench_cutoff')})
                c.execute('CREATE TABLE IF NOT EXISTS workbench_target(binding TEXT NOT NULL)')
                previous=c.execute('SELECT binding FROM workbench_target').fetchone()
                if previous and previous[0]!=wb:raise RuntimeError('workbench target/cutover changed')
                if not previous:c.execute('INSERT INTO workbench_target VALUES (?)',(wb,))
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def slot(self):
        now=time.time()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM request_window WHERE at<=?',(now-3600,))
            if c.execute('SELECT COUNT(*) FROM request_window WHERE at>?',(now-60,)).fetchone()[0]>=3 or c.execute('SELECT COUNT(*) FROM request_window').fetchone()[0]>=60:return False
            c.execute('INSERT INTO request_window VALUES (?)',(now,))
        return True

    def destination(self,key,task_id=None):
        with self.connect() as c:
            old=c.execute('SELECT destination FROM mirror WHERE key=?',(key,)).fetchone()
        if old:return old[0]
        if self.a.get('workbench'):
            return 'workbench' if task_id is not None and task_id>self.a['workbench_cutoff'] else None
        return 'legacy'

    def api_for(self,api,destination):
        return api.at_target(destination) if hasattr(api,'at_target') else api

    def apply(self, key, fields, api, destination='legacy'):
        desired = packed(fields)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT token,record,applied,status,attempts FROM mirror WHERE key=?', (key,)).fetchone()
            if old and (old[2] == desired or old[3] in ('unknown','blocked')): return
            if old:
                token, record, _, _, attempts = old
                c.execute('UPDATE mirror SET desired=?,status=? WHERE key=?', (desired,'updating' if record else 'unknown',key))
            else:
                token, record, attempts = str(uuid.uuid4()), None, 0
                # Persist unknown before external create. Never retry an uncertain create.
                c.execute('INSERT INTO mirror(key,token,desired,status) VALUES (?,?,?,"unknown")', (key,token,desired))
                c.execute('UPDATE mirror SET destination=? WHERE key=?',(destination,key))
        api=self.api_for(api,self.destination(key))
        try:
            if record: api.update(record,fields)
            else: record = api.create(fields,token)
            if not record: raise RuntimeError('missing record id')
            with self.connect() as c:
                c.execute('UPDATE mirror SET record=?,applied=?,status="synced",attempts=0,error=NULL WHERE key=?', (record,desired,key))
        except Exception as exc:
            with self.connect() as c:
                c.execute('UPDATE mirror SET status=?,attempts=?,error=? WHERE key=?',
                          ('unknown' if not record else ('blocked' if attempts+1>=3 else 'retry'),attempts+1,type(exc).__name__,key))
            # Isolate table failures from the existing task/calendar flow.

    def recover(self,key,api):
        with self.connect() as c:
            row=c.execute('SELECT desired,attempts FROM mirror WHERE key=? AND status="unknown"',(key,)).fetchone()
        if not row:return
        fields=json.loads(row[0]);attempts=row[1]+1
        api=self.api_for(api,self.destination(key))
        try:
            found=api.find(key,fields)
            if len(found)>1:raise RuntimeError('multiple matching task records')
            record=found[0] if found else None
            if record:
                actual=record['fields']
                for name in (('Agent任务ID','原始消息ID','来源群') if 'Agent任务ID' in fields else ('日程ID','日历ID','原始消息ID','来源群')):
                    if identity_text(actual.get(name))!=fields[name]:raise RuntimeError('recovery identity mismatch')
                with self.connect() as c:
                    c.execute('UPDATE mirror SET record=?,status="retry",attempts=0,error=NULL WHERE key=?',(record['record_id'],key))
                return
            error='record not found; no automatic create retry'
        except Exception as exc:error=type(exc).__name__
        with self.connect() as c:
            c.execute('UPDATE mirror SET attempts=?,status=?,error=? WHERE key=?',(attempts,'blocked' if attempts>=4 else 'unknown',error,key))

    def tick(self, api):
        with sqlite3.connect('file:'+self.a['database']+'?mode=ro',uri=True) as src:
            src.row_factory=sqlite3.Row
            rows=src.execute("""SELECT t.id,t.uid,t.version,t.status,t.title,t.deadline_at,t.chat_id,t.description,t.deliverable,t.acceptance_criteria,t.priority,t.creator_open_id,t.created_at,t.completed_at,a.open_id,p.calendar_id,p.event_id,
                p.request_body,p.synced_version,p.status AS cal_status,c.message_id
                FROM task t JOIN task_assignee a ON a.task_id=t.id AND a.role='owner'
                LEFT JOIN personal_calendar_event p ON p.task_uid=t.uid AND p.open_id=a.open_id
                JOIN task_candidate c ON c.uid=t.candidate_uid
                WHERE t.id>? AND t.chat_id=? ORDER BY t.id,a.open_id""",
                (self.a['task_table_cutoff'],self.a['chat'])).fetchall()
        # Reconcile unknown creates even if task was cancelled or lost calendar eligibility.
        with self.connect() as c:unknown=c.execute('SELECT key FROM mirror WHERE status="unknown" ORDER BY key LIMIT 3').fetchall()
        for (key,) in unknown:
            if not self.slot():return
            self.recover(key,api)
        groups={}
        for row in rows:groups.setdefault(row['uid'],[]).append(row)
        sent=0
        for key,group in groups.items():
            destination=self.destination(key,group[0]['id'])
            if destination is None:continue
            mapped=[fields_for(row) for row in group]
            fields=mapped[0] if all(mapped) else None
            if fields is not None:
                fields['负责人']=[{'id':row['open_id']} for row in group]
            else:
                if not all(row['status'] in ('CANCELLED','NEEDS_INFO') and row['cal_status'] in ('skipped','deleted') and row['synced_version']==row['version'] for row in group):continue
                with self.connect() as c:old=c.execute('SELECT applied FROM mirror WHERE key=?',(key,)).fetchone()
                if not old or not old[0]:continue
                fields=json.loads(old[0]);fields['状态']='已取消' if group[0]['status']=='CANCELLED' else '已进日历'
            if destination=='workbench':
                from workbench_profile import map_task
                try:fields=map_task(dict(group[0]),fields,[r['open_id'] for r in group],short_title)
                except (ValueError,KeyError,TypeError):continue
            with self.connect() as c:
                old=c.execute('SELECT applied,status FROM mirror WHERE key=?',(key,)).fetchone()
            if old and old[1]=='unknown':continue
            if old and (old[0]==packed(fields) or old[1]=='blocked'):continue
            if not self.slot():break
            self.apply(key,fields,api,destination);sent+=1
            if sent>=3:break


class SDKMirror:
    def __init__(self,a):self.a=a
    def at_target(self,destination):
        if destination=='legacy':return self
        if destination!='workbench' or not self.a.get('workbench'):raise RuntimeError('unapproved destination')
        return SDKMirror(dict(self.a,task_table_app=self.a['workbench_app'],task_table_table=self.a['workbench_table']))
    def create(self,fields,token):
        from feishu_api import get_client
        from lark_oapi.api.bitable.v1 import CreateAppTableRecordRequest,AppTableRecord
        req=(CreateAppTableRecordRequest.builder().app_token(self.a['task_table_app']).table_id(self.a['task_table_table'])
             .user_id_type('open_id').client_token(token).request_body(AppTableRecord.builder().fields(fields).build()).build())
        response=get_client().bitable.v1.app_table_record.create(req)
        if not response.success():raise RuntimeError('table create rejected:'+str(response.code))
        return response.data.record.record_id
    def update(self,record,fields):
        from feishu_api import get_client
        from lark_oapi.api.bitable.v1 import UpdateAppTableRecordRequest,AppTableRecord
        req=(UpdateAppTableRecordRequest.builder().app_token(self.a['task_table_app']).table_id(self.a['task_table_table'])
             .record_id(record).user_id_type('open_id').request_body(AppTableRecord.builder().fields(fields).build()).build())
        response=get_client().bitable.v1.app_table_record.update(req)
        if not response.success():raise RuntimeError('table update rejected')

    def find(self,key,fields):
        from feishu_api import get_client
        from lark_oapi.api.bitable.v1 import SearchAppTableRecordRequest,SearchAppTableRecordRequestBody,FilterInfo,Condition
        spec=recovery_filter(key,fields)
        conditions=[Condition.builder().field_name(c['field_name']).operator(c['operator']).value(c['value']).build() for c in spec['conditions']]
        body=SearchAppTableRecordRequestBody.builder().filter(FilterInfo.builder().conjunction('and').conditions(conditions).build()).automatic_fields(False).build()
        req=(SearchAppTableRecordRequest.builder().app_token(self.a['task_table_app']).table_id(self.a['task_table_table'])
             .user_id_type('open_id').page_size(100).request_body(body).build())
        response=get_client().bitable.v1.app_table_record.search(req)
        if not response.success() or response.data.has_more:raise RuntimeError('bounded recovery search rejected/incomplete')
        return [{'record_id':r.record_id,'fields':r.fields} for r in (response.data.items or [])]
