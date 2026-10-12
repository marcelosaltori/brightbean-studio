"""Bounded Instagram DM polling with resumable, JSON-safe checkpoints.

No tokens, paging URLs, message bodies or sender data enter the checkpoint.
The caller commits messages and this checkpoint together, after each batch.
"""

import copy
import json
import logging
import math
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import httpx

from .exceptions import APIError, ProviderError, RateLimitError
from .types import InboxMessage

logger = logging.getLogger(__name__)

# Work budget, not a claim about Meta's quota. The existing five-minute cycle
# does at most ten conversation reads and one listing page per account.
DM_BATCH_SIZE = 10
DM_RETRY_BASE_SECONDS = 300
DM_RETRY_MAX_SECONDS = 3600
DM_USAGE_PAUSE_PERCENT = 80


@dataclass
class InstagramInboxBatch:
    messages: list[InboxMessage]
    state: dict


def _timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _retry_seconds(headers, now):
    value = headers.get("retry-after")
    if not value:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, int((parsedate_to_datetime(value) - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


def _quota_usage(headers):
    peak, regain = 0.0, 0

    def visit(value):
        nonlocal peak, regain
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"call_count", "total_cputime", "total_time", "estimated_time_to_regain_access"}:
                    if isinstance(item, (int, float)) and math.isfinite(item) and item >= 0:
                        if key == "estimated_time_to_regain_access":
                            regain = max(regain, math.ceil(item * 60))
                        else:
                            peak = max(peak, float(item))
                elif isinstance(item, (list, dict)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for key in ("x-app-usage", "x-business-use-case-usage"):
        with suppress(TypeError, ValueError):
            visit(json.loads(headers.get(key, "{}")))
    return peak, regain


def _is_quota_error(exc):
    error = getattr(exc, "raw_response", {}).get("error") or {}
    return isinstance(exc, RateLimitError) or error.get("code") in {4, 17, 32, 613, 80002}


def pause_inbox_state(state, exc, now=None):
    """Apply the same account cooldown to a quota failure in either inbox channel."""
    now = now or datetime.now(UTC)
    failures = min(state.get("failures", 0) + 1, 10)
    state["failures"] = failures
    headers = getattr(exc, "response_headers", {})
    peak, regain = _quota_usage(headers)
    state["usage_peak_percent"] = peak
    state["estimated_regain_seconds"] = regain
    wait = max(
        min(DM_RETRY_BASE_SECONDS * 2 ** (failures - 1), DM_RETRY_MAX_SECONDS),
        _retry_seconds(headers, now),
        regain,
        getattr(exc, "retry_after", 0) or 0,
    )
    state["not_before"] = now.timestamp() + wait
    state["pause_reason"] = "quota" if _is_quota_error(exc) else "request_error"
    state["last_error"] = {
        "status": getattr(exc, "status_code", None),
        "code": (getattr(exc, "raw_response", {}).get("error") or {}).get("code"),
    }
    logger.warning("Instagram inbox batch paused: reason=%s retry_seconds=%d", state["pause_reason"], wait)


def _unreadable(exc):
    error = getattr(exc, "raw_response", {}).get("error") or {}
    return (
        isinstance(exc, APIError)
        and exc.status_code == 400
        and error.get("code") == 100
        and error.get("error_subcode") == 33
    )


def _transient(exc):
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, APIError) and exc.status_code is not None and exc.status_code >= 500
    )


class InstagramDMPoller:
    def __init__(self, request, read_thread, api_base, parse_time):
        self.request = request
        self.read_thread = read_thread
        self.api_base = api_base
        self.parse_time = parse_time

    def poll(self, access_token, state, since=None, now=None):
        now = now or datetime.now(UTC)
        state = copy.deepcopy(state or {})
        state.setdefault("version", 1)
        state.setdefault("floor", since.isoformat() if since else None)
        state.setdefault("pending", {})
        state["calls"] = 0
        state["checked_at"] = now.isoformat()
        messages = []
        if state.get("not_before", 0) > now.timestamp():
            return InstagramInboxBatch(messages, state)
        state["not_before"] = 0

        def observe(headers):
            peak, regain = _quota_usage(headers)
            state["usage_peak_percent"] = peak
            state["estimated_regain_seconds"] = regain
            if peak >= DM_USAGE_PAUSE_PERCENT:
                state["not_before"] = now.timestamp() + max(DM_RETRY_BASE_SECONDS, regain)
                state["pause_reason"] = "usage"

        def pause(exc):
            pause_inbox_state(state, exc, now)

        if not state.get("scanning"):
            state.update(
                scanning=True,
                scan_floor=state["floor"],
                scan_started=now.isoformat(),
                after=None,
                page=[],
                next_after=None,
            )

        if not state.get("page"):
            params = {"platform": "instagram", "fields": "id,updated_time", "limit": 50}
            if state.get("after"):
                params["after"] = state["after"]
            try:
                state["calls"] += 1
                resp = self.request(
                    "GET", f"{self.api_base}/me/conversations", access_token=access_token, params=params
                )
                observe(resp.headers)
                data = resp.json()
                page = []
                floor = _timestamp(state.get("scan_floor"))
                for convo in data.get("data", []):
                    updated = self.parse_time(convo.get("updated_time"))
                    if (
                        convo.get("id")
                        and str(convo["id"]) not in state["pending"]
                        and (not floor or not updated or updated >= floor)
                    ):
                        page.append(str(convo["id"]))
                paging = data.get("paging") or {}
                next_url = paging.get("next")
                next_after = (paging.get("cursors") or {}).get("after") if next_url else None
                # Some Graph responses expose the cursor only in the next URL.
                # Extract only that opaque cursor, never persist the URL/token.
                if next_url:
                    if urlparse(next_url).netloc != urlparse(self.api_base).netloc:
                        raise APIError("Off-host Instagram conversation pagination", status_code=502)
                    if not next_after:
                        next_after = (parse_qs(urlparse(next_url).query).get("after") or [None])[0]
                    if not next_after or next_after == state.get("after"):
                        raise APIError("Instagram conversation cursor missing or repeated", status_code=502)
                state["page"] = page
                state["next_after"] = next_after
            except (ProviderError, httpx.TransportError) as exc:
                pause(exc)
                return InstagramInboxBatch(messages, state)

        # Previously failed threads cannot starve readable ones. Reserve one
        # slot for a due retry, then continue the scan; retries retain their own
        # historical floor even after the scan watermark advances.
        due = [key for key, value in state["pending"].items() if value.get("retry_at", 0) <= now.timestamp()]
        work = [(key, True) for key in due[:1]]
        work += [(key, False) for key in state["page"][: DM_BATCH_SIZE - len(work)]]
        for convo_id, retry in work:
            if state["not_before"] > now.timestamp():
                break
            cutoff = (
                _timestamp(state["pending"][convo_id].get("floor")) if retry else _timestamp(state.get("scan_floor"))
            )
            try:
                state["calls"] += 1
                thread_messages, headers = self.read_thread(access_token, convo_id, cutoff)
                observe(headers)
                if cutoff is None:
                    for message in thread_messages:
                        message.extra["initial_backfill"] = True
                messages.extend(thread_messages)
                state["pending"].pop(convo_id, None)
                state["failures"] = 0
            except (ProviderError, httpx.TransportError) as exc:
                if _unreadable(exc):
                    state["pending"].pop(convo_id, None)
                    logger.warning("Skipping unreadable Instagram DM conversation (Graph 100/33)")
                elif _transient(exc) and not _is_quota_error(exc):
                    pending = state["pending"].get(convo_id, {})
                    failures = min(pending.get("failures", 0) + 1, 10)
                    wait = min(DM_RETRY_BASE_SECONDS * 2 ** (failures - 1), DM_RETRY_MAX_SECONDS)
                    state["pending"][convo_id] = {
                        "floor": cutoff.isoformat() if cutoff else None,
                        "failures": failures,
                        "retry_at": now.timestamp() + wait,
                    }
                    logger.warning("Instagram DM conversation deferred: retry_seconds=%d", wait)
                else:
                    pause(exc)
                    break
            if not retry:
                state["page"].remove(convo_id)

        if not state["page"]:
            if state.get("next_after"):
                state["after"] = state["next_after"]
            else:
                # Scan start, with overlap, is independent of MAX(message time).
                # Failed threads still carry their earlier floor in pending.
                started = _timestamp(state["scan_started"])
                state["floor"] = (started - timedelta(seconds=60)).isoformat()
                state["scanning"] = False
        state["pending_count"] = len(state["pending"])
        state["remaining_in_page"] = len(state["page"])
        return InstagramInboxBatch(messages, state)
