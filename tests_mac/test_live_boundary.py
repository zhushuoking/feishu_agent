import json,time,concurrent.futures
from pathlib import Path
import pytest
from scripts.live_boundary import Guard,Denied,digest,source_digest
@pytest.fixture
def permit(tmp_path):
 (tmp_path/'.env').write_text('placeholder')
 a={'session':'synthetic','approved':True,'other_consumers_stopped':True,'platform_verified':True,'start':time.time()-1,'end':time.time()+60,'root':str(tmp_path),'source_sha256':source_digest(tmp_path),'env_sha256':digest(tmp_path/'.env'),'limits':{'http':4,'auth':4,'model':2,'card':2,'ws':1},'model_url':'https://model.test/v1','chat':'chat','creator':'creator','owners':['a','b']}
 p=tmp_path/'permit.json';p.write_text(json.dumps(a));return p

def test_unauthorized(permit):
 a=json.loads(permit.read_text());a['approved']=False;permit.write_text(json.dumps(a))
 with pytest.raises(Denied):Guard(permit,digest(permit))
def test_expired(permit):
 with pytest.raises(Denied):Guard(permit,digest(permit),clock=lambda:time.time()+1000)
def test_restart_counts(permit):
 g=Guard(permit,digest(permit));g.reserve('ws')
 with pytest.raises(Denied):Guard(permit,digest(permit)).reserve('ws')
def test_changed_authorization(permit):
 g=Guard(permit,digest(permit));permit.write_text(permit.read_text()+' ')
 with pytest.raises(Denied):g.reserve('http')
def test_changed_config(permit):
 g=Guard(permit,digest(permit));(permit.parent/'.env').write_text('changed')
 with pytest.raises(Denied):g.check()
def test_scope(permit):
 g=Guard(permit,digest(permit))
 for url,body in [('https://evil.test/a',None),('https://open.feishu.cn/open-apis/task/v2/tasks',None),('https://open.feishu.cn/open-apis/im/v1/messages',{'receive_id':'outsider'})]:
  with pytest.raises(Denied):g.classify(url,body)
def test_concurrent(permit):
 g=Guard(permit,digest(permit))
 def reserve(_):
  try:g.reserve('http','auth');return 1
  except Denied:return 0
 with concurrent.futures.ThreadPoolExecutor(max_workers=8) as e:assert sum(e.map(reserve,range(12)))==4

def test_mock_transport_count(permit,monkeypatch):
 import requests
 from scripts.live_boundary import install_network
 class Response:
  status_code=200
  def json(self):return {'code':0}
 monkeypatch.setattr(requests.Session,'send',lambda *a,**k:Response())
 original=requests.Session.send
 import httpx,lark_oapi.ws.client as ws
 with monkeypatch.context() as m:
  m.setattr(requests.Session,'send',original);m.setattr(httpx.Client,'send',httpx.Client.send);m.setattr(httpx.AsyncClient,'send',httpx.AsyncClient.send);m.setattr(ws.websockets,'connect',ws.websockets.connect)
  g=Guard(permit,digest(permit));install_network(g)
  assert requests.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',json={}).status_code==200
  import sqlite3
  with sqlite3.connect(g.state) as c:assert dict(c.execute('select kind,n from counts'))=={'auth':1,'http':1}

@pytest.mark.parametrize("mode", ["pilot", "daily"])
def test_host_timeout_reaps_real_children(permit,monkeypatch,mode):
 import subprocess,os
 import scripts.mac_live as host
 from dotenv import set_key
 a=json.loads(permit.read_text());a.update(app_id='mock',tenant='mock',database=str(permit.parent/'fresh.db'))
 a['end']=time.time()+2
 if mode=='daily':
  a.update(mode='daily',bot_open_id='ou_bot',database=str(permit.parent/'task_agent.db'))
  import sqlite3
  with sqlite3.connect(a['database']) as c:c.execute('CREATE TABLE preserved(value)');c.execute('INSERT INTO preserved VALUES (123)')
 (permit.parent/'.local').mkdir()
 (permit.parent/'.env').write_text('FEISHU_APP_ID=mock\nFEISHU_TENANT_KEY=mock\nFEISHU_ALLOWED_CHAT_IDS=chat\n')
 a['env_sha256']=digest(permit.parent/'.env');permit.write_text(json.dumps(a))
 monkeypatch.setattr(host,'ROOT',permit.parent)
 monkeypatch.setattr(host,'scan_consumers',lambda:[])
 popen=subprocess.Popen;children=[]
 def dummy(argv,**kw):
  role=argv[3]
  code="from pathlib import Path;import time;Path("+repr(str(permit.parent/(role+'.ready')))+").touch();time.sleep(30)"
  p=popen([os.sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);children.append(p);return p
 monkeypatch.setattr(host.subprocess,'Popen',dummy)
 host.host(str(permit),digest(permit))
 assert (permit.parent/'STOPPED').exists()
 assert (permit.parent/'revoked').exists()
 assert all(p.poll() is not None for p in children)
 if mode=='daily':
  with sqlite3.connect(permit.parent/'before-start-backup.db') as c:assert c.execute('SELECT value FROM preserved').fetchone()[0]==123

def test_expiry_during_run(permit):
 g=Guard(permit,digest(permit));g.clock=lambda:time.time()+1000
 with pytest.raises(Denied):g.reserve('http')

def test_source_changed(permit):
 g=Guard(permit,digest(permit));(permit.parent/'new.py').write_text('changed=1')
 with pytest.raises(Denied):g.check()
