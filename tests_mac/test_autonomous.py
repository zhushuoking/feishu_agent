import json,sqlite3,time,subprocess,sys,os,signal
from pathlib import Path
import pytest
from scripts.live_boundary import Guard,Denied,digest,source_digest
import scripts.autonomous as auto

@pytest.fixture
def policy(tmp_path,monkeypatch):
    root=tmp_path;home=root/'.local/autonomous';home.mkdir(parents=True)
    monkeypatch.setattr(auto,'ROOT',root);monkeypatch.setattr(auto,'HOME',home)
    monkeypatch.setattr(auto,'POLICY',home/'policy.json');monkeypatch.setattr(auto,'APPROVAL',home/'confirmed.json')
    (root/'.env').write_text('offline only')
    a=dict(mode='autonomous',root=str(root),database=str(root/'task_agent.db'),app_id='cli_mock',tenant='tenant',bot_open_id='ou_bot',chat='oc_test',meeting=False,new_oauth=False,personal_calendar=False,worker_timeout=30,audit_max_mb=10,backup_max_count=30,source_sha256=source_digest(root),env_sha256=digest(root/'.env'),rate_limits={'60':{'http':2,'ws':2},'3600':{'http':3,'ws':3}})
    auto.write_private(auto.POLICY,a);sha=digest(auto.POLICY)
    auto.write_private(auto.APPROVAL,{'policy_sha256':sha})
    runtime=dict(a,session=sha,approved=True,other_consumers_stopped=True,platform_verified=True,policy_file=str(auto.POLICY),approval_file=str(auto.APPROVAL),policy_sha256=sha)
    p=home/'runs/one/runtime.json';auto.write_private(p,runtime)
    return p

def test_unlimited_time_and_persistent_totals(policy):
    now=[1000.0];g=Guard(policy,digest(policy),clock=lambda:now[0]);g.reserve('http')
    now[0]+=86400*30
    # No lease expiration; restart preserves audit and totals.
    again=Guard(policy,digest(policy),clock=lambda:now[0]);again.reserve('http')
    with sqlite3.connect(g.state) as c:assert c.execute('SELECT n FROM counts WHERE kind="http"').fetchone()[0]==2

def test_minute_budget_survives_restart(policy):
    g=Guard(policy,digest(policy),clock=lambda:1000);g.reserve('http');g.reserve('http')
    with pytest.raises(Denied,match='rate threshold'):Guard(policy,digest(policy),clock=lambda:1000).reserve('http')
    assert (policy.parent/'revoked').exists()

def test_hour_budget_and_unknown_operation(policy):
    now=[1000];g=Guard(policy,digest(policy),clock=lambda:now[0])
    for _ in range(3):g.reserve('http');now[0]+=61
    with pytest.raises(Denied):g.reserve('http')

def test_config_change_needs_confirmation(policy):
    g=Guard(policy,digest(policy));a=json.loads(auto.POLICY.read_text());a['chat']='oc_other';auto.write_private(auto.POLICY,a)
    with pytest.raises(Denied):g.check()

def test_revocation_and_no_auto_restart(policy):
    g=Guard(policy,digest(policy));g.stop()
    with pytest.raises(Denied):g.check()

def test_duplicate_start_does_not_spawn(policy,monkeypatch):
    import scripts.mac_live as live
    monkeypatch.setattr(live,'scan_consumers',lambda:['existing'])
    monkeypatch.setattr(auto.subprocess,'Popen',lambda *a,**k:pytest.fail('must not spawn'))
    with pytest.raises(SystemExit,match='已有'):auto.launch()

@pytest.mark.parametrize('fault',['stop','child_exit'])
def test_offline_supervisor_lifecycle(tmp_path,fault):
    """Real detached supervisor/children, synthetic READY, socket-denied; no business DB."""
    project=Path(__file__).resolve().parents[1]
    code=r'''
import sys,os,json,time,sqlite3,types,subprocess,socket
from pathlib import Path
import scripts.mac_live as m
root=Path(sys.argv[1]);d=root/'run';d.mkdir(parents=True);(root/'.local/autonomous/runs').mkdir(parents=True)
(root/'.env').write_text('FEISHU_APP_ID=mock\nFEISHU_TENANT_KEY=mock\nFEISHU_ALLOWED_CHAT_IDS=oc_mock\n')
m.ROOT=root;m.scan_consumers=lambda:[]
class G:
 def __init__(self,*a):self.a={'mode':'autonomous','database':str(root/'task_agent.db'),'app_id':'mock','tenant':'mock','chat':'oc_mock','backup_max_count':30,'worker_timeout':30}
 def check(self):pass
 def audit(self,*a,**k):pass
 def stop(self):(d/'revoked').touch()
m.Guard=G
sys.modules['db']=types.SimpleNamespace(configure=lambda *a:None,init_db=lambda:None,get_engine=lambda:types.SimpleNamespace(dispose=lambda:None))
sys.modules['store']=types.SimpleNamespace(init_db=lambda *a:None)
original=subprocess.Popen
children=[]
def popen(args,**kw):
 role=args[3]
 program="import socket,time;from pathlib import Path;socket.socket.connect=lambda *a:(_ for _ in ()).throw(RuntimeError('NETWORK_DENIED'));Path(%r).touch();time.sleep(%s)"%(str(d/(role+'.ready')),'0.5' if sys.argv[2]=='child_exit' and role=='worker' else '60')
 p=original([sys.executable,'-c',program],**kw);children.append(p)
 (root/'children.json').write_text(json.dumps([x.pid for x in children]));return p
m.subprocess.Popen=popen
m.host(str(d/'runtime.json'),'mock')
'''
    p=subprocess.Popen([sys.executable,'-c',code,str(tmp_path),fault],cwd=project,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        for _ in range(100):
            if (tmp_path/'run/READY').exists():break
            if p.poll() is not None:pytest.fail(p.communicate()[1].decode())
            time.sleep(.05)
        else:pytest.fail('no READY')
        if fault=='stop':p.terminate()
        p.wait(timeout=10)
        report=json.loads((tmp_path/'run/STOPPED').read_text())
        assert len(report['exit_codes'])==2 and all(x is not None for x in report['exit_codes'])
        assert (tmp_path/'run/revoked').exists()
        assert (tmp_path/'run/ALERT.json').exists()==(fault=='child_exit')
    finally:
        if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait()
