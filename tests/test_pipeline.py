import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import cards, pipeline
from bizcard_intake.contacts import ContactSaveError, SaveResult
from bizcard_intake.draft import CardDraft


class CardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.patches = [
            patch.object(cards, "CARD_DIR", self.tmp / "cards"),
            patch.object(pipeline, "SESSION_DIR", self.tmp / "sessions"),
            patch.object(pipeline, "SESSION_ARCHIVE_DIR", self.tmp / "archive"),
            patch.object(pipeline, "PENDING_CONTACT_DIR", self.tmp / "pending"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def review_card(self, draft=None, with_images=True):
        card = cards.new_card(42)
        if with_images:
            for key in ("front", "profile", "ocr"):
                path = self.tmp / f"{card['id']}_{key}.jpg"
                path.write_bytes(b"x")
                card["images"][key] = str(path)
        pipeline.set_draft(card, CardDraft.from_dict(draft or {"name": "홍길동", "title": "이사", "company": "ACME", "mobile": ["010-1234-5678"]}), "test")
        card["status"] = "review"
        cards.save_card(card)
        return card


def ok_save(draft, image, resource_name=None, checkpoint=None):
    if checkpoint:
        checkpoint("people/c1", "created")
    return SaveResult("created", "people/c1", "uploaded", [], draft.display_name)


class PipelineTest(CardTestCase):
    def test_retry_save_serializes_edit_and_cancel_without_false_success(self):
        for operation in ('edit', 'cancel'):
            with self.subTest(operation=operation):
                card = self.review_card()
                card.update(status='save_failed', approved_revision=card['revision'])
                cards.save_card(card)
                stale = cards.load_card(card['id'])
                started, release = threading.Event(), threading.Event()
                def save(*args, **kwargs):
                    started.set()
                    if not release.wait(3):
                        raise AssertionError('save was not released')
                    return ok_save(*args, **kwargs)
                with patch.object(pipeline, 'save_contact', side_effect=save), ThreadPoolExecutor(max_workers=2) as pool:
                    saving = pool.submit(pipeline.retry_due, force=True)
                    self.assertTrue(started.wait(2))
                    editing = pool.submit(pipeline.edit, stale, '직함: 과장') if operation == 'edit' else pool.submit(pipeline.cancel, stale)
                    release.set()
                    result = editing.result(timeout=3)
                    saving.result(timeout=3)
                self.assertFalse(result[0] if operation == 'edit' else result)
                stored = cards.load_card(card['id'])
                self.assertEqual(stored['status'], 'saved')
                self.assertNotEqual(pipeline.current_draft(stored).short_title, '과장')

    def test_stale_edit_and_cancel_are_rejected_after_retry_attempt_fails(self):
        for operation in ('edit', 'cancel'):
            with self.subTest(operation=operation):
                card = self.review_card()
                card.update(status='save_failed', approved_revision=card['revision'])
                cards.save_card(card)
                stale = cards.load_card(card['id'])
                with patch.object(pipeline, 'save_contact', side_effect=ContactSaveError('offline', kind='transient')):
                    pipeline.retry_due(force=True)
                result = pipeline.edit(stale, '직함: 과장') if operation == 'edit' else pipeline.cancel(stale)
                self.assertFalse(result[0] if operation == 'edit' else result)
                self.assertEqual(cards.load_card(card['id'])['status'], 'save_failed')

    def test_cancel_processing_card_preserves_download_failure_cleanup(self):
        card = cards.new_card(42)
        self.assertTrue(pipeline.cancel(card))
        self.assertEqual(cards.load_card(card['id'])['status'], 'cancelled')

    def test_approve_saves_cleans_images_and_archives(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", ok_save):
            outcome = pipeline.approve(card, card["revision"])
        self.assertTrue(outcome["ok"])
        stored = cards.load_card(card["id"])
        self.assertEqual((stored["status"], stored["resource_name"]), ("saved", "people/c1"))
        self.assertFalse(any(stored["images"].get(k) for k in ("front", "profile", "ocr")))
        self.assertTrue((self.tmp / "archive" / f"card-{card['id']}.json").exists())

    def test_stale_revision_is_refused(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            outcome = pipeline.approve(card, card["revision"] - 1)
        self.assertTrue(outcome["stale"])

    def test_invalid_draft_is_not_saved_and_explains_what_is_missing(self):
        card = self.review_card({"company": "ACME", "mobile": ["010-1"]})
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            outcome = pipeline.approve(card)
        self.assertIn("이름", outcome["message"])

    def test_transient_failure_keeps_card_and_retry_reuses_resource_name(self):
        card = self.review_card()
        calls = []

        def failing(draft, image, resource_name=None, checkpoint=None):
            calls.append(resource_name)
            checkpoint("people/c9", "created")
            raise ContactSaveError("Google 서버 일시 오류(502).", kind="transient", retryable=True)

        with patch.object(pipeline, "save_contact", failing):
            outcome = pipeline.approve(card)
        self.assertEqual(outcome["kind"], "transient")
        stored = cards.load_card(card["id"])
        self.assertEqual(stored["status"], "save_failed")
        self.assertTrue(Path(stored["images"]["profile"]).exists())

        seen = []
        with patch.object(pipeline, "save_contact", lambda d, i, resource_name=None, checkpoint=None: seen.append(resource_name) or SaveResult("updated", resource_name, "uploaded", [], "")):
            saved, failures = pipeline.retry_due(force=True)
        self.assertEqual((saved, failures, seen), (1, [], ["people/c9"]))
        self.assertEqual(cards.load_card(card["id"])["status"], "saved")

    def test_retry_never_saves_unapproved_or_edited_cards(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", side_effect=ContactSaveError("x", kind="auth")):
            pipeline.approve(card)
        stored = cards.load_card(card["id"])
        pipeline.edit(stored, "직함: 상무")  # edited after approval -> needs a new approval
        self.review_card()  # never approved
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            self.assertEqual(pipeline.retry_due(force=True), (0, []))
        self.assertEqual(cards.load_card(card["id"])["status"], "review")

    def test_backoff_delays_automatic_retry(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", side_effect=ContactSaveError("x", kind="transient", retryable=True)):
            pipeline.approve(card)
        self.assertGreater(cards.load_card(card["id"])["next_retry_at"], time.time())
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("too early")):
            self.assertEqual(pipeline.retry_due(), (0, []))

    def test_manual_edit_needs_no_ai_and_survives_recognition_retry(self):
        card = self.review_card()
        with patch.object(pipeline, "revise_draft", side_effect=AssertionError("no AI for field edits")):
            ok, _ = pipeline.edit(card, "직함: 영업팀장\n회사: 누리네트웍스")
        self.assertTrue(ok)
        self.assertEqual(pipeline.current_draft(card).display_name, "홍길동 영업팀장 누리네트웍스")
        machine = CardDraft.from_dict({"name": "홍길동", "title": "사원", "company": "OTHER", "mobile": ["010-9"]})
        with patch.object(pipeline, "scan_front", lambda path: {"images": {"front": str(path)}, "rectifier": "t", "draft": machine}):
            pipeline.recognize_front(card)
        draft = pipeline.current_draft(card)
        self.assertEqual((draft.short_title, draft.company, draft.mobile), ("영업팀장", "누리네트웍스", ["010-9"]))

    def test_recognition_crash_still_gives_editable_card(self):
        card = cards.new_card(42)
        with patch.object(pipeline, "scan_front", side_effect=RuntimeError("codex down")):
            pipeline.recognize_front(card, self.tmp / "front.jpg")
        self.assertEqual(card["status"], "review")
        self.assertIn("자동 인식 실패", " ".join(pipeline.current_draft(card).warnings))
        ok, _ = pipeline.edit(card, "이름: 이영희\n휴대전화: 010-5555-0103")
        self.assertTrue(ok and pipeline.current_draft(card).is_savable)

    def test_photo_failure_still_counts_as_saved_and_photo_is_retried(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", lambda d, i, resource_name=None, checkpoint=None: SaveResult("created", "people/c1", "failed", ["사진 업로드 실패"], "")):
            self.assertTrue(pipeline.approve(card)["ok"])
        stored = cards.load_card(card["id"])
        self.assertTrue(Path(stored["images"]["profile"]).exists())
        uploads = []
        with patch.object(pipeline, "upload_photo", lambda rn, path: uploads.append(rn)):
            pipeline.retry_due(force=True)
        self.assertEqual(uploads, ["people/c1"])
        self.assertEqual(cards.load_card(card["id"])["photo_status"], "uploaded")


class LegacyMigrationTest(CardTestCase):
    def write(self, folder, name, data):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_migrates_pending_and_sessions(self):
        draft = {"name": "홍길동", "title": "이사", "mobile": ["010-1"]}
        # already saved later: the stuck "image missing" entry from production
        self.write(self.tmp / "pending", "42-1-file_60.json", {"status": "approved", "front_image_path": "f60.jpg", "draft": draft, "pending_reason": "<html>502"})
        self.write(self.tmp / "archive", "42-x.json", {"front_image_path": "f60.jpg", "save_output": "created: x"})
        # approved but failed (auth) + duplicate active session of the same card
        self.write(self.tmp / "pending", "42-2-file_61.json", {"status": "approved", "front_image_path": "f61.jpg", "draft": draft, "pending_reason": "No credentials provided"})
        self.write(self.tmp / "sessions", "42.json", {"status": "approved", "front_image_path": "f61.jpg", "draft": draft})
        # superseded by a newer photo: must stay in review
        self.write(self.tmp / "pending", "42-3-file_62.json", {"status": "drafted", "front_image_path": "f62.jpg", "draft": draft, "pending_reason": "superseded_by_new_front_photo"})

        pipeline.migrate_legacy()
        open_cards = cards.list_cards(statuses=cards.OPEN_STATUSES)
        self.assertEqual(sorted(c["status"] for c in open_cards), ["review", "save_failed"])
        failed = next(c for c in open_cards if c["status"] == "save_failed")
        self.assertEqual((failed["error_kind"], failed["approved_revision"]), ("auth", failed["revision"]))
        self.assertEqual(list((self.tmp / "pending").glob("*.json")), [])
        self.assertEqual(pipeline.migrate_legacy(), [])  # idempotent


class OrientationPipelineTest(CardTestCase):
    def source(self):
        from PIL import Image
        source = self.tmp / "front" / "card.jpg"
        source.parent.mkdir()
        image = Image.new("RGB", (500, 300), "navy")
        image.paste("red", (0, 0, 250, 150))
        image.save(source)
        return source

    def ocr(self, quarters=0, text="abcdefghijabcdefghij"):
        from bizcard_intake.vision_ocr import OcrLine, OcrResult
        right = ((0.9, 0.5), (0.1, 0.9), (-0.7, 0.5), (0.1, 0.1))[quarters]
        return OcrResult((OcrLine(text, 1, (0.1, 0.5), right),))

    def scan(self, source, results):
        from bizcard_intake import image_processing
        with patch.object(image_processing, "_try_vision_rectify", return_value=source) as rectify, patch.object(pipeline, "_ocr", side_effect=results) as ocr, patch.object(pipeline, "parse_card", return_value=CardDraft(name="test", title="CEO")) as parse:
            result = pipeline.scan_front(source)
        self.assertEqual(rectify.call_count, 1)
        return result, ocr, parse

    def test_front_rotation_rechecks_and_parses_corrected_images_and_ocr(self):
        self.assertTrue(hasattr(pipeline, "_prepare_oriented"))
        from PIL import Image
        source = self.source()
        first, upright = self.ocr(1), self.ocr(0)
        result, ocr, parse = self.scan(source, [first, upright])
        self.assertEqual(ocr.call_count, 2)
        self.assertIs(parse.call_args.args[1], upright)
        self.assertNotIn("명함 방향 확인 필요", result["draft"].warnings)
        with Image.open(result["images"]["ocr"]) as image:
            self.assertLess(image.width, image.height)
        with Image.open(parse.call_args.args[0]) as image:
            self.assertLess(image.width, image.height)

    def test_failed_rotation_verification_restores_first_images_and_ocr(self):
        self.assertTrue(hasattr(pipeline, "_prepare_oriented"))
        from PIL import Image
        source = self.source()
        first = self.ocr(1)
        result, ocr, parse = self.scan(source, [first, self.ocr(2)])
        self.assertEqual(ocr.call_count, 2)
        self.assertIs(parse.call_args.args[1], first)
        self.assertEqual(result["draft"].warnings.count("명함 방향 확인 필요"), 1)
        with Image.open(result["images"]["ocr"]) as image:
            self.assertGreater(image.width, image.height)

    def test_low_evidence_or_legacy_output_warns_without_rotation(self):
        self.assertTrue(hasattr(pipeline, "_prepare_oriented"))
        from bizcard_intake.vision_ocr import OcrLine, OcrResult
        source = self.source()
        for result in (self.ocr(1, "ab"), OcrResult((OcrLine("legacy output", 1),)), OcrResult(()), OcrResult(self.ocr(0).lines + self.ocr(1).lines)):
            with self.subTest(ocr=result):
                scanned, ocr, parse = self.scan(source, [result])
                self.assertEqual(ocr.call_count, 1)
                self.assertIn("명함 방향 확인 필요", scanned["draft"].warnings)
                self.assertIs(parse.call_args.args[1], result)

    def test_upright_front_uses_one_ocr_without_warning(self):
        self.assertTrue(hasattr(pipeline, "_prepare_oriented"))
        result, ocr, _ = self.scan(self.source(), [self.ocr()])
        self.assertEqual(ocr.call_count, 1)
        self.assertNotIn("명함 방향 확인 필요", result["draft"].warnings)

    def test_back_uses_same_orientation_path_and_preserves_warning(self):
        self.assertTrue(hasattr(pipeline, "_prepare_oriented"))
        from bizcard_intake import image_processing
        source = self.source()
        card = self.review_card(with_images=False)
        with patch.object(image_processing, "_try_vision_rectify", return_value=source), patch.object(pipeline, "_ocr", side_effect=[self.ocr(2), self.ocr(2)]) as ocr, patch.object(pipeline, "parse_card", return_value=CardDraft(name="back", title="CEO")), patch.object(pipeline, "merge_drafts", side_effect=lambda front, back: back):
            pipeline.recognize_back(card, source)
        self.assertEqual(ocr.call_count, 2)
        self.assertIn("명함 방향 확인 필요", card["back_draft"]["warnings"])
        self.assertIn("명함 방향 확인 필요", pipeline.current_draft(card).warnings)
    def test_warm_up_runs_local_ocr_once_and_swallows_failures(self):
        self.assertTrue(hasattr(pipeline, "warm_up"))
        from bizcard_intake.vision_ocr import OcrResult
        with patch.object(pipeline, "_ocr", return_value=OcrResult(())) as ocr:
            self.assertIsNone(pipeline.warm_up())
        self.assertEqual(ocr.call_count, 1)
        with patch.object(pipeline, "_ocr", side_effect=RuntimeError("warmup error")), self.assertLogs("bizcard", level="WARNING"):
            self.assertIsNone(pipeline.warm_up())

    def test_warm_up_wait_is_bounded_by_30_seconds(self):
        self.assertTrue(hasattr(pipeline, "warm_up"))
        with patch.object(pipeline.threading, "Thread") as thread, self.assertLogs("bizcard", level="WARNING"):
            thread.return_value.is_alive.return_value = True
            pipeline.warm_up()
        thread.return_value.join.assert_called_once_with(timeout=30)

    def test_rotation_ocr_failure_restores_first_result_without_a_third_ocr(self):
        from PIL import Image
        source = self.source()
        first = self.ocr(3)
        with self.assertLogs("bizcard", level="WARNING"):
            result, ocr, parse = self.scan(source, [first, RuntimeError("OCR unavailable")])
        self.assertEqual(ocr.call_count, 2)
        self.assertIs(parse.call_args.args[1], first)
        self.assertIn("명함 방향 확인 필요", result["draft"].warnings)
        with Image.open(result["images"]["ocr"]) as image:
            self.assertGreater(image.width, image.height)

    def test_empty_verification_does_not_count_as_upright_bin_zero(self):
        from bizcard_intake.vision_ocr import OcrResult
        result, ocr, _ = self.scan(self.source(), [self.ocr(2), OcrResult(())])
        self.assertEqual(ocr.call_count, 2)
        self.assertIn("명함 방향 확인 필요", result["draft"].warnings)

    def test_rotation_accepts_exact_threshold_and_rejects_below_it(self):
        from bizcard_intake.vision_ocr import OcrLine, OcrResult
        source = self.source()
        exact = OcrResult((OcrLine("abcdef", 1, (0.1, 0.1), (0.1, 0.9)), OcrLine("abcd", 1, (0.1, 0.1), (0.9, 0.1))))
        result, ocr, _ = self.scan(source, [exact, self.ocr(0)])
        self.assertEqual(ocr.call_count, 2)
        self.assertNotIn("명함 방향 확인 필요", result["draft"].warnings)
        low = OcrResult((OcrLine("abcdef", 0.98, (0.1, 0.1), (0.1, 0.9)), OcrLine("abcde", 1, (0.1, 0.1), (0.9, 0.1))))
        result, ocr, _ = self.scan(source, [low])
        self.assertEqual(ocr.call_count, 1)
        self.assertIn("명함 방향 확인 필요", result["draft"].warnings)

    def test_back_merge_keeps_front_orientation_warning_when_merge_drops_it(self):
        from bizcard_intake import image_processing
        source = self.source()
        card = self.review_card({"name": "front", "title": "CEO", "warnings": ["명함 방향 확인 필요"]}, with_images=False)
        with patch.object(image_processing, "_try_vision_rectify", return_value=source), patch.object(pipeline, "_ocr", return_value=self.ocr(0)), patch.object(pipeline, "parse_card", return_value=CardDraft(name="back", title="CEO")), patch.object(pipeline, "merge_drafts", return_value=CardDraft(name="front", title="CEO")):
            pipeline.recognize_back(card, source)
        self.assertIn("명함 방향 확인 필요", pipeline.current_draft(card).warnings)


if __name__ == "__main__":
    unittest.main()
