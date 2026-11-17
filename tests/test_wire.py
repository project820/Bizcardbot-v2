"""Contract tests for the v2.3 channel wiring (parallel modules stay mocked)."""
import contextlib
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bizcard_intake
from bizcard_intake import cards, chat_ux, parser, pipeline, telegram
from bizcard_intake.contacts import SaveResult
from bizcard_intake.draft import CardDraft


class WireTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.tmp = Path(tmp)
        self.stack.enter_context(patch.object(cards, 'CARD_DIR', self.tmp / 'cards'))
        self.stack.enter_context(patch.object(pipeline, 'SESSION_ARCHIVE_DIR', self.tmp / 'archive'))
        self.stack.enter_context(patch.object(pipeline.contact_backend, 'find_job_change_candidate', return_value=None))
        self.stack.enter_context(patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '42', 'BIZCARD_TELEGRAM_INTERVAL': '0', 'BIZCARD_CONTACTS_BACKEND': 'google'}))
        self.stack.enter_context(patch.object(telegram, '_executor', None))
        for name in ('_note_drain_lock', '_note_notice_lock', '_automation_lock'):
            self.stack.enter_context(patch.object(telegram, name, threading.Lock()))
        self.send = self.stack.enter_context(patch.object(telegram, 'send_message', return_value={'result': {'message_id': 1}}))
        self.stack.enter_context(patch.object(telegram, 'api_post', return_value={'ok': True}))
        self.calls = [{'provider': 'openai', 'model': 'fixture', 'usage': {}, 'krw': 2.4}]
        @contextlib.contextmanager
        def collect():
            yield self.calls
        self.stack.enter_context(patch.object(parser, 'collect', collect, create=True))
        self.metrics = self.module('metrics', record_edit=Mock(), record_calls=Mock(return_value=2.4), summary=Mock(return_value={'saved': 2, 'unedited_rate': .5, 'avg_krw': 1.0}))
        self.note = self.module('meeting_note', enqueue=Mock(side_effect=lambda card: card.update(note={'state': 'pending', 'text': 'fixture', 'attempts': 0, 'next_at': 0})), drain=Mock(return_value={'written': 0, 'pending': 1, 'automation': 'not_determined'}), status=Mock(return_value={'automation': 'not_determined', 'pending': 1, 'last_notice_at': None}), request_automation=Mock(return_value='authorized'), _state=Mock(side_effect=lambda **fields: cards.save_state('notes', {**cards.load_state('notes'), **fields})))
        self.export = self.module('export_csv', build=Mock(return_value=(b'csv', 2)))

    def module(self, name, **attrs):
        module = types.ModuleType('bizcard_intake.' + name)
        module.__dict__.update(attrs)
        self.stack.enter_context(patch.dict(sys.modules, {module.__name__: module}))
        self.stack.enter_context(patch.object(bizcard_intake, name, module, create=True))
        return module

    def card(self, chat_id=42):
        card = cards.new_card(chat_id)
        pipeline.set_draft(card, CardDraft(name='Fixture', mobile=['010-0000-0001']), 'test')
        card['status'] = 'review'
        cards.save_card(card)
        return card

    def save(self, card):
        with patch.object(pipeline, 'save_contact', return_value=SaveResult('created', 'people/fixture', 'missing', [], 'Fixture')):
            return pipeline.approve(card)

    def test_success_enqueues_and_persists_note_without_changing_saved(self):
        card = self.card()
        self.assertTrue(self.save(card)['ok'])
        self.note.enqueue.assert_called_once_with(card)
        stored = cards.load_card(card['id'])
        self.assertEqual(stored.get('note', {}).get('state'), 'pending')
        self.assertEqual(stored['status'], 'saved')

    def test_enqueue_failure_does_not_fail_contact_save(self):
        self.note.enqueue.side_effect = RuntimeError('fixture')
        card = self.card()
        with self.assertLogs('bizcard', level='WARNING'):
            self.assertTrue(self.save(card)['ok'])
        self.assertEqual(cards.load_card(card['id'])['status'], 'saved')

    def test_edit_hook_gets_previous_and_new_draft(self):
        card = self.card()
        pipeline.set_draft(card, CardDraft(name='Fixture', title='과장', mobile=['010-0000-0001']), 'manual_edit')
        args = self.metrics.record_edit.call_args.args
        self.assertEqual(args[1].title, '')
        self.assertEqual(args[2].title, '과장')
        self.assertEqual(args[3], 'manual_edit')

    def test_front_and_rerecognition_collect_calls_and_show_current_cost(self):
        card = self.card()
        result = {'images': {}, 'rectifier': 'fixture', 'draft': pipeline.current_draft(card)}
        with patch.object(pipeline, 'scan_front', return_value=result):
            pipeline.recognize_front(card, self.tmp / 'front.jpg')
            pipeline.recognize_front(card)
        self.assertEqual(self.metrics.record_calls.call_count, 2)
        self.assertEqual(self.metrics.record_calls.call_args.args[1], self.calls)
        self.assertIn('참고: 이 사진의 AI 비용 2.4원(기준 2원 초과)', pipeline.draft_text(card))
        self.assertEqual(self.metrics.record_edit.call_args.args[3], 'rerecognized')

    def test_unknown_cost_clears_prior_warning(self):
        card = self.card()
        card['recognition_cost_krw'] = 3.0
        cards.save_card(card)
        self.metrics.record_calls.return_value = None
        with patch.object(pipeline, 'scan_front', return_value={'images': {}, 'rectifier': 'fixture', 'draft': pipeline.current_draft(card)}):
            pipeline.recognize_front(card, self.tmp / 'front.jpg')
        self.assertNotIn('AI 비용', pipeline.draft_text(card))

    def test_ai_edit_collects_calls_and_records_edit(self):
        card = self.card()
        with patch.object(pipeline, 'revise_draft', return_value=CardDraft(name='Revised', mobile=['010-0000-0001'])):
            self.assertTrue(pipeline.edit(card, '이름을 다시 읽어 주세요')[0])
        self.metrics.record_calls.assert_called_once()
        self.assertEqual(self.metrics.record_edit.call_args.args[3], 'ai_edit')

    def test_album_caption_is_shared_even_when_caption_arrives_late(self):
        with patch.object(telegram, 'submit'), patch.object(telegram, '_ensure_group_progress'):
            telegram.handle_photo('token', 42, {'media_group_id': 'album'}, [], self.tmp)
            telegram.handle_photo('token', 42, {'media_group_id': 'album', 'caption': '만남' * 200}, [], self.tmp)
            telegram.handle_photo('token', 42, {'media_group_id': 'album'}, [], self.tmp)
        members = cards.group_cards(42, 'album')
        self.assertEqual([c.get('caption') for c in members], [('만남' * 200)[:300]] * 3)

    def test_single_photo_caption_is_persisted(self):
        with patch.object(telegram, 'submit'), patch.object(telegram, '_remember_card_message'):
            telegram.handle_photo('token', 42, {'caption': '전시회'}, [], self.tmp)
        self.assertEqual(cards.list_cards(42)[0].get('caption'), '전시회')

    def test_export_command_and_button_route_without_card_lookup(self):
        with patch.object(telegram, 'send_export', create=True) as export:
            telegram.handle_text('token', 42, {'text': '/export'})
            telegram.handle_callback('token', {'data': 'ex'}, 42)
        self.assertEqual(export.call_count, 2)

    def test_export_rejects_other_chat_before_build(self):
        telegram.handle_callback('token', {'data': 'ex'}, 99)
        self.export.build.assert_not_called()

    def test_export_uses_memory_bytes_count_and_summary(self):
        with patch.object(telegram, 'send_document', create=True) as document:
            telegram.send_export('token', 42)
        self.export.build.assert_called_once_with(42)
        args = document.call_args
        self.assertEqual(args.args[2], b'csv')
        self.assertRegex(args.args[3], r'^bizcards-\d{8}\.csv$')
        self.assertEqual(args.kwargs['caption'], '저장한 명함 2건 · 수정 없이 저장 50% · 평균 1.0원')

    def test_empty_export_and_oversize_do_not_send_document(self):
        with patch.object(telegram, 'send_document', create=True) as document:
            self.export.build.return_value = (b'', 0)
            telegram.send_export('token', 42)
            self.assertEqual(self.send.call_args.args[2], '저장한 명함이 없습니다.')
            self.export.build.return_value = (b'x' * (45 * 1024 * 1024 + 1), 2)
            telegram.send_export('token', 42)
        document.assert_not_called()

    def test_list_with_no_open_cards_still_has_export_button(self):
        telegram.send_open_list('token', 42)
        self.assertIn({'text': 'CSV 내보내기', 'callback_data': 'ex'}, self.send.call_args.kwargs['reply_markup']['inline_keyboard'][0])

    def test_pending_notice_is_daily_and_chat_scoped(self):
        card = self.card()
        card.update(status='saved', resource_name='apple:fixture', note={'state': 'pending'})
        cards.save_card(card)
        telegram.maybe_note_notice('token', 42)
        telegram.maybe_note_notice('token', 42)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.send.call_args.args[2], '메모 1건이 대기 중입니다. Mac mini 화면을 볼 수 있을 때 누르세요.')
        telegram.maybe_note_notice('token', 99)
        self.assertEqual(self.send.call_count, 1)

    def test_permission_request_uses_dedicated_thread_not_worker_pool(self):
        with patch.object(telegram.threading, 'Thread') as thread, patch.object(telegram, 'submit') as submit:
            telegram.handle_callback('token', {'data': 'na'}, 42)
        thread.return_value.start.assert_called_once()
        submit.assert_not_called()
        self.assertEqual(self.send.call_args.args[2], "Mac mini 화면의 창에서 '허용'을 누르세요. 10분 동안 기다립니다.")

    def test_check_permission_never_requests_automation(self):
        telegram.handle_callback('token', {'data': 'nc'}, 42)
        self.note.request_automation.assert_not_called()
        self.note.drain.assert_called_once()

    def test_note_drain_submits_only_one_worker_at_a_time(self):
        with patch.object(telegram, 'submit') as submit:
            telegram.schedule_note_drain('token')
            telegram.schedule_note_drain('token')
            self.assertEqual(submit.call_count, 1)
            fn, *args = submit.call_args.args
            fn(*args)
            telegram.schedule_note_drain('token')
            self.assertEqual(submit.call_count, 2)
            fn, *args = submit.call_args.args
            fn(*args)

    def test_polling_submits_note_scan_on_start_and_after_sixty_seconds(self):
        current = [0.0]
        polls = [0]
        def updates(*args):
            polls[0] += 1
            if polls[0] == 2:
                raise KeyboardInterrupt
            current[0] = 61.0
            return []
        with contextlib.ExitStack() as stack:
            for name in ('initialize_contacts', '_inspect_webhook', 'delete_webhook', 'health_check',
                         'retry_due_and_report', '_safe_send_due_followups', 'recover_interrupted'):
                stack.enter_context(patch.object(telegram, name))
            stack.enter_context(patch.object(pipeline, 'migrate_legacy', return_value=[]))
            stack.enter_context(patch.object(telegram, 'api_get', return_value={'result': {'id': 1, 'username': 'fixture'}}))
            stack.enter_context(patch.object(telegram, 'ThreadPoolExecutor'))
            stack.enter_context(patch.object(telegram.time, 'monotonic', side_effect=lambda: current[0]))
            stack.enter_context(patch.object(telegram, 'get_updates', side_effect=updates))
            scan = stack.enter_context(patch.object(telegram, 'schedule_note_drain'))
            with self.assertRaises(KeyboardInterrupt):
                telegram._run('token', self.tmp)
        self.assertEqual(scan.call_count, 2)

    def test_startup_registers_export_command_and_ignores_registration_failure(self):
        with patch.object(telegram, 'api_post', side_effect=RuntimeError('fixture')) as post:
            telegram.register_commands('token')
        self.assertEqual(post.call_args.args[1], 'setMyCommands')
        self.assertIn({'command': 'export', 'description': '명함 내보내기'}, post.call_args.args[2]['commands'])

    def test_notice_acknowledges_through_note_state_owner(self):
        card = self.card()
        card.update(status='saved', resource_name='apple:fixture', note={'state': 'pending'})
        cards.save_card(card)
        state = {'automation': 'not_determined', 'pending': 1}
        cards.save_state('notes', state)
        telegram.maybe_note_notice('token', 42)
        self.note._state.assert_called_once()
        self.assertEqual(cards.load_state('notes')['automation'], 'not_determined')
        self.assertIsInstance(cards.load_state('notes')['last_notice_at'], float)

    def test_permission_actions_reject_other_chats(self):
        with patch.object(telegram.threading, 'Thread') as thread:
            telegram.handle_callback('token', {'data': 'na'}, 99)
            telegram.handle_callback('token', {'data': 'nc'}, 99)
        thread.assert_not_called()
        self.note.drain.assert_not_called()

    def test_batch_completion_has_export_button(self):
        self.card()
        with patch.object(pipeline, 'approve', return_value={'ok': True, 'message': 'Fixture'}), patch.object(telegram.contacts, 'contact_snapshot', return_value=contextlib.nullcontext()), patch.object(telegram, 'edit_message') as edit:
            telegram.approve_batch('token', 42, 'all')
        self.assertEqual(edit.call_args.kwargs['reply_markup'], telegram.export_buttons())

    def test_cost_threshold_can_be_configured_without_using_card_total(self):
        card = self.card()
        card.update(cost={'krw': 100, 'calls': 10}, recognition_cost_krw=2.4)
        with patch.dict(os.environ, {'BIZCARD_COST_WARN_KRW': '3'}):
            self.assertNotIn('AI 비용', pipeline.draft_text(card))
        with patch.dict(os.environ, {'BIZCARD_COST_WARN_KRW': 'invalid'}):
            self.assertIn('기준 2원 초과', pipeline.draft_text(card))

    def test_check_permission_uses_fresh_drain_status(self):
        self.note.status.return_value['automation'] = 'denied'
        self.note.drain.return_value = {'written': 1, 'pending': 0, 'automation': 'authorized'}
        telegram.handle_callback('token', {'data': 'nc'}, 42)
        self.assertIn('허용을 확인했습니다.', self.send.call_args_list[0].args[2])

    def test_back_collects_parse_and_merge_cost_and_persists_review(self):
        card = self.card()
        def record(card, calls):
            card['cost'] = {'krw': 2.4, 'calls': len(calls)}
            return 2.4
        self.metrics.record_calls.side_effect = record
        def record_edit(card, before, after, event):
            card['review'] = {'back_added': event == 'back_added'}
        self.metrics.record_edit.side_effect = record_edit
        prepared = types.SimpleNamespace(llm_path=self.tmp / 'back.jpg', profile_path=None,
                                         ocr_path=None, rectified_path=None)
        with patch.object(pipeline, '_prepare_oriented', return_value=(prepared, None, False)), patch.object(pipeline, 'parse_card', return_value=pipeline.current_draft(card)), patch.object(pipeline, 'merge_drafts', side_effect=lambda a, b: a), patch.object(pipeline, 'cleanup_paths'):
            pipeline.recognize_back(card, self.tmp / 'back.jpg')
        stored = cards.load_card(card['id'])
        self.assertEqual(stored['cost'], {'krw': 2.4, 'calls': 1})
        self.assertEqual(stored['review'], {'back_added': True})

    def test_cost_failure_does_not_prevent_review(self):
        card = self.card()
        self.metrics.record_calls.side_effect = RuntimeError('fixture')
        with patch.object(pipeline, 'scan_front', return_value={'images': {}, 'rectifier': 'fixture', 'draft': pipeline.current_draft(card)}):
            pipeline.recognize_front(card, self.tmp / 'front.jpg')
        self.assertEqual(cards.load_card(card['id'])['status'], 'review')

    def test_failed_ai_edit_still_persists_call_cost(self):
        card = self.card()
        def record(card, calls):
            card['cost'] = {'krw': 2.4, 'calls': len(calls)}
            return 2.4
        self.metrics.record_calls.side_effect = record
        with patch.object(pipeline, 'revise_draft', return_value=None):
            self.assertFalse(pipeline.edit(card, '다시 읽어 주세요')[0])
        self.assertEqual(cards.load_card(card['id'])['cost']['krw'], 2.4)

    def test_note_persist_failure_keeps_durable_saved_state(self):
        card = self.card()
        persist = pipeline._persist_save_fields
        def fail_note(card, *fields, **kwargs):
            if fields == ('note',):
                raise OSError('fixture')
            return persist(card, *fields, **kwargs)
        with patch.object(pipeline, '_persist_save_fields', side_effect=fail_note):
            self.assertTrue(self.save(card)['ok'])
        self.assertEqual(cards.load_card(card['id'])['status'], 'saved')

    def test_document_upload_is_multipart_and_keeps_bytes_in_memory(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        with patch.object(telegram, 'urlopen', return_value=response) as open_url:
            self.assertTrue(telegram.send_document('token', 42, b'CSV-BYTES', 'bizcards-20261118.csv', caption='fixture')['ok'])
        request = open_url.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/sendDocument'))
        self.assertIn(b'name="document"; filename="bizcards-20261118.csv"', request.data)
        self.assertIn(b'CSV-BYTES', request.data)

    def test_automation_authorized_queues_drain_and_denied_offers_recheck(self):
        for result in ('authorized', 'denied', 'timeout'):
            with self.subTest(result=result):
                self.note.request_automation.return_value = result
                def run_thread(**kwargs):
                    return types.SimpleNamespace(start=kwargs['target'])
                with patch.object(telegram.threading, 'Thread', side_effect=run_thread), patch.object(telegram, 'schedule_note_drain') as drain:
                    telegram.request_note_permission('token', 42)
                self.assertEqual(drain.called, result == 'authorized')
                if result != 'authorized':
                    self.assertEqual(self.send.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'nc')

    def test_note_drain_releases_claim_after_error_and_submission_failure(self):
        self.note.drain.side_effect = RuntimeError('fixture')
        telegram.schedule_note_drain('token')
        self.assertFalse(telegram._note_drain_lock.locked())
        with patch.object(telegram, 'submit', side_effect=RuntimeError('fixture')):
            telegram.schedule_note_drain('token')
        self.assertFalse(telegram._note_drain_lock.locked())

    def test_failed_notes_notify_once_and_do_not_cross_chats(self):
        for chat in (42, 99):
            card = self.card(chat)
            card.update(status='saved', note={'state': 'failed', 'notice_pending': True})
            cards.save_card(card)
        telegram.schedule_note_drain('token')
        telegram.schedule_note_drain('token')
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('메모 1건을 추가하지 못했습니다.', self.send.call_args.args[2])
        self.assertFalse(cards.list_cards(42)[0]['note']['notice_pending'])
        self.assertTrue(cards.list_cards(99)[0]['note']['notice_pending'])

    def test_google_note_never_asks_for_mac_automation(self):
        card = self.card()
        card.update(status='saved', resource_name='people/fixture', note={'state': 'pending'})
        cards.save_card(card)
        telegram.maybe_note_notice('token', 42)
        self.send.assert_not_called()
        self.assertNotIn('Mac 권한', chat_ux.save_result_text(card, {'ok': True, 'message': 'Fixture'}))

    def test_saved_pending_note_uses_design_wording(self):
        card = self.card()
        card['note'] = {'state': 'pending'}
        card['resource_name'] = 'apple:fixture'
        text = chat_ux.save_result_text(card, {'ok': True, 'message': 'Fixture'})
        self.assertIn('참고: 메모는 Mac 권한을 허용하면 자동으로 추가합니다.', text)
        self.note.status.return_value['automation'] = 'authorized'
        self.assertNotIn('Mac 권한', chat_ux.save_result_text(card, {'ok': True, 'message': 'Fixture'}))


if __name__ == '__main__':
    unittest.main()
