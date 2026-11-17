import os
import multiprocessing
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bizcard_intake import onboarding


def concurrent_save(path, chat_id, read_event, release_event, result):
    original = onboarding._read_env

    def paused_read(path):
        text = original(path)
        read_event.set()
        if release_event is not None and not release_event.wait(5):
            raise RuntimeError("test did not release first writer")
        return text

    try:
        with patch.dict(os.environ, {}, clear=True), patch.object(onboarding, "_read_env", paused_read):
            onboarding._save_env(Path(path), "123456:FAKE_CONCURRENT_TOKEN", chat_id)
        result.put(None)
    except Exception as error:
        result.put(type(error).__name__)


class EnvSaveTests(unittest.TestCase):
    def test_concurrent_processes_read_latest_env_under_separate_lock(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("TELEGRAM_ALLOWED_CHAT_IDS=100\nKEEP=original\n")
            first_read, second_read, release = (context.Event() for _ in range(3))
            results = context.Queue()
            first = context.Process(target=concurrent_save, args=(env, 200, first_read, release, results))
            second = context.Process(target=concurrent_save, args=(env, 300, second_read, None, results))
            first.start()
            try:
                self.assertTrue(first_read.wait(5))
                second.start()
                try:
                    self.assertFalse(second_read.wait(0.5), "second process read without holding the lock")
                finally:
                    release.set()
                    second.join(5)
            finally:
                release.set()
                first.join(5)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)
            self.assertEqual([results.get(timeout=2), results.get(timeout=2)], [None, None])
            self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS=100,200,300\n", env.read_text())
            self.assertIn("KEEP=original\n", env.read_text())
            self.assertTrue((env.parent / ".env.lock").exists())
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)

    def test_launcher_reads_literal_spaces_and_special_characters_without_shell_evaluation(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            script = root / "scripts/start_bot.sh"
            script.write_bytes((source / "scripts/start_bot.sh").read_bytes())
            if (source / "bizcard_intake/envfile.py").exists():
                (root / "bizcard_intake").mkdir()
                (root / "bizcard_intake/envfile.py").write_bytes((source / "bizcard_intake/envfile.py").read_bytes())
            marker = root / "executed"
            token = "123456:FAKE_LITERAL_TOKEN"
            (root / ".env").write_text(
                f"TELEGRAM_BOT_TOKEN= {token} # keep secret\n"
                "EMPTY= # empty comment\n"
                "SPACES=first second third\n"
                f"SPECIAL=$(touch {marker});`touch {marker}` $HOME & !\n"
                'QUOTED="a # b" # comment\n')
            interpreter = root / "fake-python"
            interpreter.write_text('#!/bin/bash\nprintf "%s\\n" "$TELEGRAM_BOT_TOKEN" "$EMPTY" "$SPACES" "$SPECIAL" "$QUOTED"\n')
            interpreter.chmod(0o700)
            result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True,
                                    env={"BIZCARD_PYTHON": str(interpreter), "HOME": str(root),
                                         "PATH": os.environ["PATH"]}, timeout=10)
            self.assertFalse(marker.exists(), "dotenv value was executed by the shell")
            self.assertNotIn(token, result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.stdout.splitlines(), [token, "", "first second third",
                             f"$(touch {marker});`touch {marker}` $HOME & !", "a # b"])

    def test_empty_commented_values_load_through_start_script_without_token_errors(self):
        start_script = Path(__file__).resolve().parents[1] / "scripts/start_bot.sh"
        for prefix in ("", "export ", "  export "):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "scripts").mkdir()
                script = root / "scripts/start_bot.sh"
                script.write_bytes(start_script.read_bytes())
                loader = start_script.parents[1] / "bizcard_intake/envfile.py"
                if loader.exists():
                    (root / "bizcard_intake").mkdir()
                    (root / "bizcard_intake/envfile.py").write_bytes(loader.read_bytes())
                interpreter = root / "fake-python"
                interpreter.write_text('#!/bin/bash\nprintf "%s\\n" "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_ALLOWED_CHAT_IDS"\n')
                interpreter.chmod(0o700)
                env = root / ".env"
                env.write_text(f"{prefix}TELEGRAM_BOT_TOKEN= # paste token here\n"
                               f"{prefix}TELEGRAM_ALLOWED_CHAT_IDS=\t# allowed chats\n")
                token = "123456:FAKE_SHELL_TEST_TOKEN"
                with patch.dict(os.environ, {}, clear=True):
                    onboarding._save_env(env, token, 456)
                result = subprocess.run(["/bin/bash", str(script)], capture_output=True,
                                        text=True, env={"BIZCARD_PYTHON": str(interpreter),
                                                        "HOME": str(root), "PATH": "/usr/bin:/bin"})
                self.assertNotIn(token, result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.splitlines(), [token, "456"])
                self.assertIn(" # paste token here", env.read_text())
                self.assertIn(" # allowed chats", env.read_text())


    def test_parser_matches_bash_for_escaped_spaces_and_comment_boundaries(self):
        from bizcard_intake.envfile import read_settings
        lines = ["A=foo\\ #bar", "B= # comment", "C=#x", 'D="a b" # c', "E='x # y'",
                 "F=val # c", "G=foo\\ ", 'H=tok"en"', "export I=1"]
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("\n".join(lines) + "\n")
            expected = subprocess.run(
                ["/bin/bash", "-c", 'set -a; . "$1"; set +a; for k in A B C D E F G H I; do printf "%s\\0" "${!k}"; done', "_", str(env)],
                capture_output=True, timeout=10, env={"PATH": "/usr/bin:/bin"}).stdout.decode().split("\0")[:-1]
            parsed = read_settings(env)
        self.assertEqual([parsed[k] for k in "ABCDEFGHI"], expected)
        self.assertEqual(parsed["A"], "foo #bar")
        self.assertEqual(parsed["G"], "foo ")

    def test_launcher_finds_python_after_path_bootstrap(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "bizcard_intake").mkdir()
            script = root / "scripts/start_bot.sh"
            script.write_bytes((source / "scripts/start_bot.sh").read_bytes())
            (root / "bizcard_intake/envfile.py").write_bytes((source / "bizcard_intake/envfile.py").read_bytes())
            (root / ".env").write_text("TELEGRAM_BOT_TOKEN=123456:FAKE_BOOTSTRAP_TOKEN\n")
            bin_dir = root / ".local/bin"
            bin_dir.mkdir(parents=True)
            python = bin_dir / "python3"
            python.symlink_to(os.path.realpath(subprocess.run(["/usr/bin/which", "python3"], capture_output=True, text=True).stdout.strip()))
            interpreter = root / "fake-python"
            interpreter.write_text('#!/bin/bash\necho BOT_START_OK\n')
            interpreter.chmod(0o700)
            # Initial PATH has dirname/bash only; python3 comes from the bootstrap $HOME/.local/bin.
            tools_dir = root / "tools"
            tools_dir.mkdir()
            for name in ("dirname", "bash"):
                (tools_dir / name).symlink_to(subprocess.run(["/usr/bin/which", name], capture_output=True, text=True).stdout.strip())
            result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=10,
                                    env={"BIZCARD_PYTHON": str(interpreter), "HOME": str(root), "PATH": str(tools_dir)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "BOT_START_OK")

if __name__ == "__main__":
    unittest.main()
