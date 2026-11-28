"""Mac 原版启动包装；默认仅合成数据，禁止网络。未授权 live 明确拒绝。"""
import os, sys, signal, subprocess, time, json, socket, fcntl
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATE = ROOT / '.local' / 'offline'
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)

def alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False

def child(role):
    os.environ.update(PYTHON_DOTENV_DISABLED='1', FEISHU_APP_ID='offline_mock', FEISHU_APP_SECRET='offline_mock', FEISHU_ALLOWED_CHAT_IDS='oc_001', CALENDAR_SYNC_ENABLED='0', FEISHU_NATIVE_TASK_ENABLED='0', EVENT_FLUSH_IMMEDIATE='0', CARD_SYNC_ENABLED='0', DUE_REMINDER_ENABLED='0', OVERDUE_REMINDER_ENABLED='0', INFO_REMINDER_ENABLED='0', TASK_INFO_REMINDER_ENABLED='0', DIGEST_SEND_HOUR='25', TASK_AGENT_DB=str(STATE/'agent.db'))
    def deny(*a, **k): raise RuntimeError('OFFLINE_NETWORK_DENIED')
    socket.socket.connect = deny
    socket.create_connection = deny
    import db, store, functools
    db.configure(str(STATE/'agent.db'))
    for name in ('init_db', 'is_new_event', 'count_events'):
        setattr(store, name, functools.partial(getattr(store, name), db_path=str(STATE/'agent.db')))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ready = STATE / (role+'.ready')
    if role == 'agent':
        import main, lark_oapi
        from tests.helpers import make_recognition
        from types import SimpleNamespace as NS
        import uuid
        main.detector.detect = lambda cmd: make_recognition()
        main.feishu_api.push_confirm_card = lambda *a, **k: True
        class MockClient:
            def __init__(self, *a, **k):
                assert k.get('event_handler') is not None
            def start(self):
                marker = uuid.uuid4().hex
                frame = NS(header=NS(event_id=marker), event=NS(message=NS(message_id=marker, chat_id='oc_001', chat_type='group', content=json.dumps({'text':'离线合成任务'}), mentions=[], create_time=str(int(time.time()*1000)), root_id=None, parent_id=None), sender=NS(sender_type='user', sender_id=NS(open_id='ou_sender'))))
                main.on_message(frame)
                assert main.STATS['accepted'] == 1
                ready.write_text('OFFLINE_READY: event registration + synthetic callback + candidate persisted\n')
                while True: time.sleep(1)
        lark_oapi.ws.Client = MockClient
        main.main()
    else:
        import worker
        original = worker.tick
        def tick(*a, **k):
            result = original(*a, **k)
            ready.write_text('OFFLINE_READY: original Worker first dry-run tick complete\n')
            return result
        worker.tick = tick
        sys.argv = ['worker.py', '--dry-run', '--interval', '2']
        worker.main()

def control(action):
    live=ROOT/'.local/active-live.json'
    if live.exists():
        a=json.loads(live.read_text());pid=a['host'];directory=Path(a['directory'])
        cmd=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
        running=str(ROOT/'scripts/mac_live.py') in cmd and str(directory/'runtime.json') in cmd if a.get('mode')=='autonomous' else 'scripts/mac_live.py' in cmd
        if action=='start' and running:raise SystemExit('Live session already running')
        if action=='status' and running:
            children=a.get('children',[])
            healthy=all(alive(p) for p in children)
            mode='AUTONOMOUS' if a.get('mode')=='autonomous' else 'LIVE'
            state='ALERT / STOPPING' if (directory/'ALERT.json').exists() or not healthy else ('READY' if (directory/'READY').exists() else 'STARTING')
            print(mode,state,'Agent + Worker' if len(children)==2 else '');return
        if action=='stop' and running:
            os.kill(pid,signal.SIGTERM)
            for _ in range(100):
                if (directory/'STOPPED').exists():break
                time.sleep(.1)
            else:raise SystemExit('Live stop incomplete; inspect processes')
            print('AUTONOMOUS STOPPED; persistent policy retained' if a.get('mode')=='autonomous' else 'LIVE STOPPED; authorization revoked');return
        if not running and a.get('children'):
            orphans=[]
            for child_pid in a['children']:
                command=subprocess.run(['ps','-p',str(child_pid),'-o','command='],capture_output=True,text=True).stdout
                if str(ROOT/'scripts/mac_live.py') in command and str(directory) in command:orphans.append(child_pid)
            if orphans:
                if action!='stop':raise SystemExit('ALERT: host exited; owned children remain. Run ./stop.sh')
                for child_pid in orphans:os.kill(child_pid,signal.SIGTERM)
                for _ in range(50):
                    if all(not alive(p) for p in orphans):break
                    time.sleep(.1)
                if any(alive(p) for p in orphans):raise SystemExit('STOP incomplete; inspect owned processes')
                print('Orphan children stopped; audit retained');return
        if action=='status' and (directory/'ALERT.json').exists():
            print('STOPPED / ALERT; ./logs.sh 查看告警');return
    lock = open(STATE/'control.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    manifest = STATE/'processes.json'
    data = json.loads(manifest.read_text()) if manifest.exists() else {}
    if action == 'status':
        for role, pid in data.items(): print(role, 'RUNNING' if alive(pid) else 'STOPPED', 'OFFLINE')
        if not data: print('STOPPED')
        return
    if action == 'stop':
        # Only signal PIDs whose command line is this exact wrapper and role.
        for role, pid in data.items():
            cmd = subprocess.run(['ps','-p',str(pid),'-o','command='], capture_output=True, text=True).stdout
            if str(Path(__file__).resolve()) in cmd and ('child '+role) in cmd:
                os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if all(not alive(p) for p in data.values()): break
            time.sleep(.1)
        if any(alive(p) for p in data.values()): raise SystemExit('STOP incomplete; inspect processes')
        manifest.unlink(missing_ok=True)
        print('STOPPED; database and logs retained')
        return
    if action != 'start': raise SystemExit('Usage: start.sh [offline]; status.sh; stop.sh')
    if any(alive(p) for p in data.values()): raise SystemExit('Already running; use status.sh')
    os.environ.update(PYTHON_DOTENV_DISABLED='1', TASK_AGENT_DB=str(STATE/'agent.db'))
    import db, store
    db.configure(str(STATE/'agent.db')); store.init_db(str(STATE/'agent.db')); db.init_db(); db.get_engine().dispose()
    data = {}
    try:
        for role in ('agent','worker'):
            (STATE/(role+'.ready')).unlink(missing_ok=True)
            log = open(STATE/(role+'.log'), 'a')
            p = subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve()),'child',role],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            log.close(); data[role] = p.pid
            manifest.write_text(json.dumps(data))
            for _ in range(100):
                if p.poll() is not None: raise RuntimeError(role+' exited; inspect offline log')
                if (STATE/(role+'.ready')).exists(): break
                time.sleep(.1)
            else: raise RuntimeError(role+' READY timeout')
        print('OFFLINE READY: Agent + Worker; real Feishu is NOT connected')
    except BaseException:
        for p in data.values():
            if alive(p): os.kill(p, signal.SIGTERM)
        raise

if __name__ == '__main__':
    if sys.argv[1:2] == ['child']: child(sys.argv[2])
    elif len(sys.argv)>2 and sys.argv[2]!='offline': raise SystemExit('Live requires new explicit authorization; no real connection was made')
    else: control(sys.argv[1] if len(sys.argv)>1 else 'status')
