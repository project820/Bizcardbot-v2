"""Apple adapter tests never access the real address book."""
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from bizcard_intake import cards, cli, contacts, doctor, telegram
from bizcard_intake.draft import CardDraft
from bizcard_intake.envfile import read_settings


def draft(**extra):
    return CardDraft.from_dict(dict(name="테스트", title="담당", company="Example",
                                   mobile=["010-0000-0000"], **extra))


class AppleTests(unittest.TestCase):
    def apple(self):
        self.assertIsNotNone(importlib.util.find_spec("bizcard_intake.apple_contacts"),
                             "Apple Contacts adapter must exist")
        return importlib.import_module("bizcard_intake.apple_contacts")

    def test_mapping_uses_given_name_labels_and_omits_biography(self):
        apple = self.apple()
        person = apple.build_person(draft(work=["02-000-0000"], fax=["02-000-0001"],
                                          email=["test@example.com"], website=["https://example.com"],
                                          address=["Example street"], notes="사용자 메모"))
        self.assertEqual(person["names"], [{"givenName": "테스트 담당 Example"}])
        self.assertEqual([p["type"] for p in person["phoneNumbers"]], ["mobile", "work", "workFax"])
        self.assertEqual(person["addresses"][0]["formattedValue"], "Example street")
        self.assertNotIn("biographies", person)

    def test_update_preserves_old_lists_and_organization_if_empty(self):
        apple = self.apple()
        old = {"resourceName": "apple:id", "phoneNumbers": [{"value": "+82 10-0000-0000"}],
               "emailAddresses": [{"value": "old@example.com"}],
               "organizations": [{"name": "Original", "title": "Role", "department": "Dept"}]}
        body = apple.build_update(old, CardDraft(name="테스트", mobile=["010-0000-0000"],
                                                email=["new@example.com"]))
        self.assertEqual(len(body["phoneNumbers"]), 1)
        self.assertEqual(len(body["emailAddresses"]), 2)
        self.assertEqual(body["organizations"], old["organizations"])

    def test_find_targets_mobile_before_work_and_container(self):
        apple = self.apple()
        with patch.dict(os.environ, {"BIZCARD_APPLE_CONTAINER": "chosen"}), patch.object(
            apple, "run_helper", return_value={"identifiers": ["id"], "count": 1}
        ) as run, patch.object(apple, "get_contact", return_value={"resourceName": "apple:id"}):
            existing, count = apple.find_existing_candidates(draft(work=["02-000-0000"]))
        self.assertEqual(existing["resourceName"], "apple:id")
        self.assertEqual(count, 1)
        self.assertEqual(run.call_args.kwargs["phones"], ["01000000000"])
        self.assertEqual(run.call_args.kwargs["container"], "chosen")

    def test_multiple_candidates_create_with_warning_and_checkpoint_before_photo(self):
        apple = self.apple()
        events = []
        with patch.object(apple, "find_existing_candidates", return_value=(None, 2)), patch.object(
            apple, "create_contact", return_value={"resourceName": "apple:new"}
        ), patch.object(apple, "verify_contact", return_value=[]), patch.object(
            apple, "_attach_photo", side_effect=lambda *args: events.append("photo")
        ):
            result = apple.save_contact(draft(), None, checkpoint=lambda *args: events.append("checkpoint"))
        self.assertEqual(result.action, "created")
        self.assertIn("중복 후보 2건", result.warnings[0])
        self.assertEqual(events, ["checkpoint", "photo"])

    def test_existing_identifier_does_not_create_or_duplicate_note_guidance(self):
        apple = self.apple()
        with patch.object(apple, "get_contact", return_value={"resourceName": "apple:id"}), patch.object(
            apple, "update_contact"
        ) as update, patch.object(apple, "create_contact") as create, patch.object(
            apple, "verify_contact", return_value=[]
        ):
            result = apple.save_contact(draft(notes="보존해야 할 메모"), None, resource_name="apple:id")
        update.assert_called_once()
        create.assert_not_called()
        self.assertNotIn(apple.NOTE_WARNING, result.warnings)

    def test_helper_errors_and_timeout_are_safe(self):
        apple = self.apple()
        for kind in ("auth", "invalid", "not_found", "transient", "other"):
            payload = {"error": {"kind": kind, "message": "safe", "retryable": kind == "transient"}}
            with patch.object(apple, "ensure_helper", return_value=Path("helper")), patch.object(
                apple.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, json.dumps(payload), "private")
            ), self.assertRaises(contacts.ContactSaveError) as caught:
                apple.run_helper("get", identifier="id")
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn("private", str(caught.exception))
        with patch.object(apple, "ensure_helper", return_value=Path("helper")), patch.object(
            apple.subprocess, "run", side_effect=subprocess.TimeoutExpired("helper", 30)
        ):
            with self.assertRaises(contacts.ContactSaveError) as caught:
                apple.run_helper("create", person={})
            self.assertEqual(caught.exception.kind, "unknown_outcome")
            with self.assertRaises(contacts.ContactSaveError) as caught:
                apple.run_helper("get", identifier="id")
            self.assertEqual(caught.exception.kind, "transient")

    def test_bad_mutation_response_is_unknown_outcome(self):
        apple = self.apple()
        with patch.object(apple, "ensure_helper", return_value=Path("helper")), patch.object(
            apple.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "not-json", "")
        ), self.assertRaises(contacts.ContactSaveError) as caught:
            apple.run_helper("create", person={})
        self.assertEqual(caught.exception.kind, "unknown_outcome")

    def test_dispatch_defaults_google_and_routes_persisted_identifiers(self):
        self.assertIsNotNone(importlib.util.find_spec("bizcard_intake.contact_backend"), "dispatcher must exist")
        backend = importlib.import_module("bizcard_intake.contact_backend")
        apple = self.apple()
        with patch.dict(os.environ, {}, clear=True), patch.object(contacts, "save_contact", return_value="google"):
            self.assertEqual(backend.save_contact(draft(), None), "google")
        with patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "apple"}), patch.object(
            apple, "save_contact", return_value="apple"
        ), patch.object(contacts, "upload_photo") as photo:
            self.assertEqual(backend.save_contact(draft(), None), "apple")
            backend.upload_photo("people/previous", "photo.jpg")
        photo.assert_called_once_with("people/previous", "photo.jpg")
        with patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "typo"}), self.assertRaises(contacts.ContactSaveError):
            backend.save_contact(draft(), None)

    @patch.object(telegram, "setup_logging", lambda: None)
    def test_setup_apple_saves_only_selected_settings_safely(self):
        apple = self.apple()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            env = root / ".env"
            env.write_text("KEEP=literal\n# retained\n")
            with patch.object(cli, "PROJECT_ROOT", root), patch("builtins.input", side_effect=["1", "1"]), patch.object(
                apple, "run_helper", side_effect=[{"status": "authorized"},
                {"containers": [{"id": "google-id", "name": "Google", "type": 3}], "defaultIdentifier": "google-id"}]
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["setup-contacts"]), 0)
            settings = read_settings(env)
            self.assertEqual(settings["BIZCARD_CONTACTS_BACKEND"], "apple")
            self.assertEqual(settings["BIZCARD_APPLE_CONTAINER"], "google-id")
            self.assertEqual(settings["KEEP"], "literal")
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    @patch.object(telegram, "setup_logging", lambda: None)
    def test_setup_google_does_not_request_apple_permission(self):
        apple = self.apple()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), patch.object(
            cli, "PROJECT_ROOT", Path(folder)
        ), patch("builtins.input", return_value="2"), patch.object(apple, "run_helper") as helper, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["setup-contacts"]), 0)
            self.assertEqual(read_settings(Path(folder) / ".env")["BIZCARD_CONTACTS_BACKEND"], "google")
        helper.assert_not_called()

    def test_doctor_apple_skips_gws_and_checks_container(self):
        apple = self.apple()
        with patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "apple", "BIZCARD_APPLE_CONTAINER": "chosen"}), patch.object(
            apple, "run_helper", side_effect=[{"status": "authorized"}, {"containers": [{"id": "chosen", "name": "Google"}]}]
        ), patch.object(doctor, "check_gws", side_effect=AssertionError("gws forbidden")):
            check = doctor.run_checks(["gws"])[0]
        self.assertTrue(check.ok)
        self.assertEqual(check.name, "apple")
        for status in ("denied", "notDetermined", "restricted", "limited"):
            with patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "apple"}), patch.object(
                apple, "run_helper", return_value={"status": status}
            ):
                self.assertFalse(doctor.run_checks(["gws"])[0].ok)
        with patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "apple", "BIZCARD_APPLE_CONTAINER": "missing"}), patch.object(
            apple, "run_helper", side_effect=[{"status": "authorized"}, {"containers": []}]
        ):
            self.assertFalse(doctor.run_checks(["gws"])[0].ok)

    def test_startup_permission_request_and_denied_alert_fallback(self):
        apple = self.apple()
        with tempfile.TemporaryDirectory() as folder, patch.object(cards, "CARD_DIR", Path(folder)), patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "apple", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}, clear=True), patch.object(
            apple, "run_helper", side_effect=[{"status": "notDetermined"}, {"status": "denied"}]
        ) as run, patch.object(telegram, "send_message") as send:
            telegram.initialize_contacts("token")
        self.assertEqual([call.args[0] for call in run.call_args_list], ["status", "request"])
        self.assertEqual(send.call_args.args[:2], ("token", 123))
        self.assertIn("연락처", send.call_args.args[2])


class AppleRecoveryTests(unittest.TestCase):
    apple = AppleTests.apple
    # Keep these regression cases separate from real Contacts access.
    def test_changed_identifier_during_photo_rebinds_before_retry(self):
        apple = self.apple()
        events = []
        with tempfile.NamedTemporaryFile(suffix=".jpg") as photo, patch.object(
            apple, "get_contact", return_value={"resourceName": "apple:old"}
        ), patch.object(apple, "update_contact"), patch.object(
            apple, "upload_photo", side_effect=[contacts.ContactSaveError("gone", kind="not_found"), None]
        ) as upload, patch.object(apple, "find_existing_candidates", return_value=({"resourceName": "apple:new"}, 1)), patch.object(
            apple, "verify_contact", return_value=[]
        ):
            result = apple.save_contact(draft(), photo.name, resource_name="apple:old", checkpoint=lambda *args: events.append(args))
        self.assertEqual(result.resource_name, "apple:new")
        self.assertEqual(result.photo, "uploaded")
        self.assertEqual(events, [("apple:old", "updated"), ("apple:new", "updated")])
        self.assertEqual(upload.call_args.args[0], "apple:new")

    def test_helper_cache_rebuilds_by_hash_and_never_uses_contacts(self):
        apple = self.apple()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"helper.swift"
            binary = Path(folder)/".cache/helper"
            source.write_text("version-one")
            def compile_helper(args, **kwargs):
                Path(args[-1]).write_bytes(b"compiled")
                return subprocess.CompletedProcess(args, 0, "", "")
            with patch.object(apple, "SOURCE", source), patch.object(apple, "BINARY", binary), patch.object(
                apple.subprocess, "run", side_effect=compile_helper
            ) as compile_mock:
                apple.ensure_helper()
                apple.ensure_helper()
                self.assertEqual(compile_mock.call_count, 1)
                source.write_text("version-two")
                apple.ensure_helper()
                self.assertEqual(compile_mock.call_count, 2)

    def test_safe_settings_writer_preserves_comments_and_literal_container_id(self):
        from bizcard_intake import onboarding
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/".env"
            path.write_text("KEEP='a # b'\nBIZCARD_APPLE_CONTAINER=old # selected\n")
            value = "account ' space $(literal)"
            onboarding.save_settings(path, {"BIZCARD_APPLE_CONTAINER": value})
            self.assertEqual(read_settings(path)["BIZCARD_APPLE_CONTAINER"], value)
            self.assertIn("# selected", path.read_text())

    def test_setup_icloud_explains_google_and_preserves_process_override(self):
        apple = self.apple()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "google"}), patch.object(
            cli, "PROJECT_ROOT", Path(folder)
        ), patch("builtins.input", side_effect=["1", "1"]), patch.object(
            apple, "run_helper", side_effect=[{"status": "authorized"}, {"containers": [{"id": "icloud", "name": "iCloud", "type": 3}]}]
        ), redirect_stdout(output):
            self.assertEqual(cli.cmd_setup_contacts(), 0)
            self.assertEqual(os.environ["BIZCARD_CONTACTS_BACKEND"], "google")
        self.assertIn("인터넷 계정", output.getvalue())
        self.assertIn("환경 변수", output.getvalue())

    def test_setup_calls_destination_choice_only_when_no_backend_selected(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), patch.object(
            cli, "PROJECT_ROOT", Path(folder)
        ), patch.object(cli.doctor, "run_checks", return_value=[]), redirect_stdout(io.StringIO()):
            (Path(folder)/".env").write_text("KEEP=value\n")
            with patch.object(cli, "cmd_setup_contacts", return_value=0) as setup:
                self.assertEqual(cli.cmd_setup(), 0)
                setup.assert_called_once()
            with patch.dict(os.environ, {"BIZCARD_CONTACTS_BACKEND": "google"}), patch.object(cli, "cmd_setup_contacts") as setup:
                self.assertEqual(cli.cmd_setup(), 0)
                setup.assert_not_called()
