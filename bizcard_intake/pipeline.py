"""Channel-independent core: card -> recognize -> review/edit -> approve -> Google Contacts.

The channel (Telegram, CLI) creates a card per photo and calls these functions;
every state change is persisted on the card before and after slow or remote
steps, so a crash, restart or network error never loses a card or makes an
unapproved draft save itself.
"""

from __future__ import annotations

import copy
import json
import logging
import secrets
import threading
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from functools import wraps
from pathlib import Path
from typing import Callable, Optional

from . import cards, apple_contacts, contact_backend
from .contact_backend import ContactSaveError, SaveResult, save_contact, upload_photo, refresh_resource_name
from .draft import CardDraft, apply_edits, parse_edits, preserve_locked
from .image_processing import PreparedImages, prepare_card_images as _prepare_card_images
from .parser import merge_drafts, parse_card, revise_draft
from .vision_ocr import OcrResult, extract_text, vote_orientation, stage_timer

DATA_DIR = Path("data/telegram_photos")
LOG_DIR = Path("data/logs")
SESSION_DIR = Path("data/sessions")  # legacy, migrated into cards
SESSION_ARCHIVE_DIR = Path("data/session_archive")
PENDING_CONTACT_DIR = Path("data/pending_contacts")  # legacy, migrated into cards
IMAGE_KEYS = ("front", "profile", "ocr", "llm", "rectified", "back")
LEGACY_IMAGE_KEYS = {"front_image_path": "front", "profile_image_path": "profile", "ocr_image_path": "ocr", "llm_image_path": "llm", "rectified_image_path": "rectified"}
RETRY_BASE_SECONDS = 120
RETRY_MAX_SECONDS = 6 * 3600
AUTH_RETRY_SECONDS = 1800
ORIENTATION_MIN_TOTAL = 10
ORIENTATION_MIN_SHARE = 0.6
ORIENTATION_WARNING = "명함 방향 확인 필요"

log = logging.getLogger("bizcard")

# Image prep and Apple Vision OCR are CPU-bound; with 30 card workers only this
# many run at once (the model calls, which only wait on the network, do not count).
import os as _os
_cpu_slots = threading.BoundedSemaphore(max(2, (_os.cpu_count() or 4)))


def prepare_card_images(*args, **kwargs):
    with _cpu_slots:
        return _prepare_card_images(*args, **kwargs)


def _ocr(path):
    with _cpu_slots:
        return extract_text(path)


def warm_up() -> None:
    """Prime local Vision OCR once, waiting at most 30 seconds; never block startup."""
    def run():
        try:
            from PIL import Image, ImageDraw
            with tempfile.TemporaryDirectory(prefix="bizcard-warmup-") as folder:
                path = Path(folder) / "warmup.jpg"
                image = Image.new("RGB", (320, 100), "white")
                ImageDraw.Draw(image).text((20, 30), "Business card", fill="black")
                image.save(path)
                _ocr(path)
        except Exception:
            log.warning("Vision OCR warm-up failed", exc_info=True)

    worker = threading.Thread(target=run, name="vision-ocr-warmup", daemon=True)
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        log.warning("Vision OCR warm-up exceeded 30 seconds")


def _orientation(prepared: PreparedImages, ocr: OcrResult) -> tuple[int, float, float]:
    from PIL import Image
    with Image.open(prepared.ocr_path) as image:
        return vote_orientation(ocr.lines, image.width, image.height)


def _prepare_oriented(source: Path, profile: Path) -> tuple[PreparedImages, OcrResult, bool]:
    """Share the same one-retry orientation check between front and back."""
    with stage_timer("prepare"):
        prepared = prepare_card_images(source, profile)
    with stage_timer("ocr"):
        ocr = _ocr(prepared.ocr_path)
    with stage_timer("orientation"):
        quarters, share, total = _orientation(prepared, ocr)
    log.info("orientation image=%s quarters=%d share=%.3f total=%.1f", source.name, quarters, share, total)
    if total < ORIENTATION_MIN_TOTAL or share < ORIENTATION_MIN_SHARE:
        return prepared, ocr, True
    if quarters == 0:
        return prepared, ocr, False
    try:
        with stage_timer("reprepare"):
            rotated = prepare_card_images(source, profile, rotate_quarters=quarters, reuse=prepared)
        with stage_timer("reocr"):
            rotated_ocr = _ocr(rotated.ocr_path)
        with stage_timer("reorientation"):
            verified, verified_share, verified_total = _orientation(rotated, rotated_ocr)
        if verified == 0 and verified_total >= ORIENTATION_MIN_TOTAL and verified_share >= ORIENTATION_MIN_SHARE:
            return rotated, rotated_ocr, False
        log.warning("orientation verification failed image=%s quarters=%d share=%.3f total=%.1f", source.name, verified, verified_share, verified_total)
    except Exception:
        log.warning("orientation retry failed image=%s", source.name, exc_info=True)
    # Outputs share paths: regenerate the first-pass images from its untouched
    # rectification, retaining its OCR without another recognition attempt.
    with stage_timer("restore_prepare"):
        restored = prepare_card_images(source, profile, reuse=prepared)
    return restored, ocr, True


_locks_guard = threading.Lock()
_card_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


@contextmanager
def card_lock(card_id: str, *, blocking: bool = True, timeout: float = -1):
    """Serialize saves, edits and cancellation for a card across channel workers."""
    with _locks_guard:
        lock = _card_locks[card_id]
    acquired = lock.acquire(blocking=blocking, timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def stale_safe(result_kind: str):
    """Translate refused writes at operation boundaries; never replay a save.

    Normal writers reload under card_lock. This also handles delayed input and
    a second session modifying storage: refresh the caller and report refusal.
    """
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except cards.StaleCardState as exc:
                for value in args:
                    if isinstance(value, dict) and value.get("id"):
                        fresh = cards.load_card(value["id"])
                        if fresh:
                            value.clear()
                            value.update(fresh)
                message = "저장 상태가 변경되어 요청을 반영하지 않았습니다. 이전 저장 결과를 확인한 뒤 다시 시도하세요."
                log.warning("card write refused operation=%s", function.__name__)
                if result_kind == "edit":
                    return False, message
                if result_kind == "bool":
                    return False
                if result_kind == "token":
                    return ""
                if result_kind == "scan":
                    return 0, [message]
                if result_kind == "notes":
                    return [message]
                if result_kind == "none":
                    return None
                if result_kind == "photo":
                    raise ContactSaveError(message + " 사진을 다시 보내주세요.", kind="invalid") from exc
                return {"ok": False, "stale": True, "message": message}
        return wrapped
    return decorate


def profile_image_path(source_path: Path) -> Path:
    return source_path.parent.parent / "profile" / f"{source_path.stem}_profile.jpg"


def current_draft(card: dict) -> CardDraft:
    return CardDraft.from_session_value(card.get("draft"))


def unresolved_save(card: dict) -> bool:
    return bool(card.get("save_intent")) and card.get("status") not in ("saved", "cancelled")


@contextmanager
def _collect_cost(card: dict, *, recognition: bool = True):
    """Keep optional accounting failures outside the recognition/save path."""
    from . import parser
    collector = getattr(parser, "collect", None)
    with collector() if collector else nullcontext([]) as calls:
        try:
            yield
        finally:
            try:
                from . import metrics
                cost = metrics.record_calls(card, calls)
                if recognition:
                    card["recognition_cost_krw"] = cost
                    threshold = _cost_warn_threshold()
                    card["recognition_cost_notice_pending"] = cost is not None and cost > threshold
                    if card["recognition_cost_notice_pending"]:
                        log.warning("AI recognition cost %.2f KRW exceeds %.2f KRW", cost, threshold)
            except ImportError:
                pass  # parallel accounting module is not present before integration
            except Exception:
                log.warning("AI cost recording failed")


def _cost_warn_threshold() -> float:
    import math
    try:
        value = float(_os.environ.get("BIZCARD_COST_WARN_KRW", "2"))
        return value if math.isfinite(value) and value >= 0 else 2.0
    except ValueError:
        return 2.0


def set_draft(card: dict, draft: CardDraft, event: str) -> None:
    if unresolved_save(card):
        raise ContactSaveError("이전 저장 결과를 먼저 확인하세요. 확인 전에는 초안을 수정할 수 없습니다.", kind="invalid")
    try:
        from . import metrics
        review_event = {"back_merged": "back_added"}.get(event, event)
        if event == "recognized" and card.get("revision", 0) > 0:
            review_event = "rerecognized"
        metrics.record_edit(card, current_draft(card), draft, review_event)
    except ImportError:
        pass
    except Exception:
        log.warning("review recording failed")
    card["draft"] = draft.to_dict()
    card.pop("job_change_lookup_key", None)
    card.pop("job_choice", None)
    card["job_change"] = None
    card["job_change_pending"] = False
    card.pop("job_change_started_at", None)
    card.pop("job_change_lookup_id", None)
    card["revision"] = int(card.get("revision") or 0) + 1
    cards.add_history(card, event, draft.display_name)


JOB_CHANGE_STALE_SECONDS = 20.0


def job_change_pending_active(card: dict) -> bool:
    if not card.get("job_change_pending"):
        return False
    started = card.get("job_change_started_at")
    if started is None:
        return False
    return isinstance(started, (int, float)) and time.time() - started < JOB_CHANGE_STALE_SECONDS


def refresh_job_change(card: dict) -> None:
    """Cache read-only matching (including failures) until relevant draft fields change."""
    if unresolved_save(card):
        return
    draft = current_draft(card)
    key = [draft.name, draft.company, draft.title, draft.mobile, draft.email,
           _os.environ.get("BIZCARD_CONTACTS_BACKEND", "google"),
           _os.environ.get("BIZCARD_APPLE_CONTAINER", "")]
    if card.get("job_change_lookup_key") == key:
        return
    card["job_change_lookup_key"] = key
    card["job_change"] = None
    card.pop("job_choice", None)
    try:
        person = contact_backend.find_job_change_candidate(draft)
        if person:
            org = (person.get("organizations") or [{}])[0]
            card["job_change"] = {"resource_name": person["resourceName"],
                                  "company": org.get("name", ""), "title": org.get("title", ""),
                                  "container": person.get("_container")}
    except Exception:
        # Neither raw helper errors nor contact data belong in this optional log.
        log.warning("job-change lookup unavailable card=%s", card["id"])


# ---------------------------------------------------------------- recognition

def scan_front(front_path: Path) -> dict:
    """Image prep + OCR + parse for one front photo. Returns images and draft."""
    started = time.perf_counter()
    prepared, ocr, orientation_warning = _prepare_oriented(front_path, profile_image_path(front_path))
    images = {
        "front": str(front_path),
        "profile": str(prepared.profile_path),
        "ocr": str(prepared.ocr_path),
        "llm": str(prepared.llm_path),
        "rectified": str(prepared.rectified_path) if prepared.rectified_path else None,
    }
    log.info("vision ocr image=%s lines=%d avg_conf=%.3f", prepared.ocr_path.name, len(ocr.lines), ocr.average_confidence)
    draft = parse_card(prepared.llm_path, ocr, "front")
    if orientation_warning and ORIENTATION_WARNING not in draft.warnings:
        draft.warnings.append(ORIENTATION_WARNING)
    elapsed = time.perf_counter() - started
    log.info("perf stage=scan_front seconds=%.6f", elapsed)
    log.info("front draft done image=%s elapsed=%.1fs", front_path.name, elapsed)
    return {"images": images, "rectifier": prepared.source_used, "draft": draft}


BUSY_MESSAGE = "처리 중입니다. 잠시 후 다시 눌러 주세요."


def cancel_path(card_id: str) -> Path:
    return cards.CARD_DIR.parent / "cancel" / card_id


def request_cancel(card: dict) -> None:
    # Separate durable mailbox: never wait for the busy card's write lock.
    cards._atomic_write(cancel_path(card["id"]), "cancel_requested")
    card["cancel_requested"] = True


def _recognize(card: dict, side: str, work, *args) -> dict:
    with card_lock(card["id"]):
        if not _refresh_for_change(card, allow_processing=True):
            raise ContactSaveError("이전 저장 결과를 먼저 확인하세요. 사진을 반영하지 않았습니다. 확인 후 다시 보내주세요.", kind="invalid")
        if cancel_path(card["id"]).exists():
            _cancel_locked(card)
            return card
        if card.get("recognition_id"):
            raise ContactSaveError(BUSY_MESSAGE, kind="invalid")
        operation = secrets.token_hex(8)
        card.update(status="processing", recognition_id=operation)
        if args and args[0] is not None:
            card["images"][side] = str(args[0])
        if side == "back":
            card["awaiting_back"] = False
        cards.save_card(card)
        version = card.get("_save_state_version", 0)
        revision = card.get("revision")
        snapshot = copy.deepcopy(card)
    completed = False
    try:
        # OCR, parsing, merging and image preparation run without a card lock.
        with _collect_cost(snapshot):
            work(snapshot, *args)
        completed = True
    finally:
        with card_lock(card["id"]):
            fresh = cards.load_card(card["id"])
            if fresh:
                owns = (fresh.get("recognition_id") == operation
                        and fresh.get("status") == "processing"
                        and fresh.get("revision") == revision
                        and fresh.get("_save_state_version", 0) == version
                        and not unresolved_save(fresh))
                if owns:
                    # Preserve message/reminder metadata written during OCR.
                    fields = ("images", "rectifier", "draft", "front_draft", "back_draft",
                              "recognition_error", "revision", "awaiting_back", "job_change",
                              "job_choice", "job_change_lookup_key", "job_change_pending",
                              "cost", "review", "recognition_cost_krw", "recognition_cost_notice_pending")
                    if completed:
                        for key in fields:
                            if key in snapshot:
                                fresh[key] = snapshot[key]
                            else:
                                fresh.pop(key, None)
                    fresh.pop("recognition_id", None)
                    fresh["status"] = "review"
                    if cancel_path(fresh["id"]).exists():
                        _cancel_locked(fresh)
                    else:
                        cards.add_history(fresh, "recognized")
                        cards.save_card(fresh)
                elif cancel_path(fresh["id"]).exists():
                    _cancel_locked(fresh)
                card.clear()
                card.update(fresh)
    return card


@stale_safe("photo")
def recognize_front(card: dict, front_path: Optional[Path] = None) -> dict:
    return _recognize(card, "front", _recognize_front_work, front_path)


def _recognize_front_work(card: dict, front_path: Optional[Path] = None) -> dict:
    """Fill the card from its front photo. A failure still leaves a reviewable card."""
    front = Path(front_path or card["images"]["front"])
    card["images"]["front"] = str(front)
    previous = current_draft(card)
    try:
        result = scan_front(front)
    except Exception as exc:  # keep the card editable whatever broke
        log.exception("recognition failed card=%s", card["id"])
        draft = preserve_locked(CardDraft(warnings=["자동 인식 실패: '직접 수정'으로 입력하거나 '인식 재시도'를 눌러주세요"]), previous)
        card["recognition_error"] = str(exc)[:300]
    else:
        card["images"].update({k: v for k, v in result["images"].items() if v})
        card["rectifier"] = result["rectifier"]
        draft = preserve_locked(result["draft"], previous)
        card["front_draft"] = draft.to_dict()
        card["recognition_error"] = None
    set_draft(card, draft, "recognized")
    card["status"] = "review"
    return card


@stale_safe("photo")
def recognize_back(card: dict, back_path: Path) -> dict:
    return _recognize(card, "back", _recognize_back_work, back_path)


def _recognize_back_work(card: dict, back_path: Path) -> dict:
    card["images"]["back"] = str(back_path)
    card["awaiting_back"] = False
    previous = current_draft(card)
    try:
        prepared, ocr, orientation_warning = _prepare_oriented(back_path, back_path.parent.parent / "back_tmp" / f"{back_path.stem}_profile.jpg")
        back = parse_card(prepared.llm_path, ocr, "back")
        if orientation_warning and ORIENTATION_WARNING not in back.warnings:
            back.warnings.append(ORIENTATION_WARNING)
        cleanup_paths(prepared.profile_path, prepared.ocr_path, prepared.llm_path, prepared.rectified_path)
        merged = merge_drafts(previous, back)
        if (orientation_warning or ORIENTATION_WARNING in previous.warnings) and ORIENTATION_WARNING not in merged.warnings:
            merged.warnings.append(ORIENTATION_WARNING)
        card["back_draft"] = back.to_dict()
    except Exception as exc:
        log.exception("back recognition failed card=%s", card["id"])
        merged = CardDraft.from_dict(previous.to_dict())
        merged.warnings.append(f"뒷면 인식 실패: {str(exc)[:80]}")
    set_draft(card, preserve_locked(merged, previous), "back_merged")
    card["status"] = "review"
    return card


# ---------------------------------------------------------------- editing

@stale_safe("edit")
def edit(card: dict, text: str) -> tuple[bool, str]:
    """Apply a reply. '필드: 값' lines are applied directly; free text goes to the AI reviser."""
    with card_lock(card["id"], blocking=False) as acquired:
        if not acquired:
            return False, BUSY_MESSAGE
        if not _refresh_for_change(card):
            if card.get("status") in ("processing", "saving"):
                return False, BUSY_MESSAGE
            if unresolved_save(card):
                return False, "이전 저장 결과를 먼저 확인하세요. 확인 전에는 초안을 수정할 수 없습니다."
            return False, "저장이 시작되었거나 종료되어 수정하지 못했습니다."
        return _edit_locked(card, text)


def _refresh_for_change(card: dict, *, allow_processing: bool = False) -> bool:
    attempts = int(card.get("save_attempts") or 0)
    fresh = cards.load_card(card["id"])
    if fresh is None:
        return False
    card.clear()
    card.update(fresh)
    allowed = cards.REVIEW_STATUSES + (("processing",) if allow_processing else ())
    return (not unresolved_save(card) and card.get("status") in allowed
            and int(card.get("save_attempts") or 0) == attempts)


def _edit_locked(card: dict, text: str) -> tuple[bool, str]:
    edits, unknown = parse_edits(text)
    previous = current_draft(card)
    if edits:
        set_draft(card, apply_edits(previous, edits), "manual_edit")
        _reopen_for_review(card)
        cards.save_card(card)
        note = f"\n반영하지 못한 줄: {' / '.join(unknown[:3])}" if unknown else ""
        return True, "직접 수정 반영" + note
    try:
        with _collect_cost(card, recognition=False):
            revised = revise_draft(previous, text)
    finally:
        cards.save_card(card)
    if revised is None:
        return False, "자동 수정 실패: '이름: 홍길동'처럼 '항목: 값' 형식으로 보내주시면 바로 반영됩니다."
    set_draft(card, preserve_locked(revised, previous), "ai_edit")
    _reopen_for_review(card)
    cards.save_card(card)
    return True, "초안 수정 반영"


def _reopen_for_review(card: dict) -> None:
    if card.get("status") == "save_failed":
        card["approved_revision"] = None
        card["next_retry_at"] = None
    if card.get("status") in ("save_failed", "processing"):
        card["status"] = "review"


# ---------------------------------------------------------------- saving

@stale_safe("outcome")
def approve(card: dict, revision: Optional[int] = None, *, job_choice: Optional[str] = None) -> dict:
    """Save the approved revision. Returns a result dict for the channel to render."""
    with card_lock(card["id"], blocking=False) as acquired:
        if not acquired:
            return {"ok": False, "busy": True, "message": BUSY_MESSAGE}
        fresh = cards.load_card(card["id"])
        if fresh is not None:
            card.clear()
            card.update(fresh)
        return _approve_locked(card, revision, job_choice)


def _approve_locked(card: dict, revision: Optional[int], job_choice: Optional[str] = None) -> dict:
    if card.get("status") in ("processing", "saving"):
        return {"ok": False, "busy": True, "message": BUSY_MESSAGE}
    if card.get("status") == "saved":
        return {"ok": True, "already": True, "message": f"이미 저장된 명함입니다: {current_draft(card).display_name}"}
    if card.get("status") not in cards.REVIEW_STATUSES:
        return {"ok": False, "message": "지금은 저장할 수 없는 상태입니다. 잠시 후 다시 눌러주세요."}
    if revision is not None and int(revision) != int(card.get("revision") or 0):
        return {"ok": False, "stale": True, "message": "초안이 그 사이 수정되었습니다. 가장 최근 초안의 '저장' 버튼을 눌러주세요."}
    if unresolved_save(card):
        intent = card["save_intent"]
        recorded_choice = {"update": "update", "create": "new"}.get(intent.get("op"))
        if job_choice and job_choice != recorded_choice:
            return {"ok": False, "invalid": True,
                    "message": "이전 저장 작업이 아직 확인되지 않았습니다. 저장 결과를 먼저 확인하세요."}
        # The durable operation is authoritative even if old UI metadata was lost.
        return _save(card)
    if card.get("job_change_pending"):
        if job_change_pending_active(card):
            return {"ok": False, "invalid": True, "message": "기존 연락처를 확인 중입니다. 잠시 후 저장 버튼을 눌러주세요."}
        card.update(job_change_pending=False, job_change=None, job_change_lookup_key=None)
        card.pop("job_change_started_at", None)
        card.pop("job_change_lookup_id", None)
        card.pop("job_choice", None)
        cards.save_card(card)
    draft = current_draft(card)
    missing = draft.missing_for_save()
    if missing:
        return {"ok": False, "invalid": True, "message": f"저장하려면 {', '.join(missing)}이(가) 필요합니다. '직접 수정'으로 입력해주세요."}
    if card.get("job_change"):
        if job_choice not in ("update", "new") and card.get("job_choice") not in ("update", "new"):
            return {"ok": False, "invalid": True, "message": "기존 연락처 갱신 또는 새 연락처로 저장을 선택하세요."}
        if job_choice:
            card["job_choice"] = job_choice
    elif job_choice:
        return {"ok": False, "stale": True, "message": "최근 초안의 저장 버튼을 눌러주세요."}
    card["approved_revision"] = card.get("revision")
    cards.add_history(card, "approved", str(card["approved_revision"]))
    return _save(card)


def _persist_save_fields(card: dict, *fields: str, event: str = "", detail: str = "") -> None:
    """Merge worker-owned fields into fresh state while the caller holds card_lock.

    Keep UI metadata and its current save-state version; never copy a stale card over
    the journal. The normal save_card operation/nonce guards still apply.
    """
    fresh = cards.load_card(card["id"]) or dict(card)
    if fresh.get("_save_state_version", 0) != card.get("_save_state_version", 0):
        raise cards.StaleCardState("저장 작업의 상태가 변경되었습니다. 이전 저장 결과를 확인하세요.")
    for field in fields:
        if field in card:
            fresh[field] = card[field]
        else:
            fresh.pop(field, None)
    if event:
        cards.add_history(fresh, event, detail)
    cards.save_card(fresh)
    card.clear()
    card.update(fresh)


def _save(card: dict, *, new_operation: bool = False) -> dict:
    draft = current_draft(card)
    card["status"] = "saving"
    card["save_attempts"] = int(card.get("save_attempts") or 0) + 1
    cards.save_card(card)

    def checkpoint(resource_name: str, action: str) -> None:
        card["resource_name"] = resource_name
        card["save_action"] = card.get("save_action") or action
        _persist_save_fields(card, "resource_name", "save_action",
                             event="contact_written", detail=f"{action} {resource_name}")

    def intent_checkpoint(intent: dict) -> None:
        if card.get("save_intent") and not new_operation:
            raise RuntimeError("Unresolved save operation cannot be replaced")
        card["save_intent"] = intent
        card["resource_name"] = None
        card.pop("save_resolution", None)
        _persist_save_fields(card, "save_intent", "resource_name", "save_resolution")

    try:
        kwargs = {}
        intent = card.get("save_intent")
        resource_name = card.get("resource_name")
        if card.get("job_choice") == "update" and not intent and not resource_name:
            resource_name = card["job_change"]["resource_name"]
        if not intent and not card.get("resource_name") and card.get("error_kind") == "unknown_outcome" and contact_backend.backend_name() == "apple":
            selected = apple_contacts.container_id() or apple_contacts.run_helper("containers").get("defaultIdentifier", "")
            if not selected:
                raise ContactSaveError("저장할 Mac 연락처 계정을 확인하지 못했습니다.", kind="invalid")
            intent_checkpoint({"op": "create", "backend": "apple", "container": selected,
                               "target_identifier": None, "fingerprint": apple_contacts.fingerprint(draft),
                               "identifiers": [], "generation": 1, "legacy_untracked": True})
            raise apple_contacts.ConfirmationRequired()
        if intent or (
                (resource_name or "").startswith("apple:") or (
                not resource_name and contact_backend.backend_name() == "apple")):
            kwargs = {"save_intent": intent, "intent_checkpoint": intent_checkpoint}
            if new_operation:
                kwargs["new_operation"] = True
        if not intent and card.get("job_choice") == "update":
            kwargs.update(job_change=True, target_container=card["job_change"].get("container"))
        elif not intent and card.get("job_choice") == "new":
            kwargs.update(force_create=True, save_intent=intent, intent_checkpoint=intent_checkpoint)
            if new_operation:
                kwargs["new_operation"] = True
        result: SaveResult = save_contact(draft, card["images"].get("profile"), resource_name=resource_name, checkpoint=checkpoint, **kwargs)
    except apple_contacts.ConfirmationRequired as exc:
        card.update(status="needs_confirmation", last_error=str(exc), error_kind="unknown_outcome",
                    next_retry_at=None, confirmation_id=secrets.token_hex(8))
        _persist_save_fields(card, "status", "last_error", "error_kind", "next_retry_at", "confirmation_id")
        return {"ok": False, "needs_confirmation": True, "message": str(exc)}
    except ContactSaveError as exc:
        return _save_failed(card, str(exc), getattr(exc, "kind", "other"), getattr(exc, "retryable", False))
    except cards.StaleCardState:
        raise  # stale-safe boundary reports refusal without replacing newer state
    except Exception as exc:  # unexpected adapter failure: keep the card, retry later
        log.exception("save failed card=%s", card["id"])
        return _save_failed(card, f"예상치 못한 오류: {str(exc)[:120]}", "other", True)

    card["status"] = "saved"
    card["resource_name"] = result.resource_name or card.get("resource_name")
    card["save_action"] = card.get("save_action") or result.action
    card["photo_status"] = result.photo
    card["last_error"] = None
    card["error_kind"] = None
    card["next_retry_at"] = None
    card["save_warnings"] = list(result.warnings)
    cards.schedule_followup(card)
    _persist_save_fields(card, "status", "resource_name", "save_action", "photo_status",
                         "last_error", "error_kind", "next_retry_at", "save_warnings",
                         "followup_saved_at", "followup_state", "followup_due_at", "followup_nonce",
                         event="saved", detail=result.summary)
    try:
        from . import meeting_note
        meeting_note.enqueue(card)
        _persist_save_fields(card, "note", "caption")
    except Exception:
        log.warning("meeting note enqueue failed; contact remains saved")
    # A crash during cleanup must leave a durable terminal state, never saving.
    keep = {"profile"} if result.photo == "failed" else set()
    cleanup_card_images(card, keep=keep)
    _persist_save_fields(card, "images")
    archive_saved(card, result.summary)
    log.info("saved card=%s %s photo=%s", card["id"], card["resource_name"], result.photo)
    return {"ok": True, "result": result, "message": result.summary, "warnings": list(result.warnings), "photo": result.photo}


@stale_safe("outcome")
def resolve_followup(card_id: str, chat_id, choice: str, nonce: str) -> dict:
    with card_lock(card_id, blocking=False) as acquired:
        if not acquired:
            return {"ok": False, "busy": True, "message": BUSY_MESSAGE}
        return _resolve_followup_locked(card_id, chat_id, choice, nonce)


def _resolve_followup_locked(card_id: str, chat_id, choice: str, nonce: str) -> dict:
    with cards.followup_lock:
        card = cards.load_card(card_id)
        if (not card or card.get("chat_id") != str(chat_id) or card.get("status") != "saved"
                or card.get("followup_state") not in ("notified", "sending")
                or not nonce or card.get("followup_nonce") != nonce):
            return {"ok": False, "stale": True, "message": "이미 처리된 알림입니다."}
        if choice not in ("sent", "later", "stop", "off"):
            return {"ok": False, "message": "최근 알림의 버튼을 눌러주세요."}
        state = cards.initialize_followup()
        if not state.get("enabled", True) or not cards.followup_days():
            return {"ok": False, "stale": True, "message": "알림이 꺼져 있습니다."}
        card["followup_nonce"] = None
        card["followup_due_at"] = None
        if choice == "later":
            card.update(followup_state="pending", followup_due_at=cards.followup_due(time.time(), 3))
            message = "3일 뒤 다시 알려드리겠습니다."
        elif choice == "off":
            state["enabled"] = False
            cards.save_state("followup", state)
            card["followup_state"] = "disabled"
            message = "모든 명함의 후속 알림을 껐습니다."
        else:
            card["followup_state"] = "done" if choice == "sent" else "stopped"
            message = "연락 완료로 기록했습니다." if choice == "sent" else "이 명함의 알림을 그만 보냅니다."
        cards.save_card(card)
        return {"ok": True, "message": message}


@stale_safe("token")
def confirmation_token(card: dict) -> str:
    """Migrate old confirmation cards under the same lock used by consumption."""
    with card_lock(card["id"], blocking=False) as acquired:
        if not acquired:
            return ""
        fresh = cards.load_card(card["id"]) or card
        if fresh.get("status") != "needs_confirmation":
            return ""
        if not fresh.get("confirmation_id"):
            fresh["confirmation_id"] = secrets.token_hex(8)
            cards.save_card(fresh)
        card.update(fresh)
        return fresh["confirmation_id"]


@stale_safe("outcome")
def resolve_confirmation(card: dict, choice: str, confirmation_id: str) -> dict:
    with card_lock(card["id"], blocking=False) as acquired:
        if not acquired:
            return {"ok": False, "busy": True, "message": BUSY_MESSAGE}
        fresh = cards.load_card(card["id"])
        if (not fresh or fresh.get("status") != "needs_confirmation" or not confirmation_id
                or fresh.get("confirmation_id") != confirmation_id):
            return {"ok": False, "stale": True, "message": "이미 처리됨"}
        if choice not in ("again", "saved", "skip"):
            return {"ok": False, "message": "확인 버튼을 다시 선택하세요."}
        card.clear()
        card.update(fresh)
        # Consume durably before a lookup, write, or slow external operation.
        # A crash after consumption leaves the old operation for read-only recovery.
        card.update(confirmation_id=None, save_resolution=choice, next_retry_at=None)
        if choice == "again":
            card.update(status="saving", error_kind=None, last_error=None)
            cards.add_history(card, "confirmed_recreate")
            cards.save_card(card)
            return _save(card, new_operation=True)
        card.update(status="saved" if choice == "saved" else "cancelled",
                    photo_status="skipped", last_error=None, error_kind=None)
        if choice == "saved":
            cards.schedule_followup(card)
        cards.add_history(card, "confirmed_" + choice)
        cards.save_card(card)
        cleanup_card_images(card)
        cards.save_card(card)
        return {"ok": True, "message": "이미 저장됨으로 확인했습니다." if choice == "saved" else "이 명함을 건너뛰었습니다."}


def _save_failed(card: dict, message: str, kind: str, retryable: bool) -> dict:
    card["status"] = "save_failed"
    card["last_error"] = message
    card["error_kind"] = kind
    if kind == "invalid":
        card["next_retry_at"] = None
    elif kind == "auth":
        card["next_retry_at"] = time.time() + AUTH_RETRY_SECONDS
    else:
        delay = min(RETRY_BASE_SECONDS * (2 ** max(0, int(card.get("save_attempts") or 1) - 1)), RETRY_MAX_SECONDS)
        card["next_retry_at"] = time.time() + delay
    _persist_save_fields(card, "status", "last_error", "error_kind", "next_retry_at",
                         event="save_failed", detail=f"{kind}: {message}")
    log.warning("save failed card=%s kind=%s: %s", card["id"], kind, message)
    return {"ok": False, "kind": kind, "retryable": kind != "invalid", "message": message}


@stale_safe("scan")
def retry_due(force: bool = False, on_result: Optional[Callable[[dict, dict], None]] = None) -> tuple[int, list[str]]:
    """Retry approved saves whose backoff elapsed, and photos that failed to upload."""
    saved, failures = 0, []
    for card in cards.list_cards(statuses=["save_failed"]):
        if card.get("approved_revision") is None or card.get("approved_revision") != card.get("revision"):
            continue  # never save a revision nobody approved
        if card.get("error_kind") == "invalid":
            continue
        if not force and (card.get("next_retry_at") or 0) > time.time():
            continue
        with card_lock(card["id"], blocking=False) as acquired:
            if not acquired:
                continue
            fresh = cards.load_card(card["id"]) or card
            if fresh.get("status") != "save_failed" or fresh.get("approved_revision") != fresh.get("revision"):
                continue  # saved or edited meanwhile
            card = fresh
            outcome = _save(card)
        if outcome["ok"]:
            saved += 1
        else:
            failures.append(f"{cards.label(card)} {current_draft(card).display_name}: {outcome['message']}")
        if on_result:
            on_result(card, outcome)
    for card in cards.list_cards(statuses=["saved"]):
        if card.get("photo_status") != "failed" or not card.get("resource_name"):
            continue
        with card_lock(card["id"], blocking=False) as acquired:
            if not acquired:
                continue
            card = cards.load_card(card["id"])
            if not card or card.get("status") != "saved" or card.get("photo_status") != "failed" or not card.get("resource_name"):
                continue
            path = card["images"].get("profile")
            if not path or not Path(path).exists():
                card["photo_status"] = "missing"
                _persist_save_fields(card, "photo_status")
                continue
            try:
                if card["resource_name"].startswith("apple:"):
                    card["resource_name"] = refresh_resource_name(card["resource_name"], current_draft(card), card.get("save_intent"))
                    _persist_save_fields(card, "resource_name")
                if card.get("save_intent", {}).get("backend") == "apple":
                    upload_photo(card["resource_name"], path, save_intent=card["save_intent"])
                else:
                    upload_photo(card["resource_name"], path)
            except ContactSaveError as exc:
                log.warning("photo retry failed card=%s: %s", card["id"], exc)
                continue
            card["photo_status"] = "uploaded"
            _persist_save_fields(card, "photo_status")
            cleanup_card_images(card)
            _persist_save_fields(card, "images")
    return saved, failures


def pending_retry_count() -> int:
    return sum(1 for c in cards.list_cards(statuses=["save_failed"]) if c.get("approved_revision") == c.get("revision") and c.get("error_kind") != "invalid")


@stale_safe("bool")
def cancel(card: dict, *, blocking: bool = True, defer_processing: bool = False) -> bool:
    with card_lock(card["id"], blocking=blocking) as acquired:
        if not acquired:
            fresh = cards.load_card(card["id"])
            if fresh and fresh.get("status") == "processing" and not unresolved_save(fresh):
                request_cancel(card)
            return False
        if not _refresh_for_change(card, allow_processing=True):
            return False
        if defer_processing and card.get("status") == "processing":
            request_cancel(card)
            return False
        return _cancel_locked(card)


def finish_cancel_request(card_id: str) -> None:
    with card_lock(card_id):
        fresh = cards.load_card(card_id)
        if fresh and not fresh.get("recognition_id") and cancel_path(card_id).exists():
            _cancel_locked(fresh)


def _cancel_locked(card: dict) -> bool:
    if card.get("status") not in (*cards.REVIEW_STATUSES, "processing") or unresolved_save(card):
        return False
    card.update(status="cancelled", awaiting_back=False)
    card.pop("recognition_id", None)
    card.pop("cancel_requested", None)
    cards.add_history(card, "cancelled")
    cards.save_card(card)
    cleanup_card_images(card)
    cards.save_card(card)
    cancel_path(card["id"]).unlink(missing_ok=True)
    return True


def archive_saved(card: dict, summary: str) -> None:
    try:
        SESSION_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archived = dict(card, save_output=summary, archived_at=cards.now())
        (SESSION_ARCHIVE_DIR / f"card-{card['id']}.json").write_text(json.dumps(archived, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.exception("archive failed card=%s", card["id"])


def cleanup_card_images(card: dict, keep: set = frozenset()) -> None:
    for key in IMAGE_KEYS:
        if key in keep:
            continue
        path = card["images"].get(key)
        if path:
            cleanup_paths(path)
            card["images"][key] = None


def cleanup_paths(*items) -> None:
    for item in items:
        if isinstance(item, PreparedImages):
            paths = [item.profile_path, item.ocr_path, item.llm_path, item.rectified_path]
        else:
            paths = [item]
        for path in paths:
            if not path:
                continue
            try:
                Path(path).unlink()
            except OSError:
                pass


def draft_text(card: dict, *, show_cost: bool = False) -> str:
    draft = current_draft(card)
    header = f"{cards.label(card)} 명함 초안"
    text = draft.to_text(header=header)
    cost = card.get("recognition_cost_krw")
    threshold = _cost_warn_threshold()
    if show_cost and cost is not None and cost > threshold:
        text += f"\n참고: 이 사진의 AI 비용 {cost:.2f}원(기준 {threshold:g}원 초과)"
    old = card.get("job_change")
    if old:
        text += f"\n기존 연락처: {old['company']} {old['title']} → 새 명함: {draft.company} {draft.title}"
    return text


# ---------------------------------------------------------------- legacy migration

@stale_safe("notes")
def migrate_legacy() -> list[str]:
    """Turn old per-chat sessions and pending_contacts files into cards (idempotent).

    - pending entries already saved later (a success archive with the same front
      photo exists) are retired, which stops the daily 'image missing' failure;
    - entries archived only because a newer photo arrived become review cards and
      are never saved without approval;
    - approved-but-failed entries become save_failed cards that retry.
    """
    notes: list[str] = []
    saved_fronts = _archived_front_images()
    seen_fronts: set[str] = set()
    migrated_dir = PENDING_CONTACT_DIR / "migrated"
    for path in sorted(PENDING_CONTACT_DIR.glob("*.json")):
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        chat_id = path.name.split("-", 1)[0]
        front = legacy.get("front_image_path") or ""
        if front and front in saved_fronts:
            notes.append(f"{path.name}: 이미 저장된 연락처라 재시도 목록에서 정리")
        elif front and front in seen_fronts:
            notes.append(f"{path.name}: 중복 기록 정리")
        else:
            approved = legacy.get("status") == "approved" and legacy.get("pending_reason") != "superseded_by_new_front_photo"
            card = _card_from_legacy(chat_id, legacy, "save_failed" if approved else "review")
            if approved:
                card["approved_revision"] = card["revision"]
                card["last_error"] = "이전 저장 실패분 (로그인/서버 오류)"
                card["error_kind"] = "auth" if "credential" in str(legacy.get("pending_reason", "")).lower() else "transient"
                card["next_retry_at"] = time.time()
            cards.save_card(card)
            notes.append(f"{path.name}: {cards.label(card)} {'자동 재시도 대상으로' if approved else '검토 대기로'} 이전")
        if front:
            seen_fronts.add(front)
        migrated_dir.mkdir(parents=True, exist_ok=True)
        path.rename(migrated_dir / path.name)
    for path in sorted(SESSION_DIR.glob("*.json")) if SESSION_DIR.exists() else []:
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        front = legacy.get("front_image_path") or ""
        if legacy.get("draft") and not (front and (front in saved_fronts or front in seen_fronts)):
            card = _card_from_legacy(path.stem, legacy, "review")
            cards.save_card(card)
            notes.append(f"세션 {path.name}: {cards.label(card)} 검토 대기로 이전")
        migrated = SESSION_DIR / "migrated"
        migrated.mkdir(parents=True, exist_ok=True)
        path.rename(migrated / path.name)
    for note in notes:
        log.info("legacy migration: %s", note)
    return notes


def _card_from_legacy(chat_id: str, legacy: dict, status: str) -> dict:
    card = cards.new_card(chat_id, source="legacy")
    card["images"] = {short: legacy.get(long) for long, short in LEGACY_IMAGE_KEYS.items() if legacy.get(long)}
    card["front_draft"] = legacy.get("front_draft")
    card["back_draft"] = legacy.get("back_draft")
    set_draft(card, CardDraft.from_session_value(legacy.get("draft")), "migrated")
    card["status"] = status
    return card


def _archived_front_images() -> set[str]:
    fronts = set()
    for path in SESSION_ARCHIVE_DIR.glob("*.json") if SESSION_ARCHIVE_DIR.exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        front = data.get("front_image_path") or (data.get("images") or {}).get("front")
        if front and str(data.get("save_output", "")).strip():
            fronts.add(front)
    return fronts
