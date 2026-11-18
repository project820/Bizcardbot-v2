import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from bizcard_intake import cards, chat_ux, doctor, multicard, pipeline, telegram
from bizcard_intake.contacts import SaveResult
from bizcard_intake.draft import CardDraft

ALLOWED = {"TELEGRAM_ALLOWED_CHAT_IDS": "42"}


class FakeTelegram:
    def __init__(self):
        self.sent, self.edited, self.photos = [], [], []
        self.next_id = 100

    def send_message(self, token, chat_id, text, reply_markup=None):
        self.next_id += 1
        self.sent.append((text, reply_markup, self.next_id))
        return {"result": {"message_id": self.next_id}}

    def edit_message(self, token, chat_id, message_id, text, reply_markup=None):
        self.edited.append((text, reply_markup, message_id))
        return {}

    def send_photo(self, token, chat_id, path, caption="", reply_markup=None):
        self.next_id += 1
        self.photos.append((caption, reply_markup, self.next_id))
        return {"result": {"message_id": self.next_id}}

    def api_post(self, token, method, payload):
        if method == "editMessageCaption":
            for index, (caption, markup, message_id) in enumerate(self.photos):
                if message_id == payload["message_id"]:
                    self.photos[index] = (payload["caption"], payload.get("reply_markup"), message_id)
        return {"ok": True}

    def all_texts(self):
        return [t for t, _, _ in self.sent] + [t for t, _, _ in self.edited] + [c for c, _, _ in self.photos]

    def last_markup(self):
        for text, markup, _ in reversed(self.edited + self.sent):
            if markup:
                return markup
        return None


def fake_scan(names):
    names = list(names)

    def scan(path):
        name = names.pop(0)
        return {"images": {"front": str(path), "profile": str(path)}, "rectifier": "test",
                "draft": CardDraft.from_dict({"name": name, "title": "과장", "company": "ACME", "mobile": [f"010-0000-{len(names)}"]})}
    return scan


def photo_update(update_id, file_id, media_group_id=None, reply_to=None):
    message = {"chat": {"id": 42}, "message_id": 1000 + update_id, "photo": [{"file_id": file_id}]}
    if media_group_id:
        message["media_group_id"] = media_group_id
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def text_update(update_id, text, reply_to=None):
    message = {"chat": {"id": 42}, "message_id": 2000 + update_id, "text": text}
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def callback(update_id, data):
    return {"update_id": update_id, "callback_query": {"id": str(update_id), "data": data, "message": {"chat": {"id": 42}}}}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        telegram._rejected_chats.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.tg = FakeTelegram()
        self.patches = [
            patch.object(pipeline.contact_backend, "find_job_change_candidate", return_value=None),
            patch.object(pipeline.contact_backend.contacts, "run_gws", return_value={"connections": []}),
            patch.dict(os.environ, {**ALLOWED, "BIZCARD_CONTACTS_BACKEND": "google"}),
            patch.object(telegram, "_executor", None),
            patch.object(telegram, "_summary_token", ""),
            patch.object(telegram, "_group_pending", {}),
            patch.object(telegram, "_group_summary_wanted", set()),
            patch.object(telegram, "_progress_at", {}),
            patch.object(cards, "CARD_DIR", self.tmp / "cards"),
            patch.object(pipeline, "SESSION_ARCHIVE_DIR", self.tmp / "archive"),
            patch.object(telegram, "send_message", self.tg.send_message),
            patch.object(telegram, "edit_message", self.tg.edit_message),
            patch.object(telegram, "send_photo", self.tg.send_photo),
            patch.dict(os.environ, {"BIZCARD_TELEGRAM_INTERVAL": "0"}),
            patch.object(telegram, "api_post", self.tg.api_post),
            patch.object(telegram, "api_get", lambda token, method, params=None, timeout=10: {"result": {"file_path": f"photos/{params['file_id']}.jpg"}}),
            patch.object(telegram, "download_file", lambda token, file_path: b"jpg"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def run_updates(self, updates):
        telegram.handle_updates("token", updates, self.tmp / "photos")

    def test_optional_lookup_fixture_never_spawns_subprocess(self):
        card = cards.new_card(42)
        pipeline.set_draft(card, CardDraft(name='Test', mobile=['010-0000-1234']), 'test')
        card['status'] = 'review'
        cards.save_card(card)
        with patch('subprocess.run', side_effect=AssertionError('lookup leaked subprocess')) as spawn:
            pipeline.refresh_job_change(card)
            self.assertEqual(spawn.call_count, 0)

    def test_album_batch_fixture_never_spawns_subprocess(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나"])), patch.object(
            pipeline, "save_contact", lambda d, i, **kw: SaveResult("created", "people/fixture")
        ), patch("subprocess.run", side_effect=AssertionError("album leaked subprocess")) as spawn:
            self.run_updates([photo_update(1, "a", "ISOLATED"), photo_update(2, "b", "ISOLATED")])
            self.run_updates([callback(3, "ba:g:ISOLATED")])
            self.assertEqual(spawn.call_count, 0)
        self.assertTrue(all(c["status"] == "saved" for c in cards.list_cards()))

    def test_first_draft_does_not_wait_for_job_lookup(self):
        import time
        sent = []
        def slow_lookup(value):
            time.sleep(0.15)
            return None
        start = time.monotonic()
        with patch.object(pipeline, 'scan_front', fake_scan(['Test'])), patch.object(pipeline.contact_backend, 'find_job_change_candidate', side_effect=slow_lookup), patch.object(telegram, 'edit_message', side_effect=lambda *a, **k: sent.append(time.monotonic())):
            self.run_updates([photo_update(1, 'fixture')])
        self.assertTrue(sent)
        self.assertLess(sent[0] - start, 0.10)

    def test_job_candidate_buttons_arrive_after_initial_draft(self):
        seen_initial = []
        def lookup(value):
            seen_initial.append(bool(self.tg.edited))
            return {'resourceName': 'people/fixture', 'organizations': [{'name': 'Old Fixture', 'title': 'Old Role'}]}
        with patch.object(pipeline, 'scan_front', fake_scan(['Test'])), patch.object(pipeline.contact_backend, 'find_job_change_candidate', side_effect=lookup):
            self.run_updates([photo_update(1, 'fixture')])
        self.assertEqual(seen_initial, [True])
        labels = [b['text'] for row in self.tg.edited[-1][1]['inline_keyboard'] for b in row]
        self.assertIn('기존 연락처 갱신', labels)
        self.assertIn('새 연락처로 저장', labels)

    def test_candidate_lookup_cannot_overwrite_newer_revision(self):
        def lookup(value):
            card = cards.list_cards(42)[0]
            pipeline.set_draft(card, CardDraft(name='Edited', mobile=['010-0000-9999']), 'newer')
            cards.save_card(card)
            return {'resourceName': 'people/old', 'organizations': [{'name': 'Old Fixture', 'title': 'Old Role'}]}
        with patch.object(pipeline, 'scan_front', fake_scan(['Test'])), patch.object(pipeline.contact_backend, 'find_job_change_candidate', side_effect=lookup):
            self.run_updates([photo_update(1, 'fixture')])
        card = cards.list_cards(42)[0]
        self.assertEqual(card['draft']['name'], 'Edited')
        self.assertIsNone(card.get('job_change'))
        self.assertEqual(len(self.tg.edited), 1)

    def test_restart_clears_interrupted_optional_lookup(self):
        card = cards.new_card(42)
        card.update(status='review', job_change_pending=True)
        cards.save_card(card)
        telegram.recover_interrupted()
        self.assertFalse(cards.load_card(card['id'])['job_change_pending'])

    def test_shared_ux_names_are_reexported(self):
        for name in ('HELP_TEXT', 'save_result_text', 'edit_prompt', 'batch_summary', 'select_card'):
            self.assertIs(getattr(telegram, name), getattr(chat_ux, name))

    def test_failed_save_notification_does_not_overwrite_concurrent_edit(self):
        card = cards.new_card(42)
        card.update(status='review', draft=CardDraft(name='Name', mobile=['010-1234']).to_dict())
        cards.save_card(card)
        def save_failure(card, revision):
            card.update(status='save_failed', approved_revision=card['revision'])
            cards.save_card(card)
            return {'ok': False, 'kind': 'transient', 'message': 'offline'}
        def edit_while_notifying(*args, **kwargs):
            fresh = cards.load_card(card['id'])
            self.assertTrue(pipeline.edit(fresh, '직함: 과장')[0])
            return {}
        with patch.object(pipeline, 'approve', side_effect=save_failure), patch.object(telegram, 'edit_message', side_effect=edit_while_notifying):
            telegram.approve_card('test', 42, card, card['revision'])
        fresh = cards.load_card(card['id'])
        self.assertEqual(fresh['status'], 'review')
        self.assertEqual(pipeline.current_draft(fresh).short_title, '과장')

    def test_cancel_callback_does_not_report_success_after_save_wins(self):
        card = cards.new_card(42)
        card.update(status='review', draft=CardDraft(name='Name').to_dict())
        cards.save_card(card)
        stale = dict(card)
        card['status'] = 'saved'
        cards.save_card(card)
        real_load = cards.load_card
        calls = []
        def load(card_id):
            calls.append(card_id)
            return stale if len(calls) == 1 else real_load(card_id)
        with patch.object(cards, 'load_card', side_effect=load):
            self.run_updates([callback(1, f"cx:{card['id']}")])
        self.assertEqual(real_load(card['id'])['status'], 'saved')
        self.assertNotIn('취소했습니다', self.tg.sent[-1][0])

    def test_chat_outside_allowlist_is_rejected_once(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": "7"}):
            for i in range(2):
                telegram.handle_update("token", photo_update(i, "x"))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(cards.list_cards(), [])

    def test_run_refuses_to_start_without_allowlist(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": ""}):
            with self.assertRaises(SystemExit):
                telegram.run("token")

    def test_polling_offset_is_bound_to_bot_identity(self):
        for stored, bot_id, expected in [
            ({"bot_id": 111, "offset": 900000000}, 222, None),
            ({"bot_id": 222, "offset": 123}, 222, 123),
            ({"offset": 900000000}, 222, None),
        ]:
            with self.subTest(stored=stored):
                cards.save_state("telegram", stored)
                with patch.object(telegram, "_executor"), patch.object(telegram, "_summary_token", ""), patch.object(
                    pipeline, "migrate_legacy", return_value=[]
                ), patch.object(
                    telegram, "recover_interrupted"
                ), patch.object(telegram, "delete_webhook"), patch.object(
                    telegram, "api_get", return_value={"result": {"id": bot_id, "username": "fixture_bot"}}
                ), patch.object(telegram, "ThreadPoolExecutor"), patch.object(
                    telegram, "health_check"
                ), patch.object(telegram, "get_updates", side_effect=KeyboardInterrupt) as updates:
                    with self.assertRaises(KeyboardInterrupt):
                        telegram.run("new_token", self.tmp / "photos")
                updates.assert_called_once_with("new_token", expected)
                self.assertEqual(cards.load_state("telegram"), {"bot_id": bot_id, "offset": expected})

    def test_handling_updates_keeps_bot_identity_in_offset_state(self):
        cards.save_state("telegram", {"bot_id": 222, "offset": 10})
        with patch.object(telegram, "handle_update", return_value=None):
            telegram.handle_updates("token", [{"update_id": 10}], self.tmp / "photos", 10)
        self.assertEqual(cards.load_state("telegram"), {"bot_id": 222, "offset": 11})

    def test_photo_creates_card_with_card_scoped_buttons(self):
        with patch.object(pipeline, "scan_front", fake_scan(["정호준"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.assertEqual(card["status"], "review")
        draft_text, markup, _ = self.tg.edited[-1]
        self.assertIn("표시 이름: 정호준 과장 ACME", draft_text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"ap:{card['id']}:{card['revision']}")
        self.assertTrue(Path(card["images"]["front"]).name.startswith(card["id"]))

    def test_album_makes_one_card_per_photo_and_batch_save(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나", "다"])):
            self.run_updates([photo_update(1, "a", "G1"), photo_update(2, "b", "G1"), photo_update(3, "c", "G1")])
        self.assertEqual(len(cards.list_cards()), 3)
        progress_id = self.tg.sent[0][2]
        self.assertEqual(len(self.tg.sent), 1)  # one progress message for the whole album
        self.assertEqual(len(self.tg.photos), 3)  # one photo+draft message per card
        caption, buttons, _ = self.tg.photos[0]
        self.assertIn("표시 이름:", caption)
        self.assertTrue(buttons["inline_keyboard"][0][0]["callback_data"].startswith("ap:"))
        summary, markup, edited_id = self.tg.edited[-1]
        self.assertEqual(edited_id, progress_id)
        self.assertIn("보낸 명함 3건", summary)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "ba:g:G1")
        saved = []
        with patch.object(pipeline, "save_contact", lambda d, i, resource_name=None, checkpoint=None: saved.append(d.name) or SaveResult("created", "people/x", "uploaded", [], d.display_name)):
            self.run_updates([callback(9, "ba:g:G1")])
        self.assertEqual(saved, ["가", "나", "다"])
        self.assertTrue(all(c["status"] == "saved" for c in cards.list_cards()))

    def test_reply_edits_only_the_card_it_replies_to(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나"])):
            self.run_updates([photo_update(1, "a"), photo_update(2, "b")])
        first, second = cards.list_cards()
        target_message = next(mid for _, _, mid in self.tg.sent
                              if (cards.find_by_message(42, mid) or {}).get('id') == first['id'])
        with patch.object(pipeline, "revise_draft", side_effect=AssertionError("no AI for field edits")):
            self.run_updates([text_update(3, "직함: 영업팀장\n회사: 누리네트웍스", reply_to=target_message)])
        self.assertEqual(pipeline.current_draft(cards.load_card(first["id"])).display_name, "가 영업팀장 누리네트웍스")
        self.assertEqual(pipeline.current_draft(cards.load_card(second["id"])).company, "ACME")

    def test_unthreaded_text_with_several_open_cards_asks_for_a_reply(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나"])):
            self.run_updates([photo_update(1, "a"), photo_update(2, "b")])
        self.run_updates([text_update(3, "직함: 부장")])
        self.assertTrue(any("답장으로" in text for text, _, _ in self.tg.sent))
        self.assertTrue(all(pipeline.current_draft(c).short_title == "과장" for c in cards.list_cards()))

    def test_old_approve_button_is_not_applied_to_a_newer_card(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            self.run_updates([callback(2, "approve")])
        self.assertIn("예전 버튼", self.tg.sent[-1][0])

    def test_stale_revision_button_after_edit(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        old = f"ap:{card['id']}:{card['revision']}"
        self.run_updates([text_update(2, "직함: 부장")])
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            self.run_updates([callback(3, old)])
        self.assertIn("수정되었습니다", self.tg.edited[-1][0])

    def test_edit_button_sends_force_reply_bound_to_card(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.run_updates([callback(2, f"ed:{card['id']}")])
        text, markup, message_id = self.tg.sent[-1]
        self.assertTrue(markup["force_reply"])
        self.assertIn("이름: 가", text)
        self.assertEqual(cards.find_by_message(42, message_id)["id"], card["id"])

    def test_back_photo_goes_to_the_requesting_card(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.run_updates([callback(2, f"bk:{card['id']}")])
        prompt_id = self.tg.sent[-1][2]
        merged = []
        with patch.object(pipeline, "recognize_back", lambda c, path: merged.append(c["id"]) or c):
            self.run_updates([photo_update(3, "back", reply_to=prompt_id)])
        self.assertEqual(merged, [card["id"]])
        self.assertEqual(len(cards.list_cards()), 1)

    def test_list_shows_open_cards(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나"])):
            self.run_updates([photo_update(1, "a"), photo_update(2, "b")])
        self.run_updates([text_update(3, "목록")])
        text, markup, _ = self.tg.sent[-1]
        self.assertIn("저장 안 된 명함 2건", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "ba:all")

    def test_save_failure_message_is_short_and_offers_retry(self):
        from bizcard_intake.contacts import ContactSaveError
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        with patch.object(pipeline, "save_contact", side_effect=ContactSaveError("Google 서버 일시 오류(502). 잠시 후 다시 시도합니다.", kind="transient", retryable=True)):
            self.run_updates([callback(2, f"ap:{card['id']}:{card['revision']}")])
        text, markup, _ = self.tg.edited[-1]
        self.assertIn("저장 보류", text)
        self.assertNotIn("<", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"sv:{card['id']}")

    def test_restart_recovers_interrupted_cards(self):
        card = cards.new_card(42)
        card["status"] = "saving"
        cards.save_card(card)
        telegram.recover_interrupted()
        self.assertEqual(cards.load_card(card["id"])["status"], "save_failed")

    def test_retry_reports_success_and_forced_failures_to_telegram(self):
        for ok, force, should_report in ((True, False, True), (False, False, False), (False, True, True)):
            with self.subTest(ok=ok, force=force):
                card = cards.new_card(-42)
                card.update(status="save_failed", draft=CardDraft(name="가").to_dict())
                cards.save_card(card)
                outcome = {"ok": ok, "message": "result", "kind": "transient"}

                def retry(*, force, on_result):
                    on_result(card, outcome)
                    return int(ok), [] if ok else ["result"]

                with patch.object(pipeline, "retry_due", side_effect=retry) as retried, patch.object(
                    telegram, "send_message", return_value={"result": {"message_id": 999}}
                ) as send:
                    telegram.retry_due_and_report("token", force=force)
                self.assertEqual(retried.call_args.kwargs["force"], force)
                if should_report:
                    send.assert_called_once()
                    self.assertEqual(send.call_args.args[:2], ("token", -42))
                    self.assertIn("자동 재시도", send.call_args.args[2])
                    self.assertEqual(send.call_args.kwargs["reply_markup"],
                                     None if ok else telegram.saved_failed_buttons(card))
                else:
                    send.assert_not_called()

    def test_retry_skips_reporting_local_cli_cards(self):
        card = cards.new_card("cli", source="cli")
        card["status"] = "save_failed"
        cards.save_card(card)

        def retry(*, force, on_result):
            on_result(card, {"ok": True, "message": "saved"})
            return 1, []

        with patch.object(pipeline, "retry_due", side_effect=retry), patch.object(
            telegram, "send_message"
        ) as send:
            telegram.retry_due_and_report("token", force=True)
        send.assert_not_called()

    def test_album_is_processed_in_parallel_and_summarised_once(self):
        import threading
        import time as _time
        from concurrent.futures import ThreadPoolExecutor

        active, peak = [0], [0]
        lock = threading.Lock()

        def slow_scan(path):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.4)
            with lock:
                active[0] -= 1
            return {"images": {"front": str(path), "profile": str(path)}, "rectifier": "t", "draft": CardDraft.from_dict({"name": Path(path).stem[-1], "mobile": ["010-1"]})}

        executor = ThreadPoolExecutor(max_workers=4)
        with patch.object(telegram, "_executor", executor), patch.object(pipeline, "scan_front", slow_scan):
            started = _time.monotonic()
            self.run_updates([photo_update(i, f"p{i}", "G2") for i in range(1, 5)])
            returned = _time.monotonic() - started
            executor.shutdown(wait=True)
            total = _time.monotonic() - started
        self.assertLess(returned, 0.3)  # polling loop is free while cards are processed
        self.assertLess(total, 1.2)     # 4 x 0.4 s ran concurrently
        self.assertGreaterEqual(peak[0], 3)
        self.assertEqual([c["seq"] for c in cards.list_cards()], sorted(c["seq"] for c in cards.list_cards()))
        summaries = [text for text, _, _ in self.tg.edited if "보낸 명함 4건" in text]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(len(self.tg.sent), 1)

    def test_thirty_card_album_with_thirty_workers(self):
        import time as _time
        from concurrent.futures import ThreadPoolExecutor

        def scan(path):
            _time.sleep(0.3)
            return {"images": {"front": str(path), "profile": str(path)}, "rectifier": "t", "draft": CardDraft.from_dict({"name": "가", "mobile": ["010-1"]})}

        executor = ThreadPoolExecutor(max_workers=telegram.worker_count())
        with patch.object(telegram, "_executor", executor), patch.object(pipeline, "scan_front", scan):
            started = _time.monotonic()
            self.run_updates([photo_update(i, f"p{i}", "G30") for i in range(1, 31)])
            executor.shutdown(wait=True)
            total = _time.monotonic() - started
        self.assertEqual(telegram.worker_count(), 30)
        self.assertLess(total, 2.0)  # 30 x 0.3 s sequentially would be 9 s
        self.assertEqual(len(self.tg.photos), 30)
        self.assertTrue(any("보낸 명함 30건" in text for text, _, _ in self.tg.edited))

    def test_rate_limit_wait_does_not_use_up_attempts(self):
        import io
        import json as _json
        from urllib.error import HTTPError

        calls = []

        def limited():
            calls.append(1)
            if len(calls) <= 5:
                raise HTTPError("u", 429, "x", {}, io.BytesIO(_json.dumps({"parameters": {"retry_after": 1}}).encode()))
            return {"ok": True}

        with patch.object(telegram.time, "sleep", lambda s: None):
            self.assertEqual(telegram.with_retry(limited, "x"), {"ok": True})
        self.assertEqual(len(calls), 6)

    def test_double_tap_saves_once(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        calls, gate = [], threading.Event()

        def slow_save(draft, image, resource_name=None, checkpoint=None):
            calls.append(1)
            gate.wait(1)
            return SaveResult("created", "people/x", "uploaded", [], draft.display_name)

        executor = ThreadPoolExecutor(max_workers=4)
        data = f"ap:{card['id']}:{card['revision']}"
        with patch.object(telegram, "_executor", executor), patch.object(pipeline, "save_contact", slow_save):
            self.run_updates([callback(2, data), callback(3, data)])
            gate.set()
            executor.shutdown(wait=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cards.load_card(card["id"])["status"], "saved")

    def test_health_check_alerts_admin_chat_on_failed_checks(self):
        failed = [doctor.Check("gws", False, "Google 인증 무효", "gws auth login"), doctor.Check("parser", True, "ok")]
        with patch.object(doctor, "run_checks", lambda names=None: failed), patch.dict(os.environ, {"BIZCARD_ALERT_CHAT_ID": "42"}), patch.object(
            pipeline, "retry_due", side_effect=AssertionError("no retry while Google auth is broken")
        ):
            problems = telegram.health_check("token")
        self.assertEqual([c.name for c in problems], ["gws"])
        self.assertIn("gws auth login", self.tg.sent[0][0])

    def test_api_calls_retry_transient_network_errors(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise URLError("Connection reset by peer")
            return {"ok": True}

        with patch.object(telegram.time, "sleep", lambda s: None):
            self.assertEqual(telegram.with_retry(flaky, "x"), {"ok": True})
            with self.assertRaises(URLError):
                telegram.with_retry(lambda: (_ for _ in ()).throw(URLError("down")), "y")

    def test_rate_limit_honours_retry_after_and_client_errors_fail_fast(self):
        import io
        import json as _json
        from urllib.error import HTTPError

        def http_error(code, body):
            return HTTPError("u", code, "x", {}, io.BytesIO(_json.dumps(body).encode()))

        sleeps, attempts = [], []

        def limited():
            attempts.append(1)
            if len(attempts) == 1:
                raise http_error(429, {"parameters": {"retry_after": 7}})
            return {"ok": True}

        with patch.object(telegram.time, "sleep", sleeps.append):
            telegram.with_retry(limited, "x")
            with self.assertRaises(RuntimeError):
                telegram.with_retry(lambda: (_ for _ in ()).throw(http_error(400, {"description": "Bad Request"})), "y")
        self.assertEqual(sleeps, [7.0])

    def test_get_updates_uses_long_polling(self):
        seen = {}

        def fake_api_get(token, method, params=None, timeout=10):
            seen.update(params or {})
            seen["timeout_arg"] = timeout
            return {"result": []}

        with patch.object(telegram, "api_get", fake_api_get):
            telegram.get_updates("token", 5)
        self.assertEqual(seen["timeout"], telegram.LONG_POLL_SECONDS)
        self.assertGreater(seen["timeout_arg"], telegram.LONG_POLL_SECONDS)


def fake_split(*counts):
    """Pretend each received photo holds this many cards, in update order."""
    counts = list(counts)

    def detect(source, work_dir):
        count = counts.pop(0)
        leftover = isinstance(count, str)
        count = int(count.rstrip("+")) if leftover else count
        if count == 1:
            return multicard.Split("single")
        if count > multicard.MAX_CARDS:
            return multicard.Split("too_many", count=count)
        if count < 0:
            return multicard.Split("too_small", count=-count)
        work_dir.mkdir(parents=True, exist_ok=True)
        crops = []
        for index in range(count):
            crop = work_dir / f"candidate-{index:03d}.jpg"
            crop.write_bytes(b"crop")
            crops.append(crop)
        return multicard.Split("split", tuple(crops), count, leftover=leftover)
    return detect


class MulticardTelegramTest(unittest.TestCase):
    setUp = TelegramTest.setUp
    tearDown = TelegramTest.tearDown
    run_updates = TelegramTest.run_updates

    def front_dir(self):
        return self.tmp / "photos" / "42" / "front"

    def test_three_cards_in_one_photo_become_three_drafts_with_batch_save(self):
        with patch.object(multicard, "detect", fake_split(3)), \
                patch.object(pipeline, "scan_front", fake_scan(["가", "나", "다"])):
            self.run_updates([photo_update(1, "a")])
        members = cards.list_cards()
        self.assertEqual(len(members), 3)
        fronts = [Path(c["images"]["front"]) for c in members]
        self.assertEqual(len({p.stem for p in fronts}), 3)
        self.assertTrue(all(p.exists() for p in fronts))
        self.assertEqual(sorted(p.name for p in self.front_dir().iterdir()), sorted(p.name for p in fronts))
        group = members[0]["media_group_id"]
        self.assertTrue(group.startswith("split-"))
        self.assertTrue(all(c["media_group_id"] == group for c in members))
        self.assertEqual(len(self.tg.photos), 3)
        status_id = self.tg.sent[0][2]
        self.assertIn("명함 3장을 찾았습니다", self.tg.edited[0][0])
        summary, markup, edited_id = self.tg.edited[-1]
        self.assertEqual(edited_id, status_id)
        self.assertIn("보낸 명함 3건", summary)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"ba:g:{group}")
        saved = []
        with patch.object(pipeline, "save_contact", lambda d, i, resource_name=None, checkpoint=None: saved.append(d.name) or SaveResult("created", "people/x", "uploaded", [], d.display_name)):
            self.run_updates([callback(9, f"ba:g:{group}")])
        self.assertEqual(sorted(saved), ["가", "나", "다"])

    def test_text_outside_found_cards_warns_about_a_missing_card(self):
        with patch.object(multicard, "detect", fake_split("2+")), \
                patch.object(pipeline, "scan_front", fake_scan(["가", "나"])):
            self.run_updates([photo_update(1, "a")])
        self.assertEqual(len(cards.list_cards()), 2)
        warnings = [mid for text, _, mid in self.tg.sent if "빠진 명함이 있으면 따로 보내주세요" in text]
        self.assertEqual(len(warnings), 1)
        self.assertFalse(any(mid == warnings[0] for _, _, mid in self.tg.edited))  # never overwritten
        before = len(self.tg.sent)
        with patch.object(multicard, "detect", fake_split("2+", 1)), \
                patch.object(pipeline, "scan_front", fake_scan(["다", "라", "마"])):
            self.run_updates([photo_update(2, "b", "G2"), photo_update(3, "c", "G2")])
        album_warnings = [text for text, _, _ in self.tg.sent[before:] if "빠진 명함" in text]
        self.assertEqual(len(album_warnings), 1)
        self.assertTrue(album_warnings[0].startswith("[명함 "))

    def test_notice_failure_still_recognizes_every_split_card(self):
        def broken_edit(*args, **kwargs):
            raise URLError("deleted status message")
        with patch.object(multicard, "detect", fake_split(3)), \
                patch.object(telegram, "edit_message", broken_edit), \
                patch.object(pipeline, "scan_front", fake_scan(["가", "나", "다"])):
            self.run_updates([photo_update(1, "a")])
        self.assertEqual(sorted(c["status"] for c in cards.list_cards()), ["review"] * 3)

    def test_every_split_card_keeps_its_crop_before_recognition_starts(self):
        queued = []
        with patch.object(multicard, "detect", fake_split(3)), \
                patch.object(telegram, "_recognize_and_show", lambda *args: queued.append(args)):
            self.run_updates([photo_update(1, "a")])
        self.assertEqual(len(queued), 3)
        for card in cards.list_cards():
            self.assertTrue(Path(card["images"]["front"]).exists())

    def test_more_than_four_cards_asks_for_four_or_fewer_with_help_button(self):
        with patch.object(multicard, "detect", fake_split(5)), \
                patch.object(pipeline, "scan_front", side_effect=AssertionError("no OCR when refused")):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.assertEqual(card["status"], "cancelled")
        text, markup, _ = self.tg.edited[-1]
        self.assertIn("명함이 5장 보입니다", text)
        self.assertIn("4장 이하", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "mh")
        self.assertEqual(list(self.front_dir().iterdir()), [])
        self.run_updates([callback(2, "mh")])
        self.assertIn("HD", self.tg.sent[-1][0])

    def test_small_cards_ask_for_hd_or_file(self):
        with patch.object(multicard, "detect", fake_split(-3)), \
                patch.object(pipeline, "scan_front", side_effect=AssertionError("no OCR when refused")):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.assertEqual(card["status"], "cancelled")
        text, markup, _ = self.tg.edited[-1]
        self.assertIn("HD 화질이나 파일로", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "mh")

    def test_single_card_keeps_the_original_photo_and_path(self):
        with patch.object(multicard, "detect", fake_split(1)), \
                patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.assertIsNone(card["media_group_id"])
        self.assertEqual(Path(card["images"]["front"]).name, f"{card['id']}_a.jpg")

    def test_back_photo_skips_detection(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.run_updates([callback(2, f"bk:{card['id']}")])
        prompt_id = self.tg.sent[-1][2]
        with patch.object(multicard, "detect", side_effect=AssertionError("back photo must not split")), \
                patch.object(pipeline, "recognize_back", lambda c, path: c):
            self.run_updates([photo_update(3, "back", reply_to=prompt_id)])
        self.assertEqual(len(cards.list_cards()), 1)

    def test_album_photo_with_two_cards_joins_the_album_summary(self):
        with patch.object(multicard, "detect", fake_split(2, 1)), \
                patch.object(pipeline, "scan_front", fake_scan(["가", "나", "다"])):
            self.run_updates([photo_update(1, "a", "G1"), photo_update(2, "b", "G1")])
        members = cards.list_cards()
        self.assertEqual(len(members), 3)
        self.assertTrue(all(c["media_group_id"] == "G1" for c in members))
        self.assertEqual(len(self.tg.photos), 3)
        summary, markup, _ = self.tg.edited[-1]
        self.assertIn("보낸 명함 3건", summary)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "ba:g:G1")


if __name__ == "__main__":
    unittest.main()
