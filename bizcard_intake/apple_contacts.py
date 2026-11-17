"""macOS Contacts adapter. Helper JSON uses the existing People-shaped mapping.

Contact data travels over stdin, never command arguments or logs. A mutation's
uncertain outcome is not retried as a transient failure without checking first.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from contextlib import contextmanager

from . import contacts
from .contacts import ContactSaveError, SaveResult
from .draft import CardDraft

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apple_contacts.swift"
BINARY = ROOT / ".cache" / "apple_contacts"
HELPER_TIMEOUT = 30
AUTH_MESSAGE = "Mac 연락처 접근을 허용하세요. 시스템 설정 > 개인정보 보호 및 보안 > 연락처에서 봇의 Python을 켜세요."
UNKNOWN_MESSAGE = "Mac 연락처 저장 응답을 확인하지 못했습니다. 연락처 앱에서 저장 여부를 확인하세요."
NOTE_WARNING = "Mac 연락처는 메모·검증 경고를 저장하지 않습니다. 명함 초안에서 확인하세요."
_build_lock = threading.Lock()


def ensure_helper() -> Path:
    """Build once per source hash, with the Vision helper's atomic cache pattern."""
    with _build_lock:
        try:
            digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
            stamp = BINARY.with_suffix(".sha256")
            if BINARY.is_file() and stamp.is_file() and stamp.read_text() == digest:
                return BINARY
            BINARY.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="._ensure_apple_contacts.", dir=BINARY.parent) as folder:
                temporary = Path(folder) / BINARY.name
                result = subprocess.run(["swiftc", str(SOURCE), "-O", "-o", str(temporary)],
                                        capture_output=True, text=True, timeout=30, check=False)
                if result.returncode or not temporary.is_file():
                    raise ContactSaveError("Mac 연락처 도우미 빌드에 실패했습니다. swiftc와 Command Line Tools를 확인하세요.")
                os.replace(temporary, BINARY)
                temporary_stamp = Path(folder) / "source.sha256"
                temporary_stamp.write_text(digest)
                os.replace(temporary_stamp, stamp)
            return BINARY
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContactSaveError("Mac 연락처 도우미를 준비하지 못했습니다. xcode-select --install로 빌드 도구를 확인하세요.") from exc


def run_helper(command: str, *, _timeout=None, **payload) -> dict:
    binary = ensure_helper()
    uncertain = command in ("create", "update")
    try:
        # subprocess.run kills AND waits for the child on timeout before raising.
        # Recovery scans must never overlap a still-running mutation helper.
        completed = subprocess.run([str(binary)], input=json.dumps({"command": command, **payload}, ensure_ascii=False),
                                   capture_output=True, text=True, check=False, timeout=HELPER_TIMEOUT if _timeout is None else _timeout)
    except subprocess.TimeoutExpired as exc:
        if uncertain:
            raise ContactSaveError(UNKNOWN_MESSAGE, kind="unknown_outcome") from exc
        raise ContactSaveError("Mac 연락처 응답 시간이 초과되었습니다. 잠시 후 다시 시도하세요.",
                               kind="transient", retryable=True) from exc
    except OSError as exc:
        raise ContactSaveError("Mac 연락처 도우미를 실행할 수 없습니다.", kind="other") from exc
    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("object required")
    except (ValueError, TypeError) as exc:
        raise ContactSaveError(UNKNOWN_MESSAGE if uncertain else "Mac 연락처 응답 형식이 올바르지 않습니다.",
                               kind="unknown_outcome" if uncertain else "other") from exc
    error = result.get("error")
    if isinstance(error, dict):
        kind = error.get("kind", "other")
        # Native errors can contain real contact fields; only authored messages cross this boundary.
        messages = {"auth": AUTH_MESSAGE, "invalid": "연락처 계정 또는 입력값을 확인하세요. setup-contacts로 계정을 다시 선택하세요.",
                    "not_found": "Mac 연락처를 찾을 수 없습니다.", "transient": "Mac 연락처 일시 오류입니다. 잠시 후 다시 시도하세요.",
                    "other": "Mac 연락처 작업에 실패했습니다."}
        raise ContactSaveError(messages.get(kind, messages["other"]), kind=kind,
                               retryable=kind == "transient")
    if completed.returncode:
        raise ContactSaveError(UNKNOWN_MESSAGE if uncertain else "Mac 연락처 작업에 실패했습니다.",
                               kind="unknown_outcome" if uncertain else "other")
    return result


def container_id() -> str:
    return os.environ.get("BIZCARD_APPLE_CONTAINER", "").strip()


def build_person(draft: CardDraft) -> dict:
    person = contacts.build_person(draft)
    person.pop("biographies", None)  # CNContactNoteKey requires Apple's restricted entitlement.
    return person


def build_update(existing: dict, draft: CardDraft) -> dict:
    """Monotone, idempotent merge: preserve scalars and union list fields.

    The native update also enforces this against its fresh fetch, so a concurrent
    Contacts edit between Python's read and the write cannot be overwritten.
    """
    new = build_person(draft)
    person = copy.deepcopy(existing)
    person.pop("biographies", None)
    if not any(v for name in existing.get("names", []) for v in name.values()):
        person["names"] = new.get("names", [])
    old_org = (existing.get("organizations") or [{}])[0]
    new_org = (new.get("organizations") or [{}])[0]
    merged = {**new_org, **{k: v for k, v in old_org.items() if v}}
    if merged:
        person["organizations"] = [merged]
    for key in ("phoneNumbers", "emailAddresses", "urls", "addresses"):
        items = person.setdefault(key, [])
        seen = {contacts._dedupe_key(key, item) for item in items}
        for item in new.get(key, []):
            value = contacts._dedupe_key(key, item)
            if value not in seen:
                items.append(item)
                seen.add(value)
        if not items:
            person.pop(key, None)
    return person


def _identifier(resource_name: str) -> str:
    if not resource_name.startswith("apple:") or not resource_name[6:]:
        raise ContactSaveError("Mac 연락처 식별자가 올바르지 않습니다.", kind="invalid")
    return resource_name[6:]


def _person(payload: dict) -> dict:
    person = payload.get("person", {})
    if not isinstance(person, dict):
        raise ContactSaveError(UNKNOWN_MESSAGE, kind="unknown_outcome")
    identifier = payload.get("identifier")
    if identifier:
        person["resourceName"] = f"apple:{identifier}"
    return person


def get_contact(resource_name: str, person_fields: str = "") -> dict:
    return _person(run_helper("get", identifier=_identifier(resource_name)))


def create_contact(person: dict) -> dict:
    saved = _person(run_helper("create", container=container_id(), person=person))
    if not saved.get("resourceName"):
        raise ContactSaveError(UNKNOWN_MESSAGE, kind="unknown_outcome")
    return saved


def update_contact(existing: dict, draft: CardDraft) -> dict:
    return _person(run_helper("update", identifier=_identifier(existing["resourceName"]),
                              person=build_update(existing, draft)))


class ConfirmationRequired(ContactSaveError):
    def __init__(self):
        super().__init__("저장 여부를 하나로 확인하지 못했습니다. 아래 버튼으로 선택하세요.")


def fingerprint(draft: CardDraft) -> dict:
    return {"name": " ".join(draft.name.split()),
            "displayName": " ".join(draft.display_name.split()),
            "phones": sorted({contacts.normalize_phone(v) for v in contacts.draft_phones(draft)} - {""}),
            "emails": sorted({v.strip().lower() for v in draft.email} - {""})}


def find_fingerprint(value: dict, container: str) -> set[str]:
    result = run_helper("find-fingerprint", container=container, **value)
    return set(result.get("identifiers", []))


def reconcile_intent(intent: dict) -> str:
    """Only positive evidence resolves a create; absence never authorizes a write."""
    value = intent["fingerprint"]
    if not (value.get("phones") or value.get("emails")):
        raise ConfirmationRequired()
    before = set(intent["identifiers"])
    current = find_fingerprint(value, intent["container"])
    new = current - before
    if before.issubset(current) and len(new) == 1:
        return "apple:" + next(iter(new))
    raise ConfirmationRequired()


def find_job_change_candidate(draft: CardDraft) -> dict | None:
    if not draft.name.strip() or not (draft.mobile or draft.email):
        return None
    selected = container_id() or run_helper("containers", _timeout=3).get("defaultIdentifier", "")
    if not selected:
        return None
    result = run_helper("find-job-change", _timeout=3, container=selected, name=draft.name,
                        displayName=draft.display_name,
                        phones=sorted({contacts.normalize_phone(v) for v in draft.mobile} - {""}),
                        emails=sorted({v.strip().lower() for v in draft.email} - {""}))
    identifiers = list(dict.fromkeys(result.get("identifiers", [])))
    if len(identifiers) != 1:
        return None
    person = contacts.select_job_change_candidate(draft, [_person(run_helper("get", _timeout=3, identifier=identifiers[0], container=selected))])
    if person:
        person["_container"] = selected
    return person


def build_job_change_update(existing: dict, draft: CardDraft) -> dict:
    person = build_update(existing, draft)
    person["names"] = build_person(draft)["names"]
    organization = dict((existing.get("organizations") or [{}])[0])
    if draft.company.strip():
        organization["name"] = draft.company
    if draft.title.strip():
        organization["title"] = draft.title
    person["organizations"] = [organization]
    return person


def find_existing_candidates(draft: CardDraft, container: str | None = None) -> tuple[dict | None, int]:
    phones = sorted({contacts.normalize_phone(value) for value in (draft.mobile or draft.work)} - {""})
    if not phones or not draft.name.strip():
        return None, 0
    result = run_helper("find", container=container if container is not None else container_id(), phones=phones, name=draft.name,
                        displayName=draft.display_name)
    identifiers = list(dict.fromkeys(result.get("identifiers", [])))
    count = len(identifiers)
    return (get_contact(f"apple:{identifiers[0]}") if count == 1 else None), count


def find_existing_contact(draft: CardDraft) -> dict | None:
    return find_existing_candidates(draft)[0]


@contextmanager
def contact_snapshot():
    # Native store reads are synchronous; do not cache and miss another worker's new contact.
    yield


def upload_photo(resource_name: str, image_path: str, container=None) -> None:
    path = Path(image_path)
    if not path.is_file():
        raise ContactSaveError("명함 사진 파일이 없습니다.", kind="invalid")
    run_helper("set-photo", identifier=_identifier(resource_name), imagePath=str(path.resolve()),
               **({"container": container} if container is not None else {}))


def verify_contact(resource_name: str, draft: CardDraft) -> list[str]:
    try:
        person = get_contact(resource_name)
    except ContactSaveError as exc:
        return [f"저장 확인 실패: {exc}"]
    warnings = []
    if (person.get("names") or [{}])[0].get("givenName") != draft.display_name:
        warnings.append("저장 확인: 이름이 명함 초안과 다르게 저장됨")
    stored = {contacts.normalize_phone(p.get("value", "")) for p in person.get("phoneNumbers", [])}
    if any(contacts.normalize_phone(value) not in stored for value in contacts.draft_phones(draft)):
        warnings.append("저장 확인: 전화번호 누락")
    return warnings


def _attach_photo(result: SaveResult, image_path: str | None, draft=None, checkpoint=None, operation=None) -> None:
    if not image_path:
        return
    if not Path(image_path).is_file():
        result.photo = "missing"
        result.warnings.append("명함 사진 파일이 없어 사진 없이 저장함")
        return
    try:
        try:
            upload_photo(result.resource_name, image_path, container=operation["container"]) if operation else upload_photo(result.resource_name, image_path)
        except ContactSaveError as exc:
            if exc.kind != "not_found" or draft is None or (operation and operation.get("replaceOrganization")):
                raise
            # CardDAV can replace a newly created local identifier during its first sync.
            if operation:
                matches = find_fingerprint(operation["fingerprint"], operation["container"])
                count = len(matches)
                existing = _pinned_get(next(iter(matches)), operation["container"]) if count == 1 else None
            else:
                existing, count = find_existing_candidates(draft)
            if count != 1 or not existing or existing["resourceName"] == result.resource_name:
                raise
            result.resource_name = existing["resourceName"]
            if checkpoint:
                try:
                    checkpoint(result.resource_name, result.action)
                except Exception:
                    result.warnings.append("연락처 식별자 변경 기록에 실패했습니다.")
            upload_photo(result.resource_name, image_path, container=operation["container"]) if operation else upload_photo(result.resource_name, image_path)
    except ContactSaveError as exc:
        if exc.kind == "not_found" and operation and operation.get("replaceOrganization"):
            raise ConfirmationRequired() from exc
        result.photo = "failed"
        result.warnings.append(f"사진 업로드 실패: {exc}")
        return
    result.photo = "uploaded"


def _pinned_get(identifier: str, container: str) -> dict:
    return _person(run_helper("get", identifier=identifier, container=container))


def _update_target(intent: dict) -> dict:
    """Job changes stay pinned; only legacy updates allow CardDAV rebinding."""
    try:
        return _pinned_get(intent["target_identifier"], intent["container"])
    except ContactSaveError as exc:
        if exc.kind != "not_found":
            raise
    if intent.get("replaceOrganization"):
        raise ConfirmationRequired()
    matches = find_fingerprint(intent["fingerprint"], intent["container"])
    if len(matches) != 1:
        raise ConfirmationRequired()
    return _pinned_get(next(iter(matches)), intent["container"])


def _save_operation(draft, resource_name, intent, persist, new_operation=False, force_create=False,
                    job_change=False, target_container=None):
    """The persisted record is a write-ahead boundary, not a retry permission.

    A process dying immediately after persist is indistinguishable from a lost
    response. Create recovery is therefore always read-only. Only a consumed
    confirmation may replace the original record and enter the write branch.
    """
    selected = intent["container"] if intent else (target_container if job_change else container_id())
    if not selected:
        selected = run_helper("containers").get("defaultIdentifier", "")
        if not selected:
            raise ContactSaveError("저장할 Mac 연락처 계정을 확인하지 못했습니다.", kind="invalid")
    warnings = []
    if intent and not new_operation:
        if intent.get("legacy_untracked") or fingerprint(draft) != intent.get("fingerprint"):
            raise ConfirmationRequired()
        op = intent.get("op")
        if op not in ("create", "update"):
            # Pre-release intents did not distinguish updates from creates.
            raise ConfirmationRequired()
        if resource_name:
            if intent.get("replaceOrganization"):
                _update_target(intent)
            # A successful write was already checkpointed before a process exit.
            return resource_name, "created" if op == "create" else "updated", warnings, intent
        if op == "create":
            return reconcile_intent(intent), "created", warnings, intent
        existing = _update_target(intent)
    else:
        if intent:
            op = intent.get("op", "update" if resource_name else "create")
            target = intent.get("target_identifier") or (_identifier(resource_name) if resource_name else None)
        elif resource_name:
            op, target = "update", _identifier(resource_name)
        elif force_create:
            op, target = "create", None
        else:
            existing, count = find_existing_candidates(draft, selected)
            op = "update" if existing else "create"
            target = _identifier(existing["resourceName"]) if existing else None
            if count > 1:
                warnings.append(f"중복 후보 {count}건: 새 연락처로 저장함")
        value = fingerprint(draft)
        next_intent = {"op": op, "backend": "apple", "container": selected,
                       "target_identifier": target, "fingerprint": value,
                       "identifiers": [] if job_change or (intent or {}).get("replaceOrganization") else sorted(find_fingerprint(value, selected)),
                       "generation": int((intent or {}).get("generation", 0)) + 1}
        if job_change or (intent or {}).get("replaceOrganization"):
            next_intent["replaceOrganization"] = True
        if persist is None:
            raise ContactSaveError("저장 작업 기록을 보존할 수 없습니다.", kind="invalid")
        persist(next_intent)  # fsync + atomic replace, before every external mutation.
        intent = next_intent
        if op == "create":
            saved = _person(run_helper("create", container=selected, person=build_person(draft)))
            if not saved.get("resourceName"):
                raise ContactSaveError(UNKNOWN_MESSAGE, kind="unknown_outcome")
            return saved["resourceName"], "created", warnings, intent
        existing = _update_target(intent)
    try:
        saved = _person(run_helper("update", container=selected,
                        identifier=_identifier(existing["resourceName"]),
                        person=build_job_change_update(existing, draft) if intent.get("replaceOrganization") else build_update(existing, draft),
                        unionOnly=True, **({"replaceOrganization": True} if intent.get("replaceOrganization") else {})))
    except ContactSaveError as exc:
        if exc.kind == "not_found" and intent.get("replaceOrganization"):
            raise ConfirmationRequired() from exc
        raise
    if not saved.get("resourceName"):
        raise ContactSaveError(UNKNOWN_MESSAGE, kind="unknown_outcome")
    return saved["resourceName"], "updated", warnings, intent


def save_contact(draft: CardDraft, profile_image_path: str | None, resource_name: str | None = None,
                 checkpoint=None, save_intent=None, intent_checkpoint=None, new_operation=False,
                 force_create=False, job_change=False, target_container=None) -> SaveResult:
    if not draft.is_savable:
        raise ContactSaveError("이름 또는 전화번호가 없어 저장하지 않았습니다.", kind="invalid")
    if job_change and not (resource_name or (save_intent or {}).get("target_identifier")):
        raise ConfirmationRequired()
    if (force_create or job_change) and not (intent_checkpoint or save_intent):
        raise ContactSaveError("저장 작업 기록을 보존할 수 없습니다.", kind="invalid")
    if intent_checkpoint or save_intent:
        saved_name, action, warnings, operation = _save_operation(
            draft, resource_name, save_intent, intent_checkpoint, new_operation, force_create,
            job_change, target_container)
    else:
        # Standalone adapter callers have no per-card state. The bot always uses
        # the write-ahead path above; keep the public low-level adapter usable.
        operation = None
        warnings = []
        if resource_name:
            try:
                existing = get_contact(resource_name)
            except ContactSaveError as exc:
                if exc.kind != "not_found":
                    raise
                raise ConfirmationRequired() from exc
        else:
            existing, count = find_existing_candidates(draft)
            if count > 1:
                warnings.append(f"중복 후보 {count}건: 새 연락처로 저장함")
        if existing:
            update_contact(existing, draft)
            saved_name, action = existing["resourceName"], "updated"
        else:
            saved_name, action = create_contact(build_person(draft))["resourceName"], "created"
    if checkpoint:
        try:
            checkpoint(saved_name, action)
        except Exception:
            warnings.append("저장 기록 실패: 연락처 앱에서 저장 여부를 확인하세요.")
    if contacts._biography(draft):
        warnings.append(NOTE_WARNING)
    result = SaveResult(action, saved_name, warnings=warnings, display_name=draft.display_name)
    if operation:
        _attach_photo(result, profile_image_path, draft, checkpoint, operation)
    else:
        _attach_photo(result, profile_image_path, draft, checkpoint)
    result.warnings.extend(verify_contact(result.resource_name, draft))
    return result
