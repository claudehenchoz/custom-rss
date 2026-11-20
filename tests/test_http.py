from __future__ import annotations

import json
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import custom_rss.http as http_module
from custom_rss.http import USER_AGENT, FetchError, HttpClient


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def http_error(url: str, code: int, *, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(url, code, "reason", headers, None)


def install(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[object]
) -> tuple[list[Request], list[float]]:
    calls: list[Request] = []
    sleeps: list[float] = []

    def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
        calls.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, bytes)
        return FakeResponse(outcome)

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    return calls, sleeps


def test_retries_429_and_honours_integer_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.example.test/thing"
    calls, sleeps = install(monkeypatch, [http_error(url, 429, retry_after="5"), b'{"ok": true}'])

    assert HttpClient(USER_AGENT).get_json(url) == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [5.0]


def test_does_not_retry_other_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.example.test/missing"
    calls, sleeps = install(monkeypatch, [http_error(url, 404)])

    with pytest.raises(FetchError, match=r"HTTP 404"):
        HttpClient(USER_AGENT).get_json(url)

    assert len(calls) == 1
    assert sleeps == []


def test_retries_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.example.test/thing"
    calls, sleeps = install(monkeypatch, [b"{not json", json.dumps({"ok": True}).encode()])

    assert HttpClient(USER_AGENT).get_json(url) == {"ok": True}
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_exhausted_retries_report_url_and_final_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.example.test/down"
    calls, sleeps = install(monkeypatch, [OSError("boom"), OSError("boom"), OSError("boom")])

    with pytest.raises(FetchError) as caught:
        HttpClient(USER_AGENT, max_attempts=3).get_json(url)

    message = str(caught.value)
    assert url in message
    assert "after 3 attempts" in message
    assert "boom" in message
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_sends_configured_user_agent_and_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = install(monkeypatch, [b"payload"])

    payload = HttpClient("custom-agent/9").get_bytes(
        "https://example.test/feed", accept="application/rss+xml"
    )

    assert payload == b"payload"
    request = calls[0]
    assert request.get_header("User-agent") == "custom-agent/9"
    assert request.get_header("Accept") == "application/rss+xml"
