"""Installer integration tests: all HTTP traffic stays on 127.0.0.1."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from bizcard_intake import cli

TOKEN = "123456:TOP_SECRET_BOT_TOKEN"
PAIRING_ID = "p" * 22
POLL_TOKEN = "s" * 43
CHALLENGE = "명함-4821abcdef12"


class FakeService:
    def __init__(self):
        self.polls = [(200, {"status": "created"})]
        self.claims = [(200, {"status": "ready", "bot_token": TOKEN, "bot_id": 123456,
                            "bot_username": "fixture_bot", "owner_user_id": 456,
                            "access_restricted": True})]
        self.calls = []
        self.invalid_token = False
        self.webhook = ""
        self.updates = [{"update_id": 1, "message": {"chat": {"id": 789, "type": "private"},
                                                   "from": {"id": 789, "is_bot": False},
                                                   "date": int(time.time()) + 1, "text": CHALLENGE}}]
        self.cosmetics_fail = False
        self.create_error = False
        self.malformed_http = False
        self.redirect = False

    def respond(self, method, path, headers, data):
        self.calls.append((method, path, dict(headers), data))
        if path == "/v1/pairings":
            if self.create_error:
                return 503, {"error": POLL_TOKEN}
            return 201, {"poll_token": POLL_TOKEN,
                         "suggested_username": "bizcard_abcdef_bot",
                         "deep_link": f"https://t.me/BizcardSetupBot?start={PAIRING_ID}",
                         "expires_at": time.time() + 600}
        if path == "/v1/pairings/claim":
            return self.claims.pop(0) if len(self.claims) > 1 else self.claims[0]
        if path == "/v1/pairings/ack":
            return 200, {"status": "claimed"}
        if path == "/v1/pairings/status":
            return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        operation = path.rsplit("/", 1)[-1]
        if operation == "getMe":
            if self.invalid_token:
                return 401, {"ok": False, "description": TOKEN}
            return 200, {"ok": True, "result": {"id": 123456, "is_bot": True,
                                                "username": "fixture_bot"}}
        if operation == "getWebhookInfo":
            return 200, {"ok": True, "result": {"url": self.webhook}}
        if operation == "getUpdates":
            return 200, {"ok": True, "result": self.updates}
        if self.cosmetics_fail:
            return 400, {"ok": False, "description": TOKEN}
        return 200, {"ok": True, "result": True}


@contextlib.contextmanager
def service_server(service):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.handle_request()

        def do_GET(self):
            self.handle_request()

        def handle_request(self):
            if service.malformed_http and self.path.endswith("getMe"):
                self.connection.sendall((TOKEN + "\r\n\r\n").encode())
                self.close_connection = True
                return
            if service.redirect and self.path == "/v1/pairings":
                self.send_response(302)
                self.send_header("Location", "/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else None
            status, payload = service.respond(self.command, self.path, self.headers, data)
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("bizcard_intake.onboarding"),
                             "setup-telegram installer module is missing")
        from bizcard_intake import onboarding
        self.onboarding = onboarding
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = Path(self.temp.name) / ".env"
        self.output = io.StringIO()
        self.service = FakeService()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        clipboard = patch.object(onboarding.subprocess, "run", side_effect=OSError)
        clipboard.start()
        self.addCleanup(clipboard.stop)
        challenge = patch("secrets.token_hex", return_value="4821abcdef12")
        challenge.start()
        self.addCleanup(challenge.stop)

    def run_setup(self, answers=("", "123456"), **kwargs):
        values = iter(answers)
        with service_server(self.service) as base:
            options = dict(env_path=self.env, relay_url=base, telegram_api_base=base,
                           allow_insecure_localhost=True, no_open=True,
                           input_fn=lambda prompt: next(values), secret_input=lambda prompt: TOKEN,
                           output=self.output, sleep=lambda delay: None)
            options.update(kwargs)
            return self.onboarding.setup_telegram(**options)

    def test_auto_creates_private_env_and_never_prints_credentials(self):
        self.service.polls.insert(0, (200, {"status": "started"}))
        self.assertEqual(self.run_setup(), 0)
        text = self.env.read_text()
        self.assertIn(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", text)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=456\n", text)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn(POLL_TOKEN, self.output.getvalue())
        self.assertIn("@fixture_bot", self.output.getvalue())
        self.assertEqual(self.service.calls[0][3], {"bot_name": "명함비서"})
        poll = next(c for c in self.service.calls if c[1] == "/v1/pairings/status")
        self.assertEqual(poll[2]["Authorization"], f"Bearer {POLL_TOKEN}")
        commands = next(c[3] for c in self.service.calls if c[1].endswith("setMyCommands"))
        self.assertEqual(commands["commands"], [{"command": "list", "description": "저장 안 된 명함"},
                                               {"command": "help", "description": "사용법"}])

    def test_v2_posts_status_claim_and_ack_after_verified_env_save(self):
        original = self.service.respond
        def respond(method, path, headers, data):
            if path == "/v1/pairings/ack":
                self.assertIn(TOKEN, self.env.read_text())
            return original(method, path, headers, data)
        self.service.respond = respond
        self.assertEqual(self.run_setup(), 0)
        calls = self.service.calls
        paths = [call[1] for call in calls]
        self.assertLess(paths.index("/v1/pairings/claim"), next(i for i, p in enumerate(paths) if p.endswith("getMe")))
        self.assertLess(next(i for i, p in enumerate(paths) if p.endswith("getMe")), paths.index("/v1/pairings/ack"))
        for method, path, headers, body in calls:
            if path in ("/v1/pairings/status", "/v1/pairings/claim", "/v1/pairings/ack"):
                self.assertEqual(method, "POST")
                self.assertEqual(headers["Authorization"], f"Bearer {POLL_TOKEN}")
                self.assertEqual(body, {"code": "123456"} if path.endswith("claim") else {})
        for notice in ("@BizcardSetupBot", "토큰을 다시 조회", "토큰을 저장하지 않습니다",
                       "명함 사진과 Google 계정에 접근하지 않습니다", "--manual"):
            self.assertIn(notice, self.output.getvalue())
        self.assertNotIn("다시 발급", self.output.getvalue())

    def test_wrong_claim_code_reprompts_and_reports_remaining_attempts(self):
        self.service.claims.insert(0, (403, {"status": "bad_code", "attempts_left": 4}))
        self.assertEqual(self.run_setup(answers=("", "000000", "123456")), 0)
        self.assertIn("4", self.output.getvalue())
        self.assertEqual([c[3] for c in self.service.calls if c[1].endswith("claim")],
                         [{"code": "000000"}, {"code": "123456"}])

    def test_invalid_code_format_is_not_submitted(self):
        self.assertEqual(self.run_setup(answers=("", "bad", "123456")), 0)
        self.assertEqual(len([c for c in self.service.calls if c[1].endswith("claim")]), 1)

    def test_no_ack_when_bot_validation_or_atomic_save_fails(self):
        self.service.invalid_token = True
        self.assertEqual(self.run_setup(), 1)
        self.assertFalse(any(c[1].endswith("ack") for c in self.service.calls))
        self.service.invalid_token = False
        with patch.object(self.onboarding.os, "replace", side_effect=OSError):
            self.assertEqual(self.run_setup(), 1)
        self.assertFalse(any(c[1].endswith("ack") for c in self.service.calls))

    def test_changed_bot_connection_is_not_overwritten_or_acknowledged(self):
        original = self.service.respond
        saved = "TELEGRAM_BOT_TOKEN=987654:OTHER_INSTALLER\nTELEGRAM_ALLOWED_CHAT_IDS=999\n"

        def respond(method, path, headers, data):
            if path.endswith("getMe"):
                self.env.write_text(saved)
            return original(method, path, headers, data)

        self.service.respond = respond
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(self.env.read_text(), saved)
        self.assertFalse(any(c[1].endswith("ack") for c in self.service.calls))
        self.assertIn("다른 설치", self.output.getvalue())

    def test_polling_uses_three_seconds(self):
        self.service.polls.insert(0, (200, {"status": "started"}))
        delays = []
        self.assertEqual(self.run_setup(sleep=delays.append), 0)
        self.assertEqual(delays, [3])

    def test_manual_rejects_old_code_and_unrelated_private_messages_across_pages(self):
        old = {"update_id": 100, "message": {"date": 1, "text": CHALLENGE,
               "chat": {"type": "private", "id": 900}, "from": {"id": 900, "is_bot": False}}}
        unrelated = {"update_id": 101, "message": {"date": int(time.time()) + 1, "text": "hello",
               "chat": {"type": "private", "id": 901}, "from": {"id": 901, "is_bot": False}}}
        good = self.service.updates[0]
        original = self.service.respond
        def respond(method, path, headers, data):
            if path.endswith("getUpdates"):
                original(method, path, headers, data)
                return 200, {"ok": True, "result": [dict(old, update_id=n) for n in range(1, 100)] + [unrelated]
                             if data.get("offset") is None else [dict(good, update_id=102)]}
            return original(method, path, headers, data)
        self.service.respond = respond
        self.assertEqual(self.run_setup(manual=True), 0)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=789", self.env.read_text())
        polls = [c[3] for c in self.service.calls if c[1].endswith("getUpdates")]
        self.assertEqual(polls[1]["offset"], 102)
        self.assertIn(CHALLENGE, self.output.getvalue())

    def test_macos_clipboard_token_requires_username_confirmation_without_exposure(self):
        with patch.object(self.onboarding.sys, "platform", "darwin"), patch.object(
            self.onboarding.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": TOKEN})()
        ) as clipboard:
            prompts = []
            def answer(prompt):
                prompts.append(prompt)
                return "y" if "클립보드" in prompt else ""
            self.assertEqual(self.run_setup(manual=True, input_fn=answer,
                             secret_input=lambda _: self.fail("accepted clipboard must skip token prompt")), 0)
        self.assertEqual(clipboard.call_args.args[0], ["pbpaste"])
        self.assertTrue(any("@fixture_bot" in prompt and "클립보드" in prompt for prompt in prompts))
        self.assertNotIn(TOKEN, self.output.getvalue() + "".join(prompts))

    def test_declined_clipboard_token_uses_hidden_prompt(self):
        with patch.object(self.onboarding.sys, "platform", "darwin"), patch.object(
            self.onboarding.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": TOKEN})()
        ):
            hidden = []
            self.assertEqual(self.run_setup(answers=("", "n"), manual=True,
                secret_input=lambda prompt: hidden.append(prompt) or TOKEN), 0)
        self.assertEqual(len(hidden), 1)

    def test_claim_response_loss_retries_same_code_before_saving(self):
        original = self.onboarding._request_json
        lost = [False]
        def request(url, **kwargs):
            if url.endswith("/claim") and not lost[0]:
                original(url, **kwargs)
                lost[0] = True
                raise self.onboarding.SetupError("connection lost")
            return original(url, **kwargs)
        with patch.object(self.onboarding, "_request_json", side_effect=request):
            self.assertEqual(self.run_setup(), 0)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=456", self.env.read_text())
        claims = [c[3] for c in self.service.calls if c[1].endswith("claim")]
        self.assertEqual(claims, [{"code": "123456"}, {"code": "123456"}])

    def test_ack_response_loss_retries_after_save_and_accepts_claimed_tombstone(self):
        original = self.service.respond
        attempts = [0]
        def respond(method, path, headers, data):
            if path.endswith("/ack"):
                self.assertTrue(self.env.exists())
                original(method, path, headers, data)
                attempts[0] += 1
                if attempts[0] == 1:
                    return 503, {"error": "unavailable"}
                return 410, {"status": "claimed"}
            return original(method, path, headers, data)
        self.service.respond = respond
        delays = []
        self.assertEqual(self.run_setup(sleep=delays.append), 0)
        self.assertEqual(attempts[0], 2)
        self.assertEqual(delays, [3])

    def test_ack_failure_preserves_saved_credentials_and_reports_partial_completion(self):
        original = self.service.respond
        def respond(method, path, headers, data):
            result = original(method, path, headers, data)
            return (503, {"error": TOKEN}) if path.endswith("/ack") else result
        self.service.respond = respond
        self.assertEqual(self.run_setup(), 1)
        self.assertIn(TOKEN, self.env.read_text())
        self.assertIn(".env는 저장", self.output.getvalue())
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertEqual(len([c for c in self.service.calls if c[1].endswith("ack")]), 3)

    def test_exhausted_claim_falls_back_without_ack(self):
        self.service.claims = [(410, {"status": "expired"})]
        self.assertEqual(self.run_setup(), 0)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=789", self.env.read_text())
        self.assertFalse(any(c[1].endswith("ack") for c in self.service.calls))

    def test_manual_ignores_matching_code_in_group_or_from_wrong_sender(self):
        good = self.service.updates[0]
        wrong_chat = dict(good["message"], chat={"id": 789, "type": "group"})
        wrong_sender = dict(good["message"], **{"from": {"id": 999, "is_bot": False}})
        self.service.updates = [{"update_id": 1, "message": wrong_chat},
                                {"update_id": 2, "message": wrong_sender}]
        now = [0.0]
        self.assertEqual(self.run_setup(manual=True, clock=lambda: now[0],
                         sleep=lambda delay: now.__setitem__(0, now[0] + delay)), 1)
        self.assertFalse(self.env.exists())

    def test_manual_getupdates_failure_does_not_allow_unverified_chat_id(self):
        original = self.service.respond
        def respond(method, path, headers, data):
            if path.endswith("getUpdates"):
                return 409, {"ok": False, "description": TOKEN}
            return original(method, path, headers, data)
        self.service.respond = respond
        self.assertEqual(self.run_setup(answers=("", "456"), manual=True), 1)
        self.assertFalse(self.env.exists())
        self.assertNotIn(TOKEN, self.output.getvalue())

    def test_invalid_clipboard_contents_fall_back_to_hidden_prompt(self):
        with patch.object(self.onboarding.sys, "platform", "darwin"), patch.object(
            self.onboarding.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": "ordinary text"})()
        ):
            hidden = []
            self.assertEqual(self.run_setup(manual=True,
                secret_input=lambda prompt: hidden.append(prompt) or TOKEN), 0)
        self.assertEqual(len(hidden), 1)

    def test_existing_lines_and_comments_survive_and_chat_ids_are_unioned(self):
        original = '# keep\r\nOTHER="a # b"\r\nexport TELEGRAM_BOT_TOKEN="111:old" # old token\r\nTELEGRAM_ALLOWED_CHAT_IDS="123,456" # friends\r\nTAIL=value'
        self.env.write_bytes(original.encode())
        self.assertEqual(self.run_setup(answers=("새 명함봇", "y", "123456")), 0)
        actual = self.env.read_bytes().decode()
        self.assertIn('# keep\r\nOTHER="a # b"\r\n', actual)
        self.assertIn(' # old token\r\n', actual)
        self.assertIn('TELEGRAM_ALLOWED_CHAT_IDS=123,456 # friends\r\n', actual)
        self.assertTrue(actual.endswith("TAIL=value"))
        self.assertNotIn("111:old", actual)

    def test_existing_token_requires_confirmation_and_decline_preserves_file(self):
        self.env.write_text("TELEGRAM_BOT_TOKEN=111:old\n")
        self.assertEqual(self.run_setup(answers=("", "n")), 1)
        self.assertEqual(self.env.read_text(), "TELEGRAM_BOT_TOKEN=111:old\n")
        self.assertEqual(self.service.calls, [])

    def test_yes_skips_replacement_prompt(self):
        self.env.write_text("TELEGRAM_BOT_TOKEN=111:old\nTELEGRAM_ALLOWED_CHAT_IDS=123\n")
        self.assertEqual(self.run_setup(yes=True), 0)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=123,456", self.env.read_text())

    def test_expired_pairing_falls_back_to_botfather(self):
        self.service.polls = [(410, {"status": "expired"})]
        self.assertEqual(self.run_setup(), 0)
        self.assertIn("만료", self.output.getvalue())
        self.assertIn("BotFather", self.output.getvalue())
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=789", self.env.read_text())

    def test_claimed_pairing_falls_back_without_retrying_claim(self):
        self.service.polls = [(410, {"status": "claimed"})]
        self.assertEqual(self.run_setup(), 0)
        self.assertIn("이미 수령", self.output.getvalue())
        self.assertEqual(len([c for c in self.service.calls if c[1] == "/v1/pairings/status"]), 1)

    def test_bad_getme_token_is_rejected_without_writing_or_disclosure(self):
        self.service.invalid_token = True
        self.assertEqual(self.run_setup(), 1)
        self.assertFalse(self.env.exists())
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn(POLL_TOKEN, self.output.getvalue())

    def test_manual_updates_discover_private_chat_without_deleting_webhook(self):
        self.service.updates.insert(0, {"message": {"chat": {"id": -100, "type": "group"}}})
        self.assertEqual(self.run_setup(manual=True), 0)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=789", self.env.read_text())
        self.assertFalse(any("pairings" in c[1] or "deleteWebhook" in c[1] for c in self.service.calls))

    def test_existing_webhook_refuses_unverified_chat_id(self):
        self.service.webhook = "https://webhook.example/"
        self.assertEqual(self.run_setup(answers=("", "456"), manual=True), 1)
        self.assertFalse(self.env.exists())
        self.assertIn("webhook", self.output.getvalue())
        self.assertFalse(any("getUpdates" in c[1] or "deleteWebhook" in c[1] for c in self.service.calls))

    def test_https_is_required_before_any_request(self):
        self.assertEqual(self.run_setup(allow_insecure_localhost=False), 1)
        self.assertEqual(self.service.calls, [])
        self.assertIn("HTTPS", self.output.getvalue())
        for url in ("http://example.com", "http://localhost", "https://user:pass@example.com", "https://example.com/#x"):
            with self.subTest(url=url), self.assertRaises(self.onboarding.SetupError):
                self.onboarding.validate_base_url(url, allow_insecure_localhost=True)

    def test_empty_relay_setting_enters_manual_without_network(self):
        with patch.dict(os.environ, {"BIZCARD_ONBOARDING_URL": ""}):
            self.assertEqual(self.run_setup(relay_url=None), 0)
        self.assertIn("수동", self.output.getvalue())
        self.assertFalse(any("pairings" in c[1] for c in self.service.calls))

    def test_dotenv_empty_relay_enters_manual_without_pairing(self):
        self.env.write_text('export BIZCARD_ONBOARDING_URL="" # manual\n')
        with patch.object(self.onboarding, "_pair", side_effect=self.onboarding.RelayError("fallback")) as pair:
            self.assertEqual(self.run_setup(relay_url=None), 0)
        pair.assert_not_called()
        self.assertIn("수동", self.output.getvalue())

    def test_dotenv_relay_uses_safe_settings_parser(self):
        self.env.write_text('export BIZCARD_ONBOARDING_URL="https://private.example/relay" # private\n')
        with patch.object(self.onboarding, "_pair", side_effect=self.onboarding.RelayError("fallback")) as pair:
            self.assertEqual(self.run_setup(relay_url=None), 0)
        self.assertEqual(pair.call_args.args[0], "https://private.example/relay")

    def test_process_relay_overrides_dotenv_including_empty_value(self):
        for value in ("https://process.example", ""):
            with self.subTest(value=value):
                self.env.write_text("BIZCARD_ONBOARDING_URL=https://private.example\n")
                with patch.dict(os.environ, {"BIZCARD_ONBOARDING_URL": value}), patch.object(
                    self.onboarding, "_pair", side_effect=self.onboarding.RelayError("fallback")
                ) as pair:
                    self.assertEqual(self.run_setup(relay_url=None), 0)
                if value:
                    self.assertEqual(pair.call_args.args[0], value)
                else:
                    pair.assert_not_called()

    def test_explicit_relay_overrides_process_and_dotenv(self):
        self.env.write_text("BIZCARD_ONBOARDING_URL=https://private.example\n")
        with patch.dict(os.environ, {"BIZCARD_ONBOARDING_URL": ""}):
            self.assertEqual(self.run_setup(), 0)
        self.assertTrue(any(c[1] == "/v1/pairings" for c in self.service.calls))

    def test_invalid_dotenv_relay_fails_without_echoing_values(self):
        self.env.write_text(f'BIZCARD_ONBOARDING_URL="{TOKEN}\n')
        with patch.object(self.onboarding, "_pair", side_effect=self.onboarding.RelayError("fallback")) as pair:
            self.assertEqual(self.run_setup(relay_url=None), 1)
        pair.assert_not_called()
        self.assertNotIn(TOKEN, self.output.getvalue())

    def test_missing_process_and_dotenv_relay_uses_default(self):
        for text in (None, "# no relay setting\n"):
            with self.subTest(text=text):
                if text is not None:
                    self.env.write_text(text)
                with patch.object(self.onboarding, "_pair", side_effect=self.onboarding.RelayError("fallback")) as pair:
                    self.assertEqual(self.run_setup(relay_url=None, yes=True), 0)
                self.assertEqual(pair.call_args.args[0], self.onboarding.DEFAULT_ONBOARDING_URL)

    def test_cosmetic_failures_warn_but_preserve_success_and_hide_errors(self):
        self.service.cosmetics_fail = True
        self.assertEqual(self.run_setup(), 0)
        self.assertTrue(self.env.exists())
        self.assertIn("경고", self.output.getvalue())
        self.assertNotIn(TOKEN, self.output.getvalue())

    def test_existing_semicolon_chat_ids_are_merged(self):
        self.env.write_text("TELEGRAM_ALLOWED_CHAT_IDS=123;456\n")
        self.assertEqual(self.run_setup(), 0)
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=123,456", self.env.read_text())

    def test_relay_failure_falls_back_without_disclosing_server_error(self):
        self.service.create_error = True
        self.assertEqual(self.run_setup(), 0)
        self.assertIn("BotFather", self.output.getvalue())
        self.assertNotIn(POLL_TOKEN, self.output.getvalue())

    def test_pairing_timeout_enters_manual_without_waiting_ten_real_minutes(self):
        self.service.polls = [(200, {"status": "pending"})]
        now = [0.0]
        def sleep(delay):
            now[0] += delay
        self.assertEqual(self.run_setup(clock=lambda: now[0], sleep=sleep), 0)
        self.assertLessEqual(now[0], 600)
        self.assertIn("만료", self.output.getvalue())

    def test_manual_timeout_refuses_unverified_chat_id_after_120_seconds(self):
        self.service.updates = []
        now = [0.0]
        def sleep(delay):
            now[0] += delay
        self.assertEqual(self.run_setup(answers=("", "456"), manual=True,
                                        clock=lambda: now[0], sleep=sleep), 1)
        self.assertEqual(now[0], 120)
        self.assertFalse(self.env.exists())

    def test_malformed_and_nonowner_updates_are_ignored(self):
        self.service.updates = [{"message": None}, {"message": {"chat": None}},
                                {"message": {"chat": {"type": "private", "id": 900},
                                             "from": {"id": 789, "is_bot": False},
                                                   "date": int(time.time()) + 1, "text": CHALLENGE}}]
        now = [0.0]
        self.assertEqual(self.run_setup(answers=("", "456"), manual=True,
                                        clock=lambda: now[0],
                                        sleep=lambda delay: now.__setitem__(0, now[0] + delay)), 1)
        self.assertFalse(self.env.exists())

    def test_existing_env_is_unchanged_if_atomic_replace_fails(self):
        self.env.write_text("TELEGRAM_ALLOWED_CHAT_IDS=123\n# preserved\n")
        original = self.env.read_bytes()
        with patch.object(self.onboarding.os, "replace", side_effect=OSError):
            self.assertEqual(self.run_setup(), 1)
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(list(self.env.parent.glob(".bizcard-env-*")), [])

    def test_hidden_token_input_refuses_getpass_echo_fallback(self):
        import warnings
        def unsafe_getpass(prompt):
            warnings.warn("Password input may be echoed.", self.onboarding.getpass.GetPassWarning)
            return TOKEN
        with patch.object(self.onboarding.getpass, "getpass", unsafe_getpass):
            self.assertEqual(self.run_setup(manual=True, secret_input=None), 1)
        self.assertFalse(self.env.exists())
        self.assertEqual(self.service.calls, [])

    def test_malformed_http_does_not_leak_raw_status_line(self):
        self.service.malformed_http = True
        self.assertEqual(self.run_setup(), 1)
        self.assertFalse(self.env.exists())
        self.assertNotIn(TOKEN, self.output.getvalue())

    def test_http_redirect_is_not_followed(self):
        self.service.redirect = True
        self.assertEqual(self.run_setup(), 0)
        self.assertFalse(any(c[1] == "/stolen" for c in self.service.calls))
        self.assertIn("BotFather", self.output.getvalue())

    def test_ready_bot_identity_must_match_getme(self):
        self.service.claims[0][1]["bot_id"] = 999
        self.assertEqual(self.run_setup(), 1)
        self.assertFalse(self.env.exists())

    def test_invalid_display_name_makes_no_requests(self):
        self.assertEqual(self.run_setup(answers=("x" * 65,)), 1)
        self.assertEqual(self.service.calls, [])

    def test_environment_url_uses_local_relay(self):
        with service_server(self.service) as base, patch.dict(os.environ, {"BIZCARD_ONBOARDING_URL": base}):
            result = self.onboarding.setup_telegram(
                env_path=self.env, telegram_api_base=base, allow_insecure_localhost=True,
                no_open=True, input_fn=lambda prompt: "123456" if "6자리" in prompt else "", output=self.output)
        self.assertEqual(result, 0)
        self.assertTrue(any(c[1] == "/v1/pairings" for c in self.service.calls))

    def test_macos_open_uses_validated_link_and_no_open_skips_it(self):
        with patch.object(self.onboarding.sys, "platform", "darwin"), patch.object(
            self.onboarding.subprocess, "run"
        ) as opened:
            self.assertEqual(self.run_setup(no_open=False), 0)
        self.assertEqual(opened.call_args.args[0],
                         ["open", f"https://t.me/BizcardSetupBot?start={PAIRING_ID}"])
        with patch.object(self.onboarding.sys, "platform", "darwin"), patch.object(
            self.onboarding.subprocess, "run"
        ) as opened:
            self.assertEqual(self.run_setup(yes=True), 0)
        opened.assert_not_called()

    def test_doctor_recommends_installer_for_missing_token(self):
        self.assertEqual(cli.doctor.check_telegram().fix,
                         "python -m bizcard_intake setup-telegram 을 실행하세요")

    def test_cli_dispatches_flags(self):
        with patch.object(cli.telegram, "setup_logging"), patch.object(
            self.onboarding, "setup_telegram", return_value=0
        ) as setup:
            self.assertEqual(cli.main(["setup-telegram", "--manual", "--no-open", "--yes"]), 0)
        self.assertTrue(setup.call_args.kwargs["manual"])
        self.assertTrue(setup.call_args.kwargs["no_open"])
        self.assertTrue(setup.call_args.kwargs["yes"])

    def test_default_relay_is_the_public_https_service(self):
        self.assertEqual(self.onboarding.DEFAULT_ONBOARDING_URL,
                         "https://bizcard-onboarding-relay.bizcardbot.workers.dev")
        self.onboarding.validate_base_url(self.onboarding.DEFAULT_ONBOARDING_URL, allow_insecure_localhost=False)


    def test_requests_send_a_named_user_agent(self):
        captured = []

        class Opener:
            def open(self, request, timeout):
                captured.append(request.get_header("User-agent"))
                raise OSError("stop")

        with patch.object(self.onboarding, "build_opener", return_value=Opener()):
            with self.assertRaises(self.onboarding.SetupError):
                self.onboarding._request_json("https://relay.example/v1/pairings", data={"bot_name": "x"})
        self.assertEqual(captured, ["Bizcardbot-setup/" + __import__("bizcard_intake").__version__])
        self.assertNotIn("Python-urllib", captured[0])

    def test_transient_getme_failure_after_claim_is_retried(self):
        real = self.onboarding._telegram
        calls = {"getMe": 0}

        def flaky(base, token, operation, data=None, **kwargs):
            if operation == "getMe":
                calls["getMe"] += 1
                if calls["getMe"] == 1:
                    raise self.onboarding.SetupError("temporary")
            return real(base, token, operation, data, **kwargs)

        with patch.object(self.onboarding, "_telegram", side_effect=flaky):
            self.assertEqual(self.run_setup(), 0)
        self.assertGreaterEqual(calls["getMe"], 2)
        self.assertTrue(self.env.exists())

if __name__ == "__main__":
    unittest.main()
