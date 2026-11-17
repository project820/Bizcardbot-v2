"""Synthetic-only regressions ported from /tmp/bizcard-apple-review-repro.py."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bizcard_intake import apple_contacts as apple, cards, contacts, doctor, pipeline, telegram
from bizcard_intake.draft import CardDraft


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(cards, 'CARD_DIR', Path(self.tmp.name)/'cards'),
                  patch.object(pipeline, 'SESSION_ARCHIVE_DIR', Path(self.tmp.name)/'archive'),
                  patch.dict(os.environ, {'BIZCARD_CONTACTS_BACKEND':'apple', 'BIZCARD_APPLE_CONTAINER':'synthetic-container', 'TELEGRAM_ALLOWED_CHAT_IDS':'42'}, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def card(self, **fields):
        card = cards.new_card(42)
        pipeline.set_draft(card, CardDraft(**(fields or dict(name='Synthetic', mobile=['01000000000']))), 'test')
        card['status'] = 'review'
        cards.save_card(card)
        return card

    def helper_store(self, records, creates, mode='lost'):
        def helper(command, **payload):
            if command in ('find', 'find-fingerprint'):
                return {'identifiers':list(records)}
            if command == 'create':
                # Durable save intent must exist before the external mutation.
                [stored] = cards.list_cards(statuses=["saving"])
                self.assertEqual(stored['save_intent']['backend'], 'apple')
                self.assertEqual(stored['save_intent']['container'], 'synthetic-container')
                creates.append('new-'+str(len(creates)+1))
                if mode != 'absent':
                    records.append(creates[-1])
                if len(creates) == 1:
                    if mode == 'ambiguous': records.append('another-new')
                    if mode == 'shifted': records.remove('before-a')
                    raise contacts.ContactSaveError(apple.UNKNOWN_MESSAGE, kind='unknown_outcome')
                return {'identifier':creates[-1]}
            if command == 'get':
                return {'identifier':payload['identifier'], 'person':{'names':[{'givenName':'Synthetic'}], 'phoneNumbers':[{'value':'01000000000'}]}}
            raise AssertionError(command)
        return helper

    def test_lost_create_reply_adopts_one_new_match_without_second_write(self):
        card = self.card()
        records, creates = ['before-a','before-b'], []
        with patch.object(apple, 'run_helper', side_effect=self.helper_store(records,creates)):
            self.assertFalse(pipeline.approve(card)['ok'])
            self.assertEqual(pipeline.retry_due(force=True)[0], 1)
        self.assertEqual(len(creates), 1)
        self.assertEqual(cards.load_card(card['id'])['resource_name'], 'apple:new-1')

    def test_ambiguous_shifted_and_fax_only_ambiguous_retries_require_buttons(self):
        for mode in ('ambiguous','shifted','fax'):
            with self.subTest(mode=mode):
                card = self.card(**(dict(name='Synthetic',fax=['020000000']) if mode=='fax' else {}))
                records, creates = ['before-a','before-b'], []
                with patch.object(apple,'run_helper',side_effect=self.helper_store(records,creates,'ambiguous' if mode == 'fax' else mode)), patch.object(telegram,'send_message',return_value={'result': {'message_id': 999}}) as send:
                    pipeline.approve(card)
                    pending = cards.load_card(card['id'])
                    pending['next_retry_at'] = 0
                    cards.save_card(pending)
                    telegram.retry_due_and_report('token')
                stored = cards.load_card(card['id'])
                self.assertEqual(stored['status'], 'needs_confirmation')
                self.assertIsNone(stored['next_retry_at'])
                self.assertEqual(len(creates),1)
                labels = [b['text'] for row in send.call_args.kwargs['reply_markup']['inline_keyboard'] for b in row]
                self.assertEqual(labels,['이미 저장됨','다시 저장','건너뛰기'])
                stored['status']='cancelled'
                cards.save_card(stored)

    def test_no_new_match_requires_confirmation_without_recreate(self):
        card = self.card()
        records, creates = ['before-a','before-b'], []
        with patch.object(apple,'run_helper',side_effect=self.helper_store(records,creates,'absent')) as helper:
            pipeline.approve(card)
            helper.reset_mock()
            self.assertEqual(pipeline.retry_due(force=True)[0],0)
            commands=[c.args[0] for c in helper.call_args_list]
        self.assertEqual(commands,['find-fingerprint'])
        self.assertEqual(cards.load_card(card['id'])['status'],'needs_confirmation')
        self.assertEqual(len(creates),1)

    def test_google_unset_unknown_outcome_keeps_v200_retry_path(self):
        card = self.card()
        with patch.dict(os.environ,{},clear=True), patch.object(contacts,'save_contact',side_effect=[contacts.ContactSaveError('unknown',kind='unknown_outcome'),contacts.SaveResult('created','people/synthetic')]) as save, patch.object(apple,'run_helper',side_effect=AssertionError('Apple forbidden')):
            result=pipeline.approve(card)
            failed=cards.load_card(card['id'])
            self.assertEqual(result['kind'],'unknown_outcome')
            self.assertIsNotNone(failed['next_retry_at'])
            self.assertNotIn('save_intent',failed)
            self.assertEqual(pipeline.retry_due(force=True)[0],1)
        self.assertEqual(save.call_count,2)
        self.assertEqual(set(save.call_args.kwargs),{'resource_name','checkpoint'})

    def test_permission_bad_admin_never_stops_initialization_or_logs_value(self):
        with patch.dict(os.environ,{'BIZCARD_ALERT_CHAT_ID':'private-invalid-value'}), patch.object(apple,'run_helper',return_value={'status':'denied'}), patch.object(telegram,'send_message') as send, self.assertLogs('bizcard',level='WARNING') as logs:
            telegram.initialize_contacts('token')
        self.assertNotIn('private-invalid-value',' '.join(logs.output))
        self.assertEqual(send.call_count,1)

    def test_initialize_catches_unexpected_error(self):
        with patch.object(apple,'run_helper',side_effect=RuntimeError('private-native-message')), self.assertLogs('bizcard',level='WARNING') as logs:
            telegram.initialize_contacts('token')
        self.assertNotIn('private-native-message',' '.join(logs.output))

    def test_google_no_admin_does_not_alert_allowed_chats(self):
        with patch.dict(os.environ,{},clear=True), patch.object(doctor,'run_checks',return_value=[doctor.Check('gws',False,'expired','login')]), patch.object(pipeline,'pending_retry_count',return_value=0), patch.object(telegram,'send_message') as send:
            telegram.health_check('token')
        send.assert_not_called()

    def test_permission_start_health_restart_share_durable_alert_limit(self):
        with patch.object(apple,'run_helper',return_value={'status':'denied'}), patch.object(doctor,'run_checks',return_value=[doctor.Check('apple',False,apple.AUTH_MESSAGE,'allow')]), patch.object(pipeline,'pending_retry_count',return_value=0), patch.object(telegram,'send_message') as send, patch.object(telegram.time,'time',return_value=100000):
            telegram.initialize_contacts('token')
            telegram.health_check('token')
            telegram.initialize_contacts('token')
            self.assertEqual(send.call_count,1)
            state=cards.load_state('contacts_alert')
            self.assertTrue(state.get('key'))
            self.assertEqual(state['timestamp'],100000)
        with patch.object(apple,'run_helper',return_value={'status':'denied'}),patch.object(telegram,'send_message') as send,patch.object(telegram.time,'time',return_value=190000):
            telegram.initialize_contacts('token')
            self.assertEqual(send.call_count,1)

    def test_full_fingerprint_includes_all_phone_types_and_emails(self):
        card=self.card(name=' Synthetic  Name ',mobile=['+82 10 0000 0000'],work=['02-000-0000'],fax=['02-000-0001'],email=['TEST@EXAMPLE.COM'])
        with patch.object(apple,'run_helper',return_value={'identifiers':[]}) as helper:
            apple.find_fingerprint(apple.fingerprint(pipeline.current_draft(card)),'synthetic-container')
        self.assertEqual(helper.call_args.kwargs['phones'],['01000000000','020000000','020000001'])
        self.assertEqual(helper.call_args.kwargs['emails'],['test@example.com'])

    def test_recovery_keeps_original_backend_container_and_photo(self):
        card=self.card()
        records,creates=['before-a','before-b'],[]
        with patch.object(apple,'run_helper',side_effect=self.helper_store(records,creates)):
            pipeline.approve(card)
        with patch.dict(os.environ,{'BIZCARD_CONTACTS_BACKEND':'google','BIZCARD_APPLE_CONTAINER':'different'}), patch.object(apple,'run_helper',side_effect=self.helper_store(records,creates)) as helper, patch.object(apple,'_attach_photo') as photo:
            self.assertEqual(pipeline.retry_due(force=True)[0],1)
        self.assertEqual(helper.call_args_list[0].kwargs['container'],'synthetic-container')
        self.assertEqual(photo.call_args.args[0].resource_name,'apple:new-1')
        self.assertEqual(len(creates),1)

    def test_missing_known_identifier_never_falls_through_to_create(self):
        card=self.card()
        card.update(status='save_failed',resource_name='apple:gone',approved_revision=card['revision'],save_intent={'backend':'apple','container':'synthetic-container','fingerprint':{'name':'Synthetic','displayName':'Synthetic','phones':['01000000000'],'emails':[]},'identifiers':['before-a'],'has_key':True})
        cards.save_card(card)
        def helper(command,**payload):
            if command=='get': raise contacts.ContactSaveError('gone',kind='not_found')
            if command in ('find','find-fingerprint'): return {'identifiers':['shifted-a']}
            if command=='create': return {'identifier':'duplicate'}
            raise AssertionError(command)
        with patch.object(apple,'run_helper',side_effect=helper) as run:
            pipeline.retry_due(force=True)
        self.assertEqual(cards.load_card(card['id'])['status'],'needs_confirmation')
        self.assertNotIn('create',[c.args[0] for c in run.call_args_list])

    def test_confirmation_callbacks_are_authorized_and_double_tap_safe(self):
        for action,status in [('cs','saved'),('ck','cancelled'),('cr','saved')]:
            with self.subTest(action=action):
                card=self.card()
                card.update(status='needs_confirmation',approved_revision=card['revision'])
                cards.save_card(card)
                data=f"{action}:{card['id']}:{pipeline.confirmation_token(card)}"
                callback={'data':data}
                with patch.object(telegram,'send_message',return_value={'result': {'message_id': 999}}), patch.object(pipeline,'save_contact',return_value=contacts.SaveResult('created','apple:synthetic')) as save:
                    telegram.handle_callback('token',callback,99)
                    self.assertEqual(cards.load_card(card['id'])['status'],'needs_confirmation')
                    telegram.handle_callback('token',callback,42)
                    telegram.handle_callback('token',callback,42)
                self.assertEqual(cards.load_card(card['id'])['status'],status)
                self.assertEqual(save.call_count,1 if action=='cr' else 0)

    def test_confirmation_copy_does_not_promise_automatic_retry(self):
        card=self.card()
        text=telegram.save_result_text(card,{'ok':False,'needs_confirmation':True,'message':'버튼으로 확인하세요.'})
        self.assertNotIn('자동 재시도',text)

    def test_permission_recovery_then_denial_alerts_immediately(self):
        with patch.object(apple,'run_helper',side_effect=[{'status':'denied'},{'status':'authorized'},{'status':'denied'}]),patch.object(telegram,'send_message') as send:
            telegram.initialize_contacts('token')
            telegram.initialize_contacts('token')
            telegram.initialize_contacts('token')
        self.assertEqual(send.call_count,2)

    def test_permission_limit_survives_unrelated_parser_alert(self):
        failed=[doctor.Check('apple',False,apple.AUTH_MESSAGE,'allow'),doctor.Check('parser',False,'parser unavailable','setup')]
        with patch.dict(os.environ,{'BIZCARD_ALERT_CHAT_ID':'42'}),patch.object(apple,'run_helper',return_value={'status':'denied'}),patch.object(doctor,'run_checks',return_value=failed),patch.object(telegram,'send_message') as send:
            telegram.initialize_contacts('token')
            telegram.health_check('token')
            telegram.initialize_contacts('token')
        permission=[c for c in send.call_args_list if c.args[2]==apple.AUTH_MESSAGE]
        self.assertEqual(len(permission),1)

    def test_confirmation_is_available_in_open_list_after_restart(self):
        card=self.card()
        card['status']='needs_confirmation'
        cards.save_card(card)
        with patch.object(telegram,'send_message',return_value={'result': {'message_id': 999}}) as send:
            telegram.send_open_list('token',42)
        labels=[b['text'] for c in send.call_args_list for row in (c.kwargs.get('reply_markup') or {}).get('inline_keyboard',[]) for b in row]
        self.assertIn('이미 저장됨',labels)

    def test_changed_draft_does_not_adopt_original_unknown_create_as_new_revision(self):
        card=self.card()
        records,creates=['before-a','before-b'],[]
        with patch.object(apple,'run_helper',side_effect=self.helper_store(records,creates)):
            pipeline.approve(card)
            pending=cards.load_card(card['id'])
            changed = CardDraft(name='Changed',mobile=['01000000001'])
            with self.assertRaises(contacts.ContactSaveError):
                pipeline.set_draft(pending,changed,'test-edit')
            # Simulate an older persisted revision created before edits were blocked.
            pending['draft'] = changed.to_dict()
            pending['revision'] += 1
            pending['approved_revision']=pending['revision']
            cards.save_card(pending)
            pipeline.retry_due(force=True)
        self.assertEqual(cards.load_card(card['id'])['status'],'needs_confirmation')
        self.assertEqual(len(creates),1)

    def test_explicit_recreate_retains_apple_destination_after_settings_change(self):
        card=self.card()
        card.update(status='needs_confirmation',approved_revision=card['revision'],error_kind='unknown_outcome',save_intent={'backend':'apple','container':'synthetic-container','fingerprint':apple.fingerprint(pipeline.current_draft(card)),'identifiers':['before-a','before-b'],'has_key':True})
        cards.save_card(card)
        records,creates=['before-a','before-b'],[]
        with patch.dict(os.environ,{'BIZCARD_CONTACTS_BACKEND':'google','BIZCARD_APPLE_CONTAINER':'different'}),patch.object(apple,'run_helper',side_effect=self.helper_store(records,creates,'normal')) as run,patch.object(contacts,'save_contact',return_value=contacts.SaveResult('created','people/wrong')) as google:
            # A confirmed write in this test succeeds, so skip the lost-response branch.
            creates.append('prior-attempt')
            outcome=pipeline.resolve_confirmation(card,'again',pipeline.confirmation_token(card))
        self.assertTrue(outcome['ok'])
        google.assert_not_called()
        create=[c for c in run.call_args_list if c.args[0]=='create']
        self.assertEqual(create[0].kwargs['container'],'synthetic-container')

    def test_legacy_apple_unknown_without_intent_requires_confirmation(self):
        card=self.card()
        card.update(status='save_failed',approved_revision=card['revision'],error_kind='unknown_outcome')
        cards.save_card(card)
        with patch.object(apple,'run_helper',return_value={'identifiers':[],'identifier':'unsafe-duplicate'}) as run:
            pipeline.retry_due(force=True)
        self.assertEqual(cards.load_card(card['id'])['status'],'needs_confirmation')
        self.assertNotIn('create',[c.args[0] for c in run.call_args_list])

    def test_cli_and_launcher_have_documented_opposite_precedence(self):
        import subprocess
        from bizcard_intake import cli
        root=Path(self.tmp.name)/'launcher'
        (root/'scripts').mkdir(parents=True)
        (root/'bizcard_intake').mkdir()
        source=Path(__file__).resolve().parents[1]
        script=root/'scripts/start_bot.sh'
        script.write_bytes((source/'scripts/start_bot.sh').read_bytes())
        (root/'bizcard_intake/envfile.py').write_bytes((source/'bizcard_intake/envfile.py').read_bytes())
        fake=root/'fake-python'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$BIZCARD_CONTACTS_BACKEND" "$BIZCARD_APPLE_CONTAINER"\n')
        fake.chmod(0o700)
        (root/'.env').write_text('TELEGRAM_BOT_TOKEN=synthetic\nBIZCARD_CONTACTS_BACKEND=apple\nBIZCARD_APPLE_CONTAINER=file-container\n')
        process={'PATH':os.environ.get('PATH',os.defpath),'HOME':str(root),'BIZCARD_PYTHON':str(fake),'BIZCARD_CONTACTS_BACKEND':'google','BIZCARD_APPLE_CONTAINER':'process-container'}
        with patch.dict(os.environ,process,clear=True),patch.object(cli,'PROJECT_ROOT',root):
            self.assertTrue(cli._load_project_settings())
            self.assertEqual(os.environ['BIZCARD_CONTACTS_BACKEND'],'google')
            self.assertEqual(os.environ['BIZCARD_APPLE_CONTAINER'],'process-container')
        result=subprocess.run(['/bin/bash',str(script)],capture_output=True,text=True,env=process,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.splitlines(),['apple','file-container'])

    def test_gws_always_uses_documented_file_keyring_and_preserves_other_env(self):
        import subprocess
        with patch.dict(os.environ,{'GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND':'keychain','SYNTHETIC_PRESERVE':'yes'}),patch.object(contacts.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')) as run:
            contacts.run_gws(['auth','status'])
        self.assertEqual(run.call_args.kwargs['env']['GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND'],'file')
        self.assertEqual(run.call_args.kwargs['env']['SYNTHETIC_PRESERVE'],'yes')

    def test_missing_apple_identifier_without_intent_never_creates(self):
        def helper(command,**payload):
            if command=='get': raise contacts.ContactSaveError('gone',kind='not_found')
            if command in ('find','find-fingerprint'): return {'identifiers':[]}
            if command=='create': return {'identifier':'duplicate'}
            raise AssertionError(command)
        with patch.object(apple,'run_helper',side_effect=helper) as run:
            with self.assertRaises(apple.ConfirmationRequired):
                apple.save_contact(CardDraft(name='Synthetic',mobile=['01000000000']),None,resource_name='apple:shifted')
        self.assertNotIn('create',[c.args[0] for c in run.call_args_list])
