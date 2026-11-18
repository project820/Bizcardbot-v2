"""Telegram channel: long polling, per-card buttons, batches, chat allowlist, health alerts."""

from __future__ import annotations

from . import writer

import json
import secrets
import logging
import logging.handlers
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import cards, contact_backend as contacts, doctor, multicard, pipeline
from . import apple_contacts
from .chat_ux import HELP_TEXT, HELP_WORDS, LIST_WORDS, batch_summary, edit_prompt, save_result_text, select_card, followup_text
from .pipeline import DATA_DIR, LOG_DIR

API_BASE = "https://api.telegram.org"
HEALTH_CHECK_INTERVAL = 24 * 3600
RETRY_SCAN_INTERVAL = 60
LONG_POLL_SECONDS = 25  # Telegram allows up to 50; fewer new connections, no added latency
RETRY_ATTEMPTS = 4
RETRY_DELAY = 1.5
TOKEN_CONFLICT_SECONDS = 60
TOKEN_ALERT_INTERVAL = 3600

log = logging.getLogger("bizcard")
_rejected_chats: set[int] = set()

# Photo work (image prep, OCR, model calls) runs on a small worker pool so an
# album of cards is processed in parallel and buttons stay responsive. Tests and
# the CLI leave _executor as None, which runs the work inline.
_executor: Optional[ThreadPoolExecutor] = None
_group_lock = threading.Lock()
_group_pending: dict[tuple[int, str], int] = {}
_group_summary_wanted: set[tuple[int, str]] = set()

_note_drain_lock = threading.Lock()
_note_notice_lock = threading.Lock()
_automation_lock = threading.Lock()


def export_buttons() -> dict:
    return {"inline_keyboard": [[{"text": "CSV 내보내기", "callback_data": "ex"}]]}


def register_commands(token: str) -> None:
    try:
        api_post(token, "setMyCommands", {"commands": [
            {"command": "list", "description": "저장 안 된 명함"},
            {"command": "help", "description": "사용법"},
            {"command": "alerts", "description": "알림 설정"},
            {"command": "export", "description": "명함 내보내기"},
        ]})
    except Exception:
        log.warning("Telegram command registration failed; continuing polling")


def send_export(token: str, chat_id: int) -> None:
    if chat_id not in allowed_chat_ids():
        return
    try:
        from . import export_csv, metrics
        content, count = export_csv.build(chat_id)
        if not count:
            send_message(token, chat_id, "저장한 명함이 없습니다.")
            return
        if len(content) > 45 * 1024 * 1024:
            send_message(token, chat_id, "CSV 파일이 45MB를 초과해 보낼 수 없습니다.")
            return
        summary = metrics.summary(chat_id)
        parts = [f"저장한 명함 {count}건"]
        rate = summary.get("unedited_rate")
        cost = summary.get("avg_krw")
        if rate is not None:
            parts.append(f"수정 없이 저장 {rate:.0%}(기록 {summary.get('reviewed', 0)}건)")
        if cost is not None:
            parts.append(f"평균 {cost:.2f}원(기록 {summary.get('costed', 0)}건)")
        filename = "bizcards-" + cards.followup_localtime(time.time()).strftime("%Y%m%d") + ".csv"
        send_document(token, chat_id, content, filename, caption=" · ".join(parts))
    except Exception:
        log.warning("CSV export failed")
        send_message(token, chat_id, "CSV 내보내기에 실패했습니다. 잠시 후 다시 눌러주세요.", reply_markup=export_buttons())


def _note_buttons(*, check: bool = False, request: bool = False) -> dict:
    row = [{"text": "다시 확인" if check else "메모 권한 요청", "callback_data": "nc" if check else "na"}]
    if check and request:
        row.append({"text": "메모 권한 요청", "callback_data": "na"})
    return {"inline_keyboard": [row]}


def _pending_notes(chat_id: int) -> int:
    return sum(1 for card in cards.list_cards(chat_id, ["saved"])
               if (card.get("note") or {}).get("state") == "pending"
               and (card.get("resource_name") or "").startswith("apple:"))


def maybe_note_notice(token: str, chat_id: int) -> None:
    """Notice only this chat's pending cards; never request permission here."""
    if chat_id not in allowed_chat_ids():
        return
    try:
        from . import meeting_note
        if not meeting_note.enabled():
            return
        pending = _pending_notes(chat_id)
        if not pending or meeting_note.status().get("automation") == "authorized":
            return
        with _note_notice_lock:
            notices = cards.load_state("note_notices")
            now = time.time()
            last = notices.get(str(chat_id))
            if last is not None and now - last < 24 * 3600:
                return
            send_message(token, chat_id,
                         f"메모 {pending}건이 대기 중입니다. Mac mini 화면을 볼 수 있을 때 누르세요.",
                         reply_markup=_note_buttons())
            notices[str(chat_id)] = now
            cards.save_state("note_notices", notices)
            # A owns the state lock; its updater preserves concurrent permission results.
            meeting_note._state(last_notice_at=now)
    except ImportError:
        pass
    except Exception:
        log.warning("meeting note notice failed")


def _note_permission_result(token: str, chat_id: int, automation: str, pending: Optional[int] = None) -> None:
    if automation == "authorized":
        count = _pending_notes(chat_id) if pending is None else pending
        send_message(token, chat_id, f"허용을 확인했습니다. 밀린 메모 {count}건을 추가합니다.")
    elif automation == "denied":
        send_message(token, chat_id, "시스템 설정 > 개인정보 보호 및 보안 > 자동화에서 Python 아래 '연락처'를 켜세요.",
                     reply_markup=_note_buttons(check=True))
    else:
        send_message(token, chat_id, "메모 권한을 확인하지 못했습니다. Mac mini 화면을 확인한 뒤 권한을 요청하거나 다시 확인하세요.",
                     reply_markup=_note_buttons(check=True, request=True))


def schedule_note_drain(token: str, check_chat_id: Optional[int] = None) -> None:
    """At most one queued or running scan; Apple events run on the card pool."""
    if not _note_drain_lock.acquire(blocking=False):
        if check_chat_id is not None:
            send_message(token, check_chat_id, "메모 권한을 확인 중입니다. 잠시 후 다시 확인하세요.",
                         reply_markup=_note_buttons(check=True))
        return

    def drain() -> None:
        try:
            from . import meeting_note
            if not meeting_note.enabled():
                return
            pending = _pending_notes(check_chat_id) if check_chat_id is not None else None
            result = meeting_note.drain(force_automation=True) if check_chat_id is not None else meeting_note.drain()
            if check_chat_id is not None:
                _note_permission_result(token, check_chat_id, result.get("automation", "error"), pending)
            for chat_id in allowed_chat_ids():
                maybe_note_notice(token, chat_id)
                _failed_note_notice(token, chat_id)
        except ImportError:
            pass
        except Exception:
            log.warning("meeting note drain failed")
        finally:
            _note_drain_lock.release()
    try:
        submit(drain)
    except Exception:
        _note_drain_lock.release()
        log.warning("meeting note drain submission failed")


def _failed_note_notice(token: str, chat_id: int) -> None:
    with _note_notice_lock:
        failed = [card for card in cards.list_cards(chat_id, ["saved"])
                  if (card.get("note") or {}).get("state") == "failed"
                  and card["note"].get("notice_pending")]
        if not failed:
            return
        send_message(token, chat_id, f"메모 {len(failed)}건을 추가하지 못했습니다. 연락처는 저장되어 있습니다.")
        for card in failed:
            with pipeline.card_lock(card["id"]):
                fresh = cards.load_card(card["id"])
                if (fresh and fresh.get("status") == "saved"
                        and (fresh.get("note") or {}).get("state") == "failed"
                        and fresh["note"].get("notice_pending")):
                    fresh["note"]["notice_pending"] = False
                    cards.save_card(fresh)


def request_note_permission(token: str, chat_id: int) -> None:
    """Only an explicit na button may open the Mac automation prompt."""
    from . import meeting_note
    if not meeting_note.enabled():
        return
    if not _automation_lock.acquire(blocking=False):
        send_message(token, chat_id, "메모 권한을 확인 중입니다. 잠시 후 다시 확인하세요.",
                     reply_markup=_note_buttons(check=True))
        return

    def request() -> None:
        try:
            from . import meeting_note
            result = meeting_note.request_automation()  # helper contract: at most 600 seconds
            _note_permission_result(token, chat_id, result)
            if result == "authorized":
                schedule_note_drain(token)
        except Exception:
            log.warning("meeting note automation request failed")
            _note_permission_result(token, chat_id, "error")
        finally:
            _automation_lock.release()
    try:
        send_message(token, chat_id, "Mac mini 화면의 창에서 '허용'을 누르세요. 10분 동안 기다립니다.")
        threading.Thread(target=request, name="note-permission", daemon=True).start()
    except Exception:
        _automation_lock.release()
        log.warning("meeting note automation thread failed")


def _capture_caption(card: dict, caption: str) -> None:
    group = card.get("media_group_id")
    shared, registered = cards.group_caption(card, caption) if group else (caption.strip()[:300], False)
    if not shared:
        return
    members = cards.group_cards(card["chat_id"], group) if registered else [card]
    for member in members:
        with pipeline.card_lock(member["id"], blocking=False) as acquired:
            if not acquired:
                continue  # enqueue/drain reads the durable album caption later.
            fresh = cards.load_card(member["id"])
            if fresh and not fresh.get("caption"):
                fresh["caption"] = shared
                note = fresh.get("note") or {}
                if note.get("state") == "pending":
                    from . import meeting_note
                    note["text"] = meeting_note.compose(fresh, pipeline.current_draft(fresh), shared)
                cards.save_card(fresh)
            if fresh and member["id"] == card["id"]:
                card.clear()
                card.update(fresh)


def worker_count() -> int:
    try:
        return max(1, int(os.environ.get("BIZCARD_WORKERS", "30")))
    except ValueError:
        return 30


def submit(fn, *args, group: Optional[tuple[int, str]] = None) -> None:
    """Run photo work on the pool (or inline) and track album completion."""
    if group:
        with _group_lock:
            _group_pending[group] = _group_pending.get(group, 0) + 1

    def job() -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background job failed: %s", getattr(fn, "__name__", fn))
        finally:
            if group:
                _group_job_done(group)

    if _executor is None:
        job()
    else:
        _executor.submit(job)


_progress_at: dict[tuple[int, str], float] = {}
PROGRESS_EVERY = 3.0


def _group_job_done(group: tuple[int, str]) -> None:
    with _group_lock:
        _group_pending[group] = _group_pending.get(group, 1) - 1
        finished = _group_pending[group] <= 0
        wanted = finished and group in _group_summary_wanted
        if finished:
            _group_pending.pop(group, None)
            _group_summary_wanted.discard(group)
            _progress_at.pop(group, None)
        show_progress = not finished and time.monotonic() - _progress_at.get(group, 0.0) >= PROGRESS_EVERY
        if show_progress:
            _progress_at[group] = time.monotonic()
    if wanted:
        _safe_group_summary(group)
    elif show_progress:
        _update_group_progress(group)


def _update_group_progress(group: tuple[int, str]) -> None:
    members = cards.group_cards(group[0], group[1])
    done = sum(1 for c in members if c.get("status") != "processing")
    with _summary_lock:
        message_id = cards.load_state("groups").get(f"{group[0]}:{group[1]}")
    if not message_id:
        return
    try:
        edit_message(_summary_token, group[0], message_id, f"보낸 명함 {len(members)}장 처리 중: 완료 {done}/{len(members)}")
    except Exception:
        log.warning("progress update failed group=%s", group[1])


def _request_group_summary(group: tuple[int, str]) -> None:
    with _group_lock:
        busy = _group_pending.get(group, 0) > 0
        if busy:
            _group_summary_wanted.add(group)
    if not busy:
        _safe_group_summary(group)


def _safe_group_summary(group: tuple[int, str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    try:
        send_group_summary(_summary_token or token, group[0], group[1])
    except Exception:
        log.exception("group summary failed chat=%s group=%s", *group)


_summary_token = ""
_summary_lock = threading.RLock()


def allowed_chat_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {int(part) for part in raw.replace(";", ",").split(",") if part.strip()}


class TelegramAPIError(RuntimeError):
    def __init__(self, code: int, description: str):
        self.code = code
        self.description = description
        # Conflict descriptions and HTTP exception URLs can contain secrets.
        super().__init__("Telegram conflict" if code == 409 else description)


def _conflict_reason(description: str) -> str:
    if "webhook is active" in description.lower():
        return "webhook is active"
    if "terminated by other getupdates" in description.lower():
        return "other getUpdates instance"
    return "unrecognized 409 conflict"


def _inspect_webhook(token: str) -> bool:
    try:
        info = api_get(token, "getWebhookInfo")["result"]
        active = bool(info.get("url"))
    except Exception:
        log.warning("could not inspect Telegram webhook")
        return False
    if active:
        # This application only polls: any webhook belongs to another setup.
        log.error("unexpected Telegram webhook detected; another setup may be using this bot")
    return active


def _alert_token_conflict(token: str, bot_id: int) -> None:
    state = cards.load_state("telegram_security")
    last_alerts = state.get(str(bot_id), {})
    now = time.time()
    text = ("다른 컴퓨터나 이전 실행에서 같은 봇이 돌고 있는지 확인하세요.\n"
            "설치 도우미(@BizcardSetupBot)는 토큰을 저장하지 않습니다.\n"
            "도우미는 명함 사진과 Google 계정에 접근하지 않습니다.\n"
            "다만 Telegram 구조상 도우미 운영자는 내 봇의 토큰을 다시 조회할 수 있습니다.\n"
            "토큰이 있으면 봇에 온 메시지를 읽을 수 있습니다.\n"
            "이 점이 걸리거나 모르는 사용이 의심되면 BotFather에서 새 봇을 만들고 "
            "python3 -m bizcard_intake setup-telegram --manual 로 연결하세요.")
    for chat_id in sorted(chat for chat in allowed_chat_ids() if chat > 0):
        last = last_alerts.get(str(chat_id))
        if isinstance(last, (int, float)) and now - last < TOKEN_ALERT_INTERVAL:
            continue
        # Persist before sending so a restart cannot repeat an uncertain send.
        last_alerts[str(chat_id)] = now
        state[str(bot_id)] = last_alerts
        try:
            cards.save_state("telegram_security", state)
            send_message(token, chat_id, text)
        except Exception:
            log.error("could not persist or send Telegram token-use alert to chat=%s", chat_id)


@writer.required(bot_start=True)
def run(token: str, data_dir: Path = DATA_DIR) -> None:
    try:
        _run(token, data_dir)
    finally:
        # Do not release the process writer while contact workers still run.
        global _executor
        if _executor is not None:
            _executor.shutdown(wait=True)
            _executor = None


def _run(token: str, data_dir: Path = DATA_DIR) -> None:
    if not allowed_chat_ids():
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS is required (comma-separated chat ids allowed to use the bot)")
    cards.initialize_followup()
    register_commands(token)
    try:
        initialize_contacts(token)
    except Exception:
        log.warning("contacts initialization failed; continuing polling")
    for note in pipeline.migrate_legacy():
        log.info("migrated: %s", note)
    recover_interrupted()
    _inspect_webhook(token)
    delete_webhook(token)
    me = api_get(token, "getMe")["result"]
    username = me.get("username", "unknown")
    log.info("polling @%s", username)
    print(f"Polling @{username} - Ctrl-C to stop", flush=True)

    global _executor, _summary_token
    _summary_token = token
    _executor = ThreadPoolExecutor(max_workers=worker_count(), thread_name_prefix="card")
    log.info("photo workers=%d", worker_count())
    state = cards.load_state("telegram")
    # Legacy offsets have no proven owner and must not skip a new bot's messages.
    offset = state.get("offset") if state.get("bot_id") == me["id"] else None
    cards.save_state("telegram", {"bot_id": me["id"], "offset": offset})
    next_health_check = 0.0
    next_retry_scan = 0.0
    conflict_since = None
    while True:
        if time.monotonic() >= next_health_check:
            health_check(token)
            _safe_send_due_followups(token)
            schedule_note_drain(token)
            next_health_check = time.monotonic() + HEALTH_CHECK_INTERVAL
            next_retry_scan = time.monotonic() + RETRY_SCAN_INTERVAL
        elif time.monotonic() >= next_retry_scan:
            retry_due_and_report(token)
            _safe_send_due_followups(token)
            schedule_note_drain(token)
            next_retry_scan = time.monotonic() + RETRY_SCAN_INTERVAL
        try:
            updates = get_updates(token, offset)
            conflict_since = None
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if isinstance(exc, TelegramAPIError) and exc.code == 409:
                now = time.monotonic()
                if conflict_since is None:
                    conflict_since = now
                _inspect_webhook(token)
                log.error("getUpdates conflict: %s", _conflict_reason(exc.description))
                if now - conflict_since >= TOKEN_CONFLICT_SECONDS:
                    _alert_token_conflict(token, me["id"])
            else:
                conflict_since = None
                log.warning("getUpdates failed; backing off")
            time.sleep(5)
            continue
        offset = handle_updates(token, updates, data_dir, offset)


def handle_updates(token: str, updates: list, data_dir: Path = DATA_DIR, offset: Optional[int] = None) -> Optional[int]:
    global _summary_token
    _summary_token = _summary_token or token
    groups: set[tuple[int, str]] = set()
    for update in updates:
        offset = update["update_id"] + 1
        cards.save_state("telegram", {**cards.load_state("telegram"), "offset": offset})
        try:
            group = handle_update(token, update, data_dir)
            if group:
                groups.add(group)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("handle_update failed for update_id=%s", update.get("update_id"))
            notify_failure(token, update)
    for group in sorted(groups):
        _request_group_summary(group)
    return offset


@pipeline.stale_safe("none")
def recover_interrupted() -> None:
    """Cards left mid-step by a crash or restart go back to a state the user can act on."""
    for candidate in cards.list_cards(statuses=["review", "processing", "saving"]):
        with pipeline.card_lock(candidate["id"]):
            card = cards.load_card(candidate["id"])
            if not card:
                continue
            if pipeline.cancel_path(card["id"]).exists() and pipeline._cancel_locked(card):
                continue
            card.pop("recognition_id", None)
            if card["status"] == "review":
                if card.get("job_change_pending"):
                    card["job_change_pending"] = False
                    card.pop("job_change_started_at", None)
                    card.pop("job_change_lookup_id", None)
                    cards.save_card(card)
                continue
            if card["status"] == "saving":
                card["status"] = "save_failed"
                card["error_kind"] = "unknown_outcome" if not card.get("resource_name") else "transient"
                card["last_error"] = "저장 도중 봇이 재시작되었습니다. 저장 여부를 확인한 뒤 다시 시도합니다."
                card["next_retry_at"] = time.time()
            elif card["status"] == "processing" and not pipeline.unresolved_save(card):
                card["status"] = "review"
                draft = pipeline.current_draft(card)
                draft.warnings.append("처리 도중 봇이 재시작됨: '인식 재시도'를 눌러주세요")
                card["draft"] = draft.normalize().to_dict()
            else:
                continue
            cards.add_history(card, "recovered_after_restart")
            cards.save_card(card)


def _send_health_alert(token: str, text: str, *, apple_permission: bool = False,
                       alert_key: str | None = None) -> None:
    try:
        alert = os.environ.get("BIZCARD_ALERT_CHAT_ID")
        targets = []
        if alert:
            try:
                targets = [int(alert)]
            except (ValueError, TypeError):
                log.warning("invalid contacts alert chat id")
        if not targets and apple_permission:
            targets = sorted(allowed_chat_ids())
        if not targets:
            return
        if alert_key:
            state_name = "contacts_alert" if apple_permission else "contacts_health_alert"
            state = cards.load_state(state_name)
            now = time.time()
            if state.get("key") == alert_key and now - state.get("timestamp", 0) < HEALTH_CHECK_INTERVAL:
                return
            # Persist before sending, including uncertain sends across restart.
            cards.save_state(state_name, {"key": alert_key, "timestamp": now})
        for chat_id in targets:
            try:
                send_message(token, chat_id, text)
            except Exception:
                log.warning("failed to send contacts health alert")
    except Exception:
        log.warning("contacts health alert unavailable")


def _permission_alert(token: str, status: str) -> None:
    _send_health_alert(token, apple_contacts.AUTH_MESSAGE, apple_permission=True,
                       alert_key="apple:permission:" + status)


def initialize_contacts(token: str) -> None:
    """Request TCC before workers; initialization must never stop polling."""
    try:
        if contacts.backend_name() != "apple":
            return
        status = apple_contacts.run_helper("status").get("status")
        if status == "notDetermined":
            status = apple_contacts.run_helper("request").get("status")
        if status != "authorized":
            _permission_alert(token, status or "unknown")
        else:
            cards.save_state("contacts_alert", {"key": "apple:healthy", "timestamp": time.time()})
    except contacts.ContactSaveError as exc:
        if exc.kind == "auth":
            _permission_alert(token, "denied")
        else:
            _send_health_alert(token, str(exc), alert_key="apple:initialization:" + exc.kind)
    except Exception:
        log.warning("contacts initialization failed; continuing polling")


def health_check(token: str) -> list[doctor.Check]:
    """Verify dependencies; only Apple permission errors may use allowed chats."""
    failed = [check for check in doctor.run_checks(["parser", "gws"]) if not check.ok]
    for check in failed:
        log.warning("health check: %s: %s", check.name, check.detail)
    remaining = failed
    if any(c.name == "apple" for c in failed):
        try:
            status = apple_contacts.run_helper("status").get("status", "unknown")
            if status != "authorized":
                _permission_alert(token, status)
                remaining = [c for c in failed if c.name != "apple"]
            else:
                cards.save_state("contacts_alert", {"key": "apple:healthy", "timestamp": time.time()})
        except Exception:
            log.warning("contacts permission check unavailable")
    elif contacts.backend_name() == "apple":
        # Observed recovery clears the key so a later denial alerts immediately.
        try:
            cards.save_state("contacts_alert", {"key": "apple:healthy", "timestamp": time.time()})
        except Exception:
            log.warning("contacts health state unavailable")
    if remaining:
        waiting = pipeline.pending_retry_count()
        text = "봇 점검 경고\n" + "\n".join(f"- {c.detail}\n  해결: {c.fix}" for c in remaining)
        if waiting:
            text += f"\n\n저장 대기 중인 명함 {waiting}건은 문제가 해결되면 자동으로 저장합니다."
        key = "apple:health:" + "|".join(c.name + ":" + c.detail for c in remaining) if contacts.backend_name() == "apple" else None
        _send_health_alert(token, text, alert_key=key)
    if not any(c.name in ("gws", "apple", "contacts") for c in failed):
        log.info("health check ok" if not failed else "health check: contacts ok")
        retry_due_and_report(token, force=True)
    return failed


def retry_due_and_report(token: str, force: bool = False) -> None:
    def report(card: dict, outcome: dict) -> None:
        chat_id = int(card["chat_id"]) if str(card.get("chat_id", "")).lstrip("-").isdigit() else None
        if chat_id is None or (not outcome["ok"] and not force and not outcome.get("needs_confirmation")):
            return  # quiet on background failures; the next health check summarises them
        try:
            send_message(token, chat_id, save_result_text(card, outcome, retried=True), reply_markup=None if outcome["ok"] else saved_failed_buttons(card))
        except Exception:
            log.exception("failed to report retry card=%s", card["id"])

    if not any(True for _ in cards.list_cards(statuses=["save_failed", "saved"])):
        return
    saved, failures = pipeline.retry_due(force=force, on_result=report)
    if saved or failures:
        log.info("retry: saved=%d failed=%d", saved, len(failures))


def notify_failure(token: str, update: dict) -> None:
    chat_id = chat_id_of(update)
    if chat_id is None:
        return
    try:
        send_message(token, chat_id, "처리 중 오류가 발생했습니다. 명함은 보관되어 있으니 '목록'에서 확인하거나 다시 시도해주세요.")
    except Exception:
        log.exception("failed to notify chat_id=%s of error", chat_id)


def chat_id_of(update: dict) -> Optional[int]:
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (message.get("chat") or {}).get("id")


def handle_update(token: str, update: dict, data_dir: Path = DATA_DIR) -> Optional[tuple[int, str]]:
    """Handle one update. Returns (chat_id, media_group_id) when an album photo was added."""
    chat_id = chat_id_of(update)
    if chat_id is None:
        return None
    if chat_id not in allowed_chat_ids():
        reject_chat(token, chat_id)
        return None

    callback = update.get("callback_query")
    if callback:
        handle_callback(token, callback, chat_id)
        return None

    message = update.get("message") or {}
    photos = message.get("photo") or []
    document = message.get("document") or {}
    if not photos and str(document.get("mime_type", "")).startswith("image/"):
        photos = [{"file_id": document["file_id"]}]
    if photos:
        return handle_photo(token, chat_id, message, photos, data_dir)
    handle_text(token, chat_id, message)
    return None


def reject_chat(token: str, chat_id: int) -> None:
    log.warning("rejected chat_id=%s (not in TELEGRAM_ALLOWED_CHAT_IDS)", chat_id)
    if chat_id in _rejected_chats:
        return
    _rejected_chats.add(chat_id)
    send_message(token, chat_id, f"비공개 봇입니다. 사용하려면 봇 운영자의 TELEGRAM_ALLOWED_CHAT_IDS에 이 chat id를 추가하세요: {chat_id}")


# ---------------------------------------------------------------- photos

def _report_stale_write(function):
    """Tell the chat when a delayed UI write was refused; do not replay photos."""
    from functools import wraps
    @wraps(function)
    def wrapped(token, chat_id, *args, **kwargs):
        try:
            return function(token, chat_id, *args, **kwargs)
        except cards.StaleCardState:
            send_message(token, chat_id, "저장 상태가 변경되어 요청을 반영하지 않았습니다. 이전 저장 결과를 확인하세요. 사진은 확인 후 다시 보내주세요.")
    return wrapped


@_report_stale_write
def handle_photo(token: str, chat_id: int, message: dict, photos: list, data_dir: Path) -> Optional[tuple[int, str]]:
    media_group_id = message.get("media_group_id")
    reply_card = cards.find_by_message(chat_id, (message.get("reply_to_message") or {}).get("message_id"))
    back_target = None
    if not media_group_id:
        if reply_card and reply_card.get("awaiting_back"):
            back_target = reply_card
        else:
            waiting = [c for c in cards.list_cards(chat_id, cards.REVIEW_STATUSES) if c.get("awaiting_back")]
            back_target = waiting[-1] if len(waiting) == 1 else None
    if back_target:
        with pipeline.card_lock(back_target["id"], blocking=False) as acquired:
            if not acquired:
                send_message(token, chat_id, pipeline.BUSY_MESSAGE)
                return None
            fresh = cards.load_card(back_target["id"])
            if fresh and fresh.get("status") in ("processing", "saving"):
                send_message(token, chat_id, pipeline.BUSY_MESSAGE)
                return None
            if (not fresh or fresh.get("status") not in cards.REVIEW_STATUSES
                    or pipeline.unresolved_save(fresh)):
                send_message(token, chat_id, "이전 저장 결과를 먼저 확인하세요. 뒷면 사진을 반영하지 않았습니다. 확인 후 다시 보내주세요.")
                return None
            if not fresh.get("awaiting_back"):
                send_message(token, chat_id, "뒷면 사진을 이미 받았습니다. 최근 초안을 확인하세요.")
                return None
            if not fresh.get("caption"):
                fresh["caption"] = (cards.group_caption(fresh)[0]
                                    or str(message.get("caption") or "").strip()[:300])
                if fresh["caption"] and (fresh.get("note") or {}).get("state") == "pending":
                    from . import meeting_note
                    fresh["note"]["text"] = meeting_note.compose(fresh, pipeline.current_draft(fresh), fresh["caption"])
            fresh["awaiting_back"] = False
            cards.save_card(fresh)
        submit(process_back_photo, token, chat_id, photos, data_dir, fresh)
        return None
    # Card creation happens here, in update order, so card numbers follow the
    # album; image prep, OCR and the model calls run on the worker pool.
    group = (chat_id, str(media_group_id)) if media_group_id else None
    caption = str(message.get("caption") or "")
    # Own captions must be durable before any worker can acquire the card lock.
    card = cards.new_card(chat_id, media_group_id=group[1] if group else None, caption=caption)
    _capture_caption(card, caption)
    if group:
        _ensure_group_progress(token, group)
        submit(process_front_photo, token, chat_id, photos, data_dir, card, None, True, group=group)
        return group
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 이미지 보정 · OCR")
    _remember_card_message(card, status_id)
    submit(process_front_photo, token, chat_id, photos, data_dir, card, status_id)
    return None


def _ensure_group_progress(token: str, group: tuple[int, str]) -> None:
    """One message per album that shows progress and later becomes the summary."""
    key = f"{group[0]}:{group[1]}"
    with _summary_lock:
        state = cards.load_state("groups")
        if state.get(key):
            return
        result = send_message(token, group[0], "보낸 명함 처리 중… (명함마다 사진과 초안이 하나씩 옵니다)")
        state[key] = result["result"]["message_id"]
        cards.save_state("groups", state)


@_report_stale_write
def process_front_photo(token: str, chat_id: int, photos: list, data_dir: Path, card: dict, status_id: Optional[int] = None, album: bool = False) -> None:
    log.info("front photo chat=%s card=%s", chat_id, card["id"])
    if status_id is None and not album:
        status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 이미지 보정 · OCR")
        _remember_card_message(card, status_id)
    try:
        front_path = save_largest_photo(token, photos, chat_id, data_dir, "front", card["id"])
    except Exception:
        log.exception("photo download failed card=%s", card["id"])
        pipeline.cancel(card)
        text = f"{cards.label(card)} 사진을 받지 못했습니다. 다시 보내주세요."
        if status_id is not None:
            edit_message(token, chat_id, status_id, text)
        else:
            send_message(token, chat_id, text)
        return
    if _handle_multicard(token, chat_id, card, front_path, status_id, album):
        return
    _recognize_and_show(token, chat_id, card, front_path, status_id, album)


MULTICARD_HELP = (
    "한 사진에 명함 여러 장을 보낼 때\n"
    "1. 한 사진에 명함은 4장까지 넣어주세요.\n"
    "2. HD로 보내기: 사진을 고른 뒤 화질 표시(SD)를 눌러 HD로 바꿔 보내세요.\n"
    "3. 파일로 보내기: 클립 버튼 → 파일 → 사진을 골라 보내세요.\n"
    "명함이 사진을 크게 차지할수록 잘 읽힙니다."
)


def multicard_help_buttons() -> dict:
    return {"inline_keyboard": [[{"text": "HD·파일로 보내는 방법", "callback_data": "mh"}]]}


def _handle_multicard(token: str, chat_id: int, card: dict, front_path: Path,
                      status_id: Optional[int], album: bool) -> bool:
    """Split a photo of two to four cards into one card each. False keeps the one-card path."""
    work_dir = front_path.parent / f"{card['id']}_split"
    split = multicard.detect(front_path, work_dir)
    if split.kind == "single":
        return False
    prefix = f"{cards.label(card)} " if album else ""
    if split.kind != "split":
        log.info("multicard refused card=%s kind=%s count=%s", card["id"], split.kind, split.count)
        pipeline.cancel(card)
        pipeline.cleanup_paths(front_path)
        if split.kind == "too_many":
            text = f"{prefix}명함이 {split.count}장 보입니다. 한 사진에 {multicard.MAX_CARDS}장 이하로 다시 보내주세요."
        else:
            text = f"{prefix}명함 글씨가 작게 찍혔습니다. HD 화질이나 파일로 다시 보내주세요."
        if status_id is not None:
            edit_message(token, chat_id, status_id, text, reply_markup=multicard_help_buttons())
        else:
            send_message(token, chat_id, text, reply_markup=multicard_help_buttons())
        return True
    log.info("multicard split card=%s count=%s", card["id"], split.count)
    if album and card.get("media_group_id"):
        group = (chat_id, str(card["media_group_id"]))
    else:
        group = (chat_id, f"split-{card['id']}")
    members = [card]
    for _ in split.crops[1:]:
        members.append(cards.new_card(chat_id, media_group_id=group[1], caption=str(card.get("caption") or "")))
    targets = []
    for index, (member, crop) in enumerate(zip(members, split.crops), 1):
        # Derived images are named from the source stem, so each card needs its own.
        target = front_path.parent / f"{member['id']}_card{index}.jpg"
        os.replace(crop, target)
        targets.append(target)
    # Every crop is recorded before the original goes, so a restart can retry each card.
    with pipeline.card_lock(card["id"]):
        fresh = cards.load_card(card["id"]) or card
        fresh["media_group_id"] = group[1]
        fresh["images"]["front"] = str(targets[0])
        cards.save_card(fresh)
        card.clear()
        card.update(fresh)
    for member, target in zip(members[1:], targets[1:]):
        member["images"]["front"] = str(target)
        cards.save_card(member)
    shutil.rmtree(work_dir, ignore_errors=True)
    pipeline.cleanup_paths(front_path)
    if not album:
        notice = f"사진에서 명함 {len(members)}장을 찾았습니다. 명함마다 사진과 초안이 하나씩 옵니다."
        try:
            if status_id is not None:
                edit_message(token, chat_id, status_id, notice)
            else:
                status_id = send_status(token, chat_id, notice)
            with _summary_lock:
                state = cards.load_state("groups")
                state[f"{group[0]}:{group[1]}"] = status_id
                cards.save_state("groups", state)
        except Exception:
            log.exception("multicard notice failed card=%s; recognizing anyway", card["id"])
    for member, target in zip(members, targets):
        submit(_recognize_and_show, token, chat_id, member, target, None, True, group=group)
    if not album:
        _request_group_summary(group)
    return True


@_report_stale_write
def _recognize_and_show(token: str, chat_id: int, card: dict, front_path: Path,
                        status_id: Optional[int] = None, album: bool = False) -> None:
    try:
        pipeline.recognize_front(card, front_path)
    except (pipeline.ContactSaveError, cards.StaleCardState) as exc:
        send_message(token, chat_id, str(exc))
        return
    profile = card["images"].get("profile")
    preview = Path(profile) if profile and Path(profile).exists() else None
    if album:
        show_draft(token, chat_id, card, photo=preview)
        return
    if preview:
        try:
            send_photo(token, chat_id, preview, f"{cards.label(card)} 연락처 사진 미리보기")
        except Exception:
            log.exception("preview send failed card=%s", card["id"])
    show_draft(token, chat_id, card, status_id)


@_report_stale_write
def process_back_photo(token: str, chat_id: int, photos: list, data_dir: Path, card: dict) -> None:
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 뒷면 OCR")
    back_path = save_largest_photo(token, photos, chat_id, data_dir, "back", card["id"])
    try:
        pipeline.recognize_back(card, back_path)
    except (pipeline.ContactSaveError, cards.StaleCardState) as exc:
        send_message(token, chat_id, str(exc))
        return
    show_draft(token, chat_id, card, status_id)


CAPTION_LIMIT = 1024
LOOKUP_APPLY_WAIT_SECONDS = 30.0


@_report_stale_write
def show_draft(token: str, chat_id: int, card: dict, replace_message_id: Optional[int] = None, prefix: str = "", photo: Optional[Path] = None) -> None:
    with pipeline.card_lock(card["id"], blocking=False) as acquired:
        if not acquired:
            send_message(token, chat_id, pipeline.BUSY_MESSAGE)
            return
        fresh = cards.load_card(card["id"])
        if not fresh or fresh.get("status") != "review" or pipeline.unresolved_save(fresh):
            return
        show_cost = bool(fresh.get("recognition_cost_notice_pending"))
        fresh["recognition_cost_notice_pending"] = False
        fresh.pop("job_choice", None)
        fresh["job_change_pending"] = True
        fresh["job_change_started_at"] = time.time()
        fresh["job_change_lookup_id"] = uuid.uuid4().hex
        cards.save_card(fresh)
        card.clear()
        card.update(fresh)
    text = (prefix + "\n\n" if prefix else "") + pipeline.draft_text(card, show_cost=show_cost) + "\n\n고칠 내용은 이 메시지에 답장으로 보내주세요."
    text += "\n\n기존 연락처를 확인 중입니다. 확인 후 저장 버튼이 표시됩니다."
    message_id = None
    is_caption = False
    if photo is not None:
        try:
            if len(text) <= CAPTION_LIMIT:
                message_id = send_photo(token, chat_id, photo, text, reply_markup=draft_buttons(card))["result"]["message_id"]
                is_caption = True
            else:
                send_photo(token, chat_id, photo, f"{cards.label(card)} 연락처 사진 미리보기")
        except Exception:
            log.exception("preview send failed card=%s", card["id"])
    if message_id is None and replace_message_id is not None:
        try:
            edit_message(token, chat_id, replace_message_id, text, reply_markup=draft_buttons(card))
            message_id = replace_message_id
        except Exception:
            log.warning("could not edit status message; sending a new draft message")
    if message_id is None:
        message_id = send_message(token, chat_id, text, reply_markup=draft_buttons(card))["result"]["message_id"]
    _remember_card_message(card, message_id)
    # Deliver the draft first. Optional contact lookup never adds to first-draft latency.
    # show_draft is called by photo/edit workers, never by the update loop.
    # Finish on that same worker so shutdown waits for the lookup as well.
    _complete_job_change(token, chat_id, dict(card), message_id, prefix, is_caption, show_cost=show_cost)


def _complete_job_change(token: str, chat_id: int, candidate: dict, message_id: int,
                         prefix: str, is_caption: bool, *, show_cost: bool = False) -> None:
    pipeline.refresh_job_change(candidate)
    deadline = time.monotonic() + LOOKUP_APPLY_WAIT_SECONDS
    delay = .05
    while True:
        with pipeline.card_lock(candidate["id"], blocking=False) as acquired:
            if acquired:
                fresh = cards.finish_job_change(candidate)
                break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("job-change completion lock timeout; using normal save")
            fresh = cards.finish_job_change(candidate, fallback=True)
            break
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, .5)
    if fresh is None:
        return
    text = (prefix + "\n\n" if prefix else "") + pipeline.draft_text(fresh, show_cost=show_cost) + "\n\n고칠 내용은 이 메시지에 답장으로 보내주세요."
    try:
        if is_caption and len(text) <= CAPTION_LIMIT:
            api_post(token, "editMessageCaption", {"chat_id": chat_id, "message_id": message_id,
                     "caption": text, "reply_markup": draft_buttons(fresh)})
        elif is_caption:
            api_post(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                     "reply_markup": {"inline_keyboard": []}})
            result = send_message(token, chat_id, text, reply_markup=draft_buttons(fresh))
            _remember_card_message(fresh, result["result"]["message_id"])
        else:
            edit_message(token, chat_id, message_id, text, reply_markup=draft_buttons(fresh))
    except Exception:
        log.warning("job-change draft refresh unavailable card=%s", fresh["id"])


def _record_card_message_locked(card: dict, message_id: Optional[int]) -> None:
    """Record into the current card; the caller already owns its card lock."""
    cards.remember_message(card, message_id)
    cards.save_card(card)


def _remember_card_message(card: dict, message_id: Optional[int]) -> None:
    with cards.pending_message(card, message_id):
        # v2.1.1: block in the calling thread, reload, append once, save, refresh.
        with pipeline.card_lock(card["id"]):
            fresh = cards.load_card(card["id"])
            if fresh is not None:
                _record_card_message_locked(fresh, message_id)
                card.clear()
                card.update(fresh)


def send_group_summary(token: str, chat_id: int, media_group_id: str) -> None:
    with _summary_lock:
        _send_group_summary(token, chat_id, media_group_id)


def _send_group_summary(token: str, chat_id: int, media_group_id: str) -> None:
    members = cards.group_cards(chat_id, media_group_id)
    if len(members) < 2:
        return
    text, markup = batch_summary(members, f"g:{media_group_id}", "보낸 명함")
    state = cards.load_state("groups")
    key = f"{chat_id}:{media_group_id}"
    message_id = state.get(key)
    if message_id:
        try:
            edit_message(token, chat_id, message_id, text, reply_markup=markup)
            return
        except Exception:
            pass
    result = send_message(token, chat_id, text, reply_markup=markup)
    state[key] = result["result"]["message_id"]
    cards.save_state("groups", state)


# ---------------------------------------------------------------- text replies

def handle_text(token: str, chat_id: int, message: dict) -> None:
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        send_message(token, chat_id, "명함 사진을 보내주세요.")
        return
    if text.lower().split("@", 1)[0] == "/export":
        submit(send_export, token, chat_id)
        return
    if text.lower() in HELP_WORDS:
        send_message(token, chat_id, HELP_TEXT)
        return
    if text.lower() in ("/alerts", "알림 설정"):
        send_alerts_menu(token, chat_id)
        return
    if text.lower() in LIST_WORDS:
        send_open_list(token, chat_id)
        return

    card = select_card(chat_id, message_id=(message.get("reply_to_message") or {}).get("message_id"))
    if card is None:
        open_cards = cards.list_cards(chat_id, cards.REVIEW_STATUSES)
        if len(open_cards) == 1:
            card = open_cards[0]
        elif not open_cards:
            send_message(token, chat_id, "명함 사진을 보내주세요. (사용법: 도움말)")
            return
        else:
            send_message(token, chat_id, f"검토 중인 명함이 {len(open_cards)}건입니다. 고칠 명함의 초안 메시지에 답장으로 보내주세요.")
            send_open_list(token, chat_id)
            return
    if card["status"] not in cards.REVIEW_STATUSES:
        send_message(token, chat_id, f"{cards.label(card)}은(는) 이미 {'저장' if card['status'] == 'saved' else '종료'}된 명함입니다.")
        return
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 초안 수정")
    ok, note = pipeline.edit(card, text)
    if not ok:
        edit_message(token, chat_id, status_id, f"{cards.label(card)} {note}")
        _remember_card_message(card, status_id)
        return
    submit(show_draft, token, chat_id, card, status_id, f"{cards.label(card)} {note}")


def send_open_list(token: str, chat_id: int) -> None:
    open_cards = cards.list_cards(chat_id, cards.OPEN_STATUSES)
    if not open_cards:
        send_message(token, chat_id, "저장 안 된 명함이 없습니다.", reply_markup=export_buttons())
        return
    text, markup = batch_summary(open_cards, "all", "저장 안 된 명함")
    edit_rows = [[{"text": f"{cards.label(card)} 직접 수정", "callback_data": f"ed:{card['id']}"}]
                 for card in open_cards if card["status"] in cards.REVIEW_STATUSES]
    if edit_rows:
        markup = {"inline_keyboard": (markup or {}).get("inline_keyboard", []) + edit_rows}
    if any(card["status"] in cards.REVIEW_STATUSES for card in open_cards):
        text += "\n\n검토 중이거나 저장 보류된 명함을 고치려면 해당 초안 메시지에 답장하세요."
    markup = {"inline_keyboard": (markup or {}).get("inline_keyboard", []) + export_buttons()["inline_keyboard"]}
    send_message(token, chat_id, text, reply_markup=markup)
    maybe_note_notice(token, chat_id)
    for card in open_cards:
        if card["status"] == "needs_confirmation":
            send_message(token, chat_id, f"{cards.label(card)} 저장 여부를 버튼으로 확인하세요.", reply_markup=saved_failed_buttons(card))


# ---------------------------------------------------------------- buttons

def handle_callback(token: str, callback: dict, chat_id: int) -> None:
    callback_id = callback.get("id")
    if callback_id:
        try:
            api_post(token, "answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception:
            log.warning("answerCallbackQuery failed; continuing")

    data = str(callback.get("data") or "")
    log.info("callback chat=%s data=%s", chat_id, data)
    action, _, rest = data.partition(":")
    if action == "mh":
        send_message(token, chat_id, MULTICARD_HELP)
        return
    if action in ("na", "nc", "ex"):
        if chat_id not in allowed_chat_ids():
            return
        if action in ("na", "nc"):
            from . import meeting_note
            if not meeting_note.enabled():
                send_message(token, chat_id, "메모 기능이 꺼져 있습니다(BIZCARD_MEETING_NOTE=off).")
                return
        if action == "na":
            request_note_permission(token, chat_id)
        elif action == "nc":
            schedule_note_drain(token, check_chat_id=chat_id)
        else:
            submit(send_export, token, chat_id)
        return
    if action in ("ae", "ad"):
        if chat_id not in allowed_chat_ids():
            return
        with cards.followup_lock:
            state = cards.initialize_followup()
            state["enabled"] = action == "ae"
            cards.save_state("followup", state)
        send_alerts_menu(token, chat_id)
        return
    if action in ("fm", "fl", "fx", "fo"):
        if chat_id not in allowed_chat_ids():
            return
        card_id, _, nonce = rest.partition(":")
        outcome = pipeline.resolve_followup(card_id, chat_id,
                                            {"fm": "sent", "fl": "later", "fx": "stop", "fo": "off"}[action], nonce)
        message_id = (callback.get("message") or {}).get("message_id")
        markup = alerts_buttons(False) if outcome["ok"] and action == "fo" else {"inline_keyboard": []}
        if outcome["ok"] and message_id:
            edit_message(token, chat_id, message_id, outcome["message"], reply_markup=markup)
        else:
            send_message(token, chat_id, outcome["message"], reply_markup=markup)
        return
    if action == "ba":
        submit(approve_batch, token, chat_id, rest)
        return
    if action not in ("ap", "ju", "jn", "cx", "ed", "bk", "rs", "sv", "cs", "cr", "ck"):
        send_message(token, chat_id, "예전 버튼입니다. 최근 초안 메시지의 버튼을 눌러주세요. ('목록'으로 확인)")
        return
    card_id, _, revision = rest.partition(":")
    card = cards.load_card(card_id)
    if card is None or card.get("chat_id") != str(chat_id):
        send_message(token, chat_id, "해당 명함을 찾을 수 없습니다. '목록'으로 확인해주세요.")
        return

    if action in ("cs", "cr", "ck"):
        # Nonce is independent of the draft revision; old numeric callbacks are stale.
        submit(confirm_card, token, chat_id, card, {"cs": "saved", "cr": "again", "ck": "skip"}[action], revision, callback)
    elif action in ("ju", "jn"):
        if not revision.isdigit():
            send_message(token, chat_id, "최근 초안의 버튼을 눌러주세요.")
            return
        submit(approve_card, token, chat_id, card, int(revision), "update" if action == "ju" else "new")
    elif action in ("ap", "sv"):
        submit(approve_card, token, chat_id, card, int(revision) if revision.isdigit() and action == "ap" else None)
    elif action == "cx":
        if card["status"] == "saved":
            send_message(token, chat_id, f"{cards.label(card)}은(는) 이미 저장되어 취소할 수 없습니다.")
            return
        if pipeline.cancel(card, blocking=False, defer_processing=True):
            send_message(token, chat_id, f"{cards.label(card)} 취소했습니다.")
        elif card.get("cancel_requested"):
            send_message(token, chat_id, f"{cards.label(card)} 인식이 끝나면 취소합니다")
            # Close the marker-write/recognition-finish race without making the
            # update thread wait. An active recognition consumes it in finally.
            if _executor is None:
                threading.Thread(target=pipeline.finish_cancel_request,
                                 args=(card["id"],), daemon=True).start()
            else:
                submit(pipeline.finish_cancel_request, card["id"])
        elif card.get("status") in ("processing", "saving", "review"):
            send_message(token, chat_id, pipeline.BUSY_MESSAGE)
        else:
            send_message(token, chat_id, f"{cards.label(card)} 저장이 시작되었거나 종료되어 취소하지 못했습니다.")
    elif action in ("ed", "bk", "rs"):
        _handle_draft_callback(token, chat_id, card, action)


@_report_stale_write
def _handle_draft_callback(token: str, chat_id: int, card: dict, action: str) -> None:
    # Do not block polling behind a slow Contacts write or mutate its version.
    with pipeline.card_lock(card["id"], blocking=False) as acquired:
        if not acquired:
            send_message(token, chat_id, f"{cards.label(card)} {pipeline.BUSY_MESSAGE}")
            return
        fresh = cards.load_card(card["id"])
        if not fresh or fresh["status"] not in cards.REVIEW_STATUSES:
            note = pipeline.BUSY_MESSAGE if fresh and fresh["status"] in ("processing", "saving") else "지금은 수정할 수 없는 상태입니다. 최근 안내를 확인하세요."
            send_message(token, chat_id, f"{cards.label(card)} {note}")
            return
        card.clear()
        card.update(fresh)
        if pipeline.unresolved_save(card):
            send_message(token, chat_id, "이전 저장 결과를 먼저 확인하세요. 확인 전에는 초안을 수정할 수 없습니다.",
                         reply_markup=saved_failed_buttons(card))
            return
        if action == "rs":
            if not card["images"].get("front") or not Path(card["images"]["front"]).exists():
                send_message(token, chat_id, f"{cards.label(card)} 원본 사진이 없어 재인식할 수 없습니다. 사진을 다시 보내주세요.")
                return
            status_id = send_status(token, chat_id, f"{cards.label(card)} 다시 인식하는 중")
            card["status"] = "processing"
            cards.save_card(card)
        else:
            if action == "ed":
                prompt = edit_prompt(card)
                markup = {"force_reply": True, "input_field_placeholder": "직함: 과장"}
            else:
                card["awaiting_back"] = True
                prompt = f"{cards.label(card)} 뒷면 사진을 이 메시지에 답장으로 보내주세요."
                markup = {"force_reply": True}
            result = send_message(token, chat_id, prompt, reply_markup=markup)
            message_id = result["result"]["message_id"]
            with cards.pending_message(card, message_id):
                _record_card_message_locked(card, message_id)
    if action == "rs":
        submit(_recognize_again, token, chat_id, card, status_id)


def _recognize_again(token: str, chat_id: int, card: dict, status_id: int) -> None:
    try:
        pipeline.recognize_front(card)
    except (pipeline.ContactSaveError, cards.StaleCardState) as exc:
        send_message(token, chat_id, str(exc))
        return
    show_draft(token, chat_id, card, status_id)


def confirm_card(token: str, chat_id: int, card: dict, choice: str, confirmation_id: str,
                 callback: Optional[dict] = None) -> None:
    outcome = pipeline.resolve_confirmation(card, choice, confirmation_id)
    callback = callback or {}
    message_id = (callback.get("message") or {}).get("message_id")
    if message_id is not None:
        try:
            api_post(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                     "reply_markup": {"inline_keyboard": []}})
        except Exception:
            log.warning("confirmation keyboard removal failed")
    if outcome.get("stale"):
        send_message(token, chat_id, "이미 처리됨: 최근 안내를 확인하세요.")
    else:
        text = save_result_text(card, outcome) if choice == "again" else outcome["message"]
        send_message(token, chat_id, text,
                              reply_markup=None if outcome["ok"] else saved_failed_buttons(card))


def approve_card(token: str, chat_id: int, card: dict, revision: Optional[int], job_choice: Optional[str] = None) -> dict:
    display = pipeline.current_draft(card).display_name
    status_id = send_status(token, chat_id, f"{cards.label(card)} 저장 중: {display}")
    _remember_card_message(card, status_id)
    outcome = pipeline.approve(card, revision, job_choice=job_choice) if job_choice else pipeline.approve(card, revision)
    markup = None if outcome["ok"] or outcome.get("stale") else (draft_buttons(card) if outcome.get("invalid") else saved_failed_buttons(card))
    edit_message(token, chat_id, status_id, save_result_text(card, outcome), reply_markup=markup)
    if outcome["ok"]:
        maybe_note_notice(token, chat_id)
    return outcome


def approve_batch(token: str, chat_id: int, scope: str) -> None:
    if scope.startswith("g:"):
        members = cards.group_cards(chat_id, scope[2:])
    else:
        members = cards.list_cards(chat_id, cards.REVIEW_STATUSES)
    targets = [c for c in members if c["status"] == "review" and pipeline.current_draft(c).is_savable and not c.get("job_change") and not c.get("job_change_pending")]
    choices = [c for c in members if c["status"] == "review" and c.get("job_change")]
    for card in choices:
        send_message(token, chat_id, f"{cards.label(card)} {pipeline.current_draft(card).display_name}: 일괄 저장에서 제외했습니다. 갱신 방법을 선택하세요.",
                              reply_markup={"inline_keyboard": [draft_buttons(card)["inline_keyboard"][0]]})
    if not targets:
        if not choices:
            send_message(token, chat_id, "저장할 수 있는 명함이 없습니다.")
        return
    status_id = send_status(token, chat_id, f"{len(targets)}건 저장 중… ({contacts.destination_name()}에 한 건씩 저장합니다)")
    lines = []
    ok = 0
    last_progress = time.monotonic()
    with contacts.contact_snapshot():
        for index, card in enumerate(targets, 1):
            outcome = pipeline.approve(card)
            ok += 1 if outcome["ok"] else 0
            lines.append(f"- {cards.label(card)} {pipeline.current_draft(card).display_name}: {'저장' if outcome['ok'] else '보류 · ' + outcome['message']}")
            if index < len(targets) and time.monotonic() - last_progress >= 5:
                last_progress = time.monotonic()
                try:
                    edit_message(token, chat_id, status_id, f"{len(targets)}건 저장 중… {index}/{len(targets)}")
                except Exception:
                    pass
    skipped = [c for c in members if c["status"] == "review" and c not in targets]
    text = f"일괄 저장 {ok}/{len(targets)}건 완료\n" + "\n".join(lines)
    if skipped:
        text += f"\n\n별도 확인이 필요한 {len(skipped)}건은 남겨두었습니다. 각 초안의 버튼을 눌러주세요."
    if ok < len(targets):
        text += "\n저장 보류된 명함은 자동으로 다시 시도합니다."
    edit_message(token, chat_id, status_id, text, reply_markup=export_buttons())
    maybe_note_notice(token, chat_id)
    if scope.startswith("g:"):
        try:
            send_group_summary(token, chat_id, scope[2:])
        except Exception:
            log.exception("group summary refresh failed")


def alerts_buttons(enabled: bool) -> dict:
    return {"inline_keyboard": [[{"text": "알림 끄기" if enabled else "알림 다시 켜기",
                                  "callback_data": "ad" if enabled else "ae"}]]}


def send_alerts_menu(token: str, chat_id: int) -> None:
    enabled = cards.initialize_followup().get("enabled", True)
    message = "후속 알림이 켜져 있습니다." if enabled else "후속 알림이 꺼져 있습니다."
    if not cards.followup_days():
        message += " BIZCARD_FOLLOWUP_DAYS=0이므로 발송하지 않습니다. 일수를 설정한 뒤 봇을 다시 시작하세요."
    else:
        message += " 다시 켜면 대기 중인 알림과 앞으로 저장하는 명함에 적용합니다."
    send_message(token, chat_id, message, reply_markup=alerts_buttons(enabled))


def followup_buttons(card: dict) -> dict:
    suffix = f"{card['id']}:{card['followup_nonce']}"
    return {"inline_keyboard": [
        [{"text": "보냈어요", "callback_data": f"fm:{suffix}"},
         {"text": "3일 뒤 다시", "callback_data": f"fl:{suffix}"}],
        [{"text": "이 명함은 그만", "callback_data": f"fx:{suffix}"},
         {"text": "알림 끄기", "callback_data": f"fo:{suffix}"}]]}


def send_followup_message(token: str, card: dict) -> dict:
    chat_id = int(card["chat_id"])
    _pace(chat_id)
    # Telegram has no sendMessage idempotency key. One attempt, including 429;
    # failures retain the durable 'sending' claim and are never auto-retried.
    return api_post(token, "sendMessage", {"chat_id": chat_id,
                    "text": followup_text(card)[:4000], "reply_markup": followup_buttons(card)}, attempts=1)


def _safe_send_due_followups(token: str) -> None:
    try:
        send_due_followups(token)
    except Exception:
        log.warning("follow-up scan failed; continuing polling")


@pipeline.stale_safe("none")
def send_due_followups(token: str) -> None:
    now = time.time()
    if not 10 <= cards.followup_localtime(now).hour < 19:
        return
    if not cards.followup_days() or not cards.initialize_followup().get("enabled", True):
        return
    allowed = allowed_chat_ids()
    for candidate in cards.list_cards(statuses=["saved"]):
        if candidate.get("followup_state") != "pending" or (candidate.get("followup_due_at") or float("inf")) > now:
            continue
        with pipeline.card_lock(candidate["id"], blocking=False) as acquired:
            if not acquired:
                continue
            card = _claim_due_followup_locked(candidate, allowed)
        if card is None:
            continue
        try:
            result = send_followup_message(token, card)
        except Exception:
            log.warning("follow-up delivery uncertain card=%s", card["id"])
            continue
        message_id = (result.get("result") or {}).get("message_id")
        with cards.pending_message(card, message_id):
            with pipeline.card_lock(card["id"]):
                with cards.followup_lock:
                    fresh = cards.load_card(card["id"])
                    if (not fresh or fresh.get("status") != "saved"
                            or fresh.get("chat_id") != card.get("chat_id")
                            or fresh.get("followup_nonce") != card.get("followup_nonce")
                            or fresh.get("followup_state") != "sending"):
                        continue
                    fresh.update(followup_state="notified", followup_message_id=message_id)
                    _record_card_message_locked(fresh, message_id)


def _claim_due_followup_locked(candidate: dict, allowed: set) -> Optional[dict]:
    with cards.followup_lock:
        now = time.time()
        if not 10 <= cards.followup_localtime(now).hour < 19:
            return
        card = cards.load_card(candidate["id"])
        if (not card or card.get("status") != "saved" or card.get("followup_state") != "pending"
                or card.get("followup_nonce") != candidate.get("followup_nonce")
                or (card.get("followup_due_at") or float("inf")) > now
                or not cards.initialize_followup().get("enabled", True)):
            return
        if str(card.get("chat_id")) not in {str(value) for value in allowed}:
            return
        # Durable at-most-once claim precedes the network call. A crash or
        # timeout leaves 'sending'; restart must not risk a duplicate message.
        card.update(followup_state="sending", followup_nonce=secrets.token_hex(8))
        cards.save_card(card)
        return card

def draft_buttons(card: dict) -> dict:
    if pipeline.unresolved_save(card):
        return saved_failed_buttons(card)
    cid, rev = card["id"], card.get("revision") or 0
    rows = [
        [{"text": "저장", "callback_data": f"ap:{cid}:{rev}"}, {"text": "직접 수정", "callback_data": f"ed:{cid}"}],
        [{"text": "뒷면 추가", "callback_data": f"bk:{cid}"}, {"text": "인식 재시도", "callback_data": f"rs:{cid}"}, {"text": "취소", "callback_data": f"cx:{cid}"}],
    ]
    if pipeline.job_change_pending_active(card):
        rows[0] = [{"text": "직접 수정", "callback_data": f"ed:{cid}"}]
    elif card.get("job_change") and not card.get("job_change_pending"):
        rows[0] = [{"text": "기존 연락처 갱신", "callback_data": f"ju:{cid}:{rev}"},
                   {"text": "새 연락처로 저장", "callback_data": f"jn:{cid}:{rev}"}]
        rows.insert(1, [{"text": "직접 수정", "callback_data": f"ed:{cid}"}])
    return {"inline_keyboard": rows}


def saved_failed_buttons(card: dict) -> dict:
    cid = card["id"]
    if card.get("status") == "needs_confirmation":
        rev = pipeline.confirmation_token(card)
        if not rev:
            return {"inline_keyboard": []}
        return {"inline_keyboard": [[
            {"text": "이미 저장됨", "callback_data": f"cs:{cid}:{rev}"},
            {"text": "다시 저장", "callback_data": f"cr:{cid}:{rev}"},
            {"text": "건너뛰기", "callback_data": f"ck:{cid}:{rev}"}]]}
    if pipeline.unresolved_save(card):
        return {"inline_keyboard": [[{"text": "이전 저장 확인", "callback_data": f"sv:{cid}"}]]}
    return {"inline_keyboard": [[{"text": "지금 다시 저장", "callback_data": f"sv:{cid}"}, {"text": "직접 수정", "callback_data": f"ed:{cid}"}, {"text": "취소", "callback_data": f"cx:{cid}"}]]}


def save_largest_photo(token: str, photos: list, chat_id: int, data_dir: Path, side: str, card_id: str = "") -> Path:
    file_id = photos[-1]["file_id"]
    file_info = api_get(token, "getFile", {"file_id": file_id})["result"]
    file_path = file_info["file_path"]
    name = Path(file_path).name
    target = data_dir / str(chat_id) / side / (f"{card_id}_{name}" if card_id else name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(download_file(token, file_path))
    log.info("saved %s photo: %s", side, target)
    return target


def get_updates(token: str, offset: Optional[int] = None) -> list:
    params = {"timeout": LONG_POLL_SECONDS, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    return api_get(token, "getUpdates", params, timeout=LONG_POLL_SECONDS + 10)["result"]


def send_status(token: str, chat_id: int, text: str) -> int:
    return send_message(token, chat_id, text)["result"]["message_id"]


_pace_lock = threading.Lock()
_last_send: dict[int, float] = {}


def _pace(chat_id: int) -> None:
    """Keep messages to one chat spaced out; Telegram throttles bursts per chat."""
    interval = float(os.environ.get("BIZCARD_TELEGRAM_INTERVAL", "0.35"))
    with _pace_lock:
        now = time.monotonic()
        wait = _last_send.get(chat_id, 0.0) + interval - now
        _last_send[chat_id] = max(now, _last_send.get(chat_id, 0.0) + interval)
    if wait > 0:
        time.sleep(wait)


def send_message(token: str, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> dict:
    _pace(chat_id)
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_post(token, "sendMessage", payload)


def edit_message(token: str, chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None) -> dict:
    _pace(chat_id)
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return api_post(token, "editMessageText", payload)
    except RuntimeError as exc:
        if "not modified" in str(exc):
            return {"ok": True}
        raise


def send_photo(token: str, chat_id: int, image_path: Path, caption: str = "", reply_markup: Optional[dict] = None) -> dict:
    _pace(chat_id)
    boundary = f"----bizcard-{uuid.uuid4().hex}"
    parts = [
        _multipart_field(boundary, "chat_id", str(chat_id)),
        _multipart_field(boundary, "caption", caption[:CAPTION_LIMIT]),
        *([_multipart_field(boundary, "reply_markup", json.dumps(reply_markup))] if reply_markup else []),
        _multipart_file(boundary, "photo", image_path.name, "image/jpeg", image_path.read_bytes()),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    request = Request(
        method_url(token, "sendPhoto"),
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    def call() -> dict:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    result = with_retry(call, "Telegram sendPhoto")
    if result.get("ok") is not True:
        raise RuntimeError(result.get("description") or "Telegram sendPhoto failed")
    return result


def send_document(token: str, chat_id: int, content: bytes, filename: str,
                  caption: str = "") -> dict:
    _pace(chat_id)
    boundary = f"----bizcard-{uuid.uuid4().hex}"
    request = Request(
        method_url(token, "sendDocument"),
        data=b"".join([
            _multipart_field(boundary, "chat_id", str(chat_id)),
            _multipart_field(boundary, "caption", caption[:CAPTION_LIMIT]),
            _multipart_file(boundary, "document", filename, "text/csv; charset=utf-8", content),
            f"--{boundary}--\r\n".encode("utf-8"),
        ]),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    def call() -> dict:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    result = with_retry(call, "Telegram sendDocument")
    if result.get("ok") is not True:
        raise RuntimeError("Telegram sendDocument failed")
    return result


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")


def _multipart_file(boundary: str, name: str, filename: str, content_type: str, content: bytes) -> bytes:
    header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + content + b"\r\n"


def delete_webhook(token: str) -> None:
    api_post(token, "deleteWebhook", {"drop_pending_updates": False})


RATE_LIMIT_WAITS = 8


def with_retry(call, label: str, attempts: int = RETRY_ATTEMPTS):
    """Retry transient network failures; wait out Telegram's 429 retry_after
    (not counted as a failed attempt); attempts=1 disables all retries and waits.
    Fail fast on other 4xx."""
    attempt, rate_waits = 1, 0
    while True:
        try:
            return call()
        except HTTPError as exc:
            body = _http_error_body(exc)
            if exc.code == 429 and attempts != 1 and rate_waits < RATE_LIMIT_WAITS:
                rate_waits += 1
                delay = float(((body or {}).get("parameters") or {}).get("retry_after") or RETRY_DELAY * rate_waits)
                log.warning("%s rate limited; waiting %.0fs (%d/%d)", label, delay, rate_waits, RATE_LIMIT_WAITS)
                time.sleep(min(delay, 60))
                continue
            if 400 <= exc.code < 500:
                description = (body or {}).get("description") or f"HTTP {exc.code}"
                if exc.code == 409:
                    raise TelegramAPIError(exc.code, description) from None
                raise RuntimeError(f"{label}: {description}") from exc
            if attempt >= attempts:
                raise
            log.warning("%s failed (HTTP %s); retry %d/%d", label, exc.code, attempt, attempts - 1)
            time.sleep(RETRY_DELAY * attempt)
            attempt += 1
        except (URLError, ConnectionError, TimeoutError, OSError) as exc:
            if attempt >= attempts:
                raise
            log.warning("%s failed (%s); retry %d/%d", label, exc, attempt, attempts - 1)
            time.sleep(RETRY_DELAY * attempt)
            attempt += 1


def _http_error_body(exc: HTTPError) -> Optional[dict]:
    try:
        return json.loads(exc.read().decode("utf-8"))
    except Exception:
        return None


def api_get(token: str, method: str, params: Optional[dict] = None, timeout: int = 10) -> dict:
    def call() -> dict:
        with urlopen(method_url(token, method, params), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    payload = with_retry(call, f"Telegram {method}")
    if payload.get("ok") is not True:
        if payload.get("error_code") == 409:
            raise TelegramAPIError(409, payload.get("description") or "Conflict")
        raise RuntimeError(payload.get("description") or f"Telegram {method} failed")
    return payload


def api_post(token: str, method: str, payload: dict, *, attempts: Optional[int] = None) -> dict:
    request = Request(
        method_url(token, method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def call() -> dict:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    result = with_retry(call, f"Telegram {method}", attempts=attempts) if attempts is not None else with_retry(call, f"Telegram {method}")
    if result.get("ok") is not True:
        raise RuntimeError(result.get("description") or f"Telegram {method} failed")
    return result


def download_file(token: str, file_path: str) -> bytes:
    url = f"{API_BASE}/file/bot{quote(token, safe=':')}/{quote(file_path, safe='/')}"

    def call() -> bytes:
        with urlopen(url, timeout=20) as response:
            return response.read()

    return with_retry(call, "Telegram download")


def method_url(token: str, method: str, params: Optional[dict] = None) -> str:
    url = f"{API_BASE}/bot{quote(token, safe=':')}/{method}"
    return f"{url}?{urlencode(params)}" if params else url


def setup_logging() -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    log.addHandler(stream)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
        stream.setLevel(logging.WARNING)  # bot.log rotates; launchd's stderr file does not
    except OSError:
        log.warning("could not open log file; continuing with stderr only")


def main() -> Optional[int]:
    setup_logging()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    return run(token)
