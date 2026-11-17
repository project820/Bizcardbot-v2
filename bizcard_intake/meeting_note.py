"""Durable optional meeting notes; no channel calls or implicit TCC prompts.

The scheduler submits drain() every 60 seconds. A nonblocking lock allows only
one drain at a time; all external work happens outside pipeline.card_lock.
Callers own delivery/acknowledgement of notices (last_notice_at in _notes.state,
and note.notice_pending on failed cards). No contact values enter that state.
"""
from __future__ import annotations

from datetime import datetime
import os
import threading
import time

from . import apple_contacts, cards, contact_backend, contacts
from .draft import CardDraft

_drain_lock = threading.Lock()
_state_lock = threading.RLock()
RETRY_SECONDS = (60, 120, 300, 900)


def enabled() -> bool:
    return os.environ.get("BIZCARD_MEETING_NOTE", "on").strip().lower() not in ("off", "0", "false", "no")


def compose(card: dict, draft: CardDraft, caption: str = "") -> str:
    """Build one bounded block with a stable, per-card replay marker."""
    try:
        raw = str(card.get("created_at") or "").replace("Z", "+00:00")
        try:
            received = datetime.fromisoformat(raw)
        except ValueError:
            received = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        day = cards.followup_localtime(received.timestamp()).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        day = "날짜 미기재"
    marker = f"[명함 {day} #{str(card['id'])[-6:]}]"
    lines = [marker]
    caption = caption or card.get("caption") or cards.group_caption(card)[0]
    google = (card.get("resource_name") or "").startswith("people/")
    if caption.strip():
        lines.append("만남 메모: " + caption.strip()[:300])
    if draft.notes and not google:
        lines.append(draft.notes)
    if card.get("job_choice") == "update":
        previous = card.get("job_change") or {}
        old = " ".join(value for value in (previous.get("company"), previous.get("title")) if value)
        if old:
            lines.append("이전 소속: " + old)
    if draft.warnings and not google:
        lines.append("확인 필요: " + "; ".join(draft.warnings))
    return "\n".join(lines)[:2000]


def enqueue(card: dict) -> None:
    """Set note only; the save worker persists it with its final save fields."""
    if not enabled() or card.get("note"):
        return
    if not card.get("caption"):
        card["caption"] = cards.group_caption(card)[0]
    text = compose(card, CardDraft.from_dict(card.get("draft") or {}), card.get("caption") or "")
    card["note"] = {"state": "pending", "text": text, "attempts": 0, "next_at": None}


def status() -> dict:
    with _state_lock:
        saved = cards.load_state("notes")
        return {"automation": saved.get("automation", "unknown"),
                "pending": saved.get("pending", 0),
                "last_notice_at": saved.get("last_notice_at")}


def _state(**fields) -> dict:
    with _state_lock:
        saved = cards.load_state("notes")
        saved.update(fields)
        cards.save_state("notes", saved)
        return saved


def request_automation() -> str:
    """Only a user action may call this, on a dedicated worker thread."""
    result = apple_contacts.request_automation()
    _state(automation=result, automation_checked_at=time.time())
    return result


def _pending() -> list[dict]:
    return [card for card in cards.list_cards(statuses=["saved"])
            if (card.get("note") or {}).get("state") == "pending"]


def _record(snapshot: dict, note: dict, resource: str | None = None) -> None:
    from .pipeline import card_lock
    with card_lock(snapshot["id"]) as acquired:
        fresh = cards.load_card(snapshot["id"])
        if (not acquired or not fresh or fresh.get("status") != "saved"
                or fresh.get("note") != snapshot.get("note")
                or fresh.get("resource_name") != snapshot.get("resource_name")):
            return
        fresh["note"] = note
        if resource:
            fresh["resource_name"] = resource
        cards.save_card(fresh)


def _defer(note: dict, now: float) -> None:
    note["attempts"] = note.get("attempts", 0) + 1
    started = note.setdefault("first_attempt_at", now)
    if now - started >= 86400:
        _fail(note)
    else:
        note["next_at"] = now + RETRY_SECONDS[min(note["attempts"] - 1, len(RETRY_SECONDS) - 1)]


def _fail(note: dict) -> None:
    note.update(state="failed", next_at=None, notice_pending=True)


def _write(card: dict, resource: str) -> None:
    note = card["note"]
    text = note["text"]
    marker = text.splitlines()[0]
    if resource.startswith("apple:"):
        intent = card.get("save_intent") or {}
        apple_contacts.set_note(resource, marker, text, container=intent.get("container"))
    else:
        contacts.set_note(resource, marker, text)


def drain(*, force_automation: bool = False) -> dict:
    """Process durable work synchronously; the caller submits this to its pool.

    Never sends messages. Unauthorized work does not age or consume attempts.
    Marker checks make a write with a lost reply safe to replay after restart.
    """
    if not enabled() or not _drain_lock.acquire(blocking=False):
        return {"written": 0, "pending": len(_pending()), "automation": status()["automation"]}
    written = 0
    try:
        pending = _pending()
        saved = cards.load_state("notes")
        automation = saved.get("automation", "unknown")
        now = time.time()
        checked = saved.get("automation_checked_at")
        due = (force_automation or automation not in ("denied", "not_determined")
               or checked is None or now - checked >= 900 or now < checked)
        if due and (force_automation or any((card.get("resource_name") or "").startswith("apple:") for card in pending)):
            automation = apple_contacts.automation_status()
            _state(automation=automation, automation_checked_at=now)
        for snapshot in pending:
            # Fresh snapshot before I/O; delayed UI writes cannot change this batch.
            from .pipeline import card_lock
            with card_lock(snapshot["id"]) as acquired:
                card = cards.load_card(snapshot["id"]) if acquired else None
                if (card and card.get("status") == "saved" and not card.get("caption")
                        and (card.get("note") or {}).get("state") == "pending"):
                    caption = cards.group_caption(card)[0]
                    if caption:
                        card["caption"] = caption
                        card["note"]["text"] = compose(card, CardDraft.from_dict(card.get("draft") or {}), caption)
                        cards.save_card(card)
            if not card or card.get("status") != "saved" or (card.get("note") or {}).get("state") != "pending":
                continue
            note = dict(card["note"])
            resource = card.get("resource_name") or ""
            now = time.time()
            if resource.startswith("apple:") and automation in ("denied", "not_determined"):
                note.pop("first_attempt_at", None)
                note["next_at"] = None
                if note != card["note"]:
                    _record(card, note)
                continue
            # Try due work before expiring it: the retry window may have elapsed
            # while the bot was stopped. A further failure expires in _defer.
            started = note.get("first_attempt_at")
            expired = started is not None and now - started >= 86400
            if note.get("next_at") and note["next_at"] > now and not expired:
                continue
            try:
                if resource.startswith("apple:") and automation != "authorized":
                    raise contacts.ContactSaveError("메모 권한 확인 지연", kind="transient", retryable=True)
                try:
                    _write(card, resource)
                except contacts.ContactSaveError as exc:
                    if exc.kind != "not_found" or not resource.startswith("apple:"):
                        raise
                    resource = contact_backend.refresh_resource_name(
                        resource, CardDraft.from_dict(card.get("draft") or {}), card.get("save_intent"))
                    _write(card, resource)
                note.update(state="written", next_at=None)
                written += 1
            except apple_contacts.ConfirmationRequired:
                _fail(note)
            except contacts.ContactSaveError as exc:
                if exc.kind == "auth" and resource.startswith("apple:"):
                    automation = apple_contacts.automation_status()
                    _state(automation=automation, automation_checked_at=now)
                    if automation in ("denied", "not_determined"):
                        note.pop("first_attempt_at", None)
                        note["next_at"] = None
                    else:
                        _defer(note, now)
                else:
                    _defer(note, now)
            except Exception:
                # Optional notes never change the contact's successful save state.
                _defer(note, now)
            if note != card["note"] or resource != card.get("resource_name"):
                _record(card, note, resource)
        count = len(_pending())
        _state(pending=count)
        return {"written": written, "pending": count, "automation": automation}
    finally:
        _drain_lock.release()
