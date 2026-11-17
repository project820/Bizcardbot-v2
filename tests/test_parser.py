import os
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import parser
from bizcard_intake.draft import CardDraft
from bizcard_intake.vision_ocr import OcrLine, OcrResult


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class ParserTest(unittest.TestCase):
    def test_codex_command_uses_lean_flags(self):
        with patch.object(parser, "find_binary", lambda name: "/bin/codex"), patch.dict(os.environ, {"BIZCARD_CODEX_MODEL": "m"}):
            command = parser.codex_command(True, Path("/img.jpg"), "/tmp/empty")
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--image", "-C"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("-m") + 1], "m")
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--output-schema", command)  # triples token usage on codex

    def test_claude_command_uses_json_schema_and_read_tool_for_images(self):
        with patch.object(parser, "find_binary", lambda name: "/bin/claude"):
            with_image = parser.claude_command(Path("/img.jpg"))
            text_only = parser.claude_command(None)
        self.assertIn("--json-schema", with_image)
        self.assertEqual(with_image[with_image.index("--allowedTools") + 1], "Read")
        self.assertIn("--tools", text_only)

    def test_backend_auto_prefers_codex_when_installed(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "auto"}), patch.object(parser, "openai_api_key", lambda: ""):
            with patch.object(parser, "find_binary", lambda name: "/bin/codex" if name == "codex" else None):
                self.assertEqual(parser.backend_name(), "codex")
            with patch.object(parser, "find_binary", lambda name: None):
                self.assertEqual(parser.backend_name(), "claude")
        with patch.dict(os.environ, {"BIZCARD_PARSER": "claude"}):
            self.assertEqual(parser.backend_name(), "claude")

    def test_parse_card_extracts_json_from_noisy_codex_output(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs["input"]))
            return Completed('tokens used\n5,000\n{"name": "홍길동", "title": "", "company": "ACME", "mobile": ["010-1234-5678"], "work": [], "fax": [], "email": [], "website": [], "address": [], "notes": "", "warnings": []}\n')

        ocr = OcrResult((OcrLine("홍길동", 0.9), OcrLine("010-1234-5678", 0.8)))
        with patch.dict(os.environ, {"BIZCARD_PARSER": "codex"}), patch.object(parser, "find_binary", lambda name: "/bin/codex"), patch.object(parser.subprocess, "run", fake_run):
            draft = parser.parse_card(Path("/img.jpg"), ocr, "front")

        self.assertEqual(draft.display_name, "홍길동 ACME")
        self.assertIn("010-1234-5678", calls[0][1])  # OCR text is passed to the model
        self.assertIn("--image", calls[0][0])

    def test_parse_card_falls_back_to_warning_when_backend_fails(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "codex"}), patch.object(parser, "find_binary", lambda name: "/bin/codex"), patch.object(
            parser.subprocess, "run", lambda *a, **k: Completed("", 1, "boom")
        ):
            draft = parser.parse_card(Path("/img.jpg"), OcrResult(()), "front")
        self.assertFalse(draft.is_savable)
        self.assertTrue(any("자동 파싱 실패" in w for w in draft.warnings))

    def test_revise_draft_returns_none_on_failure_and_draft_on_success(self):
        draft = CardDraft.from_dict({"name": "홍길동", "work": ["052-555-0126~7"]})
        with patch.object(parser, "run_backend", lambda prompt, image_path=None: None):
            self.assertIsNone(parser.revise_draft(draft, "두 회선"))
        with patch.object(parser, "run_backend", lambda prompt, image_path=None: {"name": "홍길동", "work": ["052-555-0126", "052-555-0127"]}):
            self.assertEqual(parser.revise_draft(draft, "두 회선").work, ["052-555-0126", "052-555-0127"])

    def test_extract_json(self):
        self.assertEqual(parser.extract_json('noise {"a": 1} tail'), {"a": 1})
        self.assertIsNone(parser.extract_json("no json"))
        self.assertIsNone(parser.extract_json("[1, 2]"))


class OpenAIBackendTest(unittest.TestCase):
    def test_auto_prefers_openai_when_a_key_is_stored(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "auto"}), patch.object(parser, "openai_api_key", lambda: "sk-test"):
            self.assertEqual(parser.backend_name(), "openai")

    def test_key_is_read_from_env_file_without_sourcing(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("# comment\nexport OPENAI_API_KEY='sk-file'\n")
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env["BIZCARD_OPENAI_ENV_FILE"] = handle.name
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(parser.openai_api_key(), "sk-file")
            self.assertEqual(parser.openai_key_source(), handle.name)
        os.unlink(handle.name)

    def test_default_key_search_is_limited_to_shared_openai_credentials(self):
        self.assertEqual(parser.OPENAI_KEY_FILES, ('~/.config/openai/credentials.env',))
        seen = []
        def missing(path, **kwargs):
            seen.append(str(path))
            raise FileNotFoundError
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'read_text', missing):
            self.assertEqual(parser.openai_api_key(), '')
        self.assertEqual(seen, [str(Path('~/.config/openai/credentials.env').expanduser())])

    def test_process_key_precedes_explicit_file_without_reading_files(self):
        env = {'OPENAI_API_KEY': 'test-env-key', 'BIZCARD_OPENAI_ENV_FILE': '/unused.env'}
        with patch.dict(os.environ, env, clear=True), patch.object(Path, 'read_text', side_effect=AssertionError('no file reads')):
            self.assertEqual(parser.openai_api_key(), 'test-env-key')
            self.assertEqual(parser.openai_key_source(), 'OPENAI_API_KEY')

    def test_request_uses_luna_medium_strict_schema_and_inline_image(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(b"jpg")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BIZCARD_OPENAI_MODEL", None)
            os.environ.pop("BIZCARD_REASONING_EFFORT", None)
            body = parser.openai_request_body("prompt", Path(handle.name))
        os.unlink(handle.name)
        self.assertEqual((body["model"], body["reasoning"]["effort"]), ("gpt-6-luna", "low"))
        fmt = body["text"]["format"]
        self.assertTrue(fmt["strict"])
        self.assertEqual(set(fmt["schema"]["required"]), set(fmt["schema"]["properties"]))
        image = body["input"][0]["content"][1]
        self.assertTrue(image["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertFalse(body["store"])

    def test_api_failure_falls_back_to_codex(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command[0])
            return Completed('{"name": "홍길동", "mobile": ["010-1"]}')

        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai"}), patch.object(parser, "run_openai", side_effect=RuntimeError("OpenAI HTTP 429")), patch.object(
            parser, "find_binary", lambda name: "/bin/codex" if name == "codex" else None
        ), patch.object(parser.subprocess, "run", fake_run):
            data = parser.run_backend("p")
        self.assertEqual(data["name"], "홍길동")
        self.assertEqual(calls, ["/bin/codex"])

    def test_run_openai_parses_output_text(self):
        import io
        import json as _json

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        payload = {"status": "completed", "model": "gpt-6-luna", "usage": {"input_tokens": 1, "output_tokens": 2},
                   "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"name": "홍길동"}'}]}]}
        seen = {}

        def fake_urlopen(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            return Response(_json.dumps(payload).encode())

        with patch.object(parser, "openai_api_key", lambda: "sk-test"), patch.object(parser.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(parser.run_openai("p", None), {"name": "홍길동"})
        self.assertEqual(seen["auth"], "Bearer sk-test")


class CrossCheckTest(unittest.TestCase):
    OCR = "새봄 엔지니어링\n대표이사\n김철수\nMobile. 010-5555-0104\nTel. 031-555-0105\nTel. +84-221-555-0108 Mobile. +84-86-5555-0109\nTEL. 052-555-0126~7"

    def test_phones_are_unioned_and_verified_against_ocr(self):
        luna = {"name": "김철수", "company": "새봄 엔지니어링", "mobile": ["010-5555-0104", "+84-86-5555-0109"], "work": ["031-555-0105", "02-5555-0106"], "fax": []}
        haiku = {"name": "김철수", "company": "새봄엔지니어링", "mobile": ["010-5555-0104"], "work": ["031-555-0105", "+84-221-555-0108", "052-555-0126", "052-555-0127"], "fax": []}
        merged = parser.crosscheck_merge(luna, haiku, self.OCR)
        self.assertEqual(merged["mobile"], ["010-5555-0104", "+84-86-5555-0109"])
        self.assertEqual(merged["work"], ["031-555-0105", "+84-221-555-0108", "052-555-0126", "052-555-0127"])
        self.assertTrue(any("02-5555-0106" in w for w in merged["warnings"]))
        self.assertFalse(any("엇갈림" in w for w in merged["warnings"]))

    def test_same_number_as_tel_and_fax_keeps_both(self):
        reading = {"name": "박영수", "work": ["052-555-0126", "052-555-0127"], "fax": ["052-555-0127"], "mobile": ["010-5555-0107"]}
        ocr = "TEL. 052)555-0126~7\nFAX. 052)555-0127\n010-5555-0107"
        merged = parser.crosscheck_merge(dict(reading), dict(reading), ocr)
        self.assertEqual(merged["work"], ["052-555-0126", "052-555-0127"])
        self.assertEqual(merged["fax"], ["052-555-0127"])

    def test_conflicting_company_is_flagged_and_ocr_match_preferred(self):
        merged = parser.crosscheck_merge({"name": "김철수", "company": "세봄 엔지니어링"}, {"name": "김철수", "company": "새봄 엔지니어링"}, self.OCR)
        self.assertEqual(merged["company"], "새봄 엔지니어링")
        self.assertTrue(any("회사명 판독이 엇갈림" in w for w in merged["warnings"]))

    def test_one_api_down_uses_the_other(self):
        with patch.object(parser, "run_openai", side_effect=RuntimeError("down")), patch.object(parser, "run_anthropic", lambda p, i: {"name": "홍길동"}):
            self.assertEqual(parser.run_crosscheck("p", Path("/x.jpg"), ""), {"name": "홍길동"})

    def test_enabled_only_with_both_keys(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai", "ANTHROPIC_API_KEY": "k", "BIZCARD_CROSSCHECK": "auto"}):
            self.assertTrue(parser.crosscheck_enabled())
        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai", "ANTHROPIC_API_KEY": "", "BIZCARD_CROSSCHECK": "auto"}):
            self.assertFalse(parser.crosscheck_enabled())
        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai", "ANTHROPIC_API_KEY": "k", "BIZCARD_CROSSCHECK": "off"}):
            self.assertFalse(parser.crosscheck_enabled())

    def test_run_anthropic_reads_forced_tool_input(self):
        import io
        import json as _json

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        seen = {}

        def fake_urlopen(request, timeout):
            seen["key"] = request.get_header("X-api-key")
            seen["body"] = _json.loads(request.data)
            return Response(_json.dumps({"model": "claude-haiku-5-5", "usage": {}, "content": [{"type": "tool_use", "name": "business_card", "input": {"name": "홍길동"}}]}).encode())

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), patch.object(parser.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(parser.run_anthropic("p", None), {"name": "홍길동"})
        self.assertEqual(seen["key"], "sk-ant-test")
        self.assertEqual(seen["body"]["tool_choice"], {"type": "tool", "name": "business_card"})


class AddressArbitrationTest(unittest.TestCase):
    def merge(self, luna, haiku, ocr):
        return parser.crosscheck_merge({'address': luna}, {'address': haiku}, ocr)

    def test_ocr_supported_haiku_replaces_luna_typo(self):
        result = self.merge(['테스트시 가상로 11'], ['테스트시 가상로 12'], '테스트시 가상로 12')
        self.assertEqual(result['address'], ['테스트시 가상로 12'])
        self.assertTrue(any('주소 판독이 엇갈림' in w for w in result['warnings']))

    def test_other_disagreement_keeps_luna_and_warns(self):
        result = self.merge(['테스트시 가상로 11'], ['테스트시 다른로 12'], '')
        self.assertEqual(result['address'], ['테스트시 가상로 11'])
        self.assertTrue(any('주소 판독이 엇갈림' in w for w in result['warnings']))

    def test_haiku_cannot_drop_an_address(self):
        result = self.merge(['테스트시 가상로 11', '다른시 다른로 22'], ['테스트시 가상로 12'], '테스트시 가상로 12')
        self.assertEqual(len(result['address']), 2)

    def test_ocr_supported_luna_is_kept_without_warning(self):
        result = self.merge(['테스트시 가상로 11'], ['테스트시 가상로 12'], '테스트시 가상로 11')
        self.assertEqual(result['address'], ['테스트시 가상로 11'])
        self.assertFalse(result['warnings'])

    def test_empty_related_and_reordered_addresses_do_not_warn(self):
        for a, b in [([], ['테스트로 12']), (['테스트로 12'], []),
                     (['테스트시 가상로 12'], ['주소: 테스트시, 가상로 12']),
                     (['테스트로 12', '다른로 34'], ['다른로 34', '테스트로 12'])]:
            with self.subTest(luna=a, haiku=b):
                self.assertFalse(self.merge(a, b, '')['warnings'])

    def test_off_retains_old_merge_without_address_warning(self):
        with patch.dict(os.environ, {'BIZCARD_ADDRESS_ARBITRATION': 'off'}):
            result = self.merge(['테스트로 11'], ['테스트로 12'], '테스트로 12')
        self.assertEqual(result['address'], ['테스트로 11'])
        self.assertFalse(result['warnings'])


class CollectionTest(unittest.TestCase):
    def response(self, provider):
        import io
        import json
        payload = {'usage': {'input_tokens': 1000, 'output_tokens': 200}}
        if provider == 'openai':
            payload.update(model='gpt-6-luna', output=[{'content': [{'type': 'output_text', 'text': '{"name":"Example","address":["테스트로 12"]}'}]}])
        else:
            payload.update(model='claude-haiku-5-5', content=[{'type': 'tool_use', 'input': {'name': 'Example', 'address': ['테스트로 13']}}])
        return io.BytesIO(json.dumps(payload).encode())

    def test_crosscheck_captures_parallel_usage_and_agreement_without_values(self):
        import json
        def response(request, **kwargs):
            return self.response('openai' if request.full_url == parser.OPENAI_RESPONSES_URL else 'anthropic')
        with patch.object(parser, 'openai_api_key', return_value='test'), \
             patch.object(parser, 'anthropic_api_key', return_value='test'), \
             patch.object(parser.urllib.request, 'urlopen', response):
            with parser.collect() as calls:
                result = parser.run_crosscheck('private-prompt', None, '테스트로 12')
        self.assertEqual(len(calls), 2)
        self.assertEqual({c['provider'] for c in calls}, {'openai', 'anthropic'})
        self.assertTrue(all(c['krw'] > 0 for c in calls))
        agree = next(c['agree'] for c in calls if 'agree' in c)
        self.assertTrue(agree['name'])
        self.assertFalse(agree['address'])
        self.assertNotIn('Example', json.dumps(calls))
        self.assertNotIn('private-prompt', json.dumps(calls))
        self.assertEqual(result['name'], 'Example')

    def test_nested_collectors_restore_outer_after_exception(self):
        with patch.object(parser, 'openai_api_key', return_value='test'), \
             patch.object(parser.urllib.request, 'urlopen', side_effect=lambda *a, **k: self.response('openai')):
            with parser.collect() as outer:
                parser.run_openai('p', None)
                with self.assertRaises(ValueError):
                    with parser.collect() as inner:
                        parser.run_openai('p', None)
                        raise ValueError('test')
                parser.run_openai('p', None)
        self.assertEqual(len(outer), 2)
        self.assertEqual(len(inner), 1)

    def test_cli_fallback_is_recorded_as_unknown(self):
        with patch.object(parser, 'backend_name', return_value='codex'), \
             patch.object(parser, 'run_codex', return_value='{"name":"Example"}'):
            with parser.collect() as calls:
                self.assertEqual(parser.run_backend('p')['name'], 'Example')
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]['krw'])
        self.assertEqual(calls[0]['provider'], 'codex')

    def test_requested_model_is_used_when_response_has_dated_model(self):
        import io
        import json
        payload = {'model': 'gpt-6-luna-2026-11-18', 'usage': {'input_tokens': 1000, 'output_tokens': 200},
                   'output': [{'content': [{'type': 'output_text', 'text': '{"name":"Example"}'}]}]}
        with patch.object(parser, 'openai_api_key', return_value='test'), \
             patch.dict(os.environ, {'BIZCARD_OPENAI_MODEL': 'gpt-6-luna'}), \
             patch.object(parser.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(json.dumps(payload).encode())):
            with parser.collect() as calls:
                parser.run_openai('p', None)
        self.assertEqual(calls[0]['model'], 'gpt-6-luna')
        self.assertAlmostEqual(calls[0]['krw'], .267)

    def test_failed_api_alongside_success_keeps_cost_unknown(self):
        import urllib.error
        def response(request, **kwargs):
            if request.full_url == parser.OPENAI_RESPONSES_URL:
                raise urllib.error.URLError('offline')
            return self.response('anthropic')
        with patch.object(parser, 'openai_api_key', return_value='test'), \
             patch.object(parser, 'anthropic_api_key', return_value='test'), \
             patch.object(parser.urllib.request, 'urlopen', response):
            with parser.collect() as calls:
                result = parser.run_crosscheck('p', None, '')
        self.assertEqual(result['name'], 'Example')
        self.assertEqual(len(calls), 2)
        self.assertIsNone(next(c['krw'] for c in calls if c['provider'] == 'openai'))

    def test_usage_collector_rejects_unexpected_value_fields(self):
        import io
        import json
        payload = {'usage': {'input_tokens': 1000, 'output_tokens': 200, 'private': 'Example'},
                   'output': [{'content': [{'type': 'output_text', 'text': '{"name":"Example"}'}]}]}
        with patch.object(parser, 'openai_api_key', return_value='test'), \
             patch.object(parser.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(json.dumps(payload).encode())):
            with parser.collect() as calls:
                parser.run_openai('p', None)
        self.assertNotIn('Example', json.dumps(calls))
        self.assertEqual(calls[0]['usage'], {'input_tokens': 1000, 'output_tokens': 200})

    def test_simultaneous_collectors_do_not_share_usage(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(2, timeout=2)
        def response(request, **kwargs):
            barrier.wait()
            return self.response('openai')
        def scan(index):
            with parser.collect() as calls:
                parser.run_openai('p', None)
            return calls
        with patch.object(parser, 'openai_api_key', return_value='test'), \
             patch.object(parser.urllib.request, 'urlopen', response):
            with ThreadPoolExecutor(max_workers=2) as pool:
                one, two = list(pool.map(scan, range(2)))
        self.assertEqual(len(one), 1)
        self.assertEqual(len(two), 1)
        self.assertIsNot(one, two)


if __name__ == "__main__":
    unittest.main()
