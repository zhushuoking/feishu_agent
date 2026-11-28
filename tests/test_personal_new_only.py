import calendar_sync,repo,db
from tests.helpers import make_task
from tests.test_personal_calendar import Accounts
from tests.test_calendar_sync import FakeApi

def strict(monkeypatch,cutoff):
 monkeypatch.setenv('CALENDAR_SYNC_MODE','personal');monkeypatch.setenv('CALENDAR_OWNER_ONLY','1');monkeypatch.setenv('CALENDAR_PERSONAL_NEW_ONLY','1');monkeypatch.setenv('CALENDAR_NEW_TASK_AFTER_ID',str(cutoff))

def test_only_new_owner_copies(database,monkeypatch):
 old=make_task();strict(monkeypatch,old.id)
 new=make_task(owners=('ou_new1','ou_new2'));accounts=Accounts();legacy=FakeApi()
 assert calendar_sync.sync_task(old.uid,legacy,accounts)['skipped']==1
 assert repo.get_calendar_event(old.uid) is None
 result=calendar_sync.sync_task(new.uid,legacy,accounts)
 assert result['created']==2 and set(accounts.apis)=={'ou_new1','ou_new2'}
 assert not legacy.created and not legacy.deleted and not legacy.attendees
 calendar_sync.sync_task(new.uid,legacy,accounts)
 assert all(len(x.created)==1 for x in accounts.apis.values())
 assert old.uid not in repo.pending_calendar_tasks(db.now(),mode='personal')

def test_no_legacy_migration_or_delete(database,monkeypatch):
 task=make_task();strict(monkeypatch,0)
 repo.reserve_calendar_event(task.uid,'legacy-calendar',task.deadline_at)
 repo.mark_calendar_event_created(task.uid,'legacy-event')
 legacy=FakeApi();accounts=Accounts()
 assert calendar_sync.sync_task(task.uid,legacy,accounts)['failed']==1
 assert not legacy.created and not legacy.deleted and not accounts.apis

def test_missing_cutoff_fails_closed(database,monkeypatch):
 task=make_task();strict(monkeypatch,'');accounts=Accounts()
 assert calendar_sync.sync_task(task.uid,FakeApi(),accounts)['skipped']==1
 assert repo.pending_calendar_tasks(db.now(),mode='personal')==[] and not accounts.apis

def test_uncertain_create_not_automatically_posted_again(database,monkeypatch):
 task=make_task();strict(monkeypatch,0)
 class FailingAccounts:
  def __init__(self):self.api=FakeApi(fail={'create'})
  def api_for_user(self,oid):return 'personal-test',self.api
 accounts=FailingAccounts();legacy=FakeApi()
 assert calendar_sync.sync_task(task.uid,legacy,accounts)['failed']==1
 assert len(accounts.api.created)==1
 assert calendar_sync.sync_task(task.uid,legacy,accounts)['failed']==1
 assert len(accounts.api.created)==1
