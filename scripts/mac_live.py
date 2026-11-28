"""受限真实验收启动入口；只有新批准文件及准确摘要可运行。"""
import os,sys,json,time,signal,subprocess,functools,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.live_boundary import Guard,digest,source_digest,Denied
ROOT=Path(__file__).resolve().parents[1]
class RedactedStream:
    encoding = 'utf-8'
    def write(self, text): return len(text)
    def flush(self): pass
    def isatty(self): return False

def child(role,path,sha):
    g=Guard(path,sha);a=g.a
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env',override=True)
    for key in ['FEISHU_NATIVE_TASK_ENABLED','CALENDAR_SYNC_ENABLED','DUE_REMINDER_ENABLED','OVERDUE_REMINDER_ENABLED','INFO_REMINDER_ENABLED','TASK_INFO_REMINDER_ENABLED','EVENT_FLUSH_IMMEDIATE','CARD_SYNC_ENABLED']:os.environ[key]='0'
    os.environ.update(DIGEST_SEND_HOUR='25',PYTHON_DOTENV_DISABLED='1',TASK_AGENT_DB=a['database'])
    os.environ['MEETING_ENABLED']='0'
    if a.get('meeting'):
        os.environ.update(MEETING_ENABLED='1',MEETING_CHAT_ID=a['chat'],MEETING_DB=a['meeting_db'],LIVE_AUTHORIZATION_FILE=str(path),LIVE_AUTHORIZATION_SHA256=sha)
    import db,store
    db.configure(a['database'])
    if a.get('mode') in ('daily','autonomous'):
        os.environ['FEISHU_BOT_OPEN_ID']=a['bot_open_id']
        os.environ['CARD_SYNC_ENABLED']='1'
    if a.get('personal_calendar'):
        os.environ.update(CALENDAR_SYNC_ENABLED='1',CALENDAR_SYNC_MODE='personal',CALENDAR_OWNER_ONLY='1',CALENDAR_PERSONAL_NEW_ONLY='1',CALENDAR_NEW_TASK_AFTER_ID=str(a['calendar_cutoff']),CALENDAR_TOKEN_KEY_FILE=a['calendar_key_file'],CALENDAR_AUTH_BASE_URL=a['oauth_base_url'],CALENDAR_AUTH_HOST='127.0.0.1',CALENDAR_AUTH_PORT='8766',CALENDAR_AUTH_REQUIRE_PUBLIC='1',CALENDAR_CONTROLLED_OAUTH='1',LIVE_AUTHORIZATION_FILE=str(path),LIVE_AUTHORIZATION_SHA256=sha)
    for key in ['init_db','is_new_event','count_events']:setattr(store,key,functools.partial(getattr(store,key),db_path=a['database']))
    # Ordinary upstream print/log messages may contain raw messages and IDs: suppress them.
    sys.stdout=sys.stderr=RedactedStream()
    import logging
    logging.disable(logging.CRITICAL)
    from scripts.live_boundary import install_network
    install_network(g)
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    if role=='agent':
        import main,card_action,repo,models,lark_oapi.ws.client as ws
        save=repo.save_candidates
        def save_one(result,cmd):
            if a.get('mode') not in ('daily','autonomous') and (len(result.tasks)!=1 or set(result.tasks[0].assignee_mention_ids)!=set(a['owners'])):
                g.stop();raise Denied('candidate count or owners mismatch')
            for task in result.tasks:g.reserve('candidate')
            return save(result,cmd)
        repo.save_candidates=save_one
        original=main.on_message
        def message(data):
            g.audit('event_arrived','message')
            cmd=models.parse_event(data.event,data.header)
            from scripts.daily_policy import valid_message
            from scripts.daily_policy import people
            if a.get('mode')=='autonomous' and cmd.chat_type=='p2p' and cmd.text.strip()=='连接日历' and (not a.get('new_oauth') or cmd.sender_open_id!=a.get('oauth_owner') or time.time()>=a.get('oauth_until',0)):
                g.audit('scope_rejected','new_oauth_disabled');return
            if a.get('personal_calendar') and cmd.chat_type=='p2p' and cmd.text.strip()=='连接日历' and not cmd.is_from_bot and cmd.sender_open_id in people(a):
                if getattr(data.header,'app_id',None)!=a['app_id'] or getattr(data.header,'tenant_key',None)!=a['tenant']:return
                g.reserve('oauth_start');original(data);return
            if (getattr(data.header,'tenant_key',None)!=a['tenant'] or getattr(data.header,'app_id',None)!=a['app_id'] or (not valid_message(a,cmd) if a.get('mode') in ('daily','autonomous') else (cmd.chat_id!=a['chat'] or cmd.sender_open_id!=a['creator'] or a['marker'] not in cmd.text or set(m.open_id for m in cmd.mentions if m.open_id)!=set(a['owners'])))):
                g.audit('scope_rejected','message');return
            g.reserve('message');original(data);g.audit('processed','message')
        main.on_message=message
        handler=card_action.handle
        def callback(data):
            from scripts.daily_policy import people
            operators=people(a) if a.get('mode') in ('daily','autonomous') else [a['creator'],*a['owners']]
            if (getattr(data.header,'app_id',None)!=a['app_id'] or getattr(data.event.operator,'tenant_key',getattr(data.header,'tenant_key',None))!=a['tenant'] or data.event.operator.open_id not in operators):g.audit('scope_rejected','callback');raise Denied('operator outside scope')
            g.reserve('callback');return handler(data)
        card_action.handle=callback
        confirm=repo.confirm_candidate
        def one(cid,*args,**kwargs):
            cand=repo.get_candidate(cid)
            if not cand or cand.chat_id!=a['chat'] or (a.get('mode') not in ('daily','autonomous') and cand.sender_open_id!=a['creator']):raise Denied('candidate outside scope')
            g.reserve('task');return confirm(cid,*args,**kwargs)
        repo.confirm_candidate=one
        connect=ws.Client._connect
        async def ready(self):
            await connect(self)
            if self._conn is not None:(Path(path).parent/'agent.ready').touch();g.audit('connected','ws')
        ws.Client._connect=ready
        init=ws.Client.__init__
        def no_reconnect(self,*args,**kwargs):kwargs['auto_reconnect']=False;init(self,*args,**kwargs)
        ws.Client.__init__=no_reconnect
        main.main()
    else:
        import worker
        tick=worker.tick
        mirror=None;last_mirror=0
        if a.get("task_table"):
            from task_table_sync import Mirror,SDKMirror
            mirror=Mirror(a);mirror_api=SDKMirror(a)
        def first(*args,**kwargs):
            nonlocal last_mirror
            g.check()
            try:r=tick(*args,**kwargs)
            except BaseException:g.stop();raise
            if mirror and time.monotonic()-last_mirror>=60:
                last_mirror=time.monotonic()
                try:mirror.tick(mirror_api)
                except Exception as exc:g.audit('mirror_failed','task_table',error=type(exc).__name__)
            (Path(path).parent/'worker.ready').touch();return r
        worker.tick=first;sys.argv=['worker.py','--interval','2'];worker.main()

def scan_consumers():
    r=subprocess.run(['ps','-axo','pid=,command='],capture_output=True,text=True)
    if r.returncode:raise Denied('cannot inspect local consumers')
    import shlex
    found=[]
    for line in r.stdout.splitlines():
        if 'python' not in line.lower():continue
        try:parts=shlex.split(line)
        except ValueError:raise Denied('cannot parse local process identity')
        names=[Path(x).name for x in parts]
        if any(x in names for x in ('main.py','worker.py')) or any(x in names and 'child' in parts for x in ('mac_local.py','mac_live.py')) or any(parts[i:i+2] in (['-m','main'],['-m','worker']) for i in range(len(parts)-1)):found.append(line)
    return found

def host(path,sha):
    os.umask(0o077)
    g=Guard(path,sha);a=g.a;directory=Path(path).parent
    if scan_consumers():raise Denied('local consumer already present')
    if a.get('mode')=='daily':
        total=sum(p.stat().st_size for p in (ROOT/'.local/daily').rglob('*') if p.is_file())
        if total>200*1024*1024:raise Denied('daily evidence size limit; archive manually')
    import fcntl
    lock=open(ROOT/'.local/live-instance.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    from dotenv import dotenv_values
    config=dotenv_values(ROOT/'.env')
    if config.get('FEISHU_APP_ID')!=a['app_id'] or config.get('FEISHU_TENANT_KEY')!=a['tenant'] or config.get('FEISHU_ALLOWED_CHAT_IDS')!=a['chat']:raise Denied('identity config mismatch')
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(SystemExit(0)))
    import db,store
    database=Path(a['database'])
    if database.exists() and a.get('mode') not in ('daily','autonomous'):raise Denied('fresh session database required')
    if a.get('mode') in ('daily','autonomous') and database.resolve()!=(ROOT/'task_agent.db').resolve():raise Denied('daily database must be final own database')
    if a.get('mode')=='autonomous' and sum(p.stat().st_size for p in (ROOT/'.local/autonomous/runs').glob('*/before-start-backup.db'))>=a.get('backup_max_mb',1024)*1024*1024:
        raise Denied('backup storage threshold; archive backups before restart')
    if database.exists():
        backup=directory/'before-start-backup.db'
        with sqlite3.connect(database) as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise Denied('backup verification failed')
        backup.chmod(0o600)
    db.configure(str(database));store.init_db(str(database));db.init_db();db.get_engine().dispose()
    children=[]
    (ROOT/'.local/active-live.json').write_text(json.dumps({'host':os.getpid(),'directory':str(directory)}))
    try:
        for role in ['agent','worker']:
            p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'child',role,path,sha],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            children.append(p)
            (ROOT/'.local/active-live.json').write_text(json.dumps({'host':os.getpid(),'directory':str(directory),'mode':a.get('mode'),'children':[x.pid for x in children]}))
            for _ in range(200):
                g.check()
                if any(x.poll() is not None for x in children):raise Denied('child exited')
                if (directory/(role+'.ready')).exists():break
                time.sleep(.1)
            else:raise Denied('READY timeout')
        if a.get('personal_calendar') and (a.get('mode')!='autonomous' or a.get('new_oauth')):
            for _ in range(30):
                check=subprocess.run(['lsof','-nP','-a','-p',str(children[0].pid),'-iTCP:8766','-sTCP:LISTEN','-t'],capture_output=True,text=True)
                if str(children[0].pid) in check.stdout.split():break
                g.check();time.sleep(.1)
            else:raise Denied('OAuth callback listener not ready')
            (ROOT/'.local/personal-tunnel/route.json').write_text(json.dumps({'agent_pid':children[0].pid,'permit':str(path),'sha256':sha}))
        (directory/'READY').touch()
        while all(x.poll() is None for x in children):
            g.check()
            if a.get('mode')=='autonomous' and time.time()-(directory/'worker.ready').stat().st_mtime>a['worker_timeout']:raise Denied('worker heartbeat stale')
            if a.get('personal_calendar') and a.get('mode')!='autonomous':
                cmd=subprocess.run(['ps','-p',str(a['tunnel_pid']),'-o','command='],capture_output=True,text=True).stdout
                if 'cloudflared' not in cmd:raise Denied('HTTPS tunnel exited')
            time.sleep(.5)
        raise Denied('child exited unexpectedly')
    except BaseException as e:
        g.audit('host_stop','process',error=type(e).__name__)
        if not isinstance(e,SystemExit):
            (directory/'ALERT.json').write_text(json.dumps({'stage':'host_stop','error_type':type(e).__name__,'reason':str(e) if isinstance(e,Denied) else 'inspect audit','automatic_restart':False}))
    finally:
        g.stop()
        (directory/'STOP_REASON.json').write_text(json.dumps({'automatic_restart':False,'action':'检查审计；结果不确定时人工核查；不得自动重发或重置预算'}))
        for p in children:
            if p.poll() is None:p.terminate()
        for p in children:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        if a.get('personal_calendar') and a.get('mode')!='autonomous':
            cmd=subprocess.run(['ps','-p',str(a['tunnel_pid']),'-o','command='],capture_output=True,text=True).stdout
            if 'cloudflared' in cmd:
                os.kill(a['tunnel_supervisor_pid'],signal.SIGTERM)
        (directory/'STOPPED').write_text(json.dumps({'exit_codes':[p.returncode for p in children],'revoked':True}))

if __name__=='__main__':
    if sys.argv[1]=='child':
        try:child(*sys.argv[2:])
        except BaseException as e:
            try:
                import traceback
                directory=Path(sys.argv[3]).resolve().parent
                if directory.is_relative_to(ROOT/'.local'):
                    frames=[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(e.__traceback__)]
                    (directory/('child-'+('agent' if sys.argv[2]=='agent' else 'worker')+'-failure.json')).write_text(json.dumps({'error_type':type(e).__name__,'cause_type':type(e.__cause__).__name__ if e.__cause__ else None,'frames':frames}))
            except BaseException:pass
            try:
                g=Guard(sys.argv[3],sys.argv[4]);g.audit('child_exit',sys.argv[2],error=type(e).__name__);g.stop()
            except BaseException:pass
            raise SystemExit(1)
    else:host(*sys.argv[1:])
