"""Fault injection at the native boundary; no address book or network access."""
import copy
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from bizcard_intake import apple_contacts as apple, cards, contacts, pipeline, telegram
from bizcard_intake.draft import CardDraft


class FakeStore:
    def __init__(self, test, card, op, fault):
        self.test, self.card, self.op, self.fault = test, card, op, fault
        self.records = {'target'} if op == 'update' else {'before-a', 'before-b'}
        self.baseline = sorted(self.records)
        self.writes = []
        self.hidden = False
        self.first = True
        self.original = None

    def __call__(self, command, **payload):
        t = self.test
        if command in ('find', 'find-fingerprint'):
            t.assertEqual(payload['container'], 'original')
            return {'identifiers': self.baseline if self.hidden else sorted(self.records)}
        if command == 'get':
            if payload['identifier'] not in self.records:
                raise contacts.ContactSaveError('gone', kind='not_found')
            return {'identifier': payload['identifier'], 'person': {
                'names': [{'givenName': 'Synthetic'}],
                'phoneNumbers': [{'value': '01000000000'}, {'value': '020000000'}],
                'emailAddresses': [{'value': 'keep@example.test'}, {'value': 'new@example.test'}]}}
        if command in ('create', 'update'):
            persisted = cards.load_card(self.card['id'])['save_intent']
            t.assertEqual(persisted.get('op'), self.op)
            t.assertEqual(persisted['backend'], 'apple')
            t.assertEqual(persisted['container'], 'original')
            t.assertEqual(payload.get('container'), 'original')
            t.assertGreaterEqual(persisted.get('generation', 0), 1)
            if self.original is None:
                self.original = copy.deepcopy(persisted)
                t.assertEqual(persisted['identifiers'], self.baseline)
            self.writes.append((command, copy.deepcopy(payload), persisted['generation']))
            if command == 'update':
                t.assertEqual(persisted['target_identifier'], 'target')
                t.assertIn(payload['identifier'], self.records)
                t.assertTrue(payload.get('unionOnly'))
                identifier = payload['identifier']
            else:
                identifier = 'created-' + str(len(self.writes))
            if self.first:
                self.first = False
                if self.fault != 'before':
                    self.records.add(identifier)
                if self.fault == 'shift':
                    self.records.discard(identifier)
                    self.records.add('shifted')
                if self.fault == 'ambiguous':
                    self.records.discard(identifier)
                    self.records.update(('new-a', 'new-b'))
                if self.fault == 'delayed':
                    self.hidden = True
                    if command == 'update':
                        self.records.discard(identifier)
                        self.records.add('shifted')
                        self.baseline = []
                raise contacts.ContactSaveError('synthetic lost response', kind='unknown_outcome')
            self.records.add(identifier)
            return {'identifier': identifier}
        raise AssertionError(command)


class SaveOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for item in (patch.object(cards, 'CARD_DIR', Path(self.tmp.name)/'cards'),
                     patch.object(pipeline, 'SESSION_ARCHIVE_DIR', Path(self.tmp.name)/'archive'),
                     patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND': 'apple',
                         'BIZCARD_APPLE_CONTAINER': 'original', 'TELEGRAM_ALLOWED_CHAT_IDS': '42'}, clear=True)):
            item.start()
            self.addCleanup(item.stop)

    def card(self):
        card = cards.new_card(42)
        pipeline.set_draft(card, CardDraft(name='Synthetic', mobile=['01000000000'],
            fax=['020000000'], email=['new@example.test']), 'test')
        card['status'] = 'review'
        cards.save_card(card)
        return card

    def test_fault_matrix_pins_write_ahead_and_never_automatically_recreates(self):
        for op in ('create', 'update'):
            for fault in ('before', 'lost', 'delayed', 'shift', 'ambiguous'):
                for restart in (False, True):
                    with self.subTest(op=op, fault=fault, restart=restart):
                        card = self.card()
                        fake = FakeStore(self, card, op, fault)
                        with patch.object(apple, 'run_helper', side_effect=fake), patch.object(
                            contacts, 'save_contact', side_effect=AssertionError('Google write forbidden')):
                            pipeline.approve(card)
                            original = cards.load_card(card['id']).get('save_intent')
                            self.assertIsNotNone(original)
                            self.assertEqual(original.get('op'), op)
                            if restart:
                                saved = cards.load_card(card['id'])
                                saved['status'] = 'saving'
                                cards.save_card(saved)
                                telegram.recover_interrupted()
                            with patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND': 'google',
                                                        'BIZCARD_APPLE_CONTAINER': 'other'}):
                                pipeline.retry_due(force=True)
                                final = cards.load_card(card['id'])
                                self.assertEqual(final['save_intent'], original)
                                self.assertTrue(all(w[1].get('container') == 'original' for w in fake.writes))
                                self.assertGreaterEqual(len(fake.writes), 1)
                                self.assertLessEqual(sum(w[0] == 'create' for w in fake.writes), 1)
                                if (op == 'create' and fault in ('before', 'delayed', 'ambiguous')) or (op == 'update' and fault in ('delayed', 'ambiguous')):
                                    self.assertEqual(final['status'], 'needs_confirmation')
                                    self.assertTrue(final.get('confirmation_id'))
                                    fake.hidden = False
                                    pipeline.retry_due(force=True)
                                    self.assertEqual(cards.load_card(card['id'])['status'], 'needs_confirmation')
                                else:
                                    self.assertEqual(final['status'], 'saved')
                        final['status'] = 'cancelled'
                        cards.save_card(final)

    def test_double_and_stale_taps_across_reentry_for_both_operations(self):
        for op in ('create', 'update'):
            card = self.card()
            fake = FakeStore(self, card, op, 'delayed')
            with patch.object(apple, 'run_helper', side_effect=fake), patch.object(
                contacts, 'save_contact', side_effect=AssertionError('Google forbidden')):
                pipeline.approve(card)
                pipeline.retry_due(force=True)
                card = cards.load_card(card['id'])
                nonce = card.get('confirmation_id')
                self.assertTrue(nonce)
                buttons = telegram.saved_failed_buttons(card)['inline_keyboard'][0]
                self.assertTrue(all(len(b['callback_data'].encode()) <= 64 for b in buttons))
                fake.first = True
                fake.hidden = False
                if op == 'update':
                    fake.records = {'target'}
                with patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND': 'google', 'BIZCARD_APPLE_CONTAINER': 'other'}):
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        outcomes = list(pool.map(lambda _: pipeline.resolve_confirmation(copy.deepcopy(card), 'again', nonce), range(8)))
                    self.assertEqual(sum(not (o.get('stale') or o.get('busy')) for o in outcomes), 1)
                    pipeline.retry_due(force=True)
                    fresh = cards.load_card(card['id'])
                    self.assertEqual(fresh['status'], 'needs_confirmation')
                    self.assertNotEqual(fresh['confirmation_id'], nonce)
                    count = len(fake.writes)
                    outcome = pipeline.resolve_confirmation(copy.deepcopy(card), 'again', nonce)
                    self.assertTrue(outcome.get('stale'))
                    self.assertEqual(len(fake.writes), count)
                    self.assertLessEqual(sum(w[0] == 'create' for w in fake.writes), 2)
                    self.assertEqual(fresh['save_intent']['generation'], 2)
                    callback = {'id': 'synthetic', 'data': buttons[1]['callback_data'],
                                'message': {'message_id': 77, 'chat': {'id': 42}}}
                    with patch.object(telegram, 'api_post') as api, patch.object(telegram, 'send_message'):
                        telegram.handle_callback('token', callback, 42)
                    answers = [c.args[2] for c in api.call_args_list if c.args[1] == 'answerCallbackQuery']
                    self.assertEqual(answers, [{'callback_query_id': 'synthetic'}])
                    edits = [c.args[2] for c in api.call_args_list if c.args[1] == 'editMessageReplyMarkup']
                    self.assertTrue(any(p['message_id'] == 77 and p['reply_markup'] == {'inline_keyboard': []} for p in edits))
            fresh['status'] = 'cancelled'
            cards.save_card(fresh)

    def test_update_merge_is_pure_union_and_idempotent(self):
        draft = CardDraft(name='New Name', company='New Org', title='New Role', mobile=['01000000000'],
                          email=['new@example.test'], address=['New address'])
        old = {'resourceName': 'apple:target', 'names': [{'givenName': 'Old', 'familyName': 'Name'}],
               'organizations': [{'name': 'Old Org', 'title': 'Old Role', 'department': 'Keep'}],
               'phoneNumbers': [{'value': '020000000', 'type': 'custom'}],
               'emailAddresses': [{'value': 'old@example.test'}],
               'addresses': [{'formattedValue': 'Old address'}]}
        merged = apple.build_update(old, draft)
        for key, values in old.items():
            if not isinstance(values, list):
                continue
            for value in values:
                self.assertIn(value, merged[key], key)
        self.assertEqual(apple.build_update(merged, draft), merged)

    def test_v200_file_without_operation_keeps_google_dispatch_and_retry(self):
        card = self.card()
        # Explicit old on-disk JSON, with no Apple or confirmation keys.
        old = {k: v for k, v in card.items() if not k.startswith(('save_intent', 'confirmation'))}
        cards.card_path(card['id']).write_text(json.dumps(old))
        with patch.dict(os.environ, {}, clear=True), patch.object(contacts, 'save_contact', side_effect=[
            contacts.ContactSaveError('lost', kind='unknown_outcome'), contacts.SaveResult('created', 'people/synthetic')]) as google, patch.object(apple, 'run_helper', side_effect=AssertionError('Apple forbidden')):
            pipeline.approve(cards.load_card(card['id']))
            self.assertEqual(pipeline.retry_due(force=True)[0], 1)
        self.assertEqual(google.call_count, 2)
        self.assertTrue(all(set(c.kwargs) == {'resource_name', 'checkpoint'} for c in google.call_args_list))
        self.assertNotIn('save_intent', cards.load_card(card['id']))
        self.assertNotIn('_apple_state_version', cards.load_card(card['id']))

    def test_timeout_kills_and_reaps_helper_before_reconcile(self):
        helper = Path(self.tmp.name)/'slow-helper'
        helper.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n')
        helper.chmod(0o700)
        children = []
        original_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child
        for command in ('create', 'update'):
            with patch.object(apple, 'ensure_helper', return_value=helper), patch.object(apple, 'HELPER_TIMEOUT', .05), patch.object(subprocess, 'Popen', side_effect=spawn):
                with self.assertRaises(contacts.ContactSaveError) as caught:
                    apple.run_helper(command, person={})
                self.assertEqual(caught.exception.kind, 'unknown_outcome')
            child = children[-1]
            self.assertEqual(child.returncode, -signal.SIGKILL)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
        self.assertEqual(len(children), 2)
        def scan(*args, **kwargs):
            self.assertIsNotNone(children[0].returncode)
            return {'identifiers': ['new']}
        with patch.object(apple, 'run_helper', side_effect=scan):
            self.assertEqual(apple.reconcile_intent({'fingerprint': {'phones': ['01000000000']},
                'has_key': True, 'identifiers': [], 'container': 'original'}), 'apple:new')

    def test_process_exit_after_journal_before_helper_never_recreates(self):
        for op in ('create', 'update'):
            card = self.card()
            fake = FakeStore(self, card, op, 'lost')
            real_save = cards.save_card
            def crash_after_journal(value):
                real_save(value)
                if value.get('save_intent'):
                    raise SystemExit('synthetic process death')
            with patch.object(apple, 'run_helper', side_effect=fake), patch.object(cards, 'save_card', side_effect=crash_after_journal):
                with self.assertRaises(SystemExit):
                    pipeline.approve(card)
            self.assertEqual(fake.writes, [])
            telegram.recover_interrupted()
            fake.first = False
            with patch.object(apple, 'run_helper', side_effect=fake):
                pipeline.retry_due(force=True)
            final = cards.load_card(card['id'])
            self.assertEqual(final['status'], 'needs_confirmation' if op == 'create' else 'saved')
            self.assertEqual(sum(w[0] == 'create' for w in fake.writes), 0)
            final['status'] = 'cancelled'
            cards.save_card(final)

    def test_photo_retry_uses_pinned_container_after_identifier_shift(self):
        card = self.card()
        photo = Path(self.tmp.name)/'synthetic.jpg'
        photo.write_bytes(b'synthetic')
        card.update(status='saved', resource_name='apple:gone', photo_status='failed',
            save_intent={'op': 'create', 'backend': 'apple', 'container': 'original',
                         'fingerprint': apple.fingerprint(pipeline.current_draft(card)),
                         'identifiers': [], 'target_identifier': None, 'generation': 1})
        card['images']['profile'] = str(photo)
        cards.save_card(card)
        writes = []
        def helper(command, **payload):
            self.assertEqual(payload.get('container'), 'original')
            if command == 'get':
                if payload['identifier'] == 'gone':
                    raise contacts.ContactSaveError('gone', kind='not_found')
                return {'identifier': 'shifted', 'person': {}}
            if command == 'find-fingerprint':
                return {'identifiers': ['shifted']}
            if command == 'set-photo':
                writes.append(payload)
                return {'identifier': 'shifted'}
            raise AssertionError(command)
        with patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND': 'google', 'BIZCARD_APPLE_CONTAINER': 'other'}), patch.object(apple, 'run_helper', side_effect=helper):
            pipeline.retry_due(force=True)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['identifier'], 'shifted')

    def test_new_generation_drops_old_checkpoint_before_uncertain_write(self):
        card = self.card()
        fake = FakeStore(self, card, 'update', 'before')
        with patch.object(apple, 'run_helper', side_effect=fake):
            pipeline.approve(card)
            card = cards.load_card(card['id'])
            card.update(status='needs_confirmation', confirmation_id='nonce', resource_name='apple:target')
            cards.save_card(card)
            fake.first = True
            pipeline.resolve_confirmation(card, 'again', 'nonce')
            pending = cards.load_card(card['id'])
            self.assertIsNone(pending['resource_name'])
            fake.first = False
            pipeline.retry_due(force=True)
            self.assertEqual(len(fake.writes), 3)

    def test_persist_failure_blocks_external_create_and_update(self):
        for op in ('create', 'update'):
            card = self.card()
            fake = FakeStore(self, card, op, 'lost')
            with patch.object(apple, 'run_helper', side_effect=fake), self.assertRaises(OSError):
                apple.save_contact(pipeline.current_draft(card), None,
                    intent_checkpoint=lambda record: (_ for _ in ()).throw(OSError('synthetic disk full')))
            self.assertEqual(fake.writes, [])
            card['status'] = 'cancelled'
            cards.save_card(card)

    def test_legacy_unknown_confirmation_pins_destination_before_config_change(self):
        card = self.card()
        card.update(status='save_failed', approved_revision=card['revision'], error_kind='unknown_outcome')
        cards.save_card(card)
        with patch.object(apple, 'run_helper', return_value={'identifiers': []}):
            pipeline.retry_due(force=True)
        pending = cards.load_card(card['id'])
        self.assertEqual(pending.get('save_intent', {}).get('container'), 'original')
        nonce = pending['confirmation_id']
        def helper(command, **payload):
            if command == 'find-fingerprint':
                return {'identifiers': []}
            if command == 'create':
                self.assertEqual(payload['container'], 'original')
                return {'identifier': 'synthetic'}
            if command == 'get':
                return {'identifier': 'synthetic', 'person': {}}
            raise AssertionError(command)
        with patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND': 'google', 'BIZCARD_APPLE_CONTAINER': 'other'}), patch.object(apple, 'run_helper', side_effect=helper), patch.object(contacts, 'save_contact') as google:
            self.assertTrue(pipeline.resolve_confirmation(pending, 'again', nonce)['ok'])
        google.assert_not_called()

    def test_fax_only_lost_create_adopts_single_new_match(self):
        card = self.card()
        pipeline.set_draft(card, CardDraft(name='Synthetic', fax=['020000000']), 'fax-only')
        cards.save_card(card)
        fake = FakeStore(self, card, 'create', 'lost')
        with patch.object(apple, 'run_helper', side_effect=fake):
            pipeline.approve(card)
            self.assertEqual(pipeline.retry_due(force=True)[0], 1)
        stored = cards.load_card(card['id'])
        self.assertEqual(stored['resource_name'], 'apple:created-1')
        self.assertEqual(stored['save_intent']['fingerprint']['phones'], ['020000000'])
        self.assertEqual(len(fake.writes), 1)

    def test_restart_after_consuming_confirmation_never_reuses_tap(self):
        for op in ('create', 'update'):
            card = self.card()
            fake = FakeStore(self, card, op, 'delayed')
            with patch.object(apple, 'run_helper', side_effect=fake):
                pipeline.approve(card)
                pipeline.retry_due(force=True)
                pending = cards.load_card(card['id'])
                nonce = pending['confirmation_id']
                original = copy.deepcopy(pending['save_intent'])
                real_save = cards.save_card
                def crash_after_consumption(value):
                    real_save(value)
                    if value.get('save_resolution') == 'again':
                        raise SystemExit('synthetic confirmation crash')
                with patch.object(cards, 'save_card', side_effect=crash_after_consumption), self.assertRaises(SystemExit):
                    pipeline.resolve_confirmation(pending, 'again', nonce)
                telegram.recover_interrupted()
                self.assertTrue(pipeline.resolve_confirmation(pending, 'again', nonce).get('stale'))
                fake.hidden = False
                fake.first = False
                pipeline.retry_due(force=True)
                stored = cards.load_card(card['id'])
                self.assertEqual(stored['save_intent'], original)
                self.assertLessEqual(sum(w[0] == 'create' for w in fake.writes), 1)
                self.assertEqual(stored['status'], 'saved')
                stored['status'] = 'cancelled'
                cards.save_card(stored)

    def test_stale_card_writer_cannot_erase_operation_or_restore_consumed_nonce(self):
        card = self.card()
        before_operation = copy.deepcopy(card)
        fake = FakeStore(self, card, 'create', 'delayed')
        with patch.object(apple, 'run_helper', side_effect=fake):
            pipeline.approve(card)
            pipeline.retry_due(force=True)
            pending = cards.load_card(card['id'])
            operation = copy.deepcopy(pending['save_intent'])
            # A delayed pre-save UI callback must not erase the write-ahead record.
            try:
                cards.save_card(before_operation)
            except RuntimeError:
                pass
            self.assertEqual(cards.load_card(card['id']).get('save_intent'), operation)
            stale = copy.deepcopy(pending)
            real_save = cards.save_card
            def crash_after_consumption(value):
                real_save(value)
                if value.get('save_resolution') == 'again':
                    raise SystemExit('synthetic confirmation crash')
            with patch.object(cards, 'save_card', side_effect=crash_after_consumption), self.assertRaises(SystemExit):
                pipeline.resolve_confirmation(pending, 'again', pending['confirmation_id'])
            try:
                cards.save_card(stale)
            except RuntimeError:
                pass
            self.assertIsNone(cards.load_card(card['id'])['confirmation_id'])
            self.assertTrue(pipeline.resolve_confirmation(stale, 'again', stale['confirmation_id']).get('stale'))

    def test_original_journal_cannot_change_without_consumed_confirmation(self):
        card = self.card()
        fake = FakeStore(self, card, 'create', 'lost')
        with patch.object(apple, 'run_helper', side_effect=fake):
            pipeline.approve(card)
        original = cards.load_card(card['id'])
        for field, value in [('identifiers', ['overwritten']), ('generation', 2), ('container', 'other')]:
            changed = copy.deepcopy(original)
            changed['save_intent'][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                cards.save_card(changed)
            self.assertEqual(cards.load_card(card['id'])['save_intent'], original['save_intent'])

    def test_ui_taps_before_journal_and_during_write_do_not_mutate_saving_card(self):
        for stage in ('find-fingerprint', 'create'):
            for action in ('ed', 'bk', 'rs'):
                with self.subTest(stage=stage, action=action):
                    card = self.card()
                    front = Path(self.tmp.name) / (card['id'] + '.jpg')
                    front.write_bytes(b'synthetic')
                    card['images']['front'] = str(front)
                    cards.remember_message(card, 123)
                    cards.save_card(card)
                    def helper(command, **payload):
                        if command == stage:
                            before = cards.load_card(card['id'])
                            telegram.handle_callback('synthetic', {'data': f"{action}:{card['id']}"}, 42)
                            self.assertEqual(cards.load_card(card['id']), before)
                        if command in ('find', 'find-fingerprint'):
                            return {'identifiers': []}
                        if command in ('create', 'get'):
                            return {'identifier': 'written', 'person': {}}
                        raise AssertionError(command)
                    with patch.object(apple, 'run_helper', side_effect=helper), patch.object(
                        telegram, 'send_message', return_value={'result': {'message_id': 777}}) as send, patch.object(telegram, 'submit') as submit:
                        outcome = pipeline.approve(card)
                    self.assertTrue(outcome['ok'], outcome)
                    self.assertIn('처리 중입니다', send.call_args.args[2])
                    submit.assert_not_called()
                    final = cards.load_card(card['id'])
                    self.assertEqual(final['status'], 'saved')
                    self.assertEqual(final['resource_name'], 'apple:written')
                    self.assertEqual(final['message_ids'], [123])
                    self.assertTrue(final['save_intent'])

    def test_save_checkpoints_merge_only_owned_fields_from_latest_card(self):
        for stage in ('find-fingerprint', 'create', 'get'):
            for terminal in ('saved', 'save_failed', 'needs_confirmation'):
                with self.subTest(stage=stage, terminal=terminal):
                    card = self.card()
                    def helper(command, **payload):
                        if command == stage:
                            fresh = cards.load_card(card['id'])
                            cards.remember_message(fresh, 888)
                            cards.add_history(fresh, 'ui_marker')
                            fresh['ui_marker'] = 'preserve'
                            cards.save_card(fresh)
                        if command in ('find', 'find-fingerprint'):
                            return {'identifiers': []}
                        if command == 'create':
                            if terminal == 'save_failed':
                                raise contacts.ContactSaveError('synthetic failure', kind='transient')
                            if terminal == 'needs_confirmation':
                                raise apple.ConfirmationRequired()
                        if command in ('create', 'get'):
                            return {'identifier': 'written', 'person': {}}
                        raise AssertionError(command)
                    with patch.object(apple, 'run_helper', side_effect=helper):
                        pipeline.approve(card)
                    final = cards.load_card(card['id'])
                    self.assertEqual(final['status'], terminal)
                    # Failure paths never reach get.
                    if stage != 'get' or terminal == 'saved':
                        self.assertEqual(final['message_ids'], [888])
                        self.assertEqual(final['ui_marker'], 'preserve')
                        self.assertIn('ui_marker', [h['event'] for h in final['history']])
                    final['status'] = 'cancelled'
                    cards.save_card(final)

    def test_crash_after_image_deletion_leaves_durable_terminal_state(self):
        for operation in ('save', 'saved', 'skip', 'photo', 'cancel'):
            with self.subTest(operation=operation):
                card = self.card()
                front = Path(self.tmp.name) / (card['id'] + '.jpg')
                front.write_bytes(b'synthetic')
                card['images']['front'] = str(front)
                if operation in ('saved', 'skip'):
                    card.update(status='needs_confirmation', confirmation_id='nonce')
                elif operation == 'photo':
                    card.update(status='saved', resource_name='apple:written', photo_status='failed')
                    card['images']['profile'] = str(front)
                cards.save_card(card)
                cleanup = pipeline.cleanup_card_images
                def crash(value, **kwargs):
                    cleanup(value, **kwargs)
                    raise SystemExit('synthetic crash after deletion')
                with (patch.object(pipeline, 'cleanup_card_images', side_effect=crash), patch.object(
                    pipeline, 'save_contact', return_value=contacts.SaveResult('created', 'apple:written')),
                    patch.object(pipeline, 'refresh_resource_name', return_value='apple:written'), patch.object(pipeline, 'upload_photo')):
                    with self.assertRaises(SystemExit):
                        if operation in ('saved', 'skip'):
                            pipeline.resolve_confirmation(card, operation, 'nonce')
                        elif operation == 'photo':
                            pipeline.retry_due(force=True)
                        elif operation == 'cancel':
                            pipeline.cancel(card)
                        else:
                            pipeline.approve(card)
                self.assertFalse(front.exists())
                final = cards.load_card(card['id'])
                self.assertEqual(final['status'], 'cancelled' if operation in ('skip', 'cancel') else 'saved')
                if operation == 'save':
                    self.assertEqual(final['resource_name'], 'apple:written')
                if operation == 'photo':
                    self.assertEqual(final['photo_status'], 'uploaded')
                final['status'] = 'cancelled'
                cards.save_card(final)

    def test_failed_terminal_state_write_keeps_source_image(self):
        card = self.card()
        front = Path(self.tmp.name) / 'front.jpg'
        front.write_bytes(b'synthetic')
        card['images']['front'] = str(front)
        cards.save_card(card)
        real_save = cards.save_card
        def fail(value):
            if value['status'] == 'saved':
                raise OSError('synthetic disk failure')
            real_save(value)
        with patch.object(cards, 'save_card', side_effect=fail), patch.object(
            pipeline, 'save_contact', return_value=contacts.SaveResult('created', 'apple:written')):
            with self.assertRaises(OSError):
                pipeline.approve(card)
        self.assertTrue(front.exists())

    def test_confirmation_again_shows_photo_note_and_verification_warnings(self):
        card = self.card()
        pipeline.set_draft(card, CardDraft(name='Synthetic', mobile=['01000000000'], notes='Synthetic note'), 'test')
        photo = Path(self.tmp.name) / 'profile.jpg'
        photo.write_bytes(b'synthetic')
        card['images']['profile'] = str(photo)
        card.update(status='needs_confirmation', confirmation_id='nonce',
            save_intent={'op': 'create', 'backend': 'apple', 'container': 'original',
                         'fingerprint': apple.fingerprint(pipeline.current_draft(card)),
                         'identifiers': [], 'target_identifier': None, 'generation': 1})
        cards.save_card(card)
        def helper(command, **payload):
            if command == 'find-fingerprint':
                return {'identifiers': []}
            if command in ('create', 'get'):
                return {'identifier': 'written', 'person': {}}
            if command == 'set-photo':
                raise contacts.ContactSaveError('synthetic photo failure', kind='transient')
            raise AssertionError(command)
        with patch.object(apple, 'run_helper', side_effect=helper), patch.object(telegram, 'send_message', return_value={'result': {'message_id': 999}}) as send:
            telegram.confirm_card('synthetic', 42, card, 'again', 'nonce')
        text = send.call_args.args[2]
        self.assertIn('연락처 사진은 올리지 못해', text)
        self.assertIn(apple.NOTE_WARNING, text)
        self.assertIn('전화번호 누락', text)
        self.assertEqual(cards.load_card(card['id'])['photo_status'], 'failed')
        self.assertTrue(photo.exists())

    def test_default_google_list_labels_saving_and_limits_edit_guidance(self):
        card = self.card()
        for status in ('saving', 'needs_confirmation', 'review'):
            with self.subTest(status=status), patch.dict(os.environ, {}, clear=True):
                card = cards.load_card(card['id'])
                card.update(status=status)
                cards.save_card(card)
                with patch.object(telegram, 'send_message', return_value={'result': {'message_id': 999}}) as send:
                    telegram.send_open_list('synthetic', 42)
                text = send.call_args_list[0].args[2]
                self.assertNotIn('None', text)
                if status == 'saving':
                    self.assertIn('저장 중', text)
                if status == 'review':
                    self.assertIn('답장', text)
                else:
                    self.assertNotIn('답장', text)

    def test_draft_buttons_refuse_non_review_states_without_writing(self):
        for status in ('saving', 'needs_confirmation', 'saved', 'cancelled', 'processing'):
            for action in ('ed', 'bk', 'rs'):
                with self.subTest(status=status, action=action):
                    card = self.card()
                    card['status'] = status
                    cards.save_card(card)
                    before = cards.load_card(card['id'])
                    with patch.object(telegram, 'send_message', return_value={'result': {'message_id': 777}}), patch.object(telegram, 'submit') as submit:
                        telegram.handle_callback('synthetic', {'data': f"{action}:{card['id']}"}, 42)
                    self.assertEqual(cards.load_card(card['id']), before)
                    submit.assert_not_called()

    def test_photo_retry_records_message_after_lock_release(self):
        import threading
        card = self.card()
        photo = Path(self.tmp.name) / 'retry.jpg'
        photo.write_bytes(b'synthetic')
        card.update(status='saved', resource_name='apple:written', photo_status='failed',
            save_intent={'op': 'create', 'backend': 'apple', 'container': 'original',
                         'fingerprint': {}, 'identifiers': [], 'generation': 1})
        card['images']['profile'] = str(photo)
        cards.save_card(card)
        started, done = threading.Event(), threading.Event()
        workers = []
        real_lock = pipeline.card_lock
        def observed_lock(cid, **kwargs):
            if kwargs.get('blocking', True): started.set()
            return real_lock(cid, **kwargs)
        def update_message():
            telegram._remember_card_message(copy.deepcopy(card), 999)
            done.set()
        def upload(*args, **kwargs):
            worker = threading.Thread(target=update_message)
            workers.append(worker)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(done.is_set(), 'message recording must wait for the held card lock')
            self.assertEqual(cards.find_by_message(42, 999)['id'], card['id'])
        try:
            with patch.object(pipeline, 'card_lock', side_effect=observed_lock), patch.object(pipeline, 'refresh_resource_name', return_value='apple:written'), patch.object(pipeline, 'upload_photo', side_effect=upload):
                pipeline.retry_due(force=True)
        finally:
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive(), 'record did not finish after photo retry')
        self.assertTrue(done.is_set())
        final = cards.load_card(card['id'])
        self.assertEqual(final['photo_status'], 'uploaded')
        self.assertEqual(final['message_ids'], [999])
        self.assertEqual(cards.find_by_message(42, 999)['id'], card['id'])
