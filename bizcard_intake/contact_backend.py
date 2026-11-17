"""Select a contacts destination; persisted identifiers keep retries on their backend."""
import os

from . import apple_contacts, contacts
from .contacts import ContactSaveError, SaveResult


def backend_name() -> str:
    value = os.environ.get("BIZCARD_CONTACTS_BACKEND", "google").strip().lower() or "google"
    if value not in ("google", "apple"):
        raise ContactSaveError("BIZCARD_CONTACTS_BACKEND는 google 또는 apple이어야 합니다.", kind="invalid")
    return value


def _backend(resource_name=None):
    if resource_name:
        if resource_name.startswith("apple:"):
            return apple_contacts
        if resource_name.startswith("people/"):
            return contacts
        raise ContactSaveError("연락처 식별자가 올바르지 않습니다.", kind="invalid")
    return apple_contacts if backend_name() == "apple" else contacts


def save_contact(draft, profile_image_path, resource_name=None, checkpoint=None,
                 save_intent=None, intent_checkpoint=None, new_operation=False,
                 force_create=False, job_change=False, target_container=None):
    if save_intent:
        backend = apple_contacts if save_intent.get("backend") == "apple" else contacts
    else:
        backend = _backend(resource_name)
    if backend is apple_contacts:
        return backend.save_contact(draft, profile_image_path, resource_name=resource_name,
                                    checkpoint=checkpoint, save_intent=save_intent,
                                    intent_checkpoint=intent_checkpoint, new_operation=new_operation,
                                    **({"force_create": True} if force_create else {}),
                                    **({"job_change": True, "target_container": target_container} if job_change else {}))
    if job_change or force_create or (save_intent and save_intent.get("op") == "create"):
        return _save_selected_google(draft, profile_image_path, resource_name, checkpoint,
                                     save_intent, intent_checkpoint, new_operation, force_create)
    return backend.save_contact(draft, profile_image_path,
                                               resource_name=resource_name, checkpoint=checkpoint)


def _save_selected_google(draft, image, resource_name, checkpoint, intent, persist,
                          new_operation, force_create):
    """Explicit decisions never run the legacy automatic duplicate matcher."""
    if intent and intent.get("op") == "create":
        if not resource_name and not new_operation:
            raise apple_contacts.ConfirmationRequired()
        force_create = True
    if not draft.is_savable:
        raise ContactSaveError("이름 또는 전화번호가 없어 저장하지 않았습니다.", kind="invalid")
    if resource_name:
        try:
            existing = contacts.get_contact(resource_name)
            contacts.update_contact(existing, draft)
        except ContactSaveError as exc:
            if exc.kind == "not_found":
                raise apple_contacts.ConfirmationRequired() from exc
            raise
        action, saved_name = "updated", resource_name
    elif force_create:
        if intent and not new_operation:
            raise apple_contacts.ConfirmationRequired()
        if persist is None:
            raise ContactSaveError("저장 작업 기록을 보존할 수 없습니다.", kind="invalid")
        persist({"backend": "google", "op": "create", "generation": int((intent or {}).get("generation", 0)) + 1})
        saved = contacts.create_contact(contacts.build_person(draft))
        saved_name = saved.get("resourceName") if isinstance(saved, dict) else None
        if not saved_name:
            raise apple_contacts.ConfirmationRequired()
        action = "created"
    else:
        raise ContactSaveError("갱신할 연락처를 확인하지 못했습니다.", kind="invalid")
    result = SaveResult(action, saved_name, display_name=draft.display_name)
    if checkpoint:
        try:
            checkpoint(saved_name, action)
        except Exception:
            result.warnings.append("저장 기록 실패: 연락처 앱에서 저장 여부를 확인하세요.")
    contacts._attach_photo(result, image)
    result.warnings.extend(contacts.verify_contact(saved_name, draft))
    return result


def upload_photo(resource_name, image_path, save_intent=None):
    if save_intent and save_intent.get("backend") == "apple":
        return apple_contacts.upload_photo(resource_name, image_path, container=save_intent["container"])
    return _backend(resource_name).upload_photo(resource_name, image_path)


def find_job_change_candidate(draft):
    return _backend().find_job_change_candidate(draft)


def contact_snapshot():
    return _backend().contact_snapshot()


def destination_name():
    if backend_name() == "google":
        return "Google 연락처"
    try:
        payload = apple_contacts.run_helper("containers")
        selected = apple_contacts.container_id() or payload.get("defaultIdentifier")
        container = next((c for c in payload.get("containers", []) if c["id"] == selected), None)
        if container:
            return f"Mac 연락처({container['name']})"
    except ContactSaveError:
        pass
    return "Mac 연락처"


def refresh_resource_name(resource_name, draft, save_intent=None):
    """Recover an Apple CardDAV identifier that changed during initial sync, without creating."""
    if not resource_name.startswith("apple:"):
        return resource_name
    if save_intent:
        try:
            return apple_contacts._pinned_get(apple_contacts._identifier(resource_name), save_intent["container"])["resourceName"]
        except ContactSaveError as exc:
            if exc.kind != "not_found":
                raise
        if save_intent.get("replaceOrganization"):
            raise apple_contacts.ConfirmationRequired()
        matches = apple_contacts.find_fingerprint(save_intent["fingerprint"], save_intent["container"])
        if len(matches) == 1:
            return "apple:" + next(iter(matches))
        raise ContactSaveError("사진을 저장할 Mac 연락처를 하나로 확인하지 못했습니다.", kind="not_found")
    try:
        return apple_contacts.get_contact(resource_name)["resourceName"]
    except ContactSaveError as exc:
        if exc.kind != "not_found":
            raise
    existing, count = apple_contacts.find_existing_candidates(draft)
    if count == 1 and existing:
        return existing["resourceName"]
    raise ContactSaveError("사진을 저장할 Mac 연락처를 하나로 확인하지 못했습니다.", kind="not_found")
