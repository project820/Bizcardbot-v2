import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from bizcard_intake import cards, pipeline, telegram


TOKEN = "123456:FAKE_SECURITY_TOKEN"
OTHER_POLLER = "Conflict: terminated by other getUpdates request"
WEBHOOK_ACTIVE = "Conflict: can't use getUpdates method while webhook is active"


class TokenUseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = 0
        self.calls = []
        self.notices = []
        for context in (
            patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": "42,84,-100",
                                    "BIZCARD_ALERT_CHAT_ID": "999"}, clear=True),
            patch.object(cards, "CARD_DIR", self.root / "data/cards"),
            patch.object(pipeline, "migrate_legacy", return_value=[]),
            patch.object(telegram, "recover_interrupted"),
            patch.object(telegram, "health_check"),
            patch.object(telegram, "retry_due_and_report"),
            patch.object(telegram, "ThreadPoolExecutor"),
            patch.object(telegram, "_executor", None),
            patch.object(telegram, "_summary_token", ""),
            patch.object(telegram.time, "monotonic", side_effect=lambda: self.clock),
            patch.object(telegram.time, "time", side_effect=lambda: 100000 + self.clock),
            patch.object(telegram.time, "sleep"),
            patch.object(telegram, "api_post", side_effect=self.post),
            patch.object(telegram, "send_message", side_effect=self.send),
        ):
            context.start()
            self.addCleanup(context.stop)

    def post(self, token, method, payload):
        self.calls.append(method)
        return {"ok": True}

    def send(self, token, chat_id, text):
        self.notices.append((chat_id, text, self.clock))
        return {"ok": True}

    def poll(self, events, *, webhook=""):
        events = iter(events)

        def urlopen(url, timeout=10):
            method = str(url).split("?", 1)[0].rsplit("/", 1)[-1]
            self.calls.append(method)
            if method == "getMe":
                payload = {"id": 123456, "username": "fixture_bot"}
            elif method == "getWebhookInfo":
                payload = {"url": webhook}
            elif method == "getUpdates":
                try:
                    self.clock, outcome = next(events)
                except StopIteration:
                    raise KeyboardInterrupt
                if isinstance(outcome, tuple):
                    code, description = outcome
                    raise HTTPError(str(url), code, "Conflict", {}, io.BytesIO(json.dumps(
                        {"ok": False, "error_code": code, "description": description}).encode()))
                payload = outcome
            else:
                raise AssertionError(method)
            return io.BytesIO(json.dumps({"ok": True, "result": payload}).encode())

        with patch.object(telegram, "urlopen", side_effect=urlopen), self.assertLogs("bizcard", level="WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                telegram.run(TOKEN, self.root / "data/photos")
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        return "\n".join(logs.output)

    def test_startup_inspects_webhook_before_preserved_delete_and_only_logs_warning(self):
        logs = self.poll([], webhook="https://foreign.invalid/secret")
        self.assertLess(self.calls.index("getWebhookInfo"), self.calls.index("deleteWebhook"))
        self.assertIn("ERROR", logs)
        self.assertNotIn("foreign.invalid", logs)
        self.assertEqual(self.notices, [])

    def test_conflicts_notify_only_after_sixty_seconds_and_only_allowed_private_chats(self):
        self.poll([(0, (409, OTHER_POLLER)), (59, (409, OTHER_POLLER)), (60, (409, OTHER_POLLER))])
        self.assertEqual([chat for chat, _, _ in self.notices], [42, 84])
        for _, text, when in self.notices:
            self.assertEqual(when, 60)
            self.assertTrue(text.startswith("다른 컴퓨터나 이전 실행에서 같은 봇이 돌고 있는지 확인하세요"))
            self.assertIn("모르는", text)
            self.assertIn("BotFather", text)
            self.assertIn("setup-telegram --manual", text)
        self.assertEqual(self.calls.count("getWebhookInfo"), 4)
        self.assertEqual(self.calls.count("deleteWebhook"), 1)

    def test_success_and_non_conflict_error_reset_sixty_second_window(self):
        for reset in ([], (400, "Bad Request")):
            with self.subTest(reset=reset):
                self.poll([(0, (409, OTHER_POLLER)), (50, reset), (60, (409, OTHER_POLLER)),
                           (119, (409, OTHER_POLLER))])
                self.assertEqual(self.notices, [])

    def test_alert_hour_limit_survives_restart_and_expires(self):
        self.poll([(0, (409, OTHER_POLLER)), (60, (409, OTHER_POLLER))])
        self.assertEqual(len(self.notices), 2)
        self.poll([(100, (409, OTHER_POLLER)), (160, (409, OTHER_POLLER)),
                   (3659, (409, OTHER_POLLER)), (3660, (409, OTHER_POLLER))])
        self.assertEqual([when for _, _, when in self.notices], [60, 60, 3660, 3660])
        state = cards.load_state("telegram_security")
        self.assertTrue(state)
        self.assertNotIn(TOKEN, json.dumps(state))

    def test_webhook_and_other_instance_conflicts_have_distinct_safe_logs(self):
        for description, expected in ((OTHER_POLLER, "other getUpdates"), (WEBHOOK_ACTIVE, "webhook is active")):
            with self.subTest(description=description):
                logs = self.poll([(0, (409, description))])
                self.assertIn(expected, logs)
                self.assertIn("ERROR", logs)
        self.assertEqual(self.notices, [])

    def test_conflict_description_cannot_leak_token_into_logs(self):
        logs = self.poll([(0, (409, f"{OTHER_POLLER}: https://api.telegram.org/bot{TOKEN}/getUpdates"))])
        self.assertNotIn(TOKEN, logs)
        self.assertIn("other getUpdates", logs)

    def test_normal_polling_does_not_repeat_webhook_checks(self):
        with patch.object(telegram.log, "warning"):
            # The startup webhook warning gives assertLogs a record, while
            # successful getUpdates responses exercise the normal polling path.
            self.poll([(0, []), (30, []), (60, [])], webhook="https://foreign.invalid")
        self.assertEqual(self.calls.count("getWebhookInfo"), 1)
        self.assertEqual(self.notices, [])


if __name__ == "__main__":
    unittest.main()
