import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release.py"


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write("bizcard_intake/__init__.py", '__version__ = "2.0.0"\n')
        for folder in ("onboarding", "cloud"):
            self.write(f"{folder}/package.json", json.dumps({"version": "2.0.0"}) + "\n")
            self.write(f"{folder}/package-lock.json", json.dumps({
                "version": "2.0.0", "packages": {"": {"version": "2.0.0"},
                "node_modules/example": {"version": "1.2.3"}},
            }) + "\n")
        self.write("CHANGELOG.md", "# 변경 기록\n\n## [Unreleased]\n\n"
                   "### Fixed\n\n- 설정 안내를 고친다.\n\n"
                   "## [2.0.0] - 2026-11-17\n\n### Added\n\n- Telegram을 지원한다.\n\n"
                   "## [1.0.0] - 2025-01-01\n\n- 이전 기능이다.\n")
        self.write("README.md", "# Bot\n\n## 새 소식 (What's new)\n\nv2.0.0을 제공한다.\n\n## 설치\n")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def module(self):
        self.assertTrue(SCRIPT.is_file(), "릴리스 스크립트가 있어야 한다")
        spec = importlib.util.spec_from_file_location("release_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_command(self, *args, dirty=False, tagged=False, git_failure=False):
        module = self.module()

        def git(root, *arguments):
            self.assertEqual(root, self.root)
            if git_failure:
                raise ValueError("Git 확인에 실패했다")
            if arguments[0] == "status":
                return " M README.md\0" if dirty else ""
            if arguments[0] == "tag":
                return "v2.0.0\n" if tagged else ""
            if arguments[0] == "ls-files":
                if "-s" in arguments:
                    return ""
                return "\0".join(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) + "\0"
            self.fail(f"unexpected git command: {arguments}")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(module, "run_git", side_effect=git), redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(list(args), root=self.root)
        return result, stdout.getvalue() + stderr.getvalue()

    def test_check_accepts_consistent_release(self):
        result, output = self.run_command("check")
        self.assertEqual(result, 0, output)

    def test_check_rejects_version_mismatch_in_each_manifest(self):
        for path in ("onboarding/package.json", "cloud/package.json", "cloud/package-lock.json"):
            with self.subTest(path=path):
                original = (self.root / path).read_text()
                self.write(path, original.replace("2.0.0", "1.0.0"))
                result, output = self.run_command("check")
                self.assertEqual(result, 1)
                self.assertIn(path, output)
                self.write(path, original)

    def test_check_requires_real_date_and_unreleased(self):
        original = (self.root / "CHANGELOG.md").read_text()
        for broken in (original.replace("2026-11-17", "2026-02-30"),
                       original.replace(" - 2026-11-17", ""),
                       original.replace("## [Unreleased]", "## pending")):
            with self.subTest(broken=broken):
                self.write("CHANGELOG.md", broken)
                result, output = self.run_command("check")
                self.assertEqual(result, 1, output)

    def test_check_requires_current_version_inside_whats_new(self):
        self.write("README.md", "v2.0.0\n\n## 새 소식 (What's new)\n\nv1.0.0\n")
        self.assertEqual(self.run_command("check")[0], 1)

    def test_allow_dirty_does_not_skip_existing_tag_or_secrets(self):
        self.assertEqual(self.run_command("check", dirty=True)[0], 1)
        self.assertEqual(self.run_command("check", "--allow-dirty", dirty=True)[0], 0)
        self.assertEqual(self.run_command("check", "--allow-dirty", tagged=True)[0], 1)
        self.assertEqual(self.run_command("check", "--allow-existing-tag", tagged=True)[0], 0)

    def test_check_fails_closed_when_git_fails(self):
        self.assertEqual(self.run_command("check", "--allow-dirty", git_failure=True)[0], 1)

    def test_malformed_input_diagnostic_does_not_echo_secret(self):
        secret = "sk-" + "private" * 8
        self.write("onboarding/package.json", '{"' + secret + '": broken}')
        result, output = self.run_command("check")
        self.assertEqual(result, 1)
        self.assertNotIn(secret, output)

    def test_bump_resets_lower_version_components(self):
        for part, expected in (("major", "3.0.0"), ("minor", "2.8.0")):
            with self.subTest(part=part):
                for path in self.root.rglob("*"):
                    if path.is_file():
                        self.write(path.relative_to(self.root), path.read_text().replace("2.0.0", "2.7.9"))
                result, output = self.run_command("bump", part)
                self.assertEqual(result, 0, output)
                self.assertIn(f'__version__ = "{expected}"',
                              (self.root / "bizcard_intake/__init__.py").read_text())
                for path in self.root.rglob("*"):
                    if path.is_file():
                        self.write(path.relative_to(self.root), path.read_text().replace(expected, "2.0.0"))

    def test_check_detects_secrets_without_echoing_values(self):
        samples = ["123456789:" + "a" * 35, "sk-" + "b" * 40,
                   "AIza" + "c" * 35, "GOCSPX-" + "d" * 28,
                   "/" + "Users/private-owner/project", "private-owner" + "@gmail.com"]
        for secret in samples:
            with self.subTest(kind=secret[:4]):
                self.write("leak.txt", secret + "\n")
                result, output = self.run_command("check", "--allow-dirty", dirty=True)
                self.assertEqual(result, 1, output)
                self.assertIn("leak.txt:1", output)
                self.assertNotIn(secret, output)

    def test_check_detects_index_secret_after_worktree_is_cleaned(self):
        secret = "sk-" + "staged-fixture" * 4
        self.write("staged fixture.txt", "safe\n" + secret + "\n")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "add", "--", "staged fixture.txt"], cwd=self.root, check=True, capture_output=True)
        self.write("staged fixture.txt", "safe\n")
        module = self.module()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(["check", "--allow-dirty"], root=self.root)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("staged fixture.txt:2", output)
        self.assertNotIn(secret, output)

    def test_index_scan_skips_binary_blobs(self):
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True, capture_output=True)
        for prefix in (b"\0", b"\xff"):
            with self.subTest(prefix=prefix):
                (self.root / "binary.dat").write_bytes(prefix + b"sk-" + b"binary-fixture" * 4)
                subprocess.run(["git", "add", "--", "binary.dat"], cwd=self.root, check=True, capture_output=True)
                (self.root / "binary.dat").write_bytes(b"safe\n")
                self.assertEqual(self.module().scan_public_files(self.root), [])

    def test_history_reports_patterns_and_images_without_values_and_deduplicates_blobs(self):
        module = self.module()
        first, second = "a" * 40, "b" * 40
        secret_blob, image_blob = "c" * 40, "d" * 40
        values = ["123456789:" + "a" * 35, "sk-" + "b" * 40,
                  "AIza" + "c" * 35, "GOCSPX-" + "d" * 28,
                  "/" + "Users/private-owner/project", "private-owner" + "@gmail.com",
                  "010-" + "9876-5432"]
        tree = (f"100644 blob {secret_blob}\told fixture.txt\0"
                + "".join(f"100644 blob {image_blob}\tcard{extension}\0" for extension in (".jpg", ".PNG", ".heic")))

        def git(root, *arguments):
            self.assertEqual(root, self.root)
            if arguments == ("rev-list", "--all"):
                return first + "\n" + second + "\n"
            if arguments[:3] == ("ls-tree", "-r", "-z"):
                return tree
            if arguments == ("cat-file", "blob", secret_blob):
                return "\n".join(values)
            if arguments == ("cat-file", "blob", image_blob):
                return "\0binary"
            self.fail(f"unexpected git command: {arguments}")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(module, "run_git", side_effect=git) as calls, redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(["history"], root=self.root)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 1)
        for label, _ in module.SECRET_PATTERNS:
            self.assertIn(label, output)
        self.assertIn("한국 휴대폰 번호", output)
        self.assertIn(first[:12], output)
        self.assertIn("old fixture.txt", output)
        for extension in (".jpg", ".PNG", ".heic"):
            self.assertIn("card" + extension, output)
        for value in values:
            self.assertNotIn(value, output)
        self.assertEqual(sum(call.args[1:] == ("cat-file", "blob", secret_blob) for call in calls.call_args_list), 1)

    def test_history_allows_reserved_phone_fixtures_and_skips_binary_blobs(self):
        module = self.module()
        values = ["010-1234-5678", "010-5555-0000", "010-5555-0999", "010-0000-0000",
                  "01012345678", "+82 10-1234-5678", "+82 (0)10 5555 0101"]
        commit, text_blob, binary_blob = "a" * 40, "b" * 40, "c" * 40
        replies = {("rev-list", "--all"): commit + "\n",
                   ("ls-tree", "-r", "-z", commit): f"100644 blob {text_blob}\tfixtures.txt\0"
                       f"100644 blob {binary_blob}\tasset.dat\0",
                   ("cat-file", "blob", text_blob): "\n".join(values),
                   ("cat-file", "blob", binary_blob): "\0sk-" + "binary-fixture" * 4}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(module, "run_git", side_effect=lambda root, *args: replies[args]), redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(["history"], root=self.root)
        self.assertEqual(result, 0, stdout.getvalue() + stderr.getvalue())

    def test_history_redacts_secrets_in_filenames(self):
        module = self.module()
        commit, blob = "a" * 40, "b" * 40
        secret = "sk-" + "filename-fixture" * 3
        replies = {("rev-list", "--all"): commit + "\n",
                   ("ls-tree", "-r", "-z", commit): f"100644 blob {blob}\t{secret}.jpg\0",
                   ("cat-file", "blob", blob): "\0image"}
        with patch.object(module, "run_git", side_effect=lambda root, *args: replies[args]):
            output = "\n".join(module.scan_history(self.root))
        self.assertIn("[redacted].jpg", output)
        self.assertNotIn(secret, output)

    def test_history_rejects_nonfixture_mobile_formats(self):
        module = self.module()
        commit, blob = "a" * 40, "b" * 40
        for value in ("010-" + "5555-1000", "+82 10-" + "9876-5432",
                      "0082-10-" + "9876-5432", "010" + "98765432", "011-" + "987-6543"):
            with self.subTest(format=value[:4]):
                replies = {("rev-list", "--all"): commit + "\n",
                           ("ls-tree", "-r", "-z", commit): f"100644 blob {blob}\tfixture.txt\0",
                           ("cat-file", "blob", blob): value}
                with patch.object(module, "run_git", side_effect=lambda root, *args: replies[args]):
                    output = "\n".join(module.scan_history(self.root))
                self.assertIn("한국 휴대폰 번호", output)
                self.assertNotIn(value, output)

    def test_history_fails_closed_without_echoing_git_errors(self):
        module = self.module()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, stdout=b"", stderr=b"private diagnostic"
        )), redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(["history"], root=self.root)
        self.assertEqual(result, 1)
        self.assertNotIn("private diagnostic", stdout.getvalue() + stderr.getvalue())

    def test_check_allows_reserved_example_emails(self):
        self.write("examples.txt", "test@example.com\ntest@example.invalid\n")
        self.assertEqual(self.run_command("check")[0], 0)

    def test_check_allows_configuration_templates(self):
        self.write("cloud/.dev.vars.example", "OPENAI_API_KEY=\n")
        self.write(".env.example", "TELEGRAM_BOT_TOKEN=\n")
        result, output = self.run_command("check")
        self.assertEqual(result, 0, output)

    def test_check_rejects_env_and_operational_data_even_without_patterns(self):
        for path in (".env", "data/card.json", "cloud/.dev.vars"):
            with self.subTest(path=path):
                self.write(path, "{}\n")
                result, output = self.run_command("check")
                self.assertEqual(result, 1)
                self.assertIn(path, output)
                (self.root / path).unlink()

    def test_notes_extract_only_requested_release(self):
        result, output = self.run_command("notes", "2.0.0")
        self.assertEqual(result, 0, output)
        self.assertIn("## [2.0.0] - 2026-11-17", output)
        self.assertIn("Telegram을 지원한다", output)
        self.assertNotIn("Unreleased", output)
        self.assertNotIn("1.0.0", output)

    def test_notes_reject_missing_or_invalid_version(self):
        for version in ("9.0.0", "../README", "02.0.0"):
            with self.subTest(version=version):
                self.assertEqual(self.run_command("notes", version)[0], 1)

    def test_bump_updates_manifests_locks_and_moves_unreleased(self):
        for part, expected in (("major", "3.0.0"), ("minor", "2.1.0"), ("patch", "2.0.1")):
            with self.subTest(part=part):
                originals = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
                result, output = self.run_command("bump", part)
                self.assertEqual(result, 0, output)
                self.assertIn(f'__version__ = "{expected}"',
                              (self.root / "bizcard_intake/__init__.py").read_text())
                for folder in ("onboarding", "cloud"):
                    manifest = json.loads((self.root / folder / "package.json").read_text())
                    lock = json.loads((self.root / folder / "package-lock.json").read_text())
                    self.assertEqual(manifest["version"], expected)
                    self.assertEqual(lock["version"], expected)
                    self.assertEqual(lock["packages"][""]["version"], expected)
                    self.assertEqual(lock["packages"]["node_modules/example"]["version"], "1.2.3")
                changelog = (self.root / "CHANGELOG.md").read_text()
                marker = f"## [{expected}] - {date.today().isoformat()}"
                self.assertIn(marker, changelog)
                self.assertIn("설정 안내를 고친다", changelog.split(marker)[1])
                self.assertEqual(changelog.split("## [Unreleased]")[1].split(marker)[0].strip(), "")
                self.assertIn("## [2.0.0] - 2026-11-17", changelog)
                for path, content in originals.items():
                    path.write_bytes(content)

    def test_bump_rejects_duplicate_release_without_partial_changes(self):
        changelog = (self.root / "CHANGELOG.md").read_text()
        self.write("CHANGELOG.md", changelog + "\n## [2.0.1] - 2026-11-18\n")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(self.run_command("bump", "patch")[0], 1)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_cli_displays_help(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check", result.stdout)


if __name__ == "__main__":
    unittest.main()
