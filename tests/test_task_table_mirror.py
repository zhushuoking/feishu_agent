import json
from lark_oapi.core.json import JSON
from lark_oapi.api.bitable.v1 import AppTableRecord
from task_table_sync import Mirror

class API:
    def __init__(self): self.created=0; self.updated=0
    def create(self,fields,token): self.created+=1; return 'record_fixture'
    def update(self,record,fields): self.updated+=1

def test_one_record_updates_and_restart(tmp_path):
    policy=dict(app_id='app',tenant='tenant',chat='oc_test',task_table_app='base',task_table_table='table',task_table_cutoff=0,task_table_db=str(tmp_path/'ledger.sqlite3'))
    api=API();m=Mirror(policy);m.apply('task',{'状态':'进行中'},api);m.apply('task',{'状态':'进行中'},api)
    Mirror(policy).apply('task',{'状态':'已完成'},api)
    assert (api.created,api.updated)==(1,1)

def test_sdk_payload_does_not_drop_unfinished_completion_time():
    from workbench_profile import map_task
    f=map_task(dict(uid='task',status='IN_PROGRESS',priority='normal',creator_open_id='ou_creator',message_id='message',title='工作',chat_id='oc_test',deadline_at='2026-12-01T17:00:00+08:00',created_at='2026-11-28T10:00:00+08:00'),{'日程ID':'event','日历ID':'calendar'},['ou_owner'],lambda s:s)
    assert '完成时间' not in f
    assert json.loads(JSON.marshal(AppTableRecord.builder().fields(f).build()))['fields']==f
