from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select, update

import calendar_auth
import calendar_sync
import db
import repo
from models_db import CalendarAccount, CalendarOAuthState, TASK_CANCELLED
from tests.helpers import make_task
from tests.test_calendar_sync import FakeApi, OWNER, CREATOR


class Accounts:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.apis = {}

    def api_for_user(self, open_id):
        if open_id in self.missing:
            raise calendar_auth.AuthorizationRequired("请连接日历")
        return "personal-" + open_id, self.apis.setdefault(open_id, FakeApi())


@pytest.fixture(autouse=True)
def personal_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("CALENDAR_SYNC_MODE", "personal")
    monkeypatch.setenv("FEISHU_APP_ID", "test-cli")
    monkeypatch.setenv("CALENDAR_TOKEN_KEY_FILE", str(tmp_path / "token.key"))
    monkeypatch.setenv("CALENDAR_AUTH_BASE_URL", "http://127.0.0.1:8766")
    monkeypatch.setenv("CALENDAR_AUTH_REQUIRE_PUBLIC", "0")


def test_personal_copies_require_no_attendee_invitations(database):
    task = make_task()
    accounts, legacy = Accounts(), FakeApi()
    stats = calendar_sync.sync_task(task.uid, legacy, accounts)
    assert stats["created"] == 2
    assert legacy.created == legacy.attendees == []
    for open_id, api in accounts.apis.items():
        assert api.created[0][0] == "personal-" + open_id
        assert api.created[0][1]["need_notification"] is False
        assert api.attendees == []
    assert len(repo.list_personal_calendar_events(task.uid)) == 2
    calendar_sync.sync_task(task.uid, legacy, accounts)
    assert all(len(api.created) == 1 for api in accounts.apis.values())
    assert repo.pending_calendar_tasks(db.now(), mode="personal") == []


def test_one_unbound_account_does_not_block_other_personal_calendar(database):
    task = make_task()
    accounts = Accounts(missing={CREATOR})
    stats = calendar_sync.sync_task(task.uid, FakeApi(), accounts)
    assert stats["created"] == 1 and stats["failed"] == 1
    assert repo.get_calendar_event(task.uid).retry_at is not None
    accounts.missing.clear()
    assert calendar_sync.sync_task(task.uid, FakeApi(), accounts)["failed"] == 0
    assert len(accounts.apis[OWNER].created) == 1
    assert repo.get_calendar_event(task.uid).sync_mode == "personal"


def test_personal_copies_follow_completion_and_cancel(database):
    task = make_task()
    accounts = Accounts()
    calendar_sync.sync_task(task.uid, FakeApi(), accounts)
    assert repo.transition_task(task.uid, "submit", OWNER, note="报告已提交 https://example.test/report").ok
    calendar_sync.sync_task(task.uid, FakeApi(), accounts)
    assert all("待验收" in api.updated[-1][2]["summary"] for api in accounts.apis.values())
    assert repo.transition_task(task.uid, "accept", CREATOR).ok
    calendar_sync.sync_task(task.uid, FakeApi(), accounts)
    assert all(api.updated[-1][2]["reminders"] == [] for api in accounts.apis.values())
    assert all(api.updated[-1][2]["free_busy_status"] == "free" for api in accounts.apis.values())

    other = make_task()
    calendar_sync.sync_task(other.uid, FakeApi(), accounts)
    assert repo.transition_task(other.uid, "cancel", CREATOR, note="测试取消").ok
    calendar_sync.sync_task(other.uid, FakeApi(), accounts)
    assert all(len(api.deleted) == 1 for api in accounts.apis.values())


def test_cancelled_task_does_not_request_authorization_or_create(database):
    task = make_task(status=TASK_CANCELLED)
    accounts = Accounts(missing={OWNER, CREATOR})
    result = calendar_sync.sync_task(task.uid, FakeApi(), accounts)
    assert result["failed"] == result["created"] == 0
    assert accounts.apis == {}


def test_legacy_invitation_is_cleaned_only_after_all_copies_succeed(database):
    task = make_task()
    repo.reserve_calendar_event(task.uid, "legacy-cal", task.deadline_at)
    repo.mark_calendar_event_created(task.uid, "legacy-event")
    repo.finish_calendar_sync(task.uid, task.version, task.deadline_at)
    assert task.uid in repo.pending_calendar_tasks(db.now(), mode="personal")
    accounts, legacy = Accounts(missing={CREATOR}), FakeApi()
    calendar_sync.sync_task(task.uid, legacy, accounts)
    assert legacy.deleted == []
    accounts.missing.clear()
    calendar_sync.sync_task(task.uid, legacy, accounts)
    assert legacy.deleted == [("legacy-cal", "legacy-event")]
    calendar_sync.sync_task(task.uid, legacy, accounts)
    assert len(legacy.deleted) == 1


def tokens(access="user-token", refresh="refresh-token"):
    return {"access_token": access, "refresh_token": refresh, "expires_in": 7200}


def prepare_authorization(monkeypatch):
    exchanges = []
    monkeypatch.setattr(calendar_auth, "_exchange", lambda **grant: exchanges.append(grant) or tokens())

    def request(method, path, **kwargs):
        if path.endswith("user_info"):
            return True, {"open_id": OWNER}, ""
        return True, {"calendars": [{"calendar": {"calendar_id": "personal-cal", "role": "owner"}}]}, ""

    monkeypatch.setattr(calendar_auth.calendar_api, "_request", request)
    url = calendar_auth.begin_authorization(OWNER)
    params = parse_qs(urlparse(url).query)
    assert "offline_access" in params["scope"][0]
    return params["state"][0], exchanges


def test_oauth_binds_login_identity_and_encrypts_tokens(database, monkeypatch):
    make_task()
    state, exchanges = prepare_authorization(monkeypatch)
    assert calendar_auth.finish_authorization("auth-code", state) == OWNER
    with db.session_scope() as session:
        account = session.get(CalendarAccount, OWNER)
    assert "user-token" not in account.encrypted_tokens
    assert "refresh-token" not in account.encrypted_tokens
    calendar_id, api = calendar_auth.api_for_user(OWNER)
    assert calendar_id == "personal-cal"
    assert api._token == "user-token"
    with pytest.raises(calendar_auth.AuthorizationRequired):
        calendar_auth.finish_authorization("auth-code", state)
    assert len(exchanges) == 1


def test_oauth_rejects_different_login_identity(database, monkeypatch):
    make_task()
    state, _ = prepare_authorization(monkeypatch)
    monkeypatch.setattr(calendar_auth.calendar_api, "_request", lambda *args, **kwargs: (
        True, {"open_id": CREATOR}, "",
    ))
    with pytest.raises(calendar_auth.AuthorizationRequired, match="不一致"):
        calendar_auth.finish_authorization("code", state)
    with db.session_scope() as session:
        assert session.get(CalendarAccount, OWNER) is None
        assert session.get(CalendarAccount, CREATOR) is None


def test_expired_oauth_state_does_not_exchange_code(database, monkeypatch):
    make_task()
    state, exchanges = prepare_authorization(monkeypatch)
    with db.session_scope() as session:
        session.execute(update(CalendarOAuthState).values(expires_at=db.now() - timedelta(seconds=1)))
    with pytest.raises(calendar_auth.AuthorizationRequired, match="过期"):
        calendar_auth.finish_authorization("code", state)
    assert exchanges == []


def test_unknown_task_participant_cannot_start_binding(database):
    make_task()
    with pytest.raises(calendar_auth.AuthorizationRequired):
        calendar_auth.begin_authorization("ou_unknown")


def test_cross_device_authorization_requires_configured_https_address(database, monkeypatch):
    monkeypatch.setenv("CALENDAR_AUTH_REQUIRE_PUBLIC", "1")
    with pytest.raises(calendar_auth.AuthorizationRequired, match="HTTPS"):
        calendar_auth.authorization_link(OWNER)
    monkeypatch.setenv("CALENDAR_AUTH_BASE_URL", "https://tasks.example.test")
    assert calendar_auth.authorization_link(OWNER).startswith("https://tasks.example.test/calendar/authorize?")


def test_refresh_rotates_and_persists_refresh_token(database, monkeypatch):
    calendar_auth._save(OWNER, "personal-cal", tokens())
    with db.session_scope() as session:
        session.execute(update(CalendarAccount).values(expires_at=db.now() - timedelta(seconds=1)))
    calls = []
    monkeypatch.setattr(calendar_auth, "_exchange", lambda **grant: calls.append(grant) or tokens("new-access", "new-refresh"))
    _, api = calendar_auth.api_for_user(OWNER)
    assert api._token == "new-access"
    calendar_auth.api_for_user(OWNER)
    assert calls == [{"grant_type": "refresh_token", "refresh_token": "refresh-token"}]
    with db.session_scope() as session:
        account = session.get(CalendarAccount, OWNER)
    assert calendar_auth._decrypt(account.encrypted_tokens)["refresh_token"] == "new-refresh"


def test_personal_token_cannot_be_reused_after_app_change(database, monkeypatch):
    calendar_auth._save(OWNER, "personal-cal", tokens())
    monkeypatch.setenv("FEISHU_APP_ID", "another-app")
    with pytest.raises(calendar_auth.AuthorizationRequired):
        calendar_auth.api_for_user(OWNER)
