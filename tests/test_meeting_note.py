"""Meeting notes are durable, idempotent and independent of Telegram."""
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bizcard_intake import apple_contacts, cards, contacts, doctor, pipeline
from bizcard_intake.draft import CardDraft


class MeetingNoteTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('bizcard_intake.meeting_note'),
                             'meeting_note implementation is required')
        self.notes = importlib.import_module('bizcard_intake.meeting_note')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(cards, 'CARD_DIR', Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def card(self, resource='apple:test'):
        card = cards.new_card('42')
        card.update(status='saved', resource_name=resource,
                    created_at='2026-11-17T16:00:00+0000',
                    draft=CardDraft(name='Example', title='담당', mobile=['010-0000-0001'], notes='부가 정보').to_dict())
        self.notes.enqueue(card)
        cards.save_card(card)
        return card

    def test_compose_date_caption_previous_job_and_limit(self):
        card = {'id': '2611181a2b3c', 'created_at': '2026-11-17T16:00:00+0000',
                'job_choice': 'update', 'job_change': {'company': 'example.com', 'title': '담당'}}
        draft = CardDraft(notes='부가 정보', warnings=['a', 'b'])
        with patch.dict(os.environ, {'BIZCARD_TZ': 'Asia/Seoul'}):
            text = self.notes.compose(card, draft, '전시회')
        self.assertEqual(text, '[명함 2026-11-18 #1a2b3c]\n만남 메모: 전시회\n부가 정보\n이전 소속: example.com 담당\n확인 필요: a; b')
        draft.notes = '가' * 3000
        self.assertEqual(len(self.notes.compose(card, draft, '나' * 400)), 2000)
        self.assertIn('만남 메모: ' + '나' * 300 + '\n', self.notes.compose(card, draft, '나' * 400))

    def test_new_job_contact_does_not_claim_previous_employer(self):
        text = self.notes.compose({'id': '123456', 'job_choice': 'new', 'job_change': {'company': 'example.com'}}, CardDraft())
        self.assertNotIn('이전 소속', text)

    def test_enqueue_only_mutates_card_and_preserves_existing_note(self):
        card = {'id': '123456', 'draft': {}, 'caption': '만남'}
        with patch.object(cards, 'save_card') as save:
            self.notes.enqueue(card)
        save.assert_not_called()
        self.assertEqual(card['note']['state'], 'pending')
        self.assertIn('만남', card['note']['text'])
        card['note']['state'] = 'written'
        self.notes.enqueue(card)
        self.assertEqual(card['note']['state'], 'written')

    def test_unauthorized_never_writes_or_counts_attempts(self):
        card = self.card()
        with patch.object(apple_contacts, 'automation_status', return_value='not_determined'), patch.object(apple_contacts, 'set_note') as write:
            result = self.notes.drain()
        write.assert_not_called()
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['automation'], 'not_determined')
        self.assertEqual(cards.load_card(card['id'])['note']['attempts'], 0)
        self.assertIsNone(self.notes.status()['last_notice_at'])

    def test_write_outside_card_lock_and_replay_is_skipped(self):
        card = self.card()
        def write(*args, **kwargs):
            with pipeline.card_lock(card['id'], blocking=False) as acquired:
                self.assertTrue(acquired)
            return True
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note', side_effect=write) as call:
            self.assertEqual(self.notes.drain()['written'], 1)
            self.assertEqual(self.notes.drain()['written'], 0)
        call.assert_called_once()
        self.assertEqual(cards.load_card(card['id'])['note']['state'], 'written')

    def test_retry_intervals_and_expiry_start_with_first_transient(self):
        card = self.card()
        error = contacts.ContactSaveError('synthetic', kind='transient', retryable=True)
        now = 100000.0
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note', side_effect=error), patch.object(self.notes.time, 'time', side_effect=lambda: now):
            for interval in (60, 120, 300, 900):
                self.notes.drain()
                note = cards.load_card(card['id'])['note']
                self.assertEqual(note['next_at'], now + interval)
                now += interval
            now = 100000.0 + 86400
            self.notes.drain()
        self.assertEqual(cards.load_card(card['id'])['note']['state'], 'failed')
        self.assertEqual(cards.load_card(card['id'])['status'], 'saved')

    def test_expired_work_does_not_issue_another_write(self):
        card = self.card()
        card['note'].update(first_attempt_at=1, attempts=4, next_at=900)
        cards.save_card(card)
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note') as write, patch.object(self.notes.time, 'time', return_value=86401):
            self.notes.drain()
        write.assert_not_called()
        note = cards.load_card(card['id'])['note']
        self.assertEqual(note['state'], 'failed')
        self.assertTrue(note['notice_pending'])

    def test_notes_do_not_change_save_version(self):
        card = self.card()
        version = card['_save_state_version']
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note', return_value=True):
            self.notes.drain()
        self.assertEqual(cards.load_card(card['id'])['_save_state_version'], version)

    def test_retry_is_not_sent_before_due_time(self):
        card = self.card()
        card['note']['next_at'] = 200
        cards.save_card(card)
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note') as write, patch.object(self.notes.time, 'time', return_value=100):
            self.notes.drain()
        write.assert_not_called()

    def test_state_updates_preserve_notice_timestamp(self):
        self.card()
        cards.save_state('notes', {'automation': 'denied', 'pending': 1, 'last_notice_at': 12.0})
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note', return_value=True):
            self.notes.drain()
        self.assertEqual(self.notes.status(), {'automation': 'authorized', 'pending': 0, 'last_notice_at': 12.0})

    def test_revoked_permission_between_probe_and_write_does_not_age(self):
        card = self.card()
        with patch.object(apple_contacts, 'automation_status', side_effect=['authorized', 'denied']), patch.object(apple_contacts, 'set_note', side_effect=contacts.ContactSaveError('permission', kind='auth')), patch.object(self.notes, '_record', wraps=self.notes._record) as record:
            self.notes.drain()
        record.assert_not_called()
        note = cards.load_card(card['id'])['note']
        self.assertEqual(note['state'], 'pending')
        self.assertEqual(note['attempts'], 0)
        self.assertEqual(self.notes.status()['automation'], 'denied')

    def test_permission_wait_does_not_expire_retry_window(self):
        card = self.card()
        card['note'].update(first_attempt_at=1, attempts=4)
        cards.save_card(card)
        with patch.object(apple_contacts, 'automation_status', return_value='denied'), patch.object(self.notes.time, 'time', return_value=100000):
            self.notes.drain()
        note = cards.load_card(card['id'])['note']
        self.assertEqual(note['state'], 'pending')
        self.assertEqual(note['attempts'], 4)
        self.assertIsNone(note.get('first_attempt_at'))

    def test_not_found_refreshes_once_and_persists_new_identifier(self):
        card = self.card()
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note', side_effect=[contacts.ContactSaveError('missing', kind='not_found'), True]) as write, patch.object(self.notes.contact_backend, 'refresh_resource_name', return_value='apple:new'):
            self.assertEqual(self.notes.drain()['written'], 1)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(cards.load_card(card['id'])['resource_name'], 'apple:new')

    def test_job_change_confirmation_becomes_failed(self):
        card = self.card()
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note', side_effect=contacts.ContactSaveError('missing', kind='not_found')), patch.object(self.notes.contact_backend, 'refresh_resource_name', side_effect=apple_contacts.ConfirmationRequired()):
            self.notes.drain()
        self.assertEqual(cards.load_card(card['id'])['note']['state'], 'failed')
        self.assertEqual(cards.load_card(card['id'])['status'], 'saved')

    def test_google_queue_uses_same_composed_block_without_automation(self):
        card = self.card('people/test')
        with patch.object(apple_contacts, 'automation_status') as auth, patch.object(contacts, 'set_note', return_value=True) as write:
            self.assertEqual(self.notes.drain()['written'], 1)
        auth.assert_not_called()
        self.assertEqual(write.call_args.args[2], card['note']['text'])

    def test_retryable_note_errors_have_bounded_retry(self):
        for resource, kind in [('apple:test', 'not_found'), ('apple:test', 'other'),
                               ('people/test', 'invalid'), ('people/test', 'auth')]:
            with self.subTest(resource=resource, kind=kind):
                card = self.card(resource)
                backend = apple_contacts if resource.startswith('apple:') else contacts
                with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(
                        backend, 'set_note', side_effect=contacts.ContactSaveError('fixture', kind=kind)), patch.object(
                        self.notes.contact_backend, 'refresh_resource_name', return_value=resource), patch.object(
                        self.notes.time, 'time', return_value=100000):
                    self.notes.drain()
                note = cards.load_card(card['id'])['note']
                self.assertEqual(note['state'], 'pending')
                self.assertEqual(note['attempts'], 1)
                self.assertEqual(note['next_at'], 100060)
                with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(
                        self.notes.time, 'time', return_value=186400):
                    self.notes.drain()
                self.assertEqual(cards.load_card(card['id'])['note']['state'], 'failed')

    def test_google_note_omits_already_saved_biography_and_keeps_date(self):
        card = {'id': '123456', 'resource_name': 'people/test', 'draft': CardDraft(
            notes='부가 정보', warnings=['전화번호 확인']).to_dict()}
        self.notes.enqueue(card)
        self.assertEqual(len(card['note']['text'].splitlines()), 1)
        card.pop('note')
        card.update(caption='전시회', job_choice='update', job_change={'company': 'example.com', 'title': '담당'})
        self.notes.enqueue(card)
        text = card['note']['text']
        self.assertIn('만남 메모: 전시회', text)
        self.assertIn('이전 소속: example.com 담당', text)
        self.assertNotIn('부가 정보', text)
        self.assertNotIn('확인 필요', text)

    def test_permission_wait_skips_unchanged_records_and_probes_every_15_minutes(self):
        self.card()
        for state in ('not_determined', 'denied'):
            cards.save_state('notes', {})
            with patch.object(apple_contacts, 'automation_status', return_value=state) as probe, patch.object(
                    self.notes, '_record', wraps=self.notes._record) as record:
                for now in (100000, 100060, 100899, 100900):
                    with patch.object(self.notes.time, 'time', return_value=now):
                        self.notes.drain()
                self.assertEqual(probe.call_count, 2)
                record.assert_not_called()
                self.notes.drain(force_automation=True)
                self.assertEqual(probe.call_count, 3)

    def test_late_album_caption_reaches_previously_enqueued_card(self):
        card = self.card('people/test')
        card['media_group_id'] = 'late-album'
        cards.save_card(card)
        cards.group_caption(card, '전시회')
        with patch.object(contacts, 'set_note', return_value=True) as write:
            self.notes.drain()
        self.assertIn('만남 메모: 전시회', write.call_args.args[2])
        self.assertEqual(cards.load_card(card['id'])['caption'], '전시회')

    def test_disabled_notes_leave_existing_queue_untouched(self):
        card = self.card()
        with patch.dict(os.environ, {'BIZCARD_MEETING_NOTE': 'off'}), patch.object(
                apple_contacts, 'automation_status') as probe, patch.object(apple_contacts, 'set_note') as write:
            self.notes.drain()
        probe.assert_not_called()
        write.assert_not_called()
        self.assertEqual(cards.load_card(card['id'])['note'], card['note'])

    def test_disabled_notes_are_not_enqueued(self):
        card = {'id': '123456', 'draft': {}}
        with patch.dict(os.environ, {'BIZCARD_MEETING_NOTE': 'off'}):
            self.notes.enqueue(card)
        self.assertNotIn('note', card)

    def test_concurrent_drain_does_not_overlap(self):
        self.card()
        entered, release = threading.Event(), threading.Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return True
        with patch.object(apple_contacts, 'automation_status', return_value='authorized'), patch.object(apple_contacts, 'set_note', side_effect=write) as call:
            worker = threading.Thread(target=self.notes.drain)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.notes.drain()
            finally:
                release.set()
                worker.join(5)
        call.assert_called_once()

    def test_doctor_reads_saved_state_without_helper(self):
        cards.save_state('notes', {'automation': 'denied', 'pending': 3, 'last_notice_at': 12.0})
        with patch.object(apple_contacts, 'run_helper') as helper:
            check = doctor.check_notes()
        helper.assert_not_called()
        self.assertIn('3', check.detail)
        self.assertIn('거부됨', check.detail)
        self.assertEqual(self.notes.status()['last_notice_at'], 12.0)

    def test_request_is_explicit_and_persists_status(self):
        with patch.object(apple_contacts, 'request_automation', return_value='authorized') as request:
            self.assertEqual(self.notes.request_automation(), 'authorized')
        request.assert_called_once()
        self.assertEqual(self.notes.status()['automation'], 'authorized')


class NoteAdapterTests(unittest.TestCase):
    def test_automation_and_note_timeouts(self):
        self.assertTrue(hasattr(apple_contacts, 'automation_status'))
        with patch.object(apple_contacts, 'run_helper', return_value={'status': 'authorized', 'written': True}) as helper:
            self.assertEqual(apple_contacts.automation_status(), 'authorized')
            self.assertEqual(helper.call_args.kwargs['_timeout'], 5)
            self.assertEqual(apple_contacts.request_automation(), 'authorized')
            self.assertEqual(helper.call_args.kwargs['_timeout'], 600)
            apple_contacts.set_note('apple:test', '[marker]', 'body')
            self.assertEqual(helper.call_args.args[0], 'set-note')
            self.assertEqual(helper.call_args.kwargs['_timeout'], 20)

    def test_helper_timeout_is_retryable_and_does_not_emit_values(self):
        import subprocess
        completed = subprocess.TimeoutExpired('helper', 20)
        with patch.object(apple_contacts, 'ensure_helper', return_value=Path('/synthetic/helper')), patch.object(apple_contacts.subprocess, 'run', side_effect=completed):
            with self.assertRaises(contacts.ContactSaveError) as caught:
                apple_contacts.set_note('apple:test', '[marker]', 'private value')
        self.assertEqual(caught.exception.kind, 'transient')
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn('private value', str(caught.exception))

    def test_request_timeout_and_error_return_contract_states(self):
        for kind, state in [('transient', 'timeout'), ('other', 'error')]:
            with self.subTest(kind=kind), patch.object(apple_contacts, 'run_helper', side_effect=contacts.ContactSaveError('synthetic', kind=kind)):
                self.assertEqual(apple_contacts.request_automation(), state)

    def test_authorized_save_has_no_note_warning_or_live_permission_probe(self):
        from bizcard_intake import meeting_note
        draft = CardDraft(name='Example', title='담당', mobile=['010-0000-0001'], notes='추가')
        with patch.object(apple_contacts, 'find_existing_candidates', return_value=(None, 0)), patch.object(apple_contacts, 'create_contact', return_value={'resourceName': 'apple:test'}), patch.object(apple_contacts, 'verify_contact', return_value=[]), patch.object(meeting_note, 'status', return_value={'automation': 'authorized'}), patch.object(apple_contacts, 'automation_status') as probe:
            result = apple_contacts.save_contact(draft, None)
        self.assertNotIn(apple_contacts.NOTE_WARNING, result.warnings)
        probe.assert_not_called()

    def test_optional_status_and_verification_failures_do_not_block_save(self):
        from bizcard_intake import meeting_note
        draft = CardDraft(name='Example', mobile=['010-0000-0001'], notes='부가 정보')
        with patch.object(apple_contacts, 'find_existing_candidates', return_value=(None, 0)), patch.object(
                apple_contacts, 'create_contact', return_value={'resourceName': 'apple:test'}), patch.object(
                apple_contacts, 'verify_contact', side_effect=RuntimeError('fixture')), patch.object(
                meeting_note, 'status', side_effect=OSError('fixture')):
            result = apple_contacts.save_contact(draft, None)
        self.assertEqual(result.resource_name, 'apple:test')
        self.assertNotIn(apple_contacts.NOTE_WARNING, result.warnings)

    def test_unauthorized_save_leaves_note_guidance_to_chat_only(self):
        from bizcard_intake import chat_ux, meeting_note
        draft = CardDraft(name='Example', mobile=['010-0000-0001'], notes='부가 정보')
        with patch.object(apple_contacts, 'find_existing_candidates', return_value=(None, 0)), patch.object(
                apple_contacts, 'create_contact', return_value={'resourceName': 'apple:test'}), patch.object(
                apple_contacts, 'verify_contact', return_value=[]), patch.object(
                meeting_note, 'status', return_value={'automation': 'denied'}):
            result = apple_contacts.save_contact(draft, None)
            text = chat_ux.save_result_text({'id': '123456', 'draft': draft.to_dict(),
                'resource_name': result.resource_name, 'note': {'state': 'pending'}},
                {'ok': True, 'message': result.summary, 'warnings': result.warnings})
        self.assertEqual(text.splitlines().count(apple_contacts.NOTE_WARNING), 1)
        self.assertNotIn('참고: 참고:', text)

    def test_google_update_preserves_existing_biography(self):
        old = {'resourceName': 'people/test', 'biographies': [{'value': '기존 메모', 'contentType': 'TEXT_PLAIN'}]}
        draft = CardDraft(notes='새 메모')
        result = contacts.build_update(old, draft)
        self.assertEqual(result['biographies'][0]['value'], '기존 메모\n\n새 메모')
        self.assertEqual(contacts.build_update(result, draft)['biographies'], result['biographies'])

    def test_google_append_uses_marker_and_only_updates_biography(self):
        self.assertTrue(hasattr(contacts, 'set_note'))
        old = {'resourceName': 'people/test', 'etag': 'tag', 'biographies': [{'value': '기존 메모'}]}
        with patch.object(contacts, 'get_contact', return_value=old), patch.object(contacts, 'run_gws', return_value={}) as gws:
            contacts.set_note('people/test', '[marker]', '[marker]\n추가')
            args = gws.call_args.args[0]
        body = json.loads(args[args.index('--json') + 1])
        params = json.loads(args[args.index('--params') + 1])
        self.assertEqual(body['biographies'][0]['value'], '기존 메모\n\n[marker]\n추가')
        self.assertEqual(params['updatePersonFields'], 'biographies')
        old['biographies'] = body['biographies']
        with patch.object(contacts, 'get_contact', return_value=old), patch.object(contacts, 'run_gws') as gws:
            self.assertFalse(contacts.set_note('people/test', '[marker]', '[marker]\n추가'))
        gws.assert_not_called()
