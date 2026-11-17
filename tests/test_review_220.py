"""Review regressions; synthetic contacts and isolated card storage only."""
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import apple_contacts, cards, chat_ux, contact_backend, contacts, doctor, pipeline, telegram
from bizcard_intake.draft import CardDraft


def draft(company='New Org', title='New Role'):
    return CardDraft(name='테스트인', company=company, title=title, mobile=['010-0000-1234'])


def person():
    return {'resourceName': 'people/old', 'names': [{'givenName': '테스트인'}],
            'phoneNumbers': [{'value': '010-0000-1234'}],
            'organizations': [{'name': 'Old Org', 'title': 'Old Role', 'department': 'Old Dept'}]}


class IntentRegressionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(cards, 'CARD_DIR', Path(tmp.name)/'cards'),
                  patch.object(pipeline, 'SESSION_ARCHIVE_DIR', Path(tmp.name)/'archive'),
                  patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND': 'google'})):
            p.start(); self.addCleanup(p.stop)

    def card(self, backend):
        card = cards.new_card(42)
        pipeline.set_draft(card, draft(), 'fixture')
        card.update(status='review', job_change={'resource_name': 'apple:old' if backend == 'apple' else 'people/old', 'container': 'fixture', 'company': 'Old Org', 'title': 'Old Role'})
        cards.save_card(card)
        return card

    def test_apple_lost_update_rejects_opposite_choice_after_reload(self):
        for kind in ('transient', 'unknown_outcome'):
            for restart in (False, True):
                with self.subTest(kind=kind, restart=restart):
                    card = self.card('apple')
                    old = person(); old['resourceName'] = 'apple:old'
                    with patch.object(apple_contacts, '_pinned_get', return_value=old), patch.object(apple_contacts, 'run_helper', side_effect=contacts.ContactSaveError('fixture', kind=kind, retryable=True)) as helper:
                        self.assertFalse(pipeline.approve(card, card['revision'], job_choice='update')['ok'])
                        self.assertEqual(helper.call_count, 1)
                        if restart:
                            card = cards.load_card(card['id'])
                        outcome = pipeline.approve(card, card['revision'], job_choice='new')
                        self.assertFalse(outcome['ok'])
                        self.assertEqual(helper.call_count, 1, 'opposite button must not replay update')
                        self.assertEqual(cards.load_card(card['id'])['job_choice'], 'update')
                        self.assertIn('확인', outcome['message'])

    def test_google_lost_create_blocks_edit_and_retry_never_creates_again(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                card = self.card('google')
                created = []
                def lost(body):
                    created.append(dict(body, resourceName='people/new'))
                    raise contacts.ContactSaveError('fixture lost response', kind='unknown_outcome')
                with patch.object(contacts, 'create_contact', side_effect=lost) as create:
                    self.assertFalse(pipeline.approve(card, job_choice='new')['ok'])
                    if restart:
                        card = cards.load_card(card['id'])
                    original = copy.deepcopy(card['draft'])
                    ok, _ = pipeline.edit(card, '회사: Third Org')
                    self.assertFalse(ok, 'unresolved create must freeze the draft')
                    self.assertEqual(card['draft'], original)
                    self.assertIsNone(contacts.select_job_change_candidate(draft(), [person(), created[0]]))
                    # Older on-disk records can already have lost their UI choice.
                    card.pop('job_choice', None); card['job_change'] = None
                    cards.save_card(card)
                    outcome = pipeline.approve(cards.load_card(card['id']))
                    self.assertTrue(outcome.get('needs_confirmation'))
                    self.assertEqual(create.call_count, 1)

    def test_adapter_honors_google_create_intent_without_ui_flags(self):
        with patch.object(contacts, 'save_contact') as legacy, patch.object(contacts, 'create_contact') as create:
            with self.assertRaises(apple_contacts.ConfirmationRequired):
                contact_backend.save_contact(draft(), None, save_intent={'backend': 'google', 'op': 'create'})
            self.assertEqual(legacy.call_count, 0)
            self.assertEqual(create.call_count, 0)

    def test_legacy_unsavable_google_draft_still_checks_unresolved_create_first(self):
        with self.assertRaises(contacts.ContactSaveError) as caught:
            contact_backend.save_contact(CardDraft(), None, save_intent={'backend': 'google', 'op': 'create'})
        self.assertIsInstance(caught.exception, apple_contacts.ConfirmationRequired)

    def test_pending_operation_buttons_never_offer_edits_or_opposite_choice(self):
        for backend, op in (('apple', 'update'), ('apple', 'create'), ('google', 'create')):
            card = self.card(backend)
            card.update(status='save_failed', save_intent={'backend': backend, 'op': op})
            cards.save_card(card)
            for fn in (telegram.draft_buttons, telegram.saved_failed_buttons):
                actions = [b['callback_data'].split(':')[0] for row in fn(card)['inline_keyboard'] for b in row]
                self.assertTrue(set(actions) <= {'sv', 'cs', 'cr', 'ck'}, actions)

    def test_stale_edit_and_back_buttons_cannot_mutate_pending_intent(self):
        for action in ('ed', 'bk', 'rs'):
            with self.subTest(action=action):
                card = self.card('google')
                card.update(status='save_failed', save_intent={'backend': 'google', 'op': 'create'})
                cards.save_card(card)
                with patch.object(telegram, 'send_message', return_value={'result': {'message_id': 123}}) as send, patch.object(telegram, 'submit') as submit:
                    telegram._handle_draft_callback('fixture', 42, card, action)
                    self.assertEqual(submit.call_count, 0)
                    self.assertIn('이전 저장', send.call_args.args[2])
                    self.assertFalse(cards.load_card(card['id']).get('awaiting_back'))

    def test_pending_lookup_does_not_override_intent(self):
        card = self.card('google')
        card.update(status='save_failed', save_intent={'backend': 'google', 'op': 'create'})
        cards.save_card(card)
        with patch.object(contact_backend, 'find_job_change_candidate') as lookup:
            pipeline.refresh_job_change(card)
        self.assertEqual(lookup.call_count, 0)

    def test_stale_full_writes_cannot_erase_save_state_for_any_backend(self):
        for backend in ('google', 'apple'):
            for state in ('saving', 'save_failed', 'needs_confirmation', 'saved'):
                with self.subTest(backend=backend, state=state):
                    card = self.card(backend)
                    stale = copy.deepcopy(card)
                    card.update(status=state, save_intent={'backend': backend, 'op': 'create', 'generation': 1})
                    cards.save_card(card)
                    stale['awaiting_back'] = False
                    with self.assertRaises(RuntimeError):
                        cards.save_card(stale)
                    self.assertEqual(cards.load_card(card['id'])['status'], state)
                    self.assertEqual(cards.load_card(card['id'])['save_intent'], card['save_intent'])

    def test_back_photo_interleaving_keeps_google_create_once_after_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                card = self.card('google')
                card['awaiting_back'] = True
                cards.remember_message(card, 99)
                cards.save_card(card)
                original_find = cards.find_by_message
                def interleave(*args):
                    stale = original_find(*args)
                    self.assertFalse(pipeline.approve(cards.load_card(card['id']), job_choice='new')['ok'])
                    return stale
                with patch.object(contacts, 'create_contact', side_effect=contacts.ContactSaveError('synthetic lost response', kind='unknown_outcome')) as create, patch.object(contacts, 'verify_contact', return_value=[]), patch.object(telegram, 'submit') as submit, patch.object(telegram, 'send_message') as send, patch.object(cards, 'find_by_message', side_effect=interleave):
                    telegram.handle_photo('fixture', 42, {'reply_to_message': {'message_id': 99}}, [], Path('/synthetic'))
                    self.assertTrue(cards.load_card(card['id']).get('save_intent'))
                    self.assertEqual(submit.call_count, 0)
                    self.assertIn('확인', send.call_args.args[2])
                    if restart:
                        telegram.recover_interrupted()
                    self.assertFalse(pipeline.approve(cards.load_card(card['id']), job_choice='new')['ok'])
                    self.assertEqual(create.call_count, 1)

    def test_refused_late_writes_are_reported_and_keep_create_once(self):
        writers = {
            'text_edit': lambda c: pipeline.edit(c, '회사: Changed'),
            'recognize_front': lambda c: pipeline.recognize_front(c, Path('/synthetic/front.jpg')),
            'recognize_back': lambda c: pipeline.recognize_back(c, Path('/synthetic/back.jpg')),
            'cancel': lambda c: pipeline.cancel(c),
            'draft_lookup': lambda c: telegram.show_draft('fixture', 42, c),
            'back_photo': lambda c: telegram.handle_photo('fixture', 42, {'reply_to_message': {'message_id':99}}, [], Path('/synthetic')),
            'back_button': lambda c: telegram._handle_draft_callback('fixture', 42, c, 'bk'),
            'restart_recovery': lambda c: telegram.recover_interrupted(),
            'followup_send': lambda c: telegram.send_due_followups('fixture'),
            'followup_resolve': lambda c: pipeline.resolve_followup(c['id'], 42, 'sent', 'fixture'),
            'confirmation_token': lambda c: pipeline.confirmation_token(c),
            'confirmation_resolve': lambda c: pipeline.resolve_confirmation(c, 'saved', 'fixture'),
            'retry_scan': lambda c: pipeline.retry_due(force=True),
            'photo_retry': lambda c: pipeline.retry_due(force=True),
        }
        original_save = cards.save_card
        for name, writer in writers.items():
            with self.subTest(writer=name):
                card = self.card('google')
                card.update(job_change_pending=True, awaiting_back=True, message_ids=[99],
                            approved_revision=card['revision'], next_retry_at=0,
                            followup_state='pending', followup_due_at=1, followup_nonce='fixture')
                if name.startswith('followup'):
                    card['status'] = 'saved'
                    if name == 'followup_resolve': card['followup_state'] = 'notified'
                elif name.startswith('confirmation'):
                    card.update(status='needs_confirmation', confirmation_id='fixture' if name.endswith('resolve') else None)
                elif name == 'retry_scan':
                    card['status'] = 'save_failed'
                elif name == 'photo_retry':
                    card.update(status='saved', photo_status='failed', resource_name='people/old')
                original_save(card)
                injected = []
                def interleave(value):
                    if value['id'] == card['id'] and not injected:
                        injected.append(True)
                        # Emulate a writer already committed by another session.
                        fresh = cards.load_card(card['id'])
                        fresh.update(status='save_failed', save_intent={'backend':'google', 'op':'create', 'generation':1}, resource_name=None)
                        original_save(fresh)
                        try:
                            contacts.create_contact({})
                        except contacts.ContactSaveError:
                            pass
                    return original_save(value)
                with patch.object(cards, 'save_card', side_effect=interleave), patch.object(contacts, 'create_contact', side_effect=contacts.ContactSaveError('synthetic lost response', kind='unknown_outcome')) as create, patch.object(telegram, 'send_message', return_value={'result':{'message_id':100}}), patch.object(telegram, 'submit'), patch.object(pipeline, '_prepare_oriented', side_effect=ValueError('synthetic')), patch.object(cards, 'followup_localtime', return_value=datetime(2026,11,17,12)), patch.object(cards, 'followup_days', return_value=3), patch.object(telegram, 'allowed_chat_ids', return_value={42}):
                    if name.startswith('recognize'):
                        with self.assertRaises(contacts.ContactSaveError): writer(card)
                    else:
                        writer(card)
                    self.assertTrue(injected)
                    stored = cards.load_card(card['id'])
                    self.assertEqual(stored['save_intent']['backend'], 'google')
                    pipeline.approve(stored, job_choice='new')
                    self.assertEqual(create.call_count, 1)
                stored = cards.load_card(card['id'])
                stored.update(status='cancelled', followup_state='done')
                original_save(stored)

    def test_legacy_import_writers_preserve_an_interleaved_create(self):
        import json
        import io
        from contextlib import redirect_stdout
        from bizcard_intake import cli
        original_save = cards.save_card
        for writer in ('migration', 'cli_import'):
            with self.subTest(writer=writer), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                pending = root/'pending'; pending.mkdir()
                source = pending/'42-fixture.json'
                source.write_text(json.dumps({'draft': draft().to_dict()}))
                injected = []
                def interleave(value):
                    if cards.load_card(value['id']) and not injected:
                        injected.append(value['id'])
                        fresh = cards.load_card(value['id'])
                        fresh.update(status='save_failed', save_intent={'backend':'google','op':'create','generation':1})
                        original_save(fresh)
                        try:
                            contacts.create_contact({})
                        except contacts.ContactSaveError:
                            pass
                    return original_save(value)
                with patch.object(cards, 'save_card', side_effect=interleave), patch.object(pipeline, 'PENDING_CONTACT_DIR', pending), patch.object(pipeline, 'SESSION_DIR', root/'sessions'), patch.object(contacts, 'create_contact', side_effect=contacts.ContactSaveError('synthetic lost response', kind='unknown_outcome')) as create, redirect_stdout(io.StringIO()):
                    if writer == 'migration':
                        self.assertIn('반영하지', pipeline.migrate_legacy()[0])
                    else:
                        self.assertEqual(cli.cmd_save(source), 1)
                    self.assertEqual(len(injected), 1)
                    stored = cards.load_card(injected[0])
                    self.assertEqual(stored['save_intent']['backend'], 'google')
                    pipeline.approve(stored)
                    self.assertEqual(create.call_count, 1)
                    self.assertTrue(source.exists())

    def test_save_worker_cannot_merge_fields_over_a_newer_operation(self):
        card = self.card('google')
        stale = copy.deepcopy(card)
        card.update(status='save_failed', save_intent={'backend':'google','op':'create','generation':1})
        cards.save_card(card)
        stale.update(status='saved', resource_name='people/stale')
        with self.assertRaises(cards.StaleCardState):
            pipeline._persist_save_fields(stale, 'status', 'resource_name')
        stored = cards.load_card(card['id'])
        self.assertEqual(stored['status'], 'save_failed')
        self.assertIsNone(stored['resource_name'])

    def test_google_success_and_confirmation_choices_resolve_unfinished_state(self):
        for choice in ('success', 'saved', 'skip', 'again'):
            with self.subTest(choice=choice):
                card = self.card('google')
                with patch.object(contacts, 'create_contact', return_value={'resourceName':'people/new'}) as create, patch.object(contacts, 'verify_contact', return_value=[]):
                    if choice == 'success':
                        self.assertTrue(pipeline.approve(card, job_choice='new')['ok'])
                    else:
                        card.update(status='needs_confirmation', confirmation_id='fixture',
                                    save_intent={'backend':'google', 'op':'create', 'generation':1})
                        cards.save_card(card)
                        self.assertTrue(pipeline.resolve_confirmation(card, choice, 'fixture')['ok'])
                    stored = cards.load_card(card['id'])
                    self.assertFalse(pipeline.unresolved_save(stored))
                    self.assertEqual(stored['status'], 'cancelled' if choice == 'skip' else 'saved')
                    self.assertEqual(create.call_count, int(choice in ('success','again')))
                    if choice == 'again':
                        self.assertEqual(stored['save_intent']['generation'], 2)
                    self.assertFalse(pipeline.resolve_confirmation(stored, choice, 'fixture')['ok'])

    def test_background_lookup_and_queued_back_photo_keep_new_intent(self):
        for writer in ('lookup_after_send', 'queued_photo', 'queued_front', 'album_front'):
            with self.subTest(writer=writer):
                card = self.card('google')
                def save_during_wait(*args, **kwargs):
                    fresh = cards.load_card(card['id'])
                    fresh['job_change_pending'] = False
                    cards.save_card(fresh)
                    pipeline.approve(fresh, job_choice='new')
                    return Path('/synthetic/back.jpg')
                with patch.object(contacts, 'create_contact', side_effect=contacts.ContactSaveError('synthetic lost response', kind='unknown_outcome')) as create, patch.object(telegram, 'send_message', return_value={'result':{'message_id':100}}) as send, patch.object(telegram, 'edit_message'), patch.object(telegram, 'send_status', return_value=100), patch.object(pipeline, 'refresh_job_change', side_effect=save_during_wait), patch.object(telegram, 'save_largest_photo', side_effect=save_during_wait), patch.object(pipeline, '_prepare_oriented') as prepare:
                    if writer == 'lookup_after_send':
                        telegram.show_draft('fixture', 42, card)
                    elif writer == 'queued_photo':
                        telegram.process_back_photo('fixture', 42, [], Path('/synthetic'), card)
                        self.assertIn('다시 보내', send.call_args.args[2])
                    else:
                        telegram.process_front_photo('fixture', 42, [], Path('/synthetic'), card, album=writer == 'album_front')
                        self.assertIn('다시 보내', send.call_args.args[2])
                    self.assertTrue(cards.load_card(card['id']).get('save_intent'))
                    pipeline.approve(cards.load_card(card['id']), job_choice='new')
                    self.assertEqual(create.call_count, 1)
                    self.assertEqual(prepare.call_count, 0)

    def test_v211_card_without_version_starts_at_zero(self):
        import json
        card = self.card('google')
        old = {k: v for k, v in card.items() if not k.startswith('_') and not k.startswith('job_change')}
        cards.card_path(card['id']).write_text(json.dumps(old))
        fresh = cards.load_card(card['id'])
        self.assertEqual(fresh.get('_save_state_version', 0), 0)
        fresh['message_ids'] = [100]
        cards.save_card(fresh)
        self.assertEqual(fresh.get('_save_state_version', 0), 0)
        stale = copy.deepcopy(fresh)
        fresh.update(status='saving')
        cards.save_card(fresh)
        with self.assertRaises(RuntimeError):
            cards.save_card(stale)

    def test_every_existing_card_writer_reloads_before_mutating(self):
        from contextlib import contextmanager
        from datetime import timezone
        writers = {
            'text_edit': lambda c: pipeline.edit(c, '회사: Changed'),
            'front_recognition': lambda c: pipeline.recognize_front(c, Path('/synthetic/front.jpg')),
            'back_recognition': lambda c: pipeline.recognize_back(c, Path('/synthetic/back.jpg')),
            'cancel': lambda c: pipeline.cancel(c),
            'draft_lookup': lambda c: telegram.show_draft('fixture', 42, c),
            'back_button': lambda c: telegram._handle_draft_callback('fixture', 42, c, 'bk'),
            'restart_recovery': lambda c: telegram.recover_interrupted(),
            'retry_scan': lambda c: pipeline.retry_due(force=True),
            'photo_retry': lambda c: pipeline.retry_due(force=True),
            'followup_send': lambda c: telegram.send_due_followups('fixture'),
            'followup_resolve': lambda c: pipeline.resolve_followup(c['id'], 42, 'sent', 'fixture'),
            'confirmation_token': lambda c: pipeline.confirmation_token(c),
            'confirmation_resolve': lambda c: pipeline.resolve_confirmation(c, 'saved', 'fixture'),
        }
        original_lock = pipeline.card_lock
        for name, writer in writers.items():
            with self.subTest(writer=name):
                card = self.card('google')
                card.update(job_change_pending=True, awaiting_back=True, approved_revision=card['revision'],
                            next_retry_at=0, photo_status='failed', resource_name='people/old',
                            followup_state='pending', followup_due_at=1, followup_nonce='fixture', confirmation_id='fixture')
                if name in ('photo_retry', 'followup_send', 'followup_resolve'):
                    card['status'] = 'saved'
                elif name == 'retry_scan':
                    card['status'] = 'save_failed'
                elif name.startswith('confirmation'):
                    card['status'] = 'needs_confirmation'
                cards.save_card(card)
                injected = []
                @contextmanager
                def interleave(cid, **kwargs):
                    if cid == card['id'] and not injected:
                        injected.append(True)
                        fresh = cards.load_card(cid)
                        fresh.update(status='review', resource_name=None, job_change_pending=False)
                        cards.save_card(fresh)
                        with original_lock(cid):
                            pipeline._approve_locked(fresh, None, 'new')
                    with original_lock(cid, **kwargs) as acquired:
                        yield acquired
                with patch.object(pipeline, 'card_lock', side_effect=interleave), patch.object(contacts, 'create_contact', side_effect=contacts.ContactSaveError('synthetic lost response', kind='unknown_outcome')) as create, patch.object(telegram, 'send_message'), patch.object(telegram, 'submit'), patch.object(pipeline, 'scan_front') as scan, patch.object(pipeline, '_prepare_oriented') as prepare, patch.object(cards, 'followup_localtime', return_value=datetime(2026,11,17,12,tzinfo=timezone.utc)), patch.object(cards, 'followup_days', return_value=3), patch.object(telegram, 'allowed_chat_ids', return_value={42}):
                    if name in ('front_recognition', 'back_recognition'):
                        with self.assertRaises(contacts.ContactSaveError):
                            writer(card)
                    else:
                        writer(card)
                    self.assertTrue(injected, name + ' did not acquire card lock')
                    stored = cards.load_card(card['id'])
                    self.assertEqual(stored['save_intent']['backend'], 'google')
                    pipeline.approve(stored, job_choice='new')
                    self.assertEqual(create.call_count, 1)
                    self.assertEqual(scan.call_count, 0)
                    self.assertEqual(prepare.call_count, 0)
                # Exclude this fixture from the next scan.
                stored = cards.load_card(card['id'])
                stored.update(status='cancelled', followup_state='done')
                cards.save_card(stored)


class OrganizationRegressionTest(unittest.TestCase):
    def test_no_job_change_when_either_side_has_no_org_or_title(self):
        old = person(); old['organizations'] = []
        self.assertIsNone(contacts.select_job_change_candidate(draft(), [old]))
        self.assertIsNone(contacts.select_job_change_candidate(draft('', ''), [person()]))

    def test_apple_partial_draft_retains_empty_organization_fields(self):
        for value in (draft('', ''), draft('New Org', ''), draft('', 'New Role')):
            org = apple_contacts.build_job_change_update(person(), value)['organizations'][0]
            self.assertEqual(org['name'], value.company or 'Old Org')
            self.assertEqual(org['title'], value.title or 'Old Role')
            self.assertEqual(org.get('department'), 'Old Dept')


class TimezoneRegressionTest(unittest.TestCase):
    def test_invalid_zone_uses_system_time_and_logs_fixed_warning_once(self):
        for zone in ('KST', 'garbage', '/invalid'):
            with self.subTest(zone=zone), patch.dict(os.environ, {'BIZCARD_TZ': zone}), patch('logging.Logger.warning') as warning:
                # A fresh process has one warning budget; reset the module flag only.
                with patch.object(cards, '_followup_zone_warned', False, create=True):
                    try:
                        local = cards.followup_localtime(0)
                        due = cards.followup_due(0, 3)
                        cards.followup_localtime(1)
                    except Exception as exc:
                        self.fail('invalid zone escaped: '+type(exc).__name__)
                    self.assertEqual(local, datetime.fromtimestamp(0).astimezone())
                    self.assertIsInstance(due, float)
                    self.assertEqual(warning.call_count, 1)
                    self.assertEqual(warning.call_args.args, ('Invalid BIZCARD_TZ; using system timezone',))

    def test_doctor_warns_without_echoing_invalid_value(self):
        self.assertIn('timezone', doctor.CHECKS)
        with patch.dict(os.environ, {'BIZCARD_TZ': 'garbage'}):
            check = doctor.run_checks(['timezone'])[0]
        self.assertTrue(check.ok)
        self.assertIn('경고', check.detail)
        self.assertNotIn('garbage', check.detail)

    def test_scheduler_exception_does_not_escape_poll_loop(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cards, 'CARD_DIR', Path(tmp)), patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '42'}), patch.object(telegram, 'initialize_contacts'), patch.object(pipeline, 'migrate_legacy', return_value=[]), patch.object(telegram, 'recover_interrupted'), patch.object(telegram, '_inspect_webhook'), patch.object(telegram, 'delete_webhook'), patch.object(telegram, 'api_get', return_value={'result': {'id': 1, 'username': 'fixture'}}), patch.object(telegram, 'health_check'), patch.object(telegram, 'send_due_followups', side_effect=ValueError('fixture')), patch.object(telegram, 'get_updates', side_effect=KeyboardInterrupt) as poll, patch.object(telegram, 'ThreadPoolExecutor'), patch.object(telegram, '_executor', None):
            with self.assertRaises(KeyboardInterrupt):
                telegram.run('fixture')
            self.assertEqual(poll.call_count, 1)


class FollowupRegressionTest(unittest.TestCase):
    def test_off_button_offers_on_and_alerts_menu_can_toggle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cards, 'CARD_DIR', Path(tmp)), patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '42', 'BIZCARD_FOLLOWUP_DAYS': '3'}), patch.object(telegram, 'api_post'), patch.object(telegram, 'edit_message') as edit, patch.object(telegram, 'send_message') as send:
            card = cards.new_card(42)
            card.update(status='saved', followup_state='notified', followup_nonce='fixture')
            cards.save_card(card)
            telegram.handle_callback('fixture', {'data': f"fo:{card['id']}:fixture", 'message': {'message_id': 1}}, 42)
            self.assertFalse(cards.load_state('followup')['enabled'])
            buttons = edit.call_args.kwargs['reply_markup']['inline_keyboard']
            self.assertTrue(buttons, 're-enable button missing')
            self.assertEqual(buttons[0][0]['text'], '알림 다시 켜기')
            telegram.handle_callback('fixture', {'data': buttons[0][0]['callback_data']}, 42)
            self.assertTrue(cards.load_state('followup')['enabled'])
            telegram.handle_text('fixture', 42, {'text': '/alerts'})
            buttons = send.call_args.kwargs['reply_markup']['inline_keyboard']
            self.assertEqual(buttons[0][0]['text'], '알림 끄기')
            telegram.handle_callback('fixture', {'data': buttons[0][0]['callback_data']}, 42)
            self.assertFalse(cards.load_state('followup')['enabled'])

    def test_greeting_handles_empty_company_and_traditional_chinese(self):
        for name, phone, phrase in [('홍길동', '010-0000-1234', '안녕하세요'), ('John', '+1 555 000 1111', 'pleasure meeting you on'), ('王小明', '+886 912 000 111', '很高興'), ('王小明', '+852 5555 0100', '很高興'), ('陳美玲', '+86 138 0000 1111', '很高興'), ('王小明', '+86 138 0000 1111', '很高兴')]:
            with self.subTest(name=name, phone=phone):
                card = {'draft': CardDraft(name=name, mobile=[phone]).to_dict(), 'created_at': '2026-11-17T11:00:00+09:00'}
                text = chat_ux.followup_text(card)
                self.assertNotIn('  ', text)
                self.assertIn(phrase, text)
