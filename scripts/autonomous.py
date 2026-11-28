"""Owner-controlled persistent policy and rolling budgets; no time lease."""
import hashlib,json,os,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
HOME=ROOT/'.local/autonomous'
POLICY=HOME/'policy.json'
APPROVAL=HOME/'confirmed.json'

def write_private(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name(path.name+'.tmp')
    with temp.open('w') as f:os.chmod(temp,0o600);json.dump(data,f,ensure_ascii=False,indent=2)
    temp.replace(path)

def proposed():
    from dotenv import dotenv_values
    v=dotenv_values(ROOT/'.env');previous={}
    selection=ROOT/'.local/daily-selection.json'
    if selection.exists():
        previous=json.loads(Path(json.loads(selection.read_text())['path']).read_text())
    from scripts.live_boundary import digest,source_digest
    a={k:previous[k] for k in ['bot_open_id','creator','owners','personal_calendar','calendar_cutoff','calendar_key_file','oauth_base_url','model_url'] if k in previous}
    a.update(mode='autonomous',root=str(ROOT),app_id=v.get('FEISHU_APP_ID',''),tenant=v.get('FEISHU_TENANT_KEY',''),chat=v.get('FEISHU_ALLOWED_CHAT_IDS',''),database=str(ROOT/'task_agent.db'),meeting=False,new_oauth=False,worker_timeout=180,audit_max_mb=100,backup_max_mb=1024)
    a.setdefault('bot_open_id',v.get('FEISHU_BOT_OPEN_ID',''));a.setdefault('creator','');a.setdefault('owners',[])
    a.setdefault('personal_calendar',False);a.setdefault('model_url',v.get('DEEPSEEK_BASE_URL','https://api.deepseek.com/v1'))
    if a.get('personal_calendar'):a['calendar_key_sha256']=digest(a['calendar_key_file'])
    a.update(source_sha256=source_digest(ROOT),env_sha256=digest(ROOT/'.env'))
    minute={'http':120,'auth':10,'ws':2,'message':20,'callback':30,'candidate':40,'task':20,'model':20,'card':40,'text':40,'calendar_create':20,'calendar_read':40,'calendar_update':20,'calendar_delete':10,'oauth':10,'oauth_start':0,'oauth_callback':0,'meeting_create':0,'meeting_read':0}
    a['rate_limits']={'60':minute,'3600':{k:n*20 for k,n in minute.items()}}
    return a

def validate_policy(a):
    from scripts.live_boundary import Denied
    if a.get('mode')!='autonomous' or not a.get('app_id') or not a.get('tenant') or not a.get('bot_open_id') or not a.get('chat','').startswith('oc_') or ',' in a['chat']:raise Denied('invalid application/group identity')
    if Path(a['root']).resolve()!=ROOT or Path(a['database']).resolve()!=ROOT/'task_agent.db':raise Denied('unexpected workspace/database')
    if a.get('meeting'):
        from meeting_config import target,CONFIG_PATH
        from scripts.live_boundary import digest
        t=target(require_ready=True)
        if a.get('meeting_app')!=t['app_token'] or a.get('meeting_table')!=t['table_id'] or a.get('meeting_config_sha256')!=digest(CONFIG_PATH):raise Denied('meeting target policy mismatch')
        if not Path(a.get('meeting_db','')).resolve().is_relative_to(HOME/'meeting'):raise Denied('separate autonomous meeting ledger required')
    if a.get('task_table'):
        from meeting_config import target,CONFIG_PATH
        from scripts.live_boundary import digest
        t=target(require_ready=True)
        if a.get('task_table_app')!=t['app_token'] or a.get('task_table_table')!=t['table_id'] or a.get('task_table_config_sha256')!=digest(CONFIG_PATH):raise Denied('task table target mismatch')
        if not a.get('task_table_update_permission_verified') or not a.get('task_table_retrieve_permission_verified') or not a.get('task_table_trigger_verified'):raise Denied('task table update permission/trigger not verified')
        if type(a.get('task_table_cutoff')) is not int or a['task_table_cutoff']<0:raise Denied('task mirror requires historical cutoff')
        if not Path(a.get('task_table_db','')).resolve().is_relative_to(HOME/'task-table'):raise Denied('separate task table ledger required')
    if a.get('workbench'):
        from workbench_profile import APP,TABLE
        from scripts.live_boundary import digest
        config=ROOT/'config/workbench_target.json'
        expected=json.loads(config.read_text())
        if (a.get('workbench_app'),a.get('workbench_table'))!=(APP,TABLE) or a.get('workbench_config_sha256')!=digest(config):raise Denied('workbench target changed')
        if expected!={'app_token':APP,'table_id':TABLE,'cutoff':a.get('workbench_cutoff')}:raise Denied('workbench configuration mismatch')
        if not a.get('task_table') or type(a.get('workbench_cutoff')) is not int or a['workbench_cutoff']<a['task_table_cutoff']:raise Denied('invalid workbench cutoff')
        if not a.get('workbench_schema_verified') or not a.get('workbench_edit_verified') or not a.get('workbench_trigger_verified'):raise Denied('workbench readiness not verified')
    if a.get('new_oauth'):
        from urllib.parse import urlsplit
        if not a.get('oauth_owner','').startswith('ou_') or not isinstance(a.get('oauth_until'),(int,float)) or not urlsplit(a.get('oauth_base_url','')).hostname or urlsplit(a['oauth_base_url']).scheme!='https':raise Denied('invalid temporary owner OAuth window')
    for window in ('60','3600'):
        limits=a.get('rate_limits',{}).get(window,{})
        if not limits or any(not isinstance(n,int) or n<0 for n in limits.values()):raise Denied('invalid rolling budgets')
    if not 15<=a.get('worker_timeout',0)<=3600 or a.get('audit_max_mb',0)<1 or a.get('backup_max_mb',1024)<1:raise Denied('invalid monitoring policy')

def setup(confirm=True):
    from scripts.live_boundary import digest
    HOME.mkdir(parents=True,exist_ok=True,mode=0o700)
    a=json.loads(POLICY.read_text()) if POLICY.exists() else proposed()
    from scripts.live_boundary import source_digest
    a.update(source_sha256=source_digest(ROOT),env_sha256=digest(ROOT/'.env'))
    validate_policy(a);write_private(POLICY,a)
    print('首次/变更确认：应用',a['app_id'],'；租户',a['tenant'],'；授权群',a['chat'])
    print('功能：任务识别、真实多人@、人工确认、卡片；个人日历既有账号同步：',bool(a.get('personal_calendar')))
    print('会议：',bool(a.get('meeting')),'；新日历OAuth连接关闭（既有账号刷新与同步保留）；不随开机启动；无固定到期时间。')
    print('每分钟/小时HTTP上限：',a['rate_limits']['60']['http'],a['rate_limits']['3600']['http'],'；异常安全停止、不自动重启。')
    print('完整策略：',POLICY)
    if not confirm:return
    if not sys.stdin.isatty():raise SystemExit('请在Mac终端执行 ./start.sh setup，阅读范围后确认；未连接飞书。')
    if input('同意以上持久配置请输入「确认」：').strip()!='确认':raise SystemExit('未确认；没有启动服务。')
    write_private(APPROVAL,{'policy_sha256':digest(POLICY),'confirmed_at':time.time()})
    print('已保存首次确认。以后 ./start.sh 启动，./stop.sh 停止。此次仅配置，尚未启动。')

def check(g):
    from scripts.live_boundary import Denied,digest,source_digest
    runtime=json.loads(g.path.read_text());policy=Path(runtime['policy_file']);approval=Path(runtime['approval_file'])
    if policy.resolve()!=POLICY.resolve() or approval.resolve()!=APPROVAL.resolve():raise Denied('policy location mismatch')
    a=json.loads(policy.read_text());validate_policy(a)
    sha=digest(policy)
    if digest(g.path)!=g.sha or json.loads(approval.read_text()).get('policy_sha256')!=sha or runtime.get('policy_sha256')!=sha:raise Denied('scope/config changed; run start.sh setup')
    derived=dict(a,session=sha,approved=True,other_consumers_stopped=True,platform_verified=True,policy_file=str(policy),approval_file=str(approval),policy_sha256=sha)
    if runtime!=derived:raise Denied('runtime differs from confirmed policy')
    if (g.path.parent/'revoked').exists():raise Denied('run stopped')
    if source_digest(ROOT)!=a['source_sha256'] or digest(ROOT/'.env')!=a['env_sha256']:raise Denied('code/config changed; run start.sh setup')
    if a.get('personal_calendar'):
        from urllib.parse import urlsplit
        if urlsplit(a['oauth_base_url']).scheme!='https' or digest(a['calendar_key_file'])!=a['calendar_key_sha256']:raise Denied('calendar configuration changed')
    if g.state.exists() and g.state.stat().st_size>a['audit_max_mb']*1024*1024:raise Denied('audit storage threshold; archive before restart')
    return runtime

def reserve(g,kinds):
    from scripts.live_boundary import Denied
    g.check();now=g.clock()
    if {'oauth_start','oauth_callback'} & set(kinds):
        if not g.a.get('new_oauth') or now>=g.a.get('oauth_until',0):raise Denied('owner OAuth window closed')
    with sqlite3.connect(g.state,timeout=5) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE IF NOT EXISTS rolling(at REAL,kind TEXT)')
        c.execute('CREATE INDEX IF NOT EXISTS rolling_time ON rolling(at)')
        last=c.execute('SELECT MAX(at) FROM rolling').fetchone()[0]
        if last is not None and now<last:g.stop();raise Denied('clock moved backwards')
        for kind in set(kinds):
            for seconds,limits in g.a['rate_limits'].items():
                used=c.execute('SELECT COUNT(*) FROM rolling WHERE kind=? AND at>?',(kind,now-int(seconds))).fetchone()[0]
                if used>=limits.get(kind,0):g.stop();raise Denied('rate threshold:'+kind)
        c.execute('DELETE FROM rolling WHERE at<=?',(now-3600,))
        for kind in set(kinds):
            c.execute('INSERT INTO rolling VALUES (?,?)',(now,kind))
            c.execute('INSERT INTO counts VALUES (?,1) ON CONFLICT(kind) DO UPDATE SET n=n+1',(kind,))
        c.execute('INSERT INTO audit VALUES (?,?,?,?,?)',(now,'reserved',','.join(sorted(kinds)),None,None))

def launch():
    from scripts.live_boundary import Guard,digest
    if not APPROVAL.exists():setup()
    a=json.loads(POLICY.read_text());validate_policy(a)
    sha=digest(POLICY)
    if json.loads(APPROVAL.read_text()).get('policy_sha256')!=sha:raise SystemExit('配置范围已变化；请先 ./start.sh setup 确认。')
    import fcntl
    HOME.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (HOME/'launcher.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from scripts.mac_live import scan_consumers
        if scan_consumers():raise SystemExit('已有Agent/Worker运行；请用 ./status.sh 查看。')
        directory=HOME/'runs'/uuid.uuid4().hex
        runtime=dict(a,session=sha,approved=True,other_consumers_stopped=True,platform_verified=True,policy_file=str(POLICY),approval_file=str(APPROVAL),policy_sha256=sha)
        path=directory/'runtime.json';write_private(path,runtime)
        g=Guard(path,digest(path))
        p=subprocess.Popen([sys.executable,str(ROOT/'scripts/mac_live.py'),str(path),digest(path)],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            for _ in range(250):
                if p.poll() is not None or (directory/'STOPPED').exists():raise RuntimeError('启动失败；请用 ./logs.sh 查看脱敏告警。')
                if (directory/'READY').exists():print('自主运行 READY（后台，无固定到期时间）。./stop.sh 可随时停止。');return
                time.sleep(.2)
            raise RuntimeError('启动等待超时，正在停止。')
        except BaseException:
            p.terminate()
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait()
            raise

if __name__=='__main__':
    os.umask(0o077)
    if sys.argv[1:]==['setup']:setup()
    elif sys.argv[1:]==['prepare']:setup(False)
    else:launch()
