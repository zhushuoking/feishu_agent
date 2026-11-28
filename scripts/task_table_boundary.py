"""Mirror writes are restricted to a persisted task/destination and exact desired fields."""
import json,sqlite3
from urllib.parse import urlsplit,parse_qs
from scripts.live_boundary import Denied,digest
from meeting_config import target,CONFIG_PATH
from task_table_sync import packed,recovery_filter


def classify(a,url,body,method):
    u=urlsplit(str(url))
    if u.netloc!='open.feishu.cn' or '/bitable/' not in u.path:return None
    t=target(require_ready=True)
    if a['task_table_config_sha256']!=digest(CONFIG_PATH) or (a['task_table_app'],a['task_table_table'])!=(t['app_token'],t['table_id']):raise Denied('task mirror target changed')
    destinations={'legacy':(t['app_token'],t['table_id'])}
    if a.get('workbench'):
        from workbench_profile import APP,TABLE,STATES
        from scripts.autonomous import ROOT
        config=ROOT/'config/workbench_target.json'
        if a.get('workbench_config_sha256')!=digest(config) or (a.get('workbench_app'),a.get('workbench_table'))!=(APP,TABLE):raise Denied('workbench target changed')
        destinations['workbench']=(APP,TABLE)
    destination=None
    for name,(app,table) in destinations.items():
        prefix=f'/open-apis/bitable/v1/apps/{app}/tables/{table}/records'
        if u.path==prefix or u.path.startswith(prefix+'/'):
            destination=name;break
    if destination is None:raise Denied('unapproved task table')
    q=parse_qs(u.query)
    if method.upper()=='POST' and u.path==prefix+'/search':
        if q.get('user_id_type')!=['open_id'] or q.get('page_size')!=['100'] or 'page_token' in q:raise Denied('unbounded mirror search')
        with sqlite3.connect('file:'+a['task_table_db']+'?mode=ro',uri=True) as c:
            rows=c.execute('SELECT key,desired FROM mirror WHERE status="unknown" AND destination=?',(destination,)).fetchall()
        for key,desired in rows:
            if body=={'automatic_fields':False,'filter':recovery_filter(key,json.loads(desired))}:return 'task_table_read'
        raise Denied('mirror search not bound to an uncertain create')
    fields=body.get('fields') if isinstance(body,dict) else None
    states=tuple(STATES.values()) if destination=='workbench' else ('已进日历','已完成','已取消')
    if not isinstance(fields,dict) or fields.get('状态') not in states or not fields.get('日程ID') or not fields.get('日历ID') or fields.get('来源群')!=a['chat']:
        raise Denied('task mirror must reference an existing calendar event')
    if q.get('user_id_type')!=['open_id']:raise Denied('task mirror identity type')
    with sqlite3.connect('file:'+a['task_table_db']+'?mode=ro',uri=True) as c:
        if method.upper()=='POST' and u.path==prefix:
            token=q.get('client_token',[''])[0]
            row=c.execute('SELECT key,desired FROM mirror WHERE token=? AND status="unknown" AND destination=?',(token,destination)).fetchone()
            if row and row[1]==packed(fields) and (destination=='legacy' or fields.get('Agent任务ID')==row[0]):return 'task_table_create'
        if method.upper()=='PUT' and u.path.startswith(prefix+'/'):
            record=u.path[len(prefix)+1:]
            row=c.execute('SELECT key,desired FROM mirror WHERE record=? AND status="updating" AND destination=?',(record,destination)).fetchone()
            if '/' not in record and row and row[1]==packed(fields) and (destination=='legacy' or fields.get('Agent任务ID')==row[0]):return 'task_table_update'
    raise Denied('task mirror request outside ledger')
