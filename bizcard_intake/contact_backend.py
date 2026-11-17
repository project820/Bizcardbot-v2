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
                 save_intent=None, intent_checkpoint=None, new_operation=False):
    if save_intent and save_intent.get("backend") == "apple":
        backend = apple_contacts
    else:
        backend = _backend(resource_name)
    if backend is apple_contacts:
        return backend.save_contact(draft, profile_image_path, resource_name=resource_name,
                                    checkpoint=checkpoint, save_intent=save_intent,
                                    intent_checkpoint=intent_checkpoint, new_operation=new_operation)
    return backend.save_contact(draft, profile_image_path,
                                               resource_name=resource_name, checkpoint=checkpoint)


def upload_photo(resource_name, image_path, save_intent=None):
    if save_intent and save_intent.get("backend") == "apple":
        return apple_contacts.upload_photo(resource_name, image_path, container=save_intent["container"])
    return _backend(resource_name).upload_photo(resource_name, image_path)


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
