"""Synchronous v2.1.1 recording and temporary reply routing; synthetic I/O."""
import ast
import copy
import subprocess
import threading
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import reply_v211_reference as legacy
from git_baseline import require_tag
from test_writer_liveness import IsolatedTest
from bizcard_intake import cards, pipeline, telegram


class ReplyRecordingTest(IsolatedTest):
    def setUp(self):
        super().setUp()
        for context in (patch.object(cards, '_pending_messages', {}),
                        patch.object(telegram, 'send_message', return_value={'result': {'message_id': 999}}),
                        patch.object(telegram, 'edit_message'),
                        patch.object(pipeline, 'refresh_job_change', side_effect=lambda c: c.update(job_change=None))):
            context.start(); self.addCleanup(context.stop)

    def test_v211_recording_differential_from_git(self):
        # Execute the actual tag source in a temporary module; no reimplementation.
        require_tag(self)
        source = subprocess.check_output(['git', 'show', 'v2.1.1:bizcard_intake/cards.py'], text=True)
        reference = types.ModuleType('v211_cards')
        exec(compile(source, 'v2.1.1:bizcard_intake/cards.py', 'exec'), reference.__dict__)
        reference.CARD_DIR = self.root / 'legacy-cards'
        source = subprocess.check_output(['git', 'show', 'v2.1.1:bizcard_intake/telegram.py'], text=True)
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_remember_card_message')
        scope = {'cards': reference, 'pipeline': pipeline, 'Optional': __import__('typing').Optional}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'v2.1.1:record', 'exec'), scope)
        for initial in ([], [7, 3, 7], list(range(25))):
            current = self.card()
            current['message_ids'] = list(initial)
            cards.save_card(current)
            baseline = copy.deepcopy(current)
            reference.save_card(baseline)
            for mid in (None, 3, 3, 9, *range(30, 60), 35, 1, None):
                stale_current, stale_baseline = copy.deepcopy(current), copy.deepcopy(baseline)
                stale_current['message_ids'] = [-1]
                stale_baseline['message_ids'] = [-1]
                scope['_remember_card_message'](stale_baseline, mid)
                telegram._remember_card_message(stale_current, mid)
                self.assertEqual(cards.load_card(current['id'])['message_ids'], reference.load_card(baseline['id'])['message_ids'])
                self.assertEqual(stale_current['message_ids'], stale_baseline['message_ids'])
        # save_card itself must retain the tag's overwrite/order behavior too.
        for module in (reference, cards):
            item = module.load_card(current['id'])
            item['message_ids'] = [60, 2, 60]
            module.save_card(item)
        self.assertEqual(cards.load_card(current['id'])['message_ids'], reference.load_card(current['id'])['message_ids'])

    def test_record_runs_synchronously_in_calling_thread(self):
        card = self.card()
        writes = []
        atomic = cards._atomic_write
        def write(*args):
            writes.append(threading.get_ident())
            return atomic(*args)
        with patch.object(cards, '_atomic_write', side_effect=write):
            telegram._remember_card_message(card, 71)
            self.assertEqual(writes, [threading.get_ident()])
            self.assertIn(71, card['message_ids'])
            self.assertEqual(cards._pending_messages, {})

    def test_message_ref_pending_during_lock_then_durable_saved_target(self):
        card, other = self.card(), self.card()
        attempted = threading.Event()
        real_lock = pipeline.card_lock
        errors = []
        def observed(cid, **kwargs):
            if cid == card['id'] and kwargs.get('blocking', True):
                attempted.set()
            return real_lock(cid, **kwargs)
        def record():
            try: telegram._remember_card_message(card, 70)
            except BaseException as exc: errors.append(exc)
        with patch.object(pipeline, 'card_lock', side_effect=observed):
            with real_lock(card['id']):
                worker = threading.Thread(target=record, daemon=True)
                worker.start()
                try:
                    self.assertTrue(attempted.wait(5), repr(errors))
                    self.assertTrue(worker.is_alive())
                    self.assertEqual(cards._pending_messages[('42', 70)][0], card['id'])
                    self.assertEqual(cards.find_by_message(42, 70)['id'], card['id'])
                    self.assertIsNone(cards.find_by_message(43, 70))
                    self.assertNotIn(70, cards.load_card(card['id'])['message_ids'])
                    # Closed-state handling avoids a second record and proves MESSAGE_REF routing.
                    fresh = cards.load_card(card['id']); fresh['status'] = 'saved'; cards.save_card(fresh)
                    with patch.object(pipeline, 'edit') as edit:
                        telegram.handle_text('fixture', 42, {'text': '회사: Wrong', 'reply_to_message': {'message_id': 70}})
                    edit.assert_not_called()
                    self.assertEqual(cards.load_card(other['id'])['draft']['company'], 'New')
                finally:
                    pass  # The owning context releases the real lock before join.
            worker.join(5)
            self.assertFalse(worker.is_alive(), 'blocking record did not finish')
        self.assertEqual(errors, [])
        self.assertIn(70, cards.load_card(card['id'])['message_ids'])
        self.assertEqual(cards._pending_messages, {})
        with patch.object(pipeline, 'edit') as edit:
            telegram.handle_text('fixture', 42, {'text': '회사: Wrong', 'reply_to_message': {'message_id': 70}})
        edit.assert_not_called()

    def test_failed_record_removes_pending_and_propagates_without_retry(self):
        card = self.card()
        def fail(*args):
            self.assertEqual(cards.find_by_message(42, 74)['id'], card['id'])
            self.assertFalse(cards._pending_message_lock.locked())
            raise OSError('synthetic disk fault')
        with patch.object(cards, '_atomic_write', side_effect=fail) as write:
            with self.assertRaises(OSError): telegram._remember_card_message(card, 74)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(cards._pending_messages, {})

    def test_duplicate_pending_cleanup_preserves_latest_registration_owner(self):
        card = self.card()
        first = cards.pending_message(card, 88)
        second = cards.pending_message(card, 88)
        first_token = first.__enter__()
        second_token = second.__enter__()
        try:
            first.__exit__(None, None, None)
            found = cards.find_by_message(42, 88)
            self.assertIsNotNone(found)
            self.assertEqual(found['id'], card['id'])
            self.assertIsNot(first_token, second_token)
        finally:
            second.__exit__(None, None, None)
        self.assertIsNone(cards.find_by_message(42, 88))
        self.assertEqual(cards._pending_messages, {})

    def test_pending_mutex_released_before_lock_disk_or_logging(self):
        card = self.card()
        real_lock, atomic = pipeline.card_lock, cards._atomic_write
        def lock(*args, **kwargs):
            self.assertFalse(cards._pending_message_lock.locked())
            return real_lock(*args, **kwargs)
        def write(*args):
            self.assertFalse(cards._pending_message_lock.locked())
            return atomic(*args)
        with patch.object(pipeline, 'card_lock', side_effect=lock), patch.object(cards, '_atomic_write', side_effect=write), patch.object(telegram.log, 'warning') as warning:
            telegram._remember_card_message(card, 75)
        warning.assert_not_called()

    def test_gone_card_clears_pending_without_creating_file(self):
        card = self.card()
        path = cards.card_path(card['id'])
        path.unlink()
        telegram._remember_card_message(card, 78)
        self.assertFalse(path.exists())
        self.assertEqual(cards._pending_messages, {})

    def test_lists_and_album_summaries_do_not_add_card_references(self):
        a, b = self.card(), self.card()
        for item in (a, b):
            item['media_group_id'] = 'album'; cards.save_card(item)
        telegram.send_open_list('fixture', 42)
        telegram.send_group_summary('fixture', 42, 'album')
        self.assertIsNone(cards.find_by_message(42, 999))

    def test_draft_send_registers_before_blocking_record(self):
        card = self.card()
        card['job_change'] = None
        cards.save_card(card)
        held, release, attempted = (threading.Event() for _ in range(3))
        errors = []
        real_lock = pipeline.card_lock
        def hold():
            with real_lock(card['id']):
                held.set()
                if not release.wait(10): errors.append(AssertionError('holder release timeout'))
        holder = threading.Thread(target=hold, daemon=True)
        def send(*args, **kwargs):
            holder.start()
            self.assertTrue(held.wait(5))
            return {'result': {'message_id': 73}}
        def observed(cid, **kwargs):
            if kwargs.get('blocking', True): attempted.set()
            return real_lock(cid, **kwargs)
        def run():
            try: telegram.show_draft('fixture', 42, card)
            except BaseException as exc: errors.append(exc)
        with patch.object(telegram, 'send_message', side_effect=send), patch.object(pipeline, 'card_lock', side_effect=observed):
            worker = threading.Thread(target=run, daemon=True); worker.start()
            try:
                self.assertTrue(attempted.wait(5), repr(errors))
                self.assertEqual(cards.find_by_message(42, 73)['id'], card['id'])
                self.assertNotIn(73, cards.load_card(card['id'])['message_ids'])
            finally:
                release.set()
                worker.join(5)
                if holder.ident is not None: holder.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(holder.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(73, cards.load_card(card['id'])['message_ids'])
        self.assertEqual(cards._pending_messages, {})

    def run_joined(self, function):
        errors = []
        def run():
            try: function()
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=run, daemon=True); worker.start(); worker.join(5)
        self.assertFalse(worker.is_alive(), 'non-reentrant card lock acquired twice')
        self.assertEqual(errors, [])

    def test_edit_prompt_records_under_existing_lock_without_deadlock(self):
        card = self.card()
        self.run_joined(lambda: telegram._handle_draft_callback('fixture', 42, card, 'ed'))
        self.assertIn(999, cards.load_card(card['id'])['message_ids'])

    def test_back_prompt_records_under_existing_lock_without_deadlock(self):
        card = self.card()
        self.run_joined(lambda: telegram._handle_draft_callback('fixture', 42, card, 'bk'))
        fresh = cards.load_card(card['id'])
        self.assertIn(999, fresh['message_ids'])
        self.assertTrue(fresh['awaiting_back'])

    def test_followup_records_under_existing_lock_without_deadlock(self):
        card = self.card('saved')
        card.update(followup_state='pending', followup_due_at=1)
        cards.save_card(card)
        with patch.object(cards, 'followup_days', return_value=3), patch.object(cards, 'followup_localtime') as local, patch.object(telegram, 'allowed_chat_ids', return_value={42}), patch.object(telegram, 'send_followup_message', return_value={'result': {'message_id': 83}}):
            local.return_value.hour = 12
            self.run_joined(lambda: telegram.send_due_followups('fixture'))
        fresh = cards.load_card(card['id'])
        self.assertEqual(fresh['followup_state'], 'notified')
        self.assertIn(83, fresh['message_ids'])
        self.assertEqual(cards._pending_messages, {})

    def test_v211_differential_reply_sequences(self):
        class LegacyCards:
            find_by_message = staticmethod(legacy.find_by_message)
            def __getattr__(self, name):
                return getattr(cards, name)
        states = [[], [('review', False)], [('review', True)],
                  [('review', False), ('review', True)], [('review', True), ('review', True)],
                  [('saved', False), ('review', True)], [('cancelled', False), ('review', True)],
                  [('save_failed', True)], [('processing', False)], [('saving', False)]]
        for number, sequence in enumerate(states):
            chat = 200 + number
            for status, waiting in sequence:
                item = self.card(status)
                item.update(chat_id=str(chat), awaiting_back=waiting, message_ids=[70])
                cards.save_card(item)
            snapshots = copy.deepcopy(cards.list_cards(chat))
            for reply_id in (None, 70, 404):
                for sender in (None, False, True):
                    for album in (False, True):
                        with self.subTest(sequence=sequence, reply=reply_id, sender=sender, album=album):
                            message = {'text': '회사: Synthetic'}
                            if reply_id is not None:
                                message['reply_to_message'] = {'message_id': reply_id}
                                if sender is not None:
                                    message['reply_to_message'].update({'from': {'is_bot': sender}, 'date': 1262304000})
                            if album: message['media_group_id'] = 'synthetic'
                            outcomes = []
                            for module in (legacy, telegram):
                                edited, photos = [], []
                                def capture(fn, *args, **kwargs):
                                    if fn in (telegram.process_back_photo, telegram.process_front_photo):
                                        photos.append(('back' if fn == telegram.process_back_photo else 'front', args[4]['id']))
                                injection = {k: v for k, v in vars(telegram).items() if k not in ('handle_text', 'handle_photo', '__name__', '__builtins__')}
                                injection.update(cards=LegacyCards(), select_card=legacy.select_card, list_cards=cards.list_cards)
                                with ExitStack() as stack:
                                    for context in (patch.dict(legacy.__dict__, injection), patch.object(pipeline, 'edit', side_effect=lambda c, t: (edited.append(c['id']) or True, 'ok')), patch.object(telegram, 'submit', side_effect=capture), patch.object(legacy, 'submit', side_effect=capture), patch.object(legacy, 'show_draft'), patch.object(legacy, 'send_open_list'), patch.object(telegram, 'send_open_list'), patch.object(telegram, '_ensure_group_progress'), patch.object(legacy, '_ensure_group_progress'), patch.object(legacy, 'send_status', return_value=999), patch.object(cards, 'save_card'), patch.object(cards, 'remember_message'), patch.object(telegram, '_remember_card_message'), patch.object(legacy, '_remember_card_message'), patch.object(cards, 'load_card', side_effect=lambda cid: copy.deepcopy(next((c for c in snapshots if c['id'] == cid), None))), patch.object(cards, 'new_card', return_value={'id': 'new', 'chat_id': str(chat), 'message_ids': []})):
                                        stack.enter_context(context)
                                    selected = module.cards.find_by_message(chat, reply_id) if module == legacy else cards.find_by_message(chat, reply_id)
                                    module.handle_text('fixture', chat, message)
                                    module.handle_photo('fixture', chat, message, [{'file_id': 'fixture'}], self.root)
                                outcomes.append((selected and selected['id'], edited, photos))
                            self.assertEqual(outcomes[0], outcomes[1])
