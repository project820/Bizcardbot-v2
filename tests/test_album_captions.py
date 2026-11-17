"""Album captions preserve each photo's own context across worker races."""
import contextlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bizcard_intake import cards, contacts, meeting_note, pipeline, telegram
from bizcard_intake.draft import CardDraft


class AlbumCaptionTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(cards, 'CARD_DIR', self.tmp))
        stack.enter_context(patch.dict(os.environ, {'BIZCARD_MEETING_NOTE': 'on'}))
        stack.enter_context(patch.object(telegram, 'submit'))
        stack.enter_context(patch.object(telegram, '_ensure_group_progress'))

    def receive(self, caption='', group='album'):
        telegram.handle_photo('token', 42, {'media_group_id': group, 'caption': caption}, [], self.tmp)
        return cards.group_cards(42, group)[-1]

    def assert_album(self, captions, expected):
        for caption in captions:
            self.receive(caption)
        members = cards.group_cards(42, 'album')
        self.assertEqual([c['caption'] for c in members], expected)
        for card, caption in zip(members, expected):
            meeting_note.enqueue(card)
            self.assertIn('만남 메모: ' + caption, card['note']['text'])

    def test_each_photo_keeps_its_caption_and_note(self):
        self.assert_album(['A', 'B', 'C'], ['A', 'B', 'C'])

    def test_only_uncaptioned_photo_uses_first_caption(self):
        self.assert_album(['A', '', 'B'], ['A', 'A', 'B'])

    def test_late_first_caption_fills_previous_photo(self):
        self.assert_album(['', 'B'], ['B', 'B'])

    def test_own_caption_is_durable_before_first_save_or_lock_contention(self):
        self.receive('A')
        original_save = cards.save_card
        captured = []
        held = contextlib.ExitStack()
        self.addCleanup(held.close)

        def hold_after_first_save(card):
            initial = cards.load_card(card['id']) is None
            if initial:
                captured.append(card.get('caption'))
            original_save(card)
            if initial:
                held.enter_context(pipeline.card_lock(card['id']))

        with patch.object(cards, 'save_card', side_effect=hold_after_first_save):
            card = self.receive('B')
        held.close()
        self.assertEqual(captured, ['B'])
        meeting_note.enqueue(card)
        self.assertEqual(card['caption'], 'B')
        self.assertIn('만남 메모: B', card['note']['text'])

    def test_members_scanned_only_when_first_caption_is_registered(self):
        original = cards.group_cards
        with patch.object(cards, 'group_cards', wraps=original) as scan:
            for caption in ('', 'A', 'B', ''):
                telegram.handle_photo('token', 42, {'media_group_id': 'album', 'caption': caption}, [], self.tmp)
        self.assertEqual(scan.call_count, 1)

    def test_late_caption_updates_pending_text_but_not_written_text(self):
        pending = self.receive('')
        written = self.receive('')
        for card, state in ((pending, 'pending'), (written, 'written')):
            card['note'] = {'state': state, 'text': 'old'}
            cards.save_card(card)
        self.receive('B')
        self.assertIn('만남 메모: B', cards.load_card(pending['id'])['note']['text'])
        self.assertEqual(cards.load_card(written['id'])['note']['text'], 'old')

    def test_back_caption_updates_pending_note_without_deadlock_or_spreading(self):
        for front, back, expected in (('', 'back', 'back'), ('front', 'back', 'front')):
            with self.subTest(front=front):
                card = self.receive(front, group=expected)
                peer = self.receive('', group=expected)
                card.update(status='review', awaiting_back=True,
                            note={'state': 'pending', 'text': 'old'})
                cards.save_card(card)
                errors = []
                def receive_back():
                    try:
                        telegram.handle_photo('token', 42, {'caption': back}, [], self.tmp)
                    except Exception as exc:
                        errors.append(exc)
                worker = threading.Thread(target=receive_back, daemon=True)
                worker.start()
                worker.join(2)
                self.assertFalse(worker.is_alive(), 'back caption deadlocked')
                self.assertEqual(errors, [])
                result = cards.load_card(card['id'])
                self.assertEqual(result['caption'], expected)
                if not front:
                    self.assertIn('만남 메모: back', result['note']['text'])
                self.assertEqual(cards.load_card(peer['id'])['caption'], front)

    def test_compose_uses_card_caption_before_album_fallback(self):
        card = self.receive('A')
        card['caption'] = 'B'
        self.assertIn('만남 메모: B', meeting_note.compose(card, CardDraft()))

    def test_drain_preserves_own_caption(self):
        card = self.receive('A')
        card.update(caption='B', status='saved', resource_name='people/test')
        meeting_note.enqueue(card)
        cards.save_card(card)
        with patch.object(contacts, 'set_note', return_value=True) as write:
            meeting_note.drain()
        self.assertIn('만남 메모: B', write.call_args.args[2])
        self.assertEqual(cards.load_card(card['id'])['caption'], 'B')

    def test_album_state_expires_on_write_and_migrates_legacy_strings(self):
        cards.save_state('album_captions', {
            '42:legacy': 'legacy caption',
            '42:expired': {'caption': 'expired', 'created_at': 1},
            '42:recent': {'caption': 'recent', 'created_at': 190000},
        })
        card = {'chat_id': '42', 'media_group_id': 'legacy'}
        self.assertEqual(cards.group_caption(card), ('legacy caption', False))
        with patch.object(cards.time, 'time', return_value=200000):
            result = cards.group_caption({'chat_id': '42', 'media_group_id': 'new'}, 'new')
        self.assertEqual(result, ('new', True))
        state = cards.load_state('album_captions')
        self.assertNotIn('42:expired', state)
        self.assertEqual(state['42:legacy'], {'caption': 'legacy caption', 'created_at': 200000})
        self.assertEqual(state['42:recent']['caption'], 'recent')
        with patch.object(cards.time, 'time', return_value=400000):
            cards.group_caption({'chat_id': '42', 'media_group_id': 'latest'}, 'latest')
        self.assertEqual(list(cards.load_state('album_captions')), ['42:latest'])
