import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import parser


class CrosscheckSpeedTest(unittest.TestCase):
    def test_only_haiku_gets_concise_warning_guidance(self):
        prompts = {}
        def model(name):
            def run(prompt, image):
                prompts[name] = prompt
                return {'name': 'Example'}
            return run
        with patch.object(parser, 'run_openai', model('luna')), \
             patch.object(parser, 'run_anthropic', model('haiku')):
            result = parser.run_crosscheck('original OCR prompt', Path('/image.jpg'), '')
        self.assertEqual(prompts['luna'], 'original OCR prompt')
        self.assertTrue(prompts['haiku'].startswith('original OCR prompt'))
        self.assertIn('warnings', prompts['haiku'])
        self.assertIn('정상', prompts['haiku'])
        self.assertEqual(result['name'], 'Example')

    def test_models_still_run_concurrently(self):
        rendezvous = threading.Barrier(2, timeout=2)
        def model(prompt, image):
            rendezvous.wait()
            return {'name': 'Example'}
        with patch.object(parser, 'run_openai', model), \
             patch.object(parser, 'run_anthropic', model):
            result = parser.run_crosscheck('original', Path('/image.jpg'), '')
        self.assertEqual(result['name'], 'Example')

    def test_haiku_warnings_are_available_if_luna_fails(self):
        fallback = {'name': 'Example', 'warnings': ['이메일 판독 불명확']}
        with patch.object(parser, 'run_openai', side_effect=RuntimeError('offline')), \
             patch.object(parser, 'run_anthropic', return_value=fallback):
            self.assertEqual(parser.run_crosscheck('original', Path('/image.jpg'), ''), fallback)


if __name__ == '__main__':
    unittest.main()
