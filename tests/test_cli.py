import os
import unittest
from unittest.mock import patch

from bizcard_intake import cli, pipeline, telegram


class BotStartupTest(unittest.TestCase):
    def test_bot_requires_telegram_token(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(telegram, "main") as run:
            with self.assertRaisesRegex(SystemExit, "^TELEGRAM_BOT_TOKEN is required$"):
                cli.main(["bot"])
        run.assert_not_called()

    def test_default_and_explicit_bot_warm_up_once_before_telegram(self):
        for argv in ([], ["bot"]):
            with self.subTest(argv=argv):
                events = []
                with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test"}, clear=True), patch.object(
                    telegram, "setup_logging"
                ), patch.object(pipeline, "warm_up", side_effect=lambda: events.append("warm")), patch.object(
                    telegram, "main", side_effect=lambda: events.append("telegram")
                ):
                    self.assertEqual(cli.main(argv), 0)
                self.assertEqual(events, ["warm", "telegram"])


class ShellStartupTest(unittest.TestCase):
    def run_launcher(self, settings):
        import subprocess
        import tempfile
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "scripts" / "start_bot.sh"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / "scripts" / "start_bot.sh"
            script.parent.mkdir()
            script.write_text(source.read_text())
            loader = source.parents[1] / "bizcard_intake" / "envfile.py"
            (root / "bizcard_intake").mkdir()
            (root / "bizcard_intake" / "envfile.py").write_bytes(loader.read_bytes())
            (root / ".env").write_text(settings)
            return subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True,
                                  env={"PATH": "/usr/bin:/bin", "HOME": folder,
                                       "BIZCARD_PYTHON": "/bin/echo"}, timeout=10)

    def test_launcher_requires_telegram_token(self):
        result = self.run_launcher("")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("TELEGRAM_BOT_TOKEN is required."), result.stderr)
        self.assertEqual(result.stdout, "")

    def test_launcher_passes_bot_command_with_project_token(self):
        result = self.run_launcher("TELEGRAM_BOT_TOKEN=test\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "-m bizcard_intake bot")

    def test_launcher_rejects_malformed_values_without_secret_in_errors(self):
        token = "123456:FAKE_MALFORMED_TOKEN"
        result = self.run_launcher(f'TELEGRAM_BOT_TOKEN="{token}\n')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(token, result.stderr)
        self.assertIn("Cannot read .env settings", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
