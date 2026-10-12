"""Directed regressions for bounded DM reads, checkpoint recovery and cooldown."""

import copy
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from providers.exceptions import APIError, RateLimitError
from providers.instagram_inbox import DM_BATCH_SIZE, InstagramDMPoller
from providers.instagram_login import InstagramLoginProvider

NOW = datetime(2026, 10, 12, 8, tzinfo=UTC)
BASE = "https://graph.instagram.com/v25.0"


def response(data, headers=None):
    return SimpleNamespace(json=lambda: data, headers=headers or {})


def poller(request, read):
    return InstagramDMPoller(request, read, BASE, lambda value: datetime.fromisoformat(value) if value else None)


def message(key):
    return SimpleNamespace(platform_message_id=key, extra={})


def test_batch_resumes_without_relisting_or_rereading_successful_threads():
    request = MagicMock(return_value=response({"data": [{"id": f"t-{n}"} for n in range(DM_BATCH_SIZE + 2)]}))
    read = MagicMock(side_effect=lambda _token, key, _floor: ([message(key)], {}))
    first = poller(request, read).poll("token", {}, now=NOW)
    assert len(first.messages) == DM_BATCH_SIZE
    assert first.state["floor"] is None
    assert first.state["remaining_in_page"] == 2
    state_before = copy.deepcopy(first.state)
    second = poller(request, read).poll("token", first.state, since=NOW, now=NOW + timedelta(minutes=5))
    assert first.state == state_before
    assert [item.platform_message_id for item in second.messages] == ["t-10", "t-11"]
    request.assert_called_once()
    assert second.state["floor"] == (NOW - timedelta(seconds=60)).isoformat()
    assert all(call.args[2] is None for call in read.call_args_list)


def test_transient_thread_preserves_other_messages_and_retries_its_old_floor():
    request = MagicMock(return_value=response({"data": [{"id": "bad"}, {"id": "good"}]}))
    read = MagicMock(
        side_effect=[
            APIError("temporary", status_code=500, raw_response={"error": {"code": 2}}),
            ([message("good")], {}),
        ]
    )
    first = poller(request, read).poll("token", {}, now=NOW)
    assert [item.platform_message_id for item in first.messages] == ["good"]
    assert first.state["pending_count"] == 1
    assert first.state["pending"]["bad"]["floor"] is None
    assert first.state["pending"]["bad"]["retry_at"] == NOW.timestamp() + 300
    request.return_value = response({"data": []})
    read.side_effect = None
    read.return_value = ([message("recovered")], {})
    second = poller(request, read).poll("token", first.state, since=NOW, now=NOW + timedelta(minutes=5))
    assert second.state["pending_count"] == 0
    assert read.call_args.args == ("token", "bad", None)
    assert second.messages[0].extra["initial_backfill"] is True


@pytest.mark.parametrize(
    "error",
    [
        APIError("quota", status_code=403, raw_response={"error": {"code": 4, "error_subcode": 1349210}}),
        RateLimitError("quota", retry_after=900),
        APIError("permission", status_code=403, raw_response={"error": {"code": 10}}),
    ],
)
def test_account_errors_keep_successful_batch_and_unfinished_thread(error):
    request = MagicMock(return_value=response({"data": [{"id": "good"}, {"id": "blocked"}]}))
    read = MagicMock(side_effect=[([message("good")], {}), error])
    first = poller(request, read).poll("token", {}, now=NOW)
    assert len(first.messages) == 1
    assert first.state["page"] == ["blocked"]
    assert first.state["floor"] is None
    assert first.state["not_before"] >= NOW.timestamp() + 300
    request.reset_mock()
    read.reset_mock()
    paused = poller(request, read).poll("token", first.state, now=NOW + timedelta(seconds=60))
    assert paused.state["calls"] == 0
    request.assert_not_called()
    read.assert_not_called()


def test_quota_pause_uses_retry_after_and_meta_regain_time_then_backs_off():
    error = APIError(
        "quota",
        status_code=403,
        raw_response={"error": {"code": 4}},
        response_headers={
            "retry-after": "600",
            "x-business-use-case-usage": json.dumps(
                {"private-id": [{"call_count": 99, "estimated_time_to_regain_access": 20}]}
            ),
        },
    )
    request = MagicMock(side_effect=error)
    first = poller(request, MagicMock()).poll("token", {}, now=NOW)
    assert first.state["not_before"] == NOW.timestamp() + 1200
    assert "private-id" not in json.dumps(first.state)
    assert first.state["usage_peak_percent"] == 99
    second = poller(request, MagicMock()).poll("token", first.state, now=NOW + timedelta(minutes=20))
    assert second.state["failures"] == 2


def test_usage_threshold_stops_before_more_requests():
    request = MagicMock(return_value=response({"data": [{"id": "one"}, {"id": "two"}]}))
    read = MagicMock(return_value=([message("one")], {"x-app-usage": '{"call_count":85}'}))
    batch = poller(request, read).poll("token", {}, now=NOW)
    assert len(batch.messages) == 1
    assert batch.state["page"] == ["two"]
    assert batch.state["pause_reason"] == "usage"
    read.assert_called_once()


def test_pagination_persists_only_opaque_cursor_and_handles_empty_page():
    request = MagicMock(
        side_effect=[
            response({"data": [], "paging": {"next": f"{BASE}/me/conversations?after=cursor-2&access_token=secret"}}),
            response({"data": [{"id": "older", "updated_time": "2025-01-01T00:00:00+00:00"}]}),
        ]
    )
    read = MagicMock(return_value=([message("old")], {}))
    first = poller(request, read).poll("token", {}, now=NOW)
    assert first.state["after"] == "cursor-2"
    assert first.state["floor"] is None
    assert "secret" not in json.dumps(first.state)
    second = poller(request, read).poll("token", first.state, since=NOW, now=NOW + timedelta(minutes=5))
    assert len(second.messages) == 1
    assert request.call_args.kwargs["params"]["after"] == "cursor-2"
    assert read.call_args.args[2] is None


def test_listing_errors_are_not_skipped_as_unreadable_threads():
    error = APIError("unreadable", status_code=400, raw_response={"error": {"code": 100, "error_subcode": 33}})
    request = MagicMock(side_effect=error)
    batch = poller(request, MagicMock()).poll("token", {}, now=NOW)
    assert batch.state["floor"] is None
    assert batch.state["pause_reason"] == "request_error"


def test_real_http_error_retains_only_quota_headers_and_supports_http_date(monkeypatch):
    provider = InstagramLoginProvider({})
    reply = httpx.Response(
        429,
        json={"error": {"code": 4}},
        headers={"Retry-After": "Mon, 12 Oct 2026 08:20:00 GMT", "Set-Cookie": "private"},
    )
    client = MagicMock()
    client.__enter__.return_value.request.return_value = reply
    monkeypatch.setattr("providers.base.httpx.Client", lambda **_kwargs: client)
    with pytest.raises(RateLimitError) as caught:
        provider._request("GET", f"{BASE}/me/conversations", access_token="token")
    assert caught.value.response_headers == {"retry-after": "Mon, 12 Oct 2026 08:20:00 GMT"}
    request = MagicMock(side_effect=caught.value)
    batch = poller(request, MagicMock()).poll("token", {}, now=NOW)
    assert batch.state["not_before"] == NOW.timestamp() + 1200


def test_comment_quota_failure_pauses_following_dm_and_comment_requests():
    provider = InstagramLoginProvider({})
    provider._request = MagicMock(return_value=response({"data": []}))
    provider._fetch_media_comments = MagicMock(
        side_effect=APIError("quota", status_code=403, raw_response={"error": {"code": 4}})
    )
    batch = provider.get_inbox_batch("token", dm_state={}, dm_since=None, comment_since=None)
    assert batch.state["pause_reason"] == "quota"
    provider._request.reset_mock()
    provider._fetch_media_comments.reset_mock()
    provider.get_inbox_batch("token", dm_state=batch.state, dm_since=None, comment_since=None)
    provider._request.assert_not_called()
    provider._fetch_media_comments.assert_not_called()


def test_comment_collector_catching_quota_cannot_issue_further_http_requests():
    provider = InstagramLoginProvider({})
    provider._request = MagicMock(
        side_effect=[response({"data": []}), APIError("quota", status_code=403, raw_response={"error": {"code": 4}})]
    )

    def collect(_token, _since, *, request):
        for _ in range(3):
            with suppress(APIError):
                request("GET", f"{BASE}/comments", access_token="token")
        return [message("preserved-comment")]

    provider._fetch_media_comments = collect
    batch = provider.get_inbox_batch("token", dm_state={}, dm_since=None, comment_since=None)
    assert provider._request.call_count == 2
    assert len(batch.messages) == 1
    assert batch.state["failures"] == 1
    assert batch.state["pause_reason"] == "quota"
