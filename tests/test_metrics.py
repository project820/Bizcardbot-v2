import json
import os
import unittest
from unittest.mock import patch

from bizcard_intake import cards, metrics
from bizcard_intake.draft import CardDraft, EDITABLE_FIELDS


class CostTest(unittest.TestCase):
    def test_benchmark_fixed_costs(self):
        cases = [
            ('openai', 'gpt-6-luna', {'input_tokens': 1000, 'output_tokens': 200}, .267),
            ('openai', 'gpt-6-luna', {'input_tokens': 1000, 'output_tokens': 200,
             'input_tokens_details': {'cached_tokens': 400}}, .21894),
            ('openai', 'gpt-6-luna', {'input_tokens': 1000, 'output_tokens': 200,
             'input_tokens_details': {'cached_tokens': 400, 'cache_write_tokens': 200,
              'cache_write_tokens_details': {'ttl_12h_tokens': 50}}}, .23395875),
            ('openai', 'gpt-5.6-luna', {'input_tokens': 1000, 'output_tokens': 200,
             'input_tokens_details': {'cache_creation_tokens': 200}}, .60075),
            ('anthropic', 'claude-haiku-5-5', {'input_tokens': 1000, 'output_tokens': 200,
             'cache_read_input_tokens': 400, 'cache_creation_input_tokens': 200,
             'cache_creation': {'ephemeral_1h_input_tokens': 50}}, .31072125),
        ]
        with patch.dict(os.environ, {'BIZCARD_USD_KRW': '1335'}):
            for provider, model, usage, expected in cases:
                with self.subTest(provider=provider, usage=usage):
                    self.assertAlmostEqual(metrics.cost_krw(provider, model, usage), expected, places=10)

    def test_unknown_or_missing_usage_is_unknown(self):
        for provider, model, usage in [
            ('codex', 'gpt-6-luna', {'input_tokens': 1, 'output_tokens': 1}),
            ('openai', 'new-model', {'input_tokens': 1, 'output_tokens': 1}),
            ('openai', 'gpt-6-luna', {}),
            ('openai', 'gpt-6-luna', {'input_tokens': 1}),
            ('openai', 'gpt-6-luna', {'input_tokens': 272001, 'output_tokens': 1}),
            ('anthropic', 'claude-haiku-5-5', {'input_tokens': 100001, 'output_tokens': 1}),
            ('openai', 'gpt-6-luna', {'input_tokens': 'invalid', 'output_tokens': 1}),
        ]:
            with self.subTest(provider=provider, model=model, usage=usage):
                self.assertIsNone(metrics.cost_krw(provider, model, usage))

    def test_exchange_rate_and_invalid_setting(self):
        usage = {'input_tokens': 1000, 'output_tokens': 200}
        with patch.dict(os.environ, {'BIZCARD_USD_KRW': '1500'}):
            self.assertAlmostEqual(metrics.cost_krw('openai', 'gpt-6-luna', usage), .3)
        for setting in ('bad', 'nan', '-1', 'inf'):
            with patch.dict(os.environ, {'BIZCARD_USD_KRW': setting}):
                self.assertAlmostEqual(metrics.cost_krw('openai', 'gpt-6-luna', usage), .267)


class ReviewTest(unittest.TestCase):
    def test_compares_all_editable_draft_fields_without_values(self):
        for field in EDITABLE_FIELDS:
            before = CardDraft()
            after = CardDraft()
            setattr(after, field, ['private-value'] if isinstance(getattr(after, field), list) else 'private-value')
            card = {}
            metrics.record_edit(card, before, after, 'manual_edit')
            with self.subTest(field=field):
                self.assertEqual(card['review']['edited'], [field])
                self.assertEqual(card['review']['manual_edits'], 1)
                self.assertNotIn('private-value', json.dumps(card))

    def test_reverts_stay_edited_and_ai_and_manual_are_counted(self):
        before, after = CardDraft(title='before'), CardDraft(title='after')
        card = {}
        metrics.record_edit(card, before, after, 'manual_edit')
        metrics.record_edit(card, after, before, 'ai_edit')
        self.assertEqual(card['review']['edited'], ['title'])
        self.assertEqual(card['review']['manual_edits'], 1)
        self.assertEqual(card['review']['ai_edits'], 1)

    def test_initial_recognition_back_and_retry_are_distinguished(self):
        card = {}
        before, after = CardDraft(), CardDraft(name='Example', warnings=['private-warning'])
        metrics.record_edit(card, before, after, 'recognized')
        self.assertEqual(card['review']['edited'], [])
        self.assertEqual(card['review']['warnings'], 1)
        metrics.record_edit(card, after, after, 'rerecognized')
        metrics.record_edit(card, after, after, 'back_added')
        self.assertTrue(card['review']['rerecognized'])
        self.assertTrue(card['review']['back_added'])
        self.assertNotIn('private-warning', json.dumps(card))

    def test_existing_pipeline_events_mark_retry_and_back(self):
        card = {}
        metrics.record_edit(card, CardDraft(), CardDraft(name='Example'), 'recognized')
        self.assertFalse(card['review']['rerecognized'])
        metrics.record_edit(card, CardDraft(name='Example'), CardDraft(name='Example'), 'recognized')
        self.assertTrue(card['review']['rerecognized'])
        metrics.record_edit(card, CardDraft(name='Example'), CardDraft(name='Example'), 'back_merged')
        self.assertTrue(card['review']['back_added'])

    def test_locked_and_warning_only_changes_are_not_field_edits(self):
        card = {}
        metrics.record_edit(card, CardDraft(), CardDraft(locked=['name'], warnings=['private']), 'manual_edit')
        self.assertEqual(card['review']['edited'], [])

    def test_record_calls_accumulates_only_cost_count_and_boolean_agreement(self):
        card = {}
        calls = [{'provider': 'openai', 'model': 'gpt-6-luna', 'usage': {}, 'krw': .4,
                  'agree': {'name': True, 'address': False}}]
        self.assertEqual(metrics.record_calls(card, calls), .4)
        self.assertEqual(metrics.record_calls(card, [{'krw': .6}]), .6)
        self.assertEqual(card['cost'], {'krw': 1.0, 'calls': 2})
        self.assertEqual(card['review']['agree'], {'name': True, 'address': False})
        self.assertNotIn('usage', json.dumps(card))

    def test_missing_cost_stays_unknown_and_empty_collection_is_noop(self):
        card = {}
        self.assertIsNone(metrics.record_calls(card, []))
        self.assertNotIn('cost', card)
        metrics.record_calls(card, [{'krw': None}])
        self.assertEqual(metrics.record_calls(card, [{'krw': .5}]), .5)
        self.assertEqual(card['cost'], {'krw': None, 'calls': 2})

    def test_summary_excludes_untracked_reviews_and_unknown_costs(self):
        saved = [
            {'review': {'edited': []}, 'cost': {'krw': 1}},
            {'review': {'edited': ['title']}, 'cost': {'krw': 3}},
            {},
        ]
        with patch.object(cards, 'list_cards', return_value=saved) as listed:
            result = metrics.summary(123)
        listed.assert_called_once_with(123, ['saved'])
        self.assertEqual(result, {'saved': 3, 'unedited_rate': .5, 'avg_krw': 2.0})

    def test_summary_empty_and_legacy_cards_have_unknown_rates(self):
        for saved in ([], [{}]):
            with patch.object(cards, 'list_cards', return_value=saved):
                self.assertEqual(metrics.summary('123'), {'saved': len(saved), 'unedited_rate': None, 'avg_krw': None})


if __name__ == '__main__':
    unittest.main()
