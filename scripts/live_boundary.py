"""独立的Mac运行保护；不包含或修改原版业务状态机。"""
import hashlib,json,os,sqlite3,time
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
class Denied(RuntimeError): pass

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def source_digest(root):
    files=[]
    for base,dirs,names in os.walk(root):
        dirs[:]=[d for d in dirs if not d.startswith('.') and d!='__pycache__']
        files.extend(Path(base)/n for n in names if n.endswith('.py'))
    files.sort()
    return hashlib.sha256(''.join(str(p.relative_to(root))+digest(p) for p in files).encode()).hexdigest()
class Guard:
    def __init__(self,path,sha,clock=time.time):
        self.path=Path(path);self.sha=sha;self.clock=clock
        self.a=json.loads(self.path.read_text());self.state=self.path.parent/'budget.sqlite3'
        autonomous=self.a.get('mode')=='autonomous'
        if autonomous:
            from scripts.autonomous import HOME
            self.state=HOME/'budget.sqlite3'
        binding_sha=self.a.get('policy_sha256') if autonomous else sha
        self.check()
        with sqlite3.connect(self.state,timeout=5) as c:
            c.executescript('CREATE TABLE IF NOT EXISTS binding(session TEXT PRIMARY KEY,sha TEXT); CREATE TABLE IF NOT EXISTS counts(kind TEXT PRIMARY KEY,n INTEGER); CREATE TABLE IF NOT EXISTS audit(at REAL,stage TEXT,kind TEXT,status INTEGER,error TEXT);')
            r=c.execute('SELECT session,sha FROM binding').fetchone()
            if r and r!=(self.a['session'],binding_sha):
                if not autonomous:raise Denied('ledger binding mismatch')
                c.execute('DELETE FROM binding')
            c.execute('INSERT OR IGNORE INTO binding VALUES (?,?)',(self.a['session'],binding_sha))
    def check(self):
        a=json.loads(self.path.read_text())
        if a.get('mode')=='autonomous':
            from scripts.autonomous import check
            return check(self)
        if digest(self.path)!=self.sha or not a.get('approved') or not a.get('other_consumers_stopped') or not a.get('platform_verified') or not a['start']<=self.clock()<a['end'] or a['end']-a['start']>(43200 if a.get('mode')=='daily' else 1200) or (self.path.parent/'revoked').exists():raise Denied('authorization invalid')
        if a.get('mode')=='daily' and not a.get('bot_open_id'):raise Denied('verified bot open_id required')
        if self.state.exists() and self.state.stat().st_size>20*1024*1024:raise Denied('audit size limit')
        if a.get('personal_calendar'):
            if urlsplit(a['oauth_base_url']).scheme!='https' or digest(a['calendar_key_file'])!=a['calendar_key_sha256']:raise Denied('personal calendar config invalid')
        if a.get('meeting'):
            from meeting_config import target, CONFIG_PATH
            try:config=target(require_ready=True)
            except ValueError:raise Denied('meeting target not verified') from None
            if a.get('meeting_app')!=config['app_token'] or a.get('meeting_table')!=config['table_id'] or digest(CONFIG_PATH)!=a.get('meeting_config_sha256'):raise Denied('meeting target authorization mismatch')
        root=Path(a['root'])
        if source_digest(root)!=a['source_sha256'] or digest(root/'.env')!=a['env_sha256']:raise Denied('code/config changed')
        return a
    def reserve(self,*kinds):
        if self.a.get('mode')=='autonomous':
            from scripts.autonomous import reserve
            return reserve(self,kinds)
        self.check()
        with sqlite3.connect(self.state,timeout=5) as c:
            c.execute('BEGIN IMMEDIATE')
            for k in set(kinds):
                n=c.execute('SELECT n FROM counts WHERE kind=?',(k,)).fetchone()
                if (n[0] if n else 0)>=self.a['limits'].get(k,0):
                    self.stop();raise Denied('budget exhausted:'+k)
            for k in set(kinds):c.execute('INSERT INTO counts VALUES (?,1) ON CONFLICT(kind) DO UPDATE SET n=n+1',(k,))
            c.execute('INSERT INTO audit VALUES (?,?,?,?,?)',(self.clock(),'reserved',','.join(sorted(kinds)),None,None))
    def audit(self,stage,kind,status=None,error=None):
        with sqlite3.connect(self.state,timeout=5) as c:c.execute('INSERT INTO audit VALUES (?,?,?,?,?)',(self.clock(),stage,kind,status,error))
    def stop(self): (self.path.parent/'revoked').touch()
    def classify(self,url,body=None,method=None):
        u=urlsplit(str(url));model=urlsplit(self.a['model_url'])
        if u.scheme!='https':raise Denied('HTTPS required')
        if self.a.get('task_table') and '/bitable/' in u.path:
            from scripts.task_table_boundary import classify as task_table_classify
            try:
                result=task_table_classify(self.a,url,body,method)
                if result:return result
            except Denied:
                if not self.a.get('meeting'):raise
        if self.a.get('meeting'):
            from scripts.meeting_boundary import classify
            result=classify(self.a,url,body,method)
            if result:return result
        if self.a.get('personal_calendar'):
            from scripts.personal_boundary import classify
            personal=classify(self.a,url,body,method)
            if personal:return personal
        if u.netloc==model.netloc and u.path==model.path.rstrip('/')+'/chat/completions':return 'model'
        if u.netloc!='open.feishu.cn':raise Denied('host outside scope')
        if u.path.startswith('/open-apis/auth/') or u.path=='/callback/ws/endpoint':return 'auth'
        if '/im/v1/messages' in u.path:
            allowed=[self.a['chat'],self.a['creator'],*self.a['owners']]
            if self.a.get('mode') in ('daily','autonomous'):
                from scripts.daily_policy import people
                allowed=[self.a['chat'],*people(self.a)]
                if self.a.get('meeting'):
                    from scripts.meeting_boundary import senders
                    allowed.extend(senders(self.a))
            if isinstance(body,dict) and body.get('receive_id') and body['receive_id'] not in allowed:raise Denied('recipient outside scope')
            return 'text' if isinstance(body,dict) and body.get('msg_type')=='text' else 'card'
        raise Denied('API outside scope')

def install_network(g):
    import requests,httpx,lark_oapi.ws.client as ws
    def run(original,obj,request,kwargs):
        raw=getattr(request,'body',None)
        if raw is None:
            try:raw=request.content
            except:raw=None
        try:
            if urlsplit(str(request.url)).netloc=='accounts.feishu.cn' and raw:
                form=parse_qs(raw.decode() if isinstance(raw,bytes) else raw,strict_parsing=True)
                if any(len(v)!=1 for v in form.values()):raise Denied('duplicate OAuth fields')
                body={k:v[0] for k,v in form.items()}
            else:body=json.loads(raw) if raw else None
        except (ValueError,TypeError):g.stop();raise Denied('unparseable request')
        try:
            if str(request.method).upper()=='PUT' and g.a.get('task_table'):
                pass  # Classifier below limits PUT to known mirror records only.
            elif str(request.method).upper() not in (('GET','POST','PATCH','DELETE') if g.a.get('personal_calendar') else ('GET','POST','PATCH')):raise Denied('HTTP method outside scope')
            kind=g.classify(request.url,body,request.method)
            g.reserve('http',kind,*(['auth'] if kind=='oauth' else []))
        except Denied:
            g.audit('boundary_rejected','http');g.stop();raise
        g.audit('send',kind)
        try:
            response=original(obj,request,**kwargs)
            g.audit('response',kind,response.status_code)
            if response.status_code>=300 and kind not in ('task_table_create','task_table_update','task_table_read'):g.stop()
            try:
                payload=response.json()
                if isinstance(payload,dict) and payload.get('code',0)!=0:
                    g.audit('business_rejected',kind,error=str(payload.get('code')))
                    if kind not in ('task_table_create','task_table_update','task_table_read'):g.stop()
            except (ValueError,AttributeError):pass
            return response
        except BaseException as e:
            g.audit('exception',kind,error=type(e).__name__)
            if kind not in ('task_table_create','task_table_update','task_table_read'):g.stop()
            raise
    old=requests.Session.send
    def send(self,request,**kw):
        from urllib3.util.retry import Retry
        self.get_adapter(request.url).max_retries=Retry(total=0,redirect=0)
        kw['allow_redirects']=False;kw['timeout']=min(15,kw.get('timeout') or 15)
        return run(old,self,request,kw)
    requests.Session.send=send
    oldh=httpx.Client.send
    def hsend(self,request,**kw):
        kw['follow_redirects']=False
        request.extensions['timeout']={k:15.0 for k in ('connect','read','write','pool')}
        return run(oldh,self,request,kw)
    httpx.Client.send=hsend
    async def denied(*a,**k):raise Denied('async HTTP path prohibited')
    httpx.AsyncClient.send=denied
    connect=ws.websockets.connect
    def bounded_connect(uri,*a,**k):
        u=urlsplit(uri)
        if u.scheme!='wss' or not (u.hostname or '').endswith('.feishu.cn'):raise Denied('WS host outside scope')
        g.reserve('ws');g.audit('ws_connect','ws');return connect(uri,*a,**k)
    ws.websockets.connect=bounded_connect
