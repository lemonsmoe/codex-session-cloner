import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ai_cli_kit.codex import APP_COMMAND, __version__  # noqa: E402
from ai_cli_kit.codex.cli import create_arg_parser  # noqa: E402
from ai_cli_kit.codex.tui.app import (  # noqa: E402
    ToolkitAppContext,
    ToolkitTuiApp,
    TuiMenuAction,
    build_tui_menu_actions,
    build_tui_menu_sections,
)
from ai_cli_kit.codex.tui.terminal import LOGO_FONT_BANNER  # noqa: E402


def _module_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    # Force UTF-8 鈥?codex / claude / aik CLIs print Chinese help text, which
    # crashes with UnicodeEncodeError on CI runners whose locale is C/POSIX.
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


class PackagingSmokeTests(unittest.TestCase):
    def test_cli_parser_uses_packaged_command_name(self) -> None:
        parser = create_arg_parser()
        self.assertEqual(parser.prog, APP_COMMAND)

    def test_tui_context_uses_packaged_command_name(self) -> None:
        context = ToolkitAppContext(
            target_provider="demo-provider",
            active_sessions_dir="/tmp/demo-sessions",
            config_path="/tmp/demo-config.toml",
        )
        self.assertEqual(context.entry_command, APP_COMMAND)

    def test_tui_main_sections_are_grouped_by_domain(self) -> None:
        section_ids = [section.section_id for section in build_tui_menu_sections()]
        self.assertEqual(section_ids, ["session", "bundle", "repair"])

        actions_by_section = {}
        for action in build_tui_menu_actions():
            actions_by_section.setdefault(action.section_id, set()).add(action.action_id)

        self.assertEqual(actions_by_section["session"], {"list_sessions", "export_one"})
        self.assertEqual(
            actions_by_section["bundle"],
            {
                "browse_bundles",
                "validate_bundles",
                "export_desktop_all",
                "export_desktop_active",
                "export_cli_all",
                "import_one",
                "import_desktop_all",
            },
        )
        self.assertEqual(
            actions_by_section["repair"],
            {
                "clone",
                "clone_dry",
                "clean",
                "clean_archived",
                "clean_archived_dry",
                "clean_dry",
                "dedupe",
                "dedupe_dry",
                "promote_session",
                "repair_session_history",
                "repair_desktop",
                "repair_desktop_dry",
                "repair_desktop_cli",
                "repair_desktop_cli_dry",
                "restore_backup",
                "switch_provider",
                "switch_provider_dry",
            },
        )

    def test_tui_repair_hotkeys_follow_switch_repair_dedupe_order(self) -> None:
        repair_actions = [
            action
            for action in build_tui_menu_actions()
            if action.section_id == "repair"
        ]
        by_id = {action.action_id: action for action in repair_actions}

        self.assertEqual(by_id["switch_provider"].hotkey, "1")
        self.assertEqual(by_id["switch_provider"].cli_args, ("switch-provider",))
        self.assertFalse(by_id["switch_provider"].is_dry_run)

        expected_numeric_order = [
            ("switch_provider", "1"),
            ("restore_backup", "2"),
            ("repair_desktop", "3"),
            ("repair_desktop_dry", "4"),
            ("dedupe", "5"),
            ("dedupe_dry", "6"),
            ("promote_session", "7"),
            ("repair_session_history", "8"),
            ("clone", "9"),
            ("clone_dry", "r"),
        ]
        self.assertEqual(
            [(action.action_id, action.hotkey) for action in repair_actions[:10]],
            expected_numeric_order,
        )
        self.assertEqual(by_id["switch_provider_dry"].hotkey, "s")
        self.assertEqual(by_id["switch_provider_dry"].cli_args, ("switch-provider", "--dry-run"))
        self.assertTrue(by_id["switch_provider_dry"].is_dry_run)
        self.assertEqual(by_id["clone_dry"].hotkey, "r")

    def test_tui_promote_session_command_prompts_for_provider(self) -> None:
        context = ToolkitAppContext(
            target_provider="openai",
            active_sessions_dir="/tmp/demo-sessions",
            config_path="/tmp/demo-config.toml",
        )
        app = ToolkitTuiApp(context)
        action = TuiMenuAction(
            "promote_session",
            "7",
            "Repair one session visibility",
            "repair",
            ("promote-session", "<session_id>"),
        )

        with mock.patch.object(app, "_prompt_value", side_effect=["session-123", "right_code"]), \
                mock.patch("ai_cli_kit.codex.tui.app.detect_session_provider", return_value="right_code"):
            action_name, args = app._resolve_menu_action_request(action)

        self.assertEqual(args, ["promote-session", "session-123", "right_code"])
        self.assertIn("right_code", action_name)

    def test_tui_repair_session_history_command_can_request_rebuild(self) -> None:
        context = ToolkitAppContext(
            target_provider="openai",
            active_sessions_dir="/tmp/demo-sessions",
            config_path="/tmp/demo-config.toml",
        )
        app = ToolkitTuiApp(context)
        action = TuiMenuAction(
            "repair_session_history",
            "8",
            "Repair one session history",
            "repair",
            ("repair-session-history", "<session_id>"),
        )

        with mock.patch.object(app, "_prompt_value", side_effect=["session-123", "right_code"]), \
                mock.patch.object(app, "_confirm_toggle", return_value=True), \
                mock.patch("ai_cli_kit.codex.tui.app.detect_session_provider", return_value="right_code"):
            action_name, args = app._resolve_menu_action_request(action)

        self.assertEqual(args, ["repair-session-history", "session-123", "right_code", "--rebuild-clone"])
        self.assertIn("right_code", action_name)

    def test_logo_font_covers_toolkit_wordmark(self) -> None:
        missing = {ch for ch in "CODEX SESSION TOOLKIT" if ch != " " and ch not in LOGO_FONT_BANNER}
        self.assertEqual(missing, set())

    def test_module_help_mentions_packaged_command(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "ai_cli_kit.codex", "--help"],
            cwd=ROOT_DIR,
            env=_module_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn(f"usage: {APP_COMMAND}", result.stdout)
        self.assertIn("clone-provider", result.stdout)

    def test_module_version_matches_package_version(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "ai_cli_kit.codex", "--version"],
            cwd=ROOT_DIR,
            env=_module_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(result.stdout.strip(), f"{APP_COMMAND} {__version__}")

    def test_repo_local_launcher_help_runs(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh is not available on this platform")
        result = subprocess.run(
            ["sh", "./codex-session-toolkit", "--help"],
            cwd=ROOT_DIR,
            env=_module_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn(f"usage: {APP_COMMAND}", result.stdout)
        self.assertIn("--version", result.stdout)

    def test_repo_local_launcher_prefers_source_mode_in_git_worktree(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh is not available on this platform")
        result = subprocess.run(
            ["sh", "./codex-session-toolkit", "--help"],
            cwd=ROOT_DIR,
            env=_module_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("Launcher (Source Mode)", result.stdout)

    def test_unix_install_script_help_runs(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh is not available on this platform")
        result = subprocess.run(
            ["sh", "./install.sh", "--help"],
            cwd=ROOT_DIR,
            env=_module_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("Usage: ./install.sh", result.stdout)
        self.assertIn("--editable", result.stdout)

    def test_unix_install_force_refuses_to_delete_project_root(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_root = Path(tmp_dir) / "project"
            (fake_root / "scripts" / "install").mkdir(parents=True)
            shutil.copy2(ROOT_DIR / "install.sh", fake_root / "install.sh")
            shutil.copy2(
                ROOT_DIR / "scripts" / "install" / "install.unix.sh",
                fake_root / "scripts" / "install" / "install.unix.sh",
            )

            result = subprocess.run(
                ["sh", "./install.sh", "--force", "--python", sys.executable],
                cwd=fake_root,
                env={**_module_env(), "VENV_DIR": str(fake_root)},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("refusing to delete project root", result.stdout)
            self.assertTrue((fake_root / "install.sh").exists())

    def test_unix_install_force_refuses_to_delete_project_ancestor(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp_dir:
            parent_root = Path(tmp_dir) / "workspace"
            fake_root = parent_root / "project"
            (fake_root / "scripts" / "install").mkdir(parents=True)
            shutil.copy2(ROOT_DIR / "install.sh", fake_root / "install.sh")
            shutil.copy2(
                ROOT_DIR / "scripts" / "install" / "install.unix.sh",
                fake_root / "scripts" / "install" / "install.unix.sh",
            )

            result = subprocess.run(
                ["sh", "./install.sh", "--force", "--python", sys.executable],
                cwd=fake_root,
                env={**_module_env(), "VENV_DIR": str(parent_root)},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("project root or ancestor", result.stdout)
            self.assertTrue((fake_root / "install.sh").exists())

    def test_release_script_help_runs(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh is not available on this platform")
        result = subprocess.run(
            ["sh", "./release.sh", "--help"],
            cwd=ROOT_DIR,
            env=_module_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("Usage: ./release.sh", result.stdout)
        self.assertIn("--output-dir", result.stdout)

    def test_release_script_rejects_unsafe_version_label(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh is not available on this platform")
        result = subprocess.run(
            ["sh", "./release.sh", "--version", "../oops"],
            cwd=ROOT_DIR,
            env=_module_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe characters", result.stdout)


if __name__ == "__main__":
    unittest.main()

