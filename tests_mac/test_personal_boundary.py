import json,sqlite3,time
from pathlib import Path
import pytest
from scripts.personal_boundary import classify
from scripts.live_boundary import Guard,Denied,digest,source_digest

def setup(tmp_path):
 (tmp_path/'.env').write_text('mock');key=tmp_path/'key';key.write_bytes(b'x'*32);p=tmp_path/'business.db'
 with sqlite3.connect(p) as c:
  c.executescript('CREATE TABLE task(id,uid,chat_id);CREATE TABLE personal_calendar_event(task_uid,calendar_id,event_id);');c.execute('INSERT INTO task VALUES (2,"task","group")');c.execute('INSERT INTO personal_calendar_event VALUES ("task","personal-cal","event")')
 a={'personal_calendar':True,'app_id':'mock','oauth_base_url':'https://callback.example.test','database':str(p),'calendar_cutoff':1,'chat':'group','session':'synthetic','approved':True,'platform_verified':True,'other_consumers_stopped':True,'start':time.time()-1,'end':time.time()+60,'root':str(tmp_path),'source_sha256':source_digest(tmp_path),'env_sha256':digest(tmp_path/'.env'),'calendar_key_file':str(key),'calendar_key_sha256':digest(key),'limits':{'http':4,'auth':4,'oauth':4},'model_url':'https://model.test/v1'}
 return a

def test_personal_routes_and_legacy_rejected(tmp_path):
 a=setup(tmp_path)
 assert classify(a,'https://open.feishu.cn/open-apis/calendar/v4/calendars/personal-cal/events',{},'POST')=='calendar_create'
 assert classify(a,'https://open.feishu.cn/open-apis/calendar/v4/calendars/personal-cal/events/event',{},'PATCH')=='calendar_update'
 for url in ['https://open.feishu.cn/open-apis/calendar/v4/calendars/old-cal/events','https://open.feishu.cn/open-apis/calendar/v4/calendars/personal-cal/events/unknown']:
  with pytest.raises(Denied):classify(a,url,{},'POST')
 a['calendar_cutoff']=2
 with pytest.raises(Denied):classify(a,'https://open.feishu.cn/open-apis/calendar/v4/calendars/personal-cal/events',{},'POST')

def test_oauth_client_and_redirect_pinned(tmp_path):
 a=setup(tmp_path);body={'client_id':'mock','grant_type':'authorization_code','redirect_uri':a['oauth_base_url']+'/calendar/callback'}
 assert classify(a,'https://accounts.feishu.cn/oauth/v3/token',body,'POST')=='oauth'
 body['redirect_uri']='https://other.test/calendar/callback'
 with pytest.raises(Denied):classify(a,'https://accounts.feishu.cn/oauth/v3/token',body,'POST')

def test_form_token_transport_is_budgeted(tmp_path,monkeypatch):
 import httpx,requests,lark_oapi.ws.client as ws
 from scripts.live_boundary import install_network
 a=setup(tmp_path);p=tmp_path/'permit';p.write_text(json.dumps(a));g=Guard(p,digest(p))
 class Response:
  status_code=200
  def json(self):return {'access_token':'synthetic'}
 with monkeypatch.context() as m:
  m.setattr(httpx.Client,'send',lambda *a,**k:Response());m.setattr(requests.Session,'send',requests.Session.send);m.setattr(httpx.AsyncClient,'send',httpx.AsyncClient.send);m.setattr(ws.websockets,'connect',ws.websockets.connect)
  install_network(g)
  with httpx.Client() as client:
   assert client.post('https://accounts.feishu.cn/oauth/v3/token',data={'client_id':'mock','grant_type':'authorization_code','redirect_uri':a['oauth_base_url']+'/calendar/callback','code':'synthetic'}).status_code==200
 with sqlite3.connect(g.state) as c:assert dict(c.execute('SELECT kind,n FROM counts'))=={'http':1,'auth':1,'oauth':1}
