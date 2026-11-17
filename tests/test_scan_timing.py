import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import parser, pipeline, vision_ocr
from bizcard_intake.image_processing import PreparedImages
from bizcard_intake.vision_ocr import OcrResult


class ScanTimingTest(unittest.TestCase):
    def test_model_and_merge_timing_preserves_result_and_hides_prompt(self):
        with patch.object(parser, 'crosscheck_enabled', return_value=True), \
             patch.object(parser, 'run_openai', return_value={'name': 'Example'}), \
             patch.object(parser, 'run_anthropic', return_value={'name': 'Example'}), \
             self.assertLogs('bizcard', level='INFO') as captured:
            result = parser.parse_card(Path('/private-card.jpg'), OcrResult(()), 'front')
        self.assertEqual(result.name, 'Example')
        joined = '\n'.join(captured.output)
        for stage in ('prompt', 'luna', 'haiku', 'merge'):
            self.assertRegex(joined, rf'perf stage={stage} seconds=\d+\.\d+')
        self.assertNotIn('Example', joined)
        self.assertNotIn('private-card', joined)

    def test_orientation_retry_has_separate_stage_timings(self):
        paths = PreparedImages(*(Path('/unused') for _ in range(4)), 'vision')
        with patch.object(pipeline, 'prepare_card_images', return_value=paths), \
             patch.object(pipeline, '_ocr', return_value=OcrResult(())), \
             patch.object(pipeline, '_orientation', side_effect=[(1, 1, 20), (0, 1, 20)]), \
             self.assertLogs('bizcard', level='INFO') as captured:
            result = pipeline._prepare_oriented(Path('/input.jpg'), Path('/profile.jpg'))
        self.assertFalse(result[2])
        joined = '\n'.join(captured.output)
        for stage in ('prepare', 'ocr', 'orientation', 'reprepare', 'reocr', 'reorientation'):
            self.assertIn(f'perf stage={stage} seconds=', joined)

    def test_timer_logs_even_on_failure(self):
        self.assertTrue(hasattr(vision_ocr, 'stage_timer'))
        with self.assertLogs('bizcard', level='INFO') as captured:
            with self.assertRaisesRegex(ValueError, 'failure'):
                with vision_ocr.stage_timer('sample'):
                    raise ValueError('failure')
        self.assertIn('perf stage=sample seconds=', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
