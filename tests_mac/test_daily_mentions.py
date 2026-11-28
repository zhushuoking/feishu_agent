import pytest
from models import Mention
from detector import _validate_mentions
from tests.helpers import make_recognition,make_cmd
import repo,db

def recognition(ids):
 r=make_recognition();r.tasks[0].assignee_mention_ids=ids;return r

def test_key_normalization_dedup():
 r=_validate_mentions(recognition(['@_1','ou_other','@_1']),[Mention('@_1','某成员','ou_other')]);assert r.tasks[0].assignee_mention_ids==['ou_other']
def test_two_and_separate_tasks():
 r=recognition(['@_1']);r.tasks.append(recognition(['@_2']).tasks[0]);r=_validate_mentions(r,[Mention('@_1','甲','ou_one'),Mention('@_2','乙','ou_two')]);assert [x.assignee_mention_ids for x in r.tasks]==[['ou_one'],['ou_two']]
 r=_validate_mentions(recognition(['@_1','@_2']),[Mention('@_1','甲','ou_one'),Mention('@_2','乙','ou_two')]);assert r.tasks[0].assignee_mention_ids==['ou_one','ou_two']
@pytest.mark.parametrize('mention',[Mention('@_1','未知',''),Mention('all','所有人','all'),Mention('@_1','机器人','cli_bot')])
def test_invalid(mention):
 r=_validate_mentions(recognition([mention.key]),[mention]);assert not r.tasks[0].assignee_mention_ids;assert 'assignee' in r.tasks[0].missing_fields

def test_bot_openid(monkeypatch):
 monkeypatch.setenv('FEISHU_BOT_OPEN_ID','ou_bot');r=_validate_mentions(recognition(['@_bot']),[Mention('@_bot','任意名','ou_bot')]);assert not r.tasks[0].assignee_mention_ids

def test_unresolved_blocks_confirmation(tmp_path,monkeypatch):
 monkeypatch.setenv('FEISHU_ALLOWED_CHAT_IDS','oc_001');monkeypatch.setenv('EVENT_FLUSH_IMMEDIATE','0');monkeypatch.setenv('CALENDAR_SYNC_ENABLED','0');monkeypatch.setenv('FEISHU_NATIVE_TASK_ENABLED','0')
 db.configure(str(tmp_path/'business.db'));db.init_db()
 r=_validate_mentions(recognition(['@_good','@_bad']),[Mention('@_good','成员','ou_valid')]);c=repo.save_candidates(r,make_cmd())[0]
 with pytest.raises(repo.CandidateActionError):repo.confirm_candidate(c.uid,operator_open_id='ou_sender')
 assert repo.count_tasks()==0;db.get_engine().dispose()

def test_delivery_partial_not_success(monkeypatch):
 import feishu_api
 from types import SimpleNamespace
 monkeypatch.setattr(feishu_api,'_deliver_task_card',lambda task,oid,*a,**k:oid=='ou_one')
 monkeypatch.setattr(feishu_api.cards if hasattr(feishu_api,'cards') else __import__('cards'),'build_task_card',lambda *a,**k:{})
 assert feishu_api.push_task_card(SimpleNamespace(owner_open_ids=['ou_one','ou_two'])) is None

def test_daily_known_people(tmp_path):
 from scripts.daily_policy import people
 import sqlite3,json
 p=tmp_path/'db';c=sqlite3.connect(p);c.executescript('CREATE TABLE task_candidate(sender_open_id,assignee_open_ids,chat_id);CREATE TABLE task(creator_open_id,reviewer_open_id,chat_id);');c.execute('INSERT INTO task_candidate VALUES (?,?,?)',('ou_sender',json.dumps(['ou_new']),'group'));c.commit();c.close()
 assert people({'database':str(p),'chat':'group'})=={'ou_sender','ou_new'}

def test_arbitrary_two_confirm_and_delivery(tmp_path,monkeypatch):
 import feishu_api,cards
 monkeypatch.setenv('FEISHU_ALLOWED_CHAT_IDS','oc_001');monkeypatch.setenv('EVENT_FLUSH_IMMEDIATE','0');monkeypatch.setenv('CALENDAR_SYNC_ENABLED','0');monkeypatch.setenv('FEISHU_NATIVE_TASK_ENABLED','0')
 db.configure(str(tmp_path/'formal.db'));db.init_db()
 r=_validate_mentions(recognition(['@_new1','@_new2','@_new1']),[Mention('@_new1','新成员1','ou_new1'),Mention('@_new2','新成员2','ou_new2')])
 c=repo.save_candidates(r,make_cmd())[0];t=repo.confirm_candidate(c.uid,operator_open_id='ou_sender');assert set(t.owner_open_ids)=={'ou_new1','ou_new2'}
 assert repo.confirm_candidate(c.uid,operator_open_id='ou_sender') is None;assert repo.count_tasks()==1
 deliveries=[]
 monkeypatch.setattr(feishu_api,'_deliver_task_card',lambda t,oid,*a,**k:deliveries.append(oid) or True)
 assert feishu_api.push_task_card(t)=='ok';assert deliveries==['ou_new1','ou_new2'];db.get_engine().dispose()


def test_daily_window_and_cap(tmp_path):
 import json,time
 from scripts.live_boundary import Guard,Denied,source_digest,digest
 root=tmp_path;(root/'.env').write_text('mock')
 a={'session':'daily_synthetic','mode':'daily','approved':True,'other_consumers_stopped':True,'platform_verified':True,'start':time.time()-1,'end':time.time()+8*3600,'root':str(root),'source_sha256':source_digest(root),'env_sha256':digest(root/'.env'),'bot_open_id':'ou_bot','limits':{'ws':1}}
 p=root/'permit.json';p.write_text(json.dumps(a));g=Guard(p,digest(p));g.reserve('ws')
 with pytest.raises(Denied):Guard(p,digest(p)).reserve('ws')
