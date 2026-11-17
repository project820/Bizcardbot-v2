"""A4 lost lookup, restart overlap and callback acknowledgement regressions."""
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from unittest.mock import patch

from test_writer_liveness import IsolatedTest
from bizcard_intake import apple_contacts, cards, cli, doctor, pipeline, telegram, writer


class LookupCompletionTest(IsolatedTest):
    def setUp(self):
        super().setUp()
        for context in (patch.object(apple_contacts, 'run_helper', side_effect=AssertionError('Unexpected helper I/O')),
                        patch.object(telegram, 'api_post', return_value={}),
                        patch.object(telegram, 'edit_message')):
            context.start(); self.addCleanup(context.stop)

    def test_lost_lookup_ed_and_bk_collisions(self):
        for action in ('ed', 'bk'):
            with self.subTest(action=action):
                card = self.card()
                card['job_change'] = None
                cards.save_card(card)
                held, release, looked_up, finished = (threading.Event() for _ in range(4))
                errors, messages = [], []
                callback_thread = None
                def send(token, chat, text, **kwargs):
                    messages.append(chat)
                    if threading.current_thread() is callback_thread:
                        held.set()
                        if not release.wait(3):
                            raise AssertionError('callback barrier timed out')
                    return {'result': {'message_id': 90}}
                def lookup(candidate):
                    callback_thread.start()
                    self.assertTrue(held.wait(1))
                    candidate['job_change'] = {'resource_name': 'people/matched', 'company': 'Old', 'title': 'Old'}
                    looked_up.set()
                def work():
                    try:
                        telegram.show_draft('fixture', 42, card)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        finished.set()
                callback_thread = threading.Thread(target=telegram._handle_draft_callback,
                    args=('fixture', 42, dict(card), action))
                with patch.object(telegram, 'send_message', side_effect=send), patch.object(pipeline, 'refresh_job_change', side_effect=lookup), patch.object(telegram, 'allowed_chat_ids', return_value={42,43}):
                    worker = threading.Thread(target=work)
                    worker.start()
                    try:
                        self.assertTrue(looked_up.wait(1))
                        # Let the completion collide, but keep another chat flowing.
                        finished.wait(.15)
                        telegram.handle_updates('fixture', [{'update_id': 8, 'message': {'chat': {'id': 43}, 'text': '/help'}}], self.root)
                        self.assertIn(43, messages)
                    finally:
                        release.set()
                        if callback_thread.ident is not None:
                            callback_thread.join(3)
                        worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                fresh = cards.load_card(card['id'])
                self.assertFalse(fresh.get('job_change_pending'), 'LOST_LOOKUP')
                self.assertEqual(fresh['job_change']['resource_name'], 'people/matched')
                labels = [b['text'] for row in telegram.draft_buttons(fresh)['inline_keyboard'] for b in row]
                self.assertIn('기존 연락처 갱신', labels)

    def test_stale_pending_approval_falls_back_to_normal_save(self):
        card = self.card()
        card.update(job_change_pending=True, job_change_started_at=time.time()-21)
        cards.save_card(card)
        with patch.object(pipeline, '_save', return_value={'ok': True}) as save:
            self.assertTrue(pipeline.approve(card)['ok'])
            save.assert_called_once()
        self.assertFalse(cards.load_card(card['id'])['job_change_pending'])
        self.assertIsNone(card['job_change'])

    def test_confirmation_ack_precedes_work(self):
        card = self.card('needs_confirmation')
        for action in ('cs', 'cr', 'ck'):
            events = []
            with patch.object(telegram, 'api_post', side_effect=lambda token, method, data: events.append(method)), patch.object(telegram, 'submit', side_effect=lambda *a: events.append('work')):
                telegram.handle_callback('fixture', {'id': 'callback', 'data': action+':'+card['id']+':nonce'}, 42)
            self.assertEqual(events, ['answerCallbackQuery', 'work'])

    def test_timeout_restores_save_without_stale_callback_resurrecting_pending(self):
        card = self.card()
        card.update(job_change=None)
        cards.save_card(card)
        held, release = threading.Event(), threading.Event()
        stale = []
        def holder():
            with pipeline.card_lock(card['id']):
                stale.append(cards.load_card(card['id']))
                held.set()
                if not release.wait(3):
                    raise AssertionError('timeout fallback barrier')
                cards.remember_message(stale[0], 456)
                cards.save_card(stale[0])
        holder_thread = threading.Thread(target=holder)
        def lookup(candidate):
            candidate['job_change'] = {'resource_name': 'people/old'}
            holder_thread.start()
            self.assertTrue(held.wait(1))
        with patch.object(telegram, 'LOOKUP_APPLY_WAIT_SECONDS', .1), patch.object(pipeline, 'refresh_job_change', side_effect=lookup), patch.object(telegram, 'send_message', return_value={'result': {'message_id': 10}}), patch.object(telegram, 'edit_message') as edit:
            try:
                with self.assertLogs('bizcard', level='WARNING') as logs:
                    telegram.show_draft('fixture', 42, card)
                self.assertIn('job-change completion lock timeout; using normal save', logs.output[0])
                fresh = cards.load_card(card['id'])
                self.assertFalse(fresh['job_change_pending'])
                self.assertIsNone(fresh['job_change'])
                self.assertNotIn('job_choice', fresh)
                self.assertEqual(edit.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['text'], '저장')
            finally:
                release.set(); holder_thread.join(3)
        fresh = cards.load_card(card['id'])
        self.assertFalse(fresh['job_change_pending'])
        self.assertIsNone(fresh['job_change'])
        self.assertIn(456, fresh['message_ids'])

    def test_completion_does_not_overwrite_new_revision_state_or_lookup(self):
        for change in ('revision', 'new_lookup', 'saved', 'cancelled', 'intent'):
            with self.subTest(change=change):
                card = self.card()
                card['job_change'] = None
                cards.save_card(card)
                expected = []
                def lookup(candidate):
                    fresh = cards.load_card(card['id'])
                    if change in ('revision', 'new_lookup'):
                        pipeline.set_draft(fresh, pipeline.current_draft(fresh), 'new revision')
                        if change == 'new_lookup':
                            fresh.update(job_change_pending=True, job_change_lookup_id='new-owner', job_change_started_at=time.time())
                    elif change == 'intent':
                        fresh['save_intent'] = {'backend': 'google', 'op': 'create'}
                    else:
                        fresh['status'] = change
                    cards.save_card(fresh)
                    expected.append(fresh)
                    candidate['job_change'] = {'resource_name': 'people/old'}
                with patch.object(pipeline, 'refresh_job_change', side_effect=lookup), patch.object(telegram, 'send_message', return_value={'result': {'message_id': 10}}):
                    telegram.show_draft('fixture', 42, card)
                self.assertEqual(cards.load_card(card['id']), expected[0])

    def test_lookup_runs_on_worker_and_other_chat_flows(self):
        card = self.card()
        card.update(job_change=None, message_ids=[10])
        cards.save_card(card)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        identities, chats = [], []
        def lookup(candidate):
            identities.append(threading.get_ident())
            started.set()
            if not release.wait(3):
                raise AssertionError('lookup worker barrier')
            finished.set()
        def edit(card, text):
            return True, 'fixture'
        def send(token, chat, text, **kwargs):
            chats.append(chat)
            return {'result': {'message_id': 10}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(telegram, '_executor', pool), patch.object(pipeline, 'refresh_job_change', side_effect=lookup), patch.object(pipeline, 'edit', side_effect=edit), patch.object(telegram, 'send_message', side_effect=send), patch.object(telegram, 'allowed_chat_ids', return_value={42,43}):
                try:
                    telegram.handle_updates('fixture', [{'update_id': 1, 'message': {'chat': {'id': 42}, 'text': '회사: New', 'reply_to_message': {'message_id': 10}}}], self.root)
                    self.assertTrue(started.wait(1))
                    telegram.handle_updates('fixture', [{'update_id': 2, 'message': {'chat': {'id': 43}, 'text': '/help'}}], self.root)
                    self.assertIn(43, chats)
                    self.assertNotEqual(identities[0], threading.get_ident())
                finally:
                    release.set()
                    pool.shutdown(wait=True)
        self.assertTrue(finished.is_set())
        self.assertFalse(cards.load_card(card['id'])['job_change_pending'])

    def test_restart_clears_lookup_ownership_and_stale_pending(self):
        card = self.card()
        card.update(job_change=None, job_change_pending=True, job_change_lookup_id='old', job_change_started_at=time.time()-60)
        cards.save_card(card)
        telegram.recover_interrupted()
        fresh = cards.load_card(card['id'])
        self.assertFalse(fresh['job_change_pending'])
        self.assertNotIn('job_change_lookup_id', fresh)
        self.assertNotIn('job_change_started_at', fresh)
        self.assertEqual(telegram.draft_buttons(fresh)['inline_keyboard'][0][0]['text'], '저장')

    def test_legacy_pending_timestamp_and_stale_buttons(self):
        card = self.card()
        card.update(job_change_pending=True, job_change_started_at=100.0)
        with patch.object(pipeline.time, 'time', return_value=119.999):
            self.assertTrue(pipeline.job_change_pending_active(card))
        with patch.object(pipeline.time, 'time', return_value=120.0):
            self.assertFalse(pipeline.job_change_pending_active(card))
            self.assertEqual(telegram.draft_buttons(card)['inline_keyboard'][0][0]['text'], '저장')
        card.pop('job_change_started_at')
        self.assertFalse(pipeline.job_change_pending_active(card))
        card['updated_at'] = '2000-01-01T00:00:00+0000'
        self.assertFalse(pipeline.job_change_pending_active(card))


class StartupWaitTest(IsolatedTest):
    def test_bot_waits_for_exiting_writer_before_warmup(self):
        held, release = threading.Event(), threading.Event()
        def old_bot():
            with writer.lock():
                held.set(); release.wait(3)
        thread = threading.Thread(target=old_bot)
        thread.start(); self.assertTrue(held.wait(1))
        calls = []
        timer = threading.Timer(.15, release.set)
        timer.start()
        try:
            with patch.object(pipeline, 'warm_up', side_effect=lambda: calls.append('warm')), patch.object(telegram, 'main', return_value=0):
                self.assertEqual(cli.cmd_bot(), 0)
            self.assertEqual(calls, ['warm'])
        finally:
            release.set(); thread.join(3); timer.join(3)

    def test_doctor_reports_lock_without_creating_or_changing_files(self):
        path = self.root/'.writer.lock'
        check = doctor.check_writer()
        self.assertTrue(check.ok)
        self.assertIn('비어', check.detail)
        self.assertFalse(path.exists())
        with writer.lock():
            before = path.stat()
            check = doctor.check_writer()
            self.assertTrue(check.ok)
            self.assertIn('사용 중', check.detail)
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertIn('비어', doctor.check_writer().detail)

    def test_bot_timeout_is_bounded_and_keeps_korean_failure(self):
        held, release = threading.Event(), threading.Event()
        def holder():
            with writer.lock():
                held.set(); release.wait(3)
        thread = threading.Thread(target=holder)
        thread.start(); self.assertTrue(held.wait(1))
        try:
            for start in (cli.cmd_bot, lambda: telegram.run('fixture')):
                with patch.object(writer, 'BOT_WAIT_SECONDS', .15), patch.object(pipeline, 'warm_up') as warm, patch.object(telegram, '_run') as run, redirect_stderr(io.StringIO()) as errors:
                    begin = time.monotonic()
                    self.assertEqual(start(), 1)
                    elapsed = time.monotonic()-begin
                    self.assertGreaterEqual(elapsed, .13)
                    self.assertLess(elapsed, .8)
                    warm.assert_not_called(); run.assert_not_called()
                    self.assertIn('봇이 실행 중입니다', errors.getvalue())
        finally:
            release.set(); thread.join(3)
