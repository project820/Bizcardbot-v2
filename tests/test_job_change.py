import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import apple_contacts, cards, contact_backend, contacts, pipeline, telegram
from bizcard_intake.draft import CardDraft


def draft(**values):
    return CardDraft.from_dict(dict(name='테스트인', company='새회사', title='이사',
                                   mobile=['010-0000-1234'], email=['test@example.invalid'], **values))


def person(resource='people/test'):
    return {'resourceName': resource, 'names': [{'givenName': '테스트인 과장 이전회사'}],
            'organizations': [{'name': '이전회사', 'title': '과장'}],
            'phoneNumbers': [{'value': '010-0000-1234'}, {'value': '02-000-1111'}],
            'emailAddresses': [{'value': 'test@example.invalid'}, {'value': 'old@example.invalid'}],
            'urls': [{'value': 'https://old.example.invalid'}],
            'addresses': [{'formattedValue': '이전 주소'}]}


class LookupTest(unittest.TestCase):
    def lookup(self, people, value=None):
        self.assertTrue(callable(getattr(contacts, 'find_job_change_candidate', None)), 'read-only job lookup missing')
        with patch.object(contacts, 'iter_connections', return_value=iter(people)):
            return contacts.find_job_change_candidate(value or draft())

    def test_mobile_match(self):
        old = person()
        old['emailAddresses'] = []
        self.assertEqual(self.lookup([old])['resourceName'], 'people/test')

    def test_email_match(self):
        old = person()
        old['phoneNumbers'] = []
        self.assertIsNotNone(self.lookup([old]))

    def test_no_match_for_different_name_domain_or_work_number(self):
        for change in ('name', 'domain', 'work'):
            old = person()
            if change == 'name':
                old['names'] = [{'givenName': '다른사람'}]
            else:
                old['phoneNumbers'] = [{'value': '02-000-9999'}]
                old['emailAddresses'] = [{'value': 'test@other.invalid'}]
            value = draft()
            value.work = ['02-000-9999']
            self.assertIsNone(self.lookup([old], value))

    def test_ambiguous_including_unchanged_candidate(self):
        old, same = person(), person('people/second')
        same['organizations'] = [{'name': '새회사', 'title': '이사'}]
        self.assertIsNone(self.lookup([old, same]))
        self.assertIsNone(self.lookup([same]))

    def test_apple_lookup_is_pinned_and_read_only(self):
        self.assertTrue(callable(getattr(apple_contacts, 'find_job_change_candidate', None)))
        calls = []
        def helper(command, **kw):
            calls.append((command, kw))
            if command == 'find-job-change':
                return {'identifiers': ['old']}
            if command == 'get':
                return {'identifier': 'old', 'person': person('apple:old')}
            self.fail('unexpected mutating command ' + command)
        with patch.object(apple_contacts, 'container_id', return_value='book'), patch.object(apple_contacts, 'run_helper', side_effect=helper):
            result = apple_contacts.find_job_change_candidate(draft())
        self.assertEqual(result['resourceName'], 'apple:old')
        self.assertTrue(all(kw['container'] == 'book' for _, kw in calls))


class ChoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(cards, 'CARD_DIR', Path(self.tmp.name) / 'cards')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.card = cards.new_card(42)
        pipeline.set_draft(self.card, draft(), 'test')
        self.card['status'] = 'review'
        cards.save_card(self.card)

    def lookup(self):
        self.assertTrue(callable(getattr(pipeline, 'refresh_job_change', None)))
        with patch.object(contact_backend, 'find_job_change_candidate', return_value=person()) as lookup:
            pipeline.refresh_job_change(self.card)
            pipeline.refresh_job_change(self.card)
            self.assertEqual(lookup.call_count, 1)
        cards.save_card(self.card)

    def test_cached_lookup_preview_and_buttons(self):
        self.lookup()
        self.assertIn('기존 연락처: 이전회사 과장 → 새 명함: 새회사 이사', pipeline.draft_text(self.card))
        buttons = [b['text'] for row in telegram.draft_buttons(self.card)['inline_keyboard'] for b in row]
        self.assertIn('기존 연락처 갱신', buttons)
        self.assertIn('새 연락처로 저장', buttons)
        self.assertNotIn('저장', buttons)

    def test_lookup_failure_is_cached_and_silent(self):
        self.assertTrue(callable(getattr(pipeline, 'refresh_job_change', None)))
        with patch.object(contact_backend, 'find_job_change_candidate', side_effect=RuntimeError('offline')) as lookup:
            pipeline.refresh_job_change(self.card)
            pipeline.refresh_job_change(self.card)
        self.assertEqual(lookup.call_count, 1)
        self.assertIsNone(self.card.get('job_change'))

    def test_choice_required_and_stale_button_rejected(self):
        self.lookup()
        with patch.object(pipeline, '_save') as save:
            self.assertFalse(pipeline.approve(self.card, self.card['revision'])['ok'])
            self.assertTrue(pipeline.approve(self.card, 999, job_choice='update')['stale'])
            save.assert_not_called()

    def test_update_target_and_new_choice(self):
        self.lookup()
        for choice in ('update', 'new'):
            with patch.object(pipeline, '_save', return_value={'ok': True}) as save:
                self.assertTrue(pipeline.approve(self.card, self.card['revision'], job_choice=choice)['ok'])
                selected = save.call_args.args[0]
                self.assertEqual(selected['job_choice'], choice)
                self.assertIsNone(selected.get('resource_name'))


class MergeTest(unittest.TestCase):
    def test_google_update_keeps_lists_and_replaces_org(self):
        old = person()
        calls = []
        def gws(args, **kw):
            calls.append((args, kw))
            return old
        with patch.object(contacts, 'run_gws', side_effect=gws):
            contacts.update_contact(old, draft())
        import json
        payload = json.loads(calls[0][0][calls[0][0].index('--json') + 1])
        self.assertEqual(payload['organizations'][0]['name'], '새회사')
        self.assertEqual(len(payload['phoneNumbers']), 2)
        self.assertEqual(len(payload['emailAddresses']), 2)
        self.assertEqual(payload['urls'], old['urls'])
        self.assertEqual(payload['addresses'], old['addresses'])

    def test_apple_job_merge_keeps_lists_replaces_org(self):
        old = person('apple:old')
        original = copy.deepcopy(old)
        self.assertTrue(callable(getattr(apple_contacts, 'build_job_change_update', None)))
        merged = apple_contacts.build_job_change_update(old, draft())
        self.assertEqual(merged['organizations'][0]['name'], '새회사')
        self.assertEqual(merged['organizations'][0]['title'], '이사')
        self.assertEqual(merged['phoneNumbers'], old['phoneNumbers'])
        self.assertEqual(merged['emailAddresses'], old['emailAddresses'])
        self.assertEqual(old, original)


class SaveRoutingTest(unittest.TestCase):
    def test_google_explicit_new_bypasses_match_and_checkpoints(self):
        recorded = []
        with patch.dict('os.environ', {'BIZCARD_CONTACTS_BACKEND': 'google'}), patch.object(contacts, 'find_existing_candidates', side_effect=AssertionError('must not match')), patch.object(contacts, 'create_contact', return_value={'resourceName': 'people/new'}), patch.object(contacts, 'verify_contact', return_value=[]):
            self.assertIn('force_create', __import__('inspect').signature(contact_backend.save_contact).parameters)
            result = contact_backend.save_contact(draft(), None, force_create=True,
                intent_checkpoint=lambda value: recorded.append(('intent', value)),
                checkpoint=lambda resource, action: recorded.append((resource, action)))
        self.assertEqual(result.action, 'created')
        self.assertEqual(recorded[0][0], 'intent')
        self.assertEqual(recorded[1], ('people/new', 'created'))

    def test_google_explicit_update_uses_pinned_target_and_new_photo(self):
        recorded = []
        with patch.object(contacts, 'get_contact', return_value=person()), patch.object(contacts, 'update_contact') as update, patch.object(contacts, '_attach_photo') as photo, patch.object(contacts, 'verify_contact', return_value=[]):
            self.assertIn('job_change', __import__('inspect').signature(contact_backend.save_contact).parameters)
            result = contact_backend.save_contact(draft(), 'new.jpg', resource_name='people/test', job_change=True,
                                                  checkpoint=lambda resource, action: recorded.append(resource))
        self.assertEqual(result.action, 'updated')
        self.assertEqual(update.call_args.args[0]['resourceName'], 'people/test')
        photo.assert_called_once_with(result, 'new.jpg')
        self.assertEqual(recorded, ['people/test'])

    def test_apple_new_is_journaled_create_despite_matching_contact(self):
        events = []
        def helper(command, **kw):
            events.append((command, kw))
            if command == 'find-fingerprint':
                return {'identifiers': ['old']}
            if command == 'create':
                self.assertEqual(events[-2][0], 'intent')
                return {'identifier': 'new'}
            if command == 'get':
                return {'identifier': 'new', 'person': person('apple:new')}
            self.fail(command)
        self.assertIn('force_create', __import__('inspect').signature(contact_backend.save_contact).parameters)
        with patch.dict('os.environ', {'BIZCARD_CONTACTS_BACKEND': 'apple'}), patch.object(apple_contacts, 'container_id', return_value='book'), patch.object(apple_contacts, 'run_helper', side_effect=helper):
            result = contact_backend.save_contact(draft(), None, force_create=True,
                intent_checkpoint=lambda value: events.append(('intent', value)))
        self.assertEqual(result.action, 'created')
        intent = next(kw for command, kw in events if command == 'intent')
        self.assertEqual(intent['op'], 'create')
        self.assertEqual(intent['container'], 'book')

    def test_google_unknown_create_requires_confirmation_without_repeat(self):
        self.assertIn('force_create', __import__('inspect').signature(contact_backend.save_contact).parameters)
        with patch.dict('os.environ', {'BIZCARD_CONTACTS_BACKEND': 'google'}), patch.object(contacts, 'create_contact') as create:
            with self.assertRaises(apple_contacts.ConfirmationRequired):
                contact_backend.save_contact(draft(), None, force_create=True,
                    save_intent={'backend': 'google', 'op': 'create', 'generation': 1}, intent_checkpoint=lambda value: None)
            create.assert_not_called()

    def test_pipeline_passes_update_choice_and_pinned_target(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cards, 'CARD_DIR', Path(folder) / 'cards'), patch.object(pipeline, 'SESSION_ARCHIVE_DIR', Path(folder) / 'archive'):
            card = cards.new_card(42)
            pipeline.set_draft(card, draft(), 'test')
            card.update(status='review', job_change={'resource_name': 'people/test', 'company': '이전회사', 'title': '과장', 'container': None})
            cards.save_card(card)
            with patch.object(pipeline, 'save_contact', return_value=contacts.SaveResult('updated', 'people/test')) as save:
                self.assertTrue(pipeline.approve(card, card['revision'], job_choice='update')['ok'])
            self.assertTrue(save.call_args.kwargs.get('job_change'))
            self.assertEqual(save.call_args.kwargs['resource_name'], 'people/test')

    def test_apple_pinned_update_journal_and_restart(self):
        events, intents = [], []
        def helper(command, **kw):
            events.append((command, kw))
            if command == 'find-fingerprint':
                return {'identifiers': ['old']}
            if command == 'get':
                return {'identifier': 'old', 'person': person('apple:old')}
            if command == 'update':
                self.assertTrue(intents)
                self.assertTrue(kw.get('replaceOrganization'))
                self.assertEqual(kw['identifier'], 'old')
                self.assertEqual(kw['container'], 'pinned')
                self.assertEqual(kw['person']['names'], [{'givenName': draft().display_name}])
                return {'identifier': 'old'}
            self.fail(command)
        with patch.object(apple_contacts, 'run_helper', side_effect=helper), patch.object(apple_contacts, 'verify_contact', return_value=[]):
            result = contact_backend.save_contact(draft(), None, resource_name='apple:old', job_change=True,
                target_container='pinned', intent_checkpoint=lambda value: intents.append(value))
            self.assertEqual(result.action, 'updated')
            self.assertTrue(intents[0]['replaceOrganization'])
            self.assertEqual(intents[0]['target_identifier'], 'old')
            # Restart must replay the journal flag even without the UI choice.
            contact_backend.save_contact(draft(), None, save_intent=intents[0])
        self.assertEqual(sum(command == 'update' for command, _ in events), 2)

    def test_deleted_job_target_never_matches_or_creates(self):
        intent = dict(backend='apple', op='update', container='pinned', target_identifier='gone',
                      fingerprint=apple_contacts.fingerprint(draft()), replaceOrganization=True)
        with patch.object(apple_contacts, '_pinned_get', side_effect=contacts.ContactSaveError('gone', kind='not_found')), patch.object(apple_contacts, 'find_fingerprint') as match, patch.object(apple_contacts, 'run_helper') as helper:
            with self.assertRaises(apple_contacts.ConfirmationRequired):
                contact_backend.save_contact(draft(), None, save_intent=intent)
            match.assert_not_called()
            helper.assert_not_called()

    def test_pipeline_apple_candidate_overrides_current_google_backend(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cards, 'CARD_DIR', Path(folder)), patch.dict('os.environ', {'BIZCARD_CONTACTS_BACKEND': 'google'}):
            card = cards.new_card(42)
            pipeline.set_draft(card, draft(), 'test')
            card.update(status='review', job_change={'resource_name': 'apple:old', 'container': 'pinned'})
            cards.save_card(card)
            with patch.object(pipeline, 'save_contact', side_effect=apple_contacts.ConfirmationRequired()) as save:
                outcome = pipeline.approve(card, card['revision'], job_choice='update')
            self.assertTrue(outcome['needs_confirmation'])
            self.assertTrue(callable(save.call_args.kwargs.get('intent_checkpoint')))
            self.assertEqual(cards.load_card(card['id'])['status'], 'needs_confirmation')
            self.assertTrue(telegram.saved_failed_buttons(card)['inline_keyboard'])

    def test_normal_google_save_keeps_legacy_gws_sequence(self):
        def run(args, **kw):
            if 'list' in args:
                return {'connections': []}
            return {'resourceName': 'people/new', 'names': [{'givenName': draft().display_name}],
                    'phoneNumbers': [{'value': '010-0000-1234'}]}
        with patch.dict('os.environ', {'BIZCARD_CONTACTS_BACKEND': 'google'}), patch.object(contacts, 'run_gws', side_effect=run) as gws:
            contacts.save_contact(draft(), None)
            legacy = list(gws.call_args_list)
            gws.reset_mock()
            contact_backend.save_contact(draft(), None)
            self.assertEqual(gws.call_args_list, legacy)
            # Frozen from v2.1.1 contacts.save_contact with this synthetic fixture.
            import hashlib, json
            sequence = [(call.args, call.kwargs) for call in legacy]
            self.assertEqual(hashlib.sha256(json.dumps(sequence, sort_keys=True).encode()).hexdigest(),
                             'a2d0b891db389ba9c6d9d03aa6b0b1e8246240ab65acc1c6815f2b10887f0d5b')
            self.assertEqual([call.args[0][2] for call in legacy], ['connections', 'createContact', 'get'])

    def test_batch_skips_are_named_with_both_choices(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cards, 'CARD_DIR', Path(folder)):
            card = cards.new_card(42)
            card.update(status='review', draft=draft().to_dict(), job_change={'resource_name': 'people/test'})
            cards.save_card(card)
            with patch.object(telegram, 'send_message', return_value={'result': {'message_id': 999}}) as send:
                telegram.approve_batch('fake', 42, 'all')
            notices = [c for c in send.call_args_list if c.kwargs.get('reply_markup')]
            self.assertEqual(len(notices), 1)
            self.assertIn(cards.label(card), notices[0].args[2])
            labels = [b['text'] for row in notices[0].kwargs['reply_markup']['inline_keyboard'] for b in row]
            self.assertIn('기존 연락처 갱신', labels)
            self.assertIn('새 연락처로 저장', labels)

    def test_google_lookup_uses_read_only_mocked_gws(self):
        with patch.object(contacts, 'run_gws', return_value={'connections': [person()]}) as gws:
            self.assertEqual(contacts.find_job_change_candidate(draft())['resourceName'], 'people/test')
        self.assertEqual(gws.call_count, 1)
        self.assertNotIn('mutating', gws.call_args.kwargs)

    def test_batch_summary_excludes_choice_cards(self):
        from bizcard_intake import chat_ux
        card = {'id': 'fixture', 'seq': 1, 'status': 'review', 'draft': draft().to_dict(),
                'job_change': {'resource_name': 'people/test', 'company': '이전회사', 'title': '과장'}}
        text, markup = chat_ux.batch_summary([card], 'all', '명함')
        self.assertIsNone(markup)
        self.assertIn('갱신 선택 필요', text)

class HardeningTest(unittest.TestCase):
    def test_deleted_during_native_update_requires_confirmation(self):
        intent = dict(backend='apple', op='update', container='pinned', target_identifier='old',
                      fingerprint=apple_contacts.fingerprint(draft()), replaceOrganization=True)
        with patch.object(apple_contacts, '_pinned_get', return_value=person('apple:old')), patch.object(apple_contacts, 'run_helper', side_effect=contacts.ContactSaveError('gone', kind='not_found')):
            with self.assertRaises(apple_contacts.ConfirmationRequired):
                contact_backend.save_contact(draft(), None, save_intent=intent)

    def test_deleted_job_target_photo_retry_cannot_rebind(self):
        intent = dict(backend='apple', op='update', container='pinned', target_identifier='old',
                      fingerprint=apple_contacts.fingerprint(draft()), replaceOrganization=True)
        with patch.object(apple_contacts, '_pinned_get', side_effect=contacts.ContactSaveError('gone', kind='not_found')), patch.object(apple_contacts, 'find_fingerprint') as match:
            with self.assertRaises(contacts.ContactSaveError):
                contact_backend.refresh_resource_name('apple:old', draft(), intent)
            self.assertEqual(match.call_count, 0)

    def test_google_lookup_deadline_bounds_all_pages(self):
        import subprocess
        timeouts = []
        def run(*args, **kwargs):
            timeouts.append(kwargs['timeout'])
            return subprocess.CompletedProcess([], 0, '{"connections": [], "nextPageToken": "next"}', '')
        with patch.object(contacts.time, 'monotonic', side_effect=[10, 10.1, 12.5, 13.1]), patch.object(contacts.subprocess, 'run', side_effect=run):
            with self.assertRaises(contacts.ContactSaveError):
                contacts.find_job_change_candidate(draft())
        self.assertEqual(len(timeouts), 2)
        self.assertLess(timeouts[0], 3)
        self.assertEqual(timeouts[1], .5)

    def test_default_apple_update_does_not_opt_into_replacement(self):
        events = []
        def helper(command, **kwargs):
            events.append((command, kwargs))
            if command == 'find-fingerprint':
                return {'identifiers': ['old']}
            if command == 'get':
                return {'identifier': 'old', 'person': person('apple:old')}
            return {'identifier': 'old'}
        with patch.object(apple_contacts, 'container_id', return_value='pinned'), patch.object(apple_contacts, 'run_helper', side_effect=helper), patch.object(apple_contacts, 'verify_contact', return_value=[]):
            apple_contacts.save_contact(draft(), None, resource_name='apple:old', intent_checkpoint=lambda intent: None)
        update = next(kw for cmd, kw in events if cmd == 'update')
        self.assertNotIn('replaceOrganization', update)
        self.assertEqual(update['person']['organizations'], person()['organizations'])
        self.assertEqual(update['person']['names'], person()['names'])

    def test_deleted_google_target_requires_confirmation_without_create(self):
        with patch.object(contacts, 'get_contact', side_effect=contacts.ContactSaveError('gone', kind='not_found')), patch.object(contacts, 'create_contact') as create:
            with self.assertRaises(apple_contacts.ConfirmationRequired):
                contact_backend.save_contact(draft(), None, resource_name='people/gone', job_change=True)
            self.assertEqual(create.call_count, 0)

    def test_deleted_during_job_photo_requires_confirmation(self):
        intent = dict(backend='apple', op='update', container='pinned', replaceOrganization=True)
        result = contacts.SaveResult('updated', 'apple:old')
        with tempfile.TemporaryDirectory() as folder:
            photo = Path(folder)/'fixture.png'
            photo.write_bytes(b'fixture')
            with patch.object(apple_contacts, 'upload_photo', side_effect=contacts.ContactSaveError('gone', kind='not_found')), patch.object(apple_contacts, 'find_fingerprint') as match:
                with self.assertRaises(apple_contacts.ConfirmationRequired):
                    apple_contacts._attach_photo(result, str(photo), draft(), operation=intent)
                self.assertEqual(match.call_count, 0)

    def test_pending_candidate_lookup_cannot_save_before_choice_is_known(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cards, 'CARD_DIR', Path(folder)), patch.object(pipeline.time, 'time', return_value=110.0):
            card = cards.new_card(42)
            card.update(status='review', draft=draft().to_dict(), job_change_pending=True, job_change_started_at=100.0)
            cards.save_card(card)
            with patch.object(pipeline, '_save') as save:
                outcome = pipeline.approve(card)
                self.assertFalse(outcome['ok'])
                self.assertEqual(save.call_count, 0)
            labels = [b['text'] for row in telegram.draft_buttons(card)['inline_keyboard'] for b in row]
            self.assertNotIn('저장', labels)
