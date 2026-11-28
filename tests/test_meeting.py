from datetime import datetime
from types import SimpleNamespace as N
from zoneinfo import ZoneInfo
import pytest
from meeting import parse,submit,Ledger,Invalid
NOW=datetime(2026,11,26,tzinfo=ZoneInfo('Asia/Shanghai'))
def cmd():
 return N(text_raw='@bot 会议 主题=项目复盘 时间=2026-12-01 14:00 时长=60 参会人=@a,@b,@a',mentions=[N(key='@bot',open_id='ou_bot'),N(key='@a',open_id='ou_a'),N(key='@b',open_id='ou_b')],chat_id='oc_test',chat_type='group',is_from_bot=False,sender_open_id='ou_sender',message_id='om_test')
def test_parse():
 f=parse(cmd(),'ou_bot',NOW);assert f['结束时间']-f['开始时间']==3600000;assert len(f['负责人'])==2;assert f['状态']=='待进日历';assert '日程ID' not in f
@pytest.mark.parametrize('old,new',[('@a,@b,@a','张三'),('2026-12-01','2026-02-30'),('时长=60','时长=0'),('@bot','机器人'),('@a,@b,@a','@bot')])
def test_invalid(old,new):
 c=cmd();c.text_raw=c.text_raw.replace(old,new)
 with pytest.raises(Invalid):parse(c,'ou_bot',NOW)
def test_restart_dedup(tmp_path):
 class API:
  calls=0
  def create(self,f,t):self.calls+=1;return 'rec_test'
 api=API();path=tmp_path/'meeting.db'
 assert '会议申请已提交' in submit(cmd(),'ou_bot','oc_test',Ledger(path),api,NOW)
 submit(cmd(),'ou_bot','oc_test',Ledger(path),api,NOW);assert api.calls==1
 assert '重复' in submit(cmd(),'ou_bot','oc_test',Ledger(path),api,NOW)
def test_unknown(tmp_path):
 class API:
  calls=0
  def create(self,f,t):self.calls+=1;raise TimeoutError()
 api=API();l=Ledger(tmp_path/'meeting.db')
 with pytest.raises(Invalid):submit(cmd(),'ou_bot','oc_test',l,api,NOW)
 assert '人工核查' in submit(cmd(),'ou_bot','oc_test',l,api,NOW);assert api.calls==1

def test_poll_success_no_duplicate(tmp_path):
 from meeting_jobs import tick
 class API:
  def create(self,f,t):return 'rec_one'
  def get(self,r):return {'状态':'已进日历','日程ID':'event','日历ID':'calendar','会议链接':'https://vc.feishu.cn/j/123'}
 l=Ledger(tmp_path/'meeting.db');api=API();submit(cmd(),'ou_bot','oc_test',l,api,NOW)
 messages=[]
 def send(*a,**kw):messages.append(a);return 'om_reply'
 import time
 tick(l,api,send,time.time()+40);tick(l,api,send,time.time()+200)
 assert len(messages)==1 and 'https://vc.feishu.cn/j/123' in messages[0][1]

def test_poll_bounded_and_missing_video(tmp_path):
 from meeting_jobs import result_text,tick
 assert '未取得' in result_text({'状态':'已进日历','日程ID':'e','日历ID':'c','会议链接':''})
 class API:
  n=0
  def create(self,f,t):return 'rec_one'
  def get(self,r):self.n+=1;return {'状态':'待进日历'}
 import time
 l=Ledger(tmp_path/'meeting.db');api=API();submit(cmd(),'ou_bot','oc_test',l,api,NOW);base=time.time();sent=[]
 for i in range(20):tick(l,api,lambda *a,**k:sent.append(a) or 'mid',base+40+i*61)
 assert api.n==6 and len(sent)==1

def test_boundary_reject_other_table(tmp_path):
 from scripts.meeting_boundary import classify
 from scripts.live_boundary import Denied
 with pytest.raises(Denied):classify({'meeting_app':'wrong','meeting_table':'wrong'},'https://open.feishu.cn/open-apis/bitable/v1/apps/wrong/tables/wrong/records',{},'POST')

def test_boundary_payload(tmp_path,monkeypatch):
 import meeting_config,json,hashlib
 import scripts.meeting_boundary as boundary
 cfg=meeting_config.target();cfg.update(app_token="offlineAppToken",schema_verified=True)
 config_path=tmp_path/"target.json";config_path.write_text(json.dumps(cfg))
 monkeypatch.setattr(meeting_config,"CONFIG_PATH",config_path)
 monkeypatch.setattr(boundary,"CONFIG_PATH",config_path)
 from scripts.meeting_boundary import classify
 APP,TABLE=cfg["app_token"],cfg["table_id"]
 l=Ledger(tmp_path/'meeting.db');f=parse(cmd(),'ou_bot',NOW);token,_,_=l.claim('key',f)
 a={'meeting_app':APP,'meeting_table':TABLE,'meeting_db':l.path,'chat':'oc_test','meeting_config_sha256':hashlib.sha256(config_path.read_bytes()).hexdigest()}
 url=f'https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{TABLE}/records?client_token={token}&user_id_type=open_id'
 assert classify(a,url,{'fields':f},'POST')=='meeting_create'
 from scripts.live_boundary import Denied
 with pytest.raises(Denied):classify(a,url,{'fields':{'状态':'测试'}},'POST')

def test_new_target_fail_closed():
 from meeting_config import target,WIKI_NODE
 from meeting import SDKTable
 cfg=target()
 assert cfg['table_id']=='example_fixture2' and cfg['app_token']!=WIKI_NODE
 if not cfg['schema_verified']:
  with pytest.raises(ValueError):SDKTable()

def test_legacy_ledger_preserved(tmp_path):
 import sqlite3
 path=tmp_path/'old.db'
 with sqlite3.connect(path) as c:
  c.execute('CREATE TABLE meeting(key TEXT)');c.execute('INSERT INTO meeting VALUES ("old")')
 with pytest.raises(Invalid):Ledger(path)
 with sqlite3.connect(path) as c:
  assert c.execute('SELECT key FROM meeting').fetchone()==('old',)
  assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='meeting_target'").fetchone()

def test_ledger_rejects_changed_target(tmp_path,monkeypatch):
 import meeting_config,json
 p=tmp_path/'config.json';cfg=meeting_config.target();p.write_text(json.dumps(cfg));monkeypatch.setattr(meeting_config,'CONFIG_PATH',p)
 l=Ledger(tmp_path/'new.db');l.claim('one',{'议题':'test'})
 cfg['app_token']='verifiedDifferentApp';p.write_text(json.dumps(cfg))
 with pytest.raises(Invalid):Ledger(l.path)

def test_hyperlink_result():
 from meeting_jobs import result_text
 assert 'https://vc.feishu.cn/j/123' in result_text({'状态':'已进日历','日程ID':'e','日历ID':'c','会议链接':{'text':'会议','link':'https://vc.feishu.cn/j/123'}})
