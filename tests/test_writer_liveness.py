"""Real-process writer races and bounded Telegram lock regressions."""
import copy
import io
import inspect
import multiprocessing
import os
import signal
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import cards, cli, contacts, pipeline, telegram, writer
from bizcard_intake.draft import CardDraft

CTX = multiprocessing.get_context('fork')


def run_bot_fixture(root, cid, barrier):
    # Exercise the actual bot entry and lifetime guard; no Telegram/Contacts I/O.
    def initialize(token):
        barrier.send('ready')
        if not barrier.poll(5):
            raise AssertionError('CLI barrier timed out')
        barrier.recv()
        pipeline.approve(cards.load_card(cid))
        barrier.send('saved')
        raise KeyboardInterrupt
    def create(body):
        with (root / 'creates').open('a') as stream:
            stream.write('create\n')
        raise contacts.ContactSaveError('synthetic lost response', kind='unknown_outcome')
    with patch.object(telegram, 'allowed_chat_ids', return_value={42}), patch.object(telegram, 'initialize_contacts', side_effect=initialize), patch.object(contacts, 'create_contact', side_effect=create), patch.object(contacts, 'verify_contact', return_value=[]):
        try:
            telegram.run('fixture')
        except KeyboardInterrupt:
            pass


def run_cli_fixture(root, cid, mode, result):
    original = cards.load_card
    armed = [True]
    def paused(key):
        value = original(key)
        if armed[0] and any(frame.function == 'save_card' for frame in inspect.stack()):
            armed[0] = False
            result.send(('loaded', None))
            if not result.poll(5):
                raise AssertionError('stale-read barrier timed out')
            result.recv()
        return value
    def create(body):
        with (root / 'creates').open('a') as stream:
            stream.write('create\n')
        return {'resourceName': 'people/synthetic'}
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors, patch.object(cards, 'load_card', side_effect=paused), patch.object(contacts, 'create_contact', side_effect=create), patch.object(contacts, 'verify_contact', return_value=[]):
        code = cli.cmd_save(Path(cid)) if mode == 'save' else cli.cmd_retry_pending()
        result.send(('result', (code, errors.getvalue())))


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for context in (patch.object(cards, 'CARD_DIR', self.root/'cards'),
                        patch.object(pipeline, 'SESSION_ARCHIVE_DIR', self.root/'archive'),
                        patch.object(pipeline, 'SESSION_DIR', self.root/'sessions'),
                        patch.object(pipeline, 'PENDING_CONTACT_DIR', self.root/'pending'),
                        patch.object(contacts, 'run_gws', side_effect=AssertionError('Unexpected Contacts I/O')),
                        patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND': 'google', 'BIZCARD_FOLLOWUP_DAYS': '0'})):
            context.start(); self.addCleanup(context.stop)

    def card(self, status='review'):
        card = cards.new_card(42)
        pipeline.set_draft(card, CardDraft(name='Synthetic', company='New', mobile=['01000001234']), 'fixture')
        card.update(status=status, job_choice='new', job_change={'resource_name': 'people/old', 'container': None}, approved_revision=card['revision'])
        cards.save_card(card)
        return card

    def process(self, target, args):
        child = CTX.Process(target=target, args=args)
        child.start()
        def stop():
            if child.is_alive():
                child.kill()
            child.join(3)
        self.addCleanup(stop)
        return child


class WriterProcessTest(IsolatedTest):
    def test_cli_save_and_retry_are_refused_while_bot_holds_writer(self):
        for mode in ('save', 'retry-pending'):
            with self.subTest(mode=mode):
                counter = self.root/'creates'
                counter.write_text('')
                card = self.card('review' if mode == 'save' else 'save_failed')
                parent, child = CTX.Pipe()
                bot = self.process(run_bot_fixture, (self.root, card['id'], child))
                self.assertTrue(parent.poll(5), 'bot startup timed out')
                self.assertEqual(parent.recv(), 'ready')
                recv, send = CTX.Pipe()
                command = self.process(run_cli_fixture, (self.root, card['id'], mode, send))
                self.assertTrue(recv.poll(5), 'CLI blocked on writer lock')
                event, payload = recv.recv()
                parent.send('release')
                self.assertTrue(parent.poll(5), 'bot save timed out')
                self.assertEqual(parent.recv(), 'saved')
                if event == 'loaded':
                    # A3 repro: the unguarded CLI read before the bot persisted
                    # intent. Resume its stale read-compare-write after the bot.
                    recv.send('release')
                    self.assertTrue(recv.poll(5), 'CLI save timed out')
                    event, payload = recv.recv()
                self.assertEqual(event, 'result')
                code, message = payload
                command.join(3)
                self.assertEqual(command.exitcode, 0)
                bot.join(5)
                self.assertEqual(bot.exitcode, 0)
                self.assertEqual(len(counter.read_text().splitlines()), 1, 'create_calls must equal 1')
                self.assertNotEqual(code, 0)
                self.assertIn('봇이 실행 중입니다', message)

    def test_sigkill_releases_writer_and_second_bot_is_refused(self):
        card = self.card()
        parent, child = CTX.Pipe()
        bot = self.process(run_bot_fixture, (self.root, card['id'], child))
        self.assertTrue(parent.poll(5)); parent.recv()
        with patch.object(telegram, 'initialize_contacts') as initialize, redirect_stderr(io.StringIO()) as errors:
            # A duplicate must fail before initialization or polling.
            with patch.object(telegram, 'allowed_chat_ids', return_value=set()), patch.object(writer, 'BOT_WAIT_SECONDS', .2):
                try:
                    result = telegram.run('fixture')
                except SystemExit:
                    result = None
            self.assertEqual(result, 1)
            initialize.assert_not_called()
            self.assertIn('봇이 실행 중입니다', errors.getvalue())
        os.kill(bot.pid, signal.SIGKILL)
        bot.join(3)
        self.assertEqual(bot.exitcode, -signal.SIGKILL)
        with patch.object(pipeline, 'approve', return_value={'ok': True, 'message': 'fixture'}), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_save(Path(card['id'])), 0)
        self.assertTrue((self.root/'.writer.lock').exists())

    def test_mutating_commands_refuse_and_read_only_commands_remain_available(self):
        card = self.card()
        parent, child = CTX.Pipe()
        bot = self.process(run_bot_fixture, (self.root, card['id'], child))
        self.assertTrue(parent.poll(5)); parent.recv()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch.object(pipeline, 'migrate_legacy') as migrate, patch.object(cli.migrate_names, 'plan_migration') as plan, patch.object(cli.migrate_names, 'rollback') as rollback, patch.object(cli, '_load_project_settings', return_value=True), patch.object(cli.doctor, 'run_checks', return_value=[]):
            commands = (lambda: cli.cmd_scan(self.root/'missing.jpg'),
                        lambda: cli.cmd_save(Path(card['id'])), cli.cmd_retry_pending,
                        lambda: cli.cmd_migrate_names(True, None, None),
                        lambda: cli.cmd_migrate_names(False, None, None),
                        lambda: cli.cmd_rollback_names(self.root/'backup.json'))
            for command in commands:
                self.assertEqual(command(), 1)
            self.assertEqual(cli.cmd_list(), 0)
            self.assertEqual(cli.cmd_doctor(), 0)
            migrate.assert_not_called(); plan.assert_not_called(); rollback.assert_not_called()
        bot.kill(); bot.join(3)


class LivenessTest(IsolatedTest):
    def test_recognition_releases_lock_and_honors_cancel_at_finish(self):
        for side in ('front', 'back'):
            with self.subTest(side=side):
                card = self.card()
                entered, release = threading.Event(), threading.Event()
                def slow(*args):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('recognition barrier timed out')
                    raise ValueError('synthetic recognition failure')
                target = pipeline.recognize_front if side == 'front' else pipeline.recognize_back
                with patch.object(pipeline, 'scan_front' if side == 'front' else '_prepare_oriented', side_effect=slow):
                    worker = threading.Thread(target=target, args=(card, self.root/'front.jpg'), daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        with pipeline.card_lock(card['id'], blocking=False) as acquired:
                            self.assertTrue(acquired, 'OCR/LLM must not hold card lock')
                        with patch.object(telegram, 'send_message', return_value={'result': {'message_id': 90}}) as send, patch.object(telegram, 'api_post'):
                            telegram.handle_callback('fixture', {'data': 'cx:'+card['id']}, 42)
                            self.assertIn('인식이 끝나면 취소합니다', send.call_args.args[2])
                    finally:
                        release.set(); worker.join(3)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(cards.load_card(card['id'])['status'], 'cancelled')

    def test_update_loop_only_message_recording_waits_for_card_lock(self):
        for action in ('cx', 'ed', 'bk', 'rs', 'text', 'fm', 'fl', 'fx', 'fo', 'list', 'ap', 'sv', 'ju', 'jn', 'cs', 'cr', 'ck', 'photo'):
            with self.subTest(action=action):
                card, other = self.card(), self.card()
                card['message_ids'] = [10]
                if action == 'list':
                    card['status'] = 'needs_confirmation'
                elif action in ('cx', 'photo'):
                    card.update(status='processing', recognition_id='fixture', awaiting_back=True)
                cards.save_card(card)
                first = {'update_id': 1, 'callback_query': {'id': 'fixture', 'data': action+':'+card['id'], 'message': {'chat': {'id': 42}, 'message_id': 10}}}
                if action in ('text', 'list'):
                    first = {'update_id': 1, 'message': {'chat': {'id': 42}, 'text': '/list' if action == 'list' else '회사: Edited', 'reply_to_message': {'message_id': 10}}}
                if action == 'photo':
                    first = {'update_id': 1, 'message': {'chat': {'id': 42}, 'photo': [{'file_id': 'fixture'}], 'reply_to_message': {'message_id': 10}}}
                updates = [first, {'update_id': 2, 'message': {'chat': {'id': 43}, 'text': '/help'}}, {'update_id': 3, 'callback_query': {'data': 'ed:'+other['id'], 'message': {'chat': {'id': 42}}}}]
                messages, done = [], threading.Event()
                violations, loop_thread = [], []
                recording = threading.Event()
                original_lock = pipeline.card_lock
                @contextmanager
                def try_only(cid, *, blocking=True, timeout=-1):
                    if loop_thread and threading.get_ident() == loop_thread[0] and blocking:
                        if any(frame.function == '_remember_card_message' for frame in inspect.stack()):
                            recording.set()
                        else:
                            violations.append(cid)
                    with original_lock(cid, blocking=blocking, timeout=timeout) as acquired:
                        yield acquired
                def send(token, chat, text, **kwargs):
                    messages.append((chat, text))
                    return {'result': {'message_id': 99}}
                def loop():
                    loop_thread.append(threading.get_ident())
                    try:
                        telegram.handle_updates('fixture', updates, self.root)
                    finally:
                        done.set()
                with patch.object(pipeline, 'save_contact', return_value=contacts.SaveResult('created', 'people/fixture')), patch.object(telegram, 'send_message', side_effect=send), patch.object(telegram, 'edit_message'), patch.object(telegram, 'api_post'), patch.object(telegram, 'allowed_chat_ids', return_value={42,43}), patch.object(telegram, 'submit', side_effect=lambda fn, *args: fn(*args)):
                    with pipeline.card_lock(card['id']), patch.object(pipeline, 'card_lock', side_effect=try_only):
                        worker = threading.Thread(target=loop, daemon=True)
                        worker.start()
                        if action in ('text', 'ap', 'sv'):
                            self.assertTrue(recording.wait(5), action+' did not reach synchronous recording')
                            self.assertFalse(done.is_set(), action+' must wait for message recording')
                        else:
                            timely = done.wait(0.8)
                    worker.join(3)
                    self.assertFalse(worker.is_alive(), action+' did not finish after lock release')
                    if action not in ('text', 'ap', 'sv'):
                        self.assertTrue(timely, action+' blocked the update loop')
                    self.assertEqual(violations, [], action+' requested a blocking card lock')
                    self.assertTrue(any(chat == 43 for chat, text in messages))
                    self.assertTrue(any(cards.label(other) in text for chat, text in messages))
                    if action == 'cx':
                        self.assertTrue(pipeline.cancel_path(card['id']).exists())
                        self.assertTrue(any('인식이 끝나면 취소합니다' in text for chat, text in messages))

    def test_processing_card_actions_reply_busy_without_slow_work(self):
        for status in ('processing', 'saving'):
            card = self.card(status)
            with patch.object(pipeline, 'save_contact') as save, patch.object(pipeline, 'revise_draft') as revise:
                self.assertEqual(pipeline.approve(card)['message'], pipeline.BUSY_MESSAGE)
                self.assertEqual(pipeline.edit(card, '회사: Edited')[1], pipeline.BUSY_MESSAGE)
                save.assert_not_called(); revise.assert_not_called()
            card['message_ids'] = [10]
            cards.save_card(card)
            with patch.object(telegram, 'send_message', return_value={'result': {'message_id': 1}}) as send, patch.object(telegram, 'submit') as submit:
                telegram.handle_photo('fixture', 42, {'reply_to_message': {'message_id': 10}}, [{'file_id': 'fixture'}], self.root)
                # v2.1.1 treats a reply photo without awaiting_back as a new front.
                self.assertIn('처리 중: 이미지 보정', send.call_args_list[0].args[2])
                self.assertIs(submit.call_args.args[0], telegram.process_front_photo)

    def test_recognition_merges_only_its_fields_and_rejects_changed_state(self):
        for change in ('metadata', 'revision', 'saved', 'cancelled'):
            with self.subTest(change=change):
                card = self.card()
                def scan(path):
                    fresh = cards.load_card(card['id'])
                    if change == 'metadata':
                        cards.remember_message(fresh, 777)
                    elif change == 'revision':
                        pipeline.set_draft(fresh, CardDraft(name='Newer'), 'newer')
                    else:
                        fresh['status'] = change
                    cards.save_card(fresh)
                    return {'images': {'front': str(path)}, 'rectifier': 'fixture', 'draft': CardDraft(name='Recognized')}
                with patch.object(pipeline, 'scan_front', side_effect=scan):
                    pipeline.recognize_front(card, self.root/'front.jpg')
                fresh = cards.load_card(card['id'])
                if change == 'metadata':
                    self.assertEqual(fresh['message_ids'], [777])
                    self.assertEqual(fresh['draft']['name'], 'Recognized')
                    self.assertIsNone(fresh['job_change'])
                    self.assertNotIn('job_choice', fresh)
                elif change == 'revision':
                    self.assertEqual(fresh['draft']['name'], 'Newer')
                else:
                    self.assertEqual(fresh['status'], change)
                    self.assertEqual(fresh['draft']['name'], 'Synthetic')

    def test_recognition_finally_honors_cancel_on_interruption(self):
        card = self.card()
        def interrupted(path):
            pipeline.request_cancel(card)
            raise KeyboardInterrupt
        with patch.object(pipeline, 'scan_front', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                pipeline.recognize_front(card, self.root/'front.jpg')
        self.assertEqual(cards.load_card(card['id'])['status'], 'cancelled')
        self.assertFalse(pipeline.cancel_path(card['id']).exists())

    def test_cancel_marker_racing_with_recognition_finish_is_consumed(self):
        card = self.card()
        pipeline.request_cancel(card)
        pipeline.finish_cancel_request(card['id'])
        self.assertEqual(cards.load_card(card['id'])['status'], 'cancelled')
        self.assertFalse(pipeline.cancel_path(card['id']).exists())

    def test_restart_honors_durable_cancel_request(self):
        card = self.card('processing')
        path = self.root/'cancel'/card['id']
        path.parent.mkdir()
        path.write_text('cancel_requested')
        telegram.recover_interrupted()
        self.assertEqual(cards.load_card(card['id'])['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
