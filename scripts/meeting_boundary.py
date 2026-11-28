"""只放行批准目标表、账本已领取的创建及已知record查询。"""
import sqlite3
from urllib.parse import urlsplit,parse_qs
from meeting_config import target, CONFIG_PATH
import json,hashlib
from scripts.live_boundary import Denied


def senders(a):
    try:
        with sqlite3.connect('file:'+a['meeting_db']+'?mode=ro',uri=True) as c:
            return [r[0] for r in c.execute('SELECT sender FROM meeting_watch WHERE chat=?',(a['chat'],))]
    except sqlite3.OperationalError:return []


def classify(a,url,body,method):
    u=urlsplit(str(url))
    if u.netloc!='open.feishu.cn' or '/bitable/' not in u.path:return None
    try:config=target(require_ready=True)
    except ValueError:raise Denied('meeting target not verified') from None
    APP,TABLE=config['app_token'],config['table_id']
    if a.get('meeting_config_sha256')!=hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest():raise Denied('meeting config changed')
    prefix=f'/open-apis/bitable/v1/apps/{APP}/tables/{TABLE}/records'
    if u.netloc!='open.feishu.cn' or '/bitable/' not in u.path:return None
    if a.get('meeting_app')!=APP or a.get('meeting_table')!=TABLE:raise Denied('meeting table not approved')
    method=(method or '').upper();q=parse_qs(u.query)
    with sqlite3.connect('file:'+a['meeting_db']+'?mode=ro',uri=True) as c:
        binding=c.execute('SELECT binding FROM meeting_target').fetchone()
        if not binding or json.loads(binding[0])!=config:raise Denied('meeting ledger target mismatch')
        if u.path==prefix and method=='POST':
            token=q.get('client_token',[''])[0]
            if not token or q.get('user_id_type')!=['open_id']:raise Denied('meeting token/identity missing')
            row=c.execute('SELECT payload FROM meeting WHERE token=? AND status="unknown"',(token,)).fetchone()
            fields=body.get('fields') if isinstance(body,dict) else None
            if not row or not isinstance(fields,dict) or hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()!=row[0]:raise Denied('meeting payload mismatch')
            if fields.get('来源群')!=a['chat']:raise Denied('meeting chat mismatch')
            return 'meeting_create'
        if method=='GET' and u.path.startswith(prefix+'/'):
            rid=u.path[len(prefix)+1:]
            if '/' not in rid and c.execute('SELECT 1 FROM meeting WHERE record=? AND status="submitted"',(rid,)).fetchone():return 'meeting_read'
    raise Denied('meeting API outside scope')
