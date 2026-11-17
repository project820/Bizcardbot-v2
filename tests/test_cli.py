import os
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bizcard_intake import cards, cli, pipeline, telegram


class BotStartupTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = patch.object(cli, "PROJECT_ROOT", Path(folder.name))
        root.start()
        self.addCleanup(root.stop)
        storage = patch.object(cards, "CARD_DIR", Path(folder.name) / "cards")
        storage.start()
        self.addCleanup(storage.stop)

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


class ProjectSettingsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for context in (
            patch.object(cli, "PROJECT_ROOT", self.root),
            patch.object(cards, "CARD_DIR", self.root / "cards"),
            patch.dict(os.environ, {}, clear=True),
            patch.object(telegram, "setup_logging"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def test_setup_and_doctor_read_saved_settings_with_process_precedence(self):
        (self.root / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=synthetic-token\nTELEGRAM_ALLOWED_CHAT_IDS=123\n"
            "OPENAI_API_KEY=synthetic-key\nBIZCARD_PARSER=openai\n"
            "BIZCARD_OPENAI_MODEL=file-model\nBIZCARD_CROSSCHECK=auto\n"
            "BIZCARD_CONTACTS_BACKEND=google\n",
        )

        def inspect_settings():
            self.assertEqual(os.environ.get("TELEGRAM_BOT_TOKEN"), "synthetic-token")
            self.assertEqual(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"), "123")
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), "synthetic-key")
            self.assertEqual(os.environ.get("BIZCARD_OPENAI_MODEL"), "process-model")
            self.assertEqual(os.environ.get("BIZCARD_CROSSCHECK"), "")
            self.assertEqual(cli.doctor.parser.backend_name(), "openai")
            return [cli.doctor.Check("telegram", True, "synthetic settings loaded")]

        for command in (cli.cmd_setup, cli.cmd_doctor):
            with self.subTest(command=command.__name__), patch.dict(
                os.environ, {"BIZCARD_OPENAI_MODEL": "process-model", "BIZCARD_CROSSCHECK": ""}, clear=True,
            ), patch.object(cli.doctor, "run_checks", side_effect=inspect_settings):
                self.assertEqual(command(), 0)

    def test_other_cli_commands_load_settings_before_dispatch(self):
        (self.root / ".env").write_text("BIZCARD_PARSER=claude\nTELEGRAM_BOT_TOKEN=synthetic-token\n")
        commands = [
            (["scan", "card.jpg"], "cmd_scan"),
            (["save", "card-id"], "cmd_save"),
            (["retry-pending"], "cmd_retry_pending"),
            (["list"], "cmd_list"),
            (["migrate-names"], "cmd_migrate_names"),
            (["rollback-names", "backup.json"], "cmd_rollback_names"),
        ]

        def inspect_settings(*args):
            self.assertEqual(os.environ.get("BIZCARD_PARSER"), "claude")
            return 0

        for argv, handler in commands:
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True), patch.object(
                cli, handler, side_effect=inspect_settings,
            ):
                self.assertEqual(cli.main(argv), 0)
        for argv in ([], ["bot"]):
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True), patch.object(
                pipeline, "warm_up",
            ), patch.object(telegram, "main", side_effect=inspect_settings):
                self.assertEqual(cli.main(argv), 0)

    def test_invalid_env_is_rejected_before_dispatch_without_echoing_value(self):
        (self.root / ".env").write_text('OPENAI_API_KEY="synthetic-private-value\n')
        for argv in (["scan", "card.jpg"], ["doctor"], ["setup"]):
            with self.subTest(argv=argv), patch.object(cli, "cmd_scan") as scan, patch.object(
                cli.doctor, "run_checks",
            ) as checks, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(argv), 1)
                self.assertNotIn("synthetic-private-value", errors.getvalue())
                self.assertIn("Cannot read .env settings", errors.getvalue())
                scan.assert_not_called()
                checks.assert_not_called()

    def test_shell_expressions_are_loaded_as_data(self):
        marker = self.root / "must-not-exist"
        value = f"$(touch {marker})"
        (self.root / ".env").write_text(f"OPENAI_API_KEY='{value}'\n")

        def inspect_settings():
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), value)
            self.assertFalse(marker.exists())
            return []

        with patch.object(cli.doctor, "run_checks", side_effect=inspect_settings):
            self.assertEqual(cli.cmd_doctor(), 0)


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
