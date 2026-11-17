import tempfile
import io
import json
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
from urllib.error import HTTPError

from bizcard_intake import cards, chat_ux, pipeline, telegram
from bizcard_intake.contacts import SaveResult
from bizcard_intake.draft import CardDraft


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Seoul')).timestamp()


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(cards, 'CARD_DIR', Path(self.tmp.name) / 'cards'),
                  patch.object(pipeline, 'SESSION_ARCHIVE_DIR', Path(self.tmp.name) / 'archive'),
                  patch.dict('os.environ', {'BIZCARD_TZ': 'Asia/Seoul', 'BIZCARD_FOLLOWUP_DAYS': '3', 'TELEGRAM_ALLOWED_CHAT_IDS': '42'})):
            p.start()
            self.addCleanup(p.stop)
        self.card = cards.new_card(42)
        pipeline.set_draft(self.card, CardDraft.from_dict({'name': '테스트인', 'company': '예시회사', 'mobile': ['010-0000-1234']}), 'test')
        self.card['status'] = 'review'
        self.card['created_at'] = '2026-11-17T11:00:00+0900'
        cards.save_card(self.card)

    def schedule(self, value='2026-11-17T11:00:00'):
        self.assertTrue(callable(getattr(cards, 'schedule_followup', None)), 'follow-up scheduler missing')
        with patch('time.time', return_value=at(value)):
            self.card['status'] = 'saved'
            cards.schedule_followup(self.card)
            cards.save_card(self.card)
        return self.card

    def test_due_window_and_zero(self):
        for saved, expected in [('2026-11-17T09:00:00', '2026-11-20T10:00:00'),
                                ('2026-11-17T11:00:00', '2026-11-20T11:00:00'),
                                ('2026-11-17T19:00:00', '2026-11-21T10:00:00')]:
            self.card.pop('followup_state', None)
            self.schedule(saved)
            self.assertEqual(self.card['followup_due_at'], at(expected))
        self.card.pop('followup_state', None)
        with patch.dict('os.environ', {'BIZCARD_FOLLOWUP_DAYS': '0'}):
            self.schedule()
        self.assertEqual(self.card['followup_state'], 'disabled')

    def test_save_path_schedules_once(self):
        with patch.object(pipeline, 'save_contact', return_value=SaveResult('created', 'people/test')), patch('time.time', return_value=at('2026-11-17T11:00:00')):
            self.assertTrue(pipeline.approve(self.card)['ok'])
        saved = cards.load_card(self.card['id'])
        self.assertEqual(saved.get('followup_due_at'), at('2026-11-20T11:00:00'))
        self.assertTrue(pipeline.approve(saved)['already'])
        self.assertEqual(cards.load_card(self.card['id'])['followup_due_at'], saved['followup_due_at'])

    def test_due_scan_restart_and_quiet_hours(self):
        self.schedule()
        self.assertTrue(callable(getattr(telegram, 'send_due_followups', None)))
        with patch.object(telegram, 'send_followup_message', return_value={'result': {'message_id': 9}}) as send:
            for current in ('2026-11-20T09:59:00', '2026-11-20T10:59:00'):
                with patch('time.time', return_value=at(current)):
                    telegram.send_due_followups('fake')
            send.assert_not_called()
            with patch('time.time', return_value=at('2026-11-20T20:00:00')):
                telegram.send_due_followups('fake')
            send.assert_not_called()
            with patch('time.time', return_value=at('2026-11-21T10:00:00')):
                telegram.send_due_followups('fake')
                telegram.send_due_followups('fake')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(cards.load_card(self.card['id'])['followup_state'], 'notified')

    def notify(self):
        self.schedule()
        with patch.object(telegram, 'send_followup_message', return_value={'result': {'message_id': 9}}), patch('time.time', return_value=at('2026-11-20T11:00:00')):
            telegram.send_due_followups('fake')
        self.card = cards.load_card(self.card['id'])
        return self.card['followup_nonce']

    def test_nonce_snooze_stale_sent_stop_and_global_optout(self):
        self.assertTrue(callable(getattr(pipeline, 'resolve_followup', None)))
        nonce = self.notify()
        with patch('time.time', return_value=at('2026-11-20T20:00:00')):
            self.assertTrue(pipeline.resolve_followup(self.card['id'], 42, 'later', nonce)['ok'])
        saved = cards.load_card(self.card['id'])
        self.assertEqual(saved['followup_due_at'], at('2026-11-24T10:00:00'))
        self.assertFalse(pipeline.resolve_followup(self.card['id'], 42, 'off', nonce)['ok'])
        self.assertFalse(pipeline.resolve_followup(self.card['id'], 43, 'off', nonce)['ok'])
        for action, expected in [('sent', 'done'), ('stop', 'stopped'), ('off', 'disabled')]:
            saved.update(followup_state='notified', followup_nonce=action)
            cards.save_card(saved)
            self.assertTrue(pipeline.resolve_followup(saved['id'], 42, action, action)['ok'])
            saved = cards.load_card(saved['id'])
            self.assertEqual(saved['followup_state'], expected)
        self.assertFalse(cards.load_state('followup')['enabled'])
        with patch.object(telegram, 'send_followup_message') as send:
            telegram.send_due_followups('fake')
            send.assert_not_called()

    def test_pre_upgrade_cards_not_enrolled_and_start_persisted(self):
        self.assertTrue(callable(getattr(cards, 'initialize_followup', None)))
        self.card['status'] = 'saved'
        cards.save_card(self.card)
        with patch('time.time', return_value=at('2026-11-17T11:00:00')):
            first = cards.initialize_followup()
        with patch('time.time', return_value=at('2026-11-22T11:00:00')), patch.object(telegram, 'send_followup_message') as send:
            self.assertEqual(cards.initialize_followup()['feature_started_at'], first['feature_started_at'])
            telegram.send_due_followups('fake')
            send.assert_not_called()
        self.assertNotIn('followup_due_at', cards.load_card(self.card['id']))

    def test_allowlist_and_unknown_send_outcome_never_resends(self):
        self.schedule()
        with patch('time.time', return_value=at('2026-11-20T11:00:00')), patch.object(telegram, 'send_followup_message', side_effect=TimeoutError()) as send:
            with patch.dict('os.environ', {'TELEGRAM_ALLOWED_CHAT_IDS': '43'}):
                telegram.send_due_followups('fake')
            send.assert_not_called()
            telegram.send_due_followups('fake')
            telegram.send_due_followups('fake')
            self.assertEqual(send.call_count, 1)

    def test_templates_and_buttons(self):
        self.assertTrue(callable(getattr(chat_ux, 'followup_text', None)))
        self.schedule()
        self.card['followup_nonce'] = 'safe_nonce'
        korean = chat_ux.followup_text(self.card)
        self.assertIn('테스트인 예시회사 님께 연락할까요?', korean)
        self.assertIn('2026-11-17', korean)
        self.card['draft']['name'] = 'テスト'
        self.assertIn('こんにちは', chat_ux.followup_text(self.card))
        self.card['draft']['name'] = '王小明'
        self.assertIn('您好', chat_ux.followup_text(self.card))
        self.card['draft']['name'] = 'Test Name'
        self.assertIn('Hello', chat_ux.followup_text(self.card))
        buttons = telegram.followup_buttons(self.card)['inline_keyboard']
        self.assertEqual([b['text'] for row in buttons for b in row], ['보냈어요', '3일 뒤 다시', '이 명함은 그만', '알림 끄기'])

    def test_stale_card_save_cannot_restore_consumed_nonce(self):
        self.assertTrue(callable(getattr(pipeline, 'resolve_followup', None)))
        nonce = self.notify()
        stale = dict(self.card)
        pipeline.resolve_followup(self.card['id'], 42, 'sent', nonce)
        stale['message_ids'] = [7]
        cards.save_card(stale)
        stored = cards.load_card(self.card['id'])
        self.assertEqual(stored['followup_state'], 'done')
        self.assertIsNone(stored['followup_nonce'])

    def test_callback_routes_buttons_and_rejects_disallowed_chat(self):
        nonce = self.notify()
        callback = {'id': 'fixture', 'data': f"fm:{self.card['id']}:{nonce}", 'message': {'message_id': 9}}
        with patch.object(telegram, 'api_post'), patch.object(telegram, 'edit_message') as edit, patch.object(telegram, 'send_message') as send:
            telegram.handle_callback('fake', callback, 43)
            self.assertEqual(cards.load_card(self.card['id'])['followup_state'], 'notified')
            telegram.handle_callback('fake', callback, 42)
            telegram.handle_callback('fake', callback, 42)
        self.assertEqual(edit.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(cards.load_card(self.card['id'])['followup_state'], 'done')

    def test_transport_attempts_only_once(self):
        self.schedule()
        self.card['followup_nonce'] = 'nonce'
        with patch.object(telegram, '_pace'), patch.object(telegram, 'urlopen', side_effect=TimeoutError()) as network:
            with self.assertRaises(TimeoutError):
                telegram.send_followup_message('fake', self.card)
        self.assertEqual(network.call_count, 1)

    def test_rate_limited_followup_attempts_once_without_wait_or_resend(self):
        self.schedule()
        def limited(*args, **kwargs):
            raise HTTPError('https://synthetic.invalid', 429, 'rate limited', {},
                            io.BytesIO(json.dumps({'parameters': {'retry_after': 60}}).encode()))
        with patch('time.time', return_value=at('2026-11-20T11:00:00')), patch.object(telegram, '_pace'), patch.object(telegram, 'urlopen', side_effect=limited) as network, patch.object(telegram.time, 'sleep') as sleep:
            telegram.send_due_followups('fake')
            telegram.send_due_followups('fake')
        self.assertEqual(network.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(cards.load_card(self.card['id'])['followup_state'], 'sending')

    def test_blocked_followup_releases_both_locks_and_other_save_completes(self):
        # Port of the A10 HTTP/429 probe: block the first HTTP response instead
        # of retry_after sleep, since a follow-up must no longer wait on 429.
        self.schedule()
        other = cards.new_card(42)
        pipeline.set_draft(other, CardDraft(name='Synthetic', mobile=['01000001234']), 'fixture')
        other['status'] = 'review'
        cards.save_card(other)
        entered, release, done = (threading.Event() for _ in range(3))
        errors, workers = [], []
        def response(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('response release timeout')
            raise HTTPError('https://synthetic.invalid', 429, 'rate limited', {},
                            io.BytesIO(b'{"parameters":{"retry_after":60}}'))
        def scan():
            try:
                telegram.send_due_followups('fake')
            except BaseException as exc:
                errors.append(exc)
        def save():
            try:
                self.assertTrue(pipeline.approve(other)['ok'])
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        with patch('time.time', return_value=at('2026-11-20T11:00:00')), patch.object(telegram, '_pace'), patch.object(telegram, 'urlopen', side_effect=response) as network, patch.object(telegram.time, 'sleep') as sleep, patch.object(pipeline, 'save_contact', return_value=SaveResult('created', 'people/synthetic')):
            sender = threading.Thread(target=scan, daemon=True)
            workers.append(sender)
            sender.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(cards.load_card(self.card['id'])['followup_state'], 'sending')
                with pipeline.card_lock(self.card['id'], blocking=False) as acquired:
                    self.assertTrue(acquired)
                acquired = cards.followup_lock.acquire(blocking=False)
                if acquired:
                    cards.followup_lock.release()
                self.assertTrue(acquired)
                saver = threading.Thread(target=save, daemon=True)
                workers.append(saver)
                saver.start()
                self.assertTrue(done.wait(3), 'unrelated save blocked by follow-up send')
                self.assertTrue(sender.is_alive(), 'follow-up response must still be blocked')
                self.assertEqual(cards.load_card(other['id'])['status'], 'saved')
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(network.call_count, 1)
        sleep.assert_not_called()

    def test_inflight_button_result_is_not_overwritten_by_send_completion(self):
        for action, expected in [('sent', 'done'), ('later', 'pending'), ('stop', 'stopped'), ('off', 'disabled')]:
            with self.subTest(action=action):
                cards.save_state('followup', {'enabled': True})
                self.card.pop('followup_state', None)
                self.schedule()
                def send(token, card):
                    self.assertTrue(pipeline.resolve_followup(card['id'], 42, action, card['followup_nonce'])['ok'])
                    return {'result': {'message_id': 87}}
                with patch('time.time', return_value=at('2026-11-20T11:00:00')), patch.object(telegram, 'send_followup_message', side_effect=send):
                    telegram.send_due_followups('fake')
                fresh = cards.load_card(self.card['id'])
                self.assertEqual(fresh['followup_state'], expected)
                self.assertIsNone(fresh['followup_nonce'])
                self.card = fresh

    def test_scan_stops_when_delivery_crosses_quiet_hours(self):
        self.schedule()
        second = cards.new_card(42)
        second.update(status='saved', draft=self.card['draft'], followup_state='pending',
                      followup_due_at=self.card['followup_due_at'])
        cards.save_card(second)
        clock = [at('2026-11-20T18:59:59')]
        def send(token, card):
            clock[0] = at('2026-11-20T19:00:01')
            return {'result': {'message_id': 9}}
        with patch('time.time', side_effect=lambda: clock[0]), patch.object(telegram, 'send_followup_message', side_effect=send) as delivery:
            telegram.send_due_followups('fake')
        self.assertEqual(delivery.call_count, 1)
        self.assertEqual(len([c for c in cards.list_cards() if c.get('followup_state') == 'pending']), 1)

    def test_configured_zone_controls_due_and_send_window(self):
        with patch.dict('os.environ', {'BIZCARD_TZ': 'America/New_York'}):
            saved = datetime(2026, 11, 17, 9, tzinfo=ZoneInfo('America/New_York')).timestamp()
            due = datetime(2026, 11, 20, 10, tzinfo=ZoneInfo('America/New_York')).timestamp()
            self.assertEqual(cards.followup_due(saved, 3), due)
            self.schedule()
            with patch('time.time', return_value=due), patch.object(telegram, 'send_followup_message', return_value={}) as send:
                telegram.send_due_followups('fake')
                self.assertEqual(send.call_count, 1)

    def test_durable_claim_precedes_send_and_crash_cannot_resend(self):
        self.schedule()
        events = []
        import os
        real_fsync, real_replace = os.fsync, os.replace
        def sync(fd):
            events.append('fsync')
            real_fsync(fd)
        def replace(*args):
            events.append('replace')
            real_replace(*args)
        def crash(*args):
            self.assertEqual(cards.load_card(self.card['id'])['followup_state'], 'sending')
            self.assertEqual(events[-3:], ['fsync', 'replace', 'fsync'])
            raise SystemExit('simulated crash')
        with patch('time.time', return_value=at('2026-11-20T11:00:00')), patch.object(cards.os, 'fsync', side_effect=sync), patch.object(cards.os, 'replace', side_effect=replace):
            with patch.object(telegram, 'send_followup_message', side_effect=crash):
                with self.assertRaises(SystemExit):
                    telegram.send_due_followups('fake')
            with patch.object(telegram, 'send_followup_message') as send:
                telegram.send_due_followups('fake')
                send.assert_not_called()

    def test_unset_zone_uses_system_local_zone(self):
        import os, time
        old_tz = os.environ.get('TZ')
        try:
            with patch.dict(os.environ, {'BIZCARD_TZ': '', 'TZ': 'America/New_York'}):
                time.tzset()
                saved = datetime(2026, 11, 17, 9, tzinfo=ZoneInfo('America/New_York')).timestamp()
                expected = datetime(2026, 11, 20, 10, tzinfo=ZoneInfo('America/New_York')).timestamp()
                self.assertEqual(cards.followup_due(saved, 3), expected)
                self.assertEqual(cards.followup_localtime(expected).hour, 10)
        finally:
            if old_tz is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old_tz
            time.tzset()
