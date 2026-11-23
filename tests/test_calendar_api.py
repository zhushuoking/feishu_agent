"""日历接口边界回归，全部使用本地响应。"""

import httpx
import pytest

import calendar_api


@pytest.mark.parametrize("code,expected", [(0, True), (193001, True), (193003, True), (191002, False)])
def test_delete_retries_treat_only_missing_events_as_success(monkeypatch, code, expected):
    monkeypatch.setattr(calendar_api, "_tenant_token", lambda: "test-token")
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200 if code == 0 else 403, json={"code": code, "msg": "test"},
    ))
    client_class = httpx.Client
    monkeypatch.setattr(calendar_api.httpx, "Client", lambda **kwargs: client_class(transport=transport))
    assert calendar_api.delete_event("cal", "event") is expected
    if not expected:
        assert "191002" in calendar_api.last_error()


def test_check_uses_configured_calendar_and_reports_missing_write_access(monkeypatch):
    monkeypatch.setenv("FEISHU_CALENDAR_ID", "configured-calendar")
    calls = []

    def request(method, path):
        calls.append((method, path))
        return True, {"calendar": {"calendar_id": "configured-calendar", "role": "reader"}}, ""

    monkeypatch.setattr(calendar_api, "_request", request)
    assert calendar_api.check()["ok"] is False
    assert calls == [("GET", calendar_api.CALENDAR_PATH + "/configured-calendar")]


def test_check_reads_flat_calendar_response(monkeypatch):
    monkeypatch.setenv("FEISHU_CALENDAR_ID", "configured-calendar")
    monkeypatch.setattr(calendar_api, "_request", lambda *args: (
        True, {"calendar_id": "configured-calendar", "role": "owner", "type": "shared"}, "",
    ))
    info = calendar_api.check()
    assert info["ok"] is True
    assert info["role"] == "owner"


def test_http_errors_are_available_to_sync_diagnostics(monkeypatch):
    monkeypatch.setattr(calendar_api, "_tenant_token", lambda: "test-token")
    transport = httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway"))
    client_class = httpx.Client
    monkeypatch.setattr(calendar_api.httpx, "Client", lambda **kwargs: client_class(transport=transport))
    assert calendar_api.create_event("cal", {}, "test-key") is None
    assert calendar_api.last_error()
