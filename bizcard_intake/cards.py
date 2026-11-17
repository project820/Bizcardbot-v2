"""Durable per-card records: one JSON file per business card.

Every photo becomes a card with its own id, status and draft revision, so
several cards can be in review at once, buttons always target one card, and a
failed save keeps its draft and images until it is saved or cancelled.

Statuses:
  processing  -> image prep / OCR / parsing running
  review      -> draft ready for the approver (also after a failed recognition)
  saving      -> approved revision is being written to Google Contacts
  save_failed -> approved, but Google rejected or was unreachable; retried later
  saved       -> contact exists in Google (photo may still be pending)
  cancelled   -> discarded by the approver
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

CARD_DIR = Path("data/cards")
OPEN_STATUSES = ("processing", "review", "saving", "save_failed", "needs_confirmation")
REVIEW_STATUSES = ("review", "save_failed")
_seq_lock = threading.Lock()
followup_lock = threading.RLock()
_caption_lock = threading.Lock()
_write_locks_guard = threading.Lock()
_write_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
_pending_message_lock = threading.Lock()
_pending_messages: dict[tuple[str, int], tuple[str, object]] = {}
JOB_CHANGE_FIELDS = ("job_change", "job_change_lookup_key", "job_change_pending",
                     "job_change_lookup_id", "job_change_started_at")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_card(chat_id, *, media_group_id: Optional[str] = None, source: str = "telegram",
             caption: str = "") -> dict:
    card_id = time.strftime("%y%m%d") + secrets.token_hex(3)
    card = {
        "id": card_id,
        "seq": _next_seq(),
        "chat_id": str(chat_id),
        "source": source,
        "status": "processing",
        "revision": 0,
        "created_at": now(),
        "updated_at": now(),
        "media_group_id": media_group_id,
        "caption": caption.strip()[:300],
        "images": {},
        "draft": {},
        "front_draft": None,
        "back_draft": None,
        "awaiting_back": False,
        "message_ids": [],
        "approved_revision": None,
        "resource_name": None,
        "save_action": None,
        "photo_status": None,
        "save_attempts": 0,
        "last_error": None,
        "error_kind": None,
        "next_retry_at": None,
        "history": [],
    }
    save_card(card)
    return card


def label(card: dict) -> str:
    return f"[명함 {card.get('seq') or card['id']}]"


def card_path(card_id: str) -> Path:
    return CARD_DIR / f"{card_id}.json"


class StaleCardState(RuntimeError):
    """A delayed full-card write cannot replace durable save state."""


def save_card(card: dict) -> None:
    # Channel callbacks can hold a pre-save copy while a worker writes Contacts.
    # Save journals for every backend and consumed nonces survive delayed UI writes.
    with _write_locks_guard:
        lock = _write_locks[card["id"]]
    with lock:
        stored = load_card(card["id"]) or {}
        lookup_version = stored.get("_job_change_version", 0)
        if (card.get("_job_change_version", 0) != lookup_version
                and card.get("revision") == stored.get("revision")):
            # A callback holding card_lock may finish after timeout fallback.
            # Its old snapshot must not resurrect pending or a stale suggestion.
            for key in JOB_CHANGE_FIELDS:
                if key in stored:
                    card[key] = stored[key]
                else:
                    card.pop(key, None)
        if any(stored.get(key) != card.get(key) for key in JOB_CHANGE_FIELDS):
            lookup_version += 1
        card["_job_change_version"] = lookup_version
        followup_version = stored.get("_followup_version", 0)
        followup_keys = {key for key in (*stored, *card) if key.startswith("followup_")}
        if card.get("_followup_version", 0) != followup_version:
            # Delayed UI writes may not resurrect a consumed reminder nonce.
            for key in followup_keys:
                if key in stored:
                    card[key] = stored[key]
                else:
                    card.pop(key, None)
        elif any(stored.get(key) != card.get(key) for key in followup_keys):
            followup_version += 1
        if followup_version:
            card["_followup_version"] = followup_version
        before, after = stored.get("save_intent"), card.get("save_intent")
        # Old JSON has version zero. Advance only on save-state changes so
        # reminder/message-only writes do not invalidate a save worker.
        version = stored.get("_save_state_version", 0)
        if card.get("_save_state_version", 0) != version:
            raise StaleCardState("이전 저장 결과를 먼저 확인하세요. 카드 상태가 변경되었습니다.")
        if before and before != after:
            authorized = (stored.get("save_resolution") == "again" and after
                          and after.get("generation") == int(before.get("generation", 0)) + 1
                          and all(after.get(key) == before.get(key) for key in ("backend", "container")))
            if not authorized:
                raise StaleCardState("이전 저장 작업을 삭제하거나 바꿀 수 없습니다.")
        state_fields = ("save_intent", "status", "approved_revision", "resource_name", "save_action",
                        "photo_status", "save_attempts", "last_error", "error_kind", "next_retry_at",
                        "save_resolution", "confirmation_id", "job_choice")
        if any(stored.get(key) != card.get(key) for key in state_fields):
            version += 1
        card["updated_at"] = now()
        CARD_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(card, _save_state_version=version)
        _atomic_write(card_path(card["id"]), json.dumps(payload, ensure_ascii=False, indent=2))
        card["_save_state_version"] = version


def load_card(card_id: str) -> Optional[dict]:
    if not card_id or "/" in card_id or ".." in card_id:
        return None
    path = card_path(card_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_cards(chat_id=None, statuses: Optional[Iterable[str]] = None) -> list[dict]:
    if not CARD_DIR.exists():
        return []
    wanted = set(statuses) if statuses else None
    found = []
    for path in sorted(CARD_DIR.glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # one corrupt file must not block the others
        if chat_id is not None and card.get("chat_id") != str(chat_id):
            continue
        if wanted is not None and card.get("status") not in wanted:
            continue
        found.append(card)
    found.sort(key=lambda c: (c.get("created_at") or "", c.get("seq") or 0))
    return found


def find_by_message(chat_id, message_id) -> Optional[dict]:
    if message_id is None:
        return None
    with _pending_message_lock:
        pending = _pending_messages.get((str(chat_id), message_id))
    if pending:
        card = load_card(pending[0])
        if card is not None and card.get("chat_id") == str(chat_id):
            return card
    for card in reversed(list_cards(chat_id)):
        if message_id in card.get("message_ids", []):
            return card
    return None


def group_cards(chat_id, media_group_id: str) -> list[dict]:
    return [c for c in list_cards(chat_id) if c.get("media_group_id") == media_group_id]


def remember_message(card: dict, message_id: Optional[int]) -> None:
    if message_id is not None and message_id not in card["message_ids"]:
        card["message_ids"] = (card["message_ids"] + [message_id])[-20:]


@contextmanager
def pending_message(card: dict, message_id: Optional[int]):
    """Route replies only while this synchronous record attempt is in flight."""
    key = (str(card["chat_id"]), message_id)
    card_id = card["id"]
    token = object()
    if message_id is not None:
        with _pending_message_lock:
            _pending_messages[key] = (card_id, token)
    try:
        yield token
    finally:
        if message_id is not None:
            with _pending_message_lock:
                pending = _pending_messages.get(key)
                if pending is not None and pending[1] is token:
                    _pending_messages.pop(key, None)


def add_history(card: dict, event: str, detail: str = "") -> None:
    card.setdefault("history", []).append({"at": now(), "event": event, "detail": detail[:300]})
    card["history"] = card["history"][-30:]


def load_state(name: str) -> dict:
    path = CARD_DIR / f"_{name}.state"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(name: str, data: dict) -> None:
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(CARD_DIR / f"_{name}.state", json.dumps(data, ensure_ascii=False, indent=2))


def group_caption(card: dict, caption: str = "") -> tuple[str, bool]:
    """Return the first album caption and whether this call registered it.

    Writes expire entries after 48 hours. Legacy strings acquire a timestamp
    on the next write, since their original registration time is unknown.
    No other card's lock is acquired here.
    """
    group = card.get("media_group_id")
    if not group:
        return "", False
    key = str(card["chat_id"]) + ":" + str(group)
    with _caption_lock:
        saved = load_state("album_captions")
        registered = False
        if caption.strip():
            timestamp = time.time()
            saved = {key: {"caption": value, "created_at": timestamp} if isinstance(value, str) else value
                     for key, value in saved.items()
                     if isinstance(value, str) or timestamp - value["created_at"] <= 48 * 3600}
            if not saved.get(key):
                saved[key] = {"caption": caption.strip()[:300], "created_at": timestamp}
                registered = True
            save_state("album_captions", saved)
        value = saved.get(key, "")
        return (value if isinstance(value, str) else value["caption"]), registered


def initialize_followup() -> dict:
    with followup_lock:
        state = load_state("followup")
        if "feature_started_at" not in state:
            state.update(feature_started_at=time.time(), enabled=True)
            save_state("followup", state)
        return state


def followup_days() -> int:
    try:
        return max(0, int(os.environ.get("BIZCARD_FOLLOWUP_DAYS", "3")))
    except ValueError:
        return 3


_followup_zone_warned = False


def _followup_zone():
    global _followup_zone_warned
    zone = os.environ.get("BIZCARD_TZ", "").strip()
    if zone:
        try:
            return ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            with followup_lock:
                if not _followup_zone_warned:
                    logging.getLogger("bizcard").warning("Invalid BIZCARD_TZ; using system timezone")
                    _followup_zone_warned = True
    return None


def followup_localtime(timestamp: float) -> datetime:
    zone = _followup_zone()
    return datetime.fromtimestamp(timestamp, zone) if zone else datetime.fromtimestamp(timestamp).astimezone()


def followup_due(timestamp: float, days: int) -> float:
    zone = _followup_zone()
    # Naive local datetime keeps system timezone DST rules at the target date.
    due = (datetime.fromtimestamp(timestamp, zone) if zone else datetime.fromtimestamp(timestamp)) + timedelta(days=days)
    if due.hour < 10:
        due = due.replace(hour=10, minute=0, second=0, microsecond=0)
    elif due.hour >= 19:
        due = (due + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return due.timestamp()


def schedule_followup(card: dict) -> None:
    """Enroll only a newly successful save, never the scheduler's legacy scan."""
    if card.get("followup_state"):
        return
    state = initialize_followup()
    days = followup_days()
    saved_at = time.time()
    card["followup_saved_at"] = saved_at
    if not days or not state.get("enabled", True):
        card.update(followup_state="disabled", followup_due_at=None)
        return
    card.update(followup_state="pending", followup_due_at=followup_due(saved_at, days),
                followup_nonce=None)


def _next_seq() -> int:
    with _seq_lock:
        state = load_state("seq")
        seq = int(state.get("seq", 0)) + 1
        save_state("seq", {"seq": seq})
        return seq


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def finish_job_change(candidate: dict, *, fallback: bool = False) -> Optional[dict]:
    """Atomically finish only this lookup; timeout owns no other card fields.

    Normally called under pipeline.card_lock. On timeout the short storage lock
    permits a narrow fallback while a callback holds card_lock across network I/O.
    save_card's lookup version prevents that callback from restoring old metadata.
    """
    with _write_locks_guard:
        lock = _write_locks[candidate["id"]]
    with lock:
        fresh = load_card(candidate["id"])
        if (not fresh or fresh.get("status") != "review"
                or fresh.get("revision") != candidate.get("revision")
                or fresh.get("save_intent") != candidate.get("save_intent")
                or fresh.get("job_change_lookup_id") != candidate.get("job_change_lookup_id")
                or not fresh.get("job_change_pending")):
            return None
        fresh["job_change"] = None if fallback else candidate.get("job_change")
        fresh["job_change_lookup_key"] = None if fallback else candidate.get("job_change_lookup_key")
        fresh["job_change_pending"] = False
        fresh.pop("job_change_started_at", None)
        fresh.pop("job_change_lookup_id", None)
        save_card(fresh)
        return fresh
