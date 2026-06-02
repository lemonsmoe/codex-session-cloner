"""Codex Session Toolkit TUI."""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from .. import APP_COMMAND
from ..commands import run_cli as run_toolkit_cli
from ..errors import ToolkitError
from ..paths import CodexPaths
from ..presenters.reports import print_cleanup_result, print_clone_run_result
from ..services.clone import cleanup_clones, clone_to_provider
from ..services.provider import detect_session_provider
from ...core.tui.screen_mode import ScreenModeDecision, resolve_screen_mode
from .terminal import (
    Ansi,
    _WINDOWS_VT_OK,
    align_line,
    clear_screen,
    display_width,
    glyphs,
    read_key,
    render_box,
    style_text,
    term_height,
    term_width,
    tui_width,
)


@dataclass(frozen=True)
class ToolkitAppContext:
    target_provider: str
    active_sessions_dir: str
    config_path: str
    bundle_root_label: str = "./codex_sessions"
    desktop_bundle_root_label: str = "./codex_sessions"
    entry_command: str = APP_COMMAND


@dataclass(frozen=True)
class TuiMenuAction:
    action_id: str
    hotkey: str
    label: str
    section_id: str
    cli_args: Tuple[str, ...]
    is_dangerous: bool = False
    is_dry_run: bool = False


@dataclass(frozen=True)
class TuiMenuSection:
    title: str
    section_id: str
    border_codes: Tuple[str, ...]


def build_tui_menu_actions() -> List[TuiMenuAction]:
    return [
        TuiMenuAction("list_sessions", "l", "Browse recent sessions", "session", ("list", "--limit", "20")),
        TuiMenuAction("export_one", "e", "Export one session bundle", "session", ("export", "<session_id>")),
        TuiMenuAction("browse_bundles", "o", "Browse bundles", "bundle", ("list-bundles", "--limit", "20")),
        TuiMenuAction("validate_bundles", "y", "Validate bundles", "bundle", ("validate-bundles", "--source", "all")),
        TuiMenuAction("export_desktop_all", "b", "Export all Desktop sessions", "bundle", ("export-desktop-all",)),
        TuiMenuAction("export_desktop_active", "a", "Export active Desktop sessions", "bundle", ("export-active-desktop-all",)),
        TuiMenuAction("export_cli_all", "c", "Export all CLI sessions", "bundle", ("export-cli-all",)),
        TuiMenuAction("import_one", "i", "Import one bundle", "bundle", ("import", "<session_id|bundle_dir>")),
        TuiMenuAction("import_desktop_all", "m", "Import Desktop bundles", "bundle", ("import-desktop-all",)),
        TuiMenuAction("switch_provider", "1", "Switch to current provider", "repair", ("switch-provider",)),
        TuiMenuAction("restore_backup", "2", "Restore from backup", "repair", ("restore-backup", "<backup_dir>")),
        TuiMenuAction("repair_desktop", "3", "Repair Desktop visibility", "repair", ("repair-desktop",)),
        TuiMenuAction("repair_desktop_dry", "4", "Dry-run repair Desktop", "repair", ("repair-desktop", "--dry-run"), is_dry_run=True),
        TuiMenuAction("dedupe", "5", "Dedupe duplicate lineages", "repair", ("dedupe-clones",), is_dangerous=True),
        TuiMenuAction("dedupe_dry", "6", "Dry-run dedupe lineages", "repair", ("dedupe-clones", "--dry-run"), is_dangerous=True, is_dry_run=True),
        TuiMenuAction("promote_session", "7", "Repair one session visibility", "repair", ("promote-session", "<session_id>")),
        TuiMenuAction("repair_session_history", "8", "Repair one session history", "repair", ("repair-session-history", "<session_id>")),
        TuiMenuAction("clone", "9", "Clone to current provider", "repair", ("clone-provider",)),
        TuiMenuAction("clone_dry", "r", "Dry-run clone", "repair", ("clone-provider", "--dry-run"), is_dry_run=True),
        TuiMenuAction("switch_provider_dry", "s", "Dry-run switch provider", "repair", ("switch-provider", "--dry-run"), is_dry_run=True),
        TuiMenuAction("clean", "d", "Clean legacy unmarked clones", "repair", ("clean-clones",), is_dangerous=True),
        TuiMenuAction("clean_dry", "n", "Dry-run clean legacy clones", "repair", ("clean-clones", "--dry-run"), is_dangerous=True, is_dry_run=True),
        TuiMenuAction("clean_archived", "k", "Clean archived sessions", "repair", ("clean-archived", "--yes"), is_dangerous=True),
        TuiMenuAction("clean_archived_dry", "v", "Dry-run clean archived sessions", "repair", ("clean-archived", "--dry-run"), is_dangerous=True, is_dry_run=True),
        TuiMenuAction("repair_desktop_cli", "x", "Repair Desktop and include CLI", "repair", ("repair-desktop", "--include-cli")),
        TuiMenuAction("repair_desktop_cli_dry", "g", "Dry-run repair including CLI", "repair", ("repair-desktop", "--include-cli", "--dry-run"), is_dry_run=True),
        TuiMenuAction("exit", "0", "Exit", "system", tuple()),
    ]


def build_tui_menu_sections() -> List[TuiMenuSection]:
    return [
        TuiMenuSection("Session / Browse", "session", (Ansi.DIM, Ansi.CYAN)),
        TuiMenuSection("Bundle / Transfer", "bundle", (Ansi.DIM, Ansi.MAGENTA)),
        TuiMenuSection("Repair / Maintenance", "repair", (Ansi.DIM, Ansi.GREEN)),
    ]


def run_clone_mode(*, target_provider: str, dry_run: bool) -> int:
    try:
        return print_clone_run_result(clone_to_provider(CodexPaths(), target_provider=target_provider, dry_run=dry_run))
    except ToolkitError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def run_cleanup_mode(
    *,
    target_provider: str,
    dry_run: bool,
    delete_warning: Optional[str] = None,
) -> int:
    if delete_warning and not dry_run:
        print(style_text(delete_warning, Ansi.BOLD, Ansi.YELLOW))
    try:
        return print_cleanup_result(cleanup_clones(CodexPaths(), target_provider=target_provider, dry_run=dry_run))
    except ToolkitError as exc:
        print(str(exc), file=sys.stderr)
        return 1


class ToolkitTuiApp:
    def __init__(self, context: ToolkitAppContext, screen_mode: Optional[ScreenModeDecision] = None) -> None:
        self.context = context
        self.paths = CodexPaths()
        self.menu_actions = build_tui_menu_actions()
        self.menu_sections = build_tui_menu_sections()
        self.hotkey_to_index = {menu_action.hotkey: idx for idx, menu_action in enumerate(self.menu_actions)}
        self.screen_mode = screen_mode or resolve_screen_mode()

    def _cli_preview(self, args: Sequence[str]) -> str:
        return " ".join((self.context.entry_command, *args)) if args else self.context.entry_command

    def _screen_layout(self) -> Tuple[int, int, bool]:
        screen_width = term_width()
        return screen_width, min(tui_width(screen_width), 96), True

    def _print_centered_text(self, text: str) -> None:
        screen_width, _, center = self._screen_layout()
        print(align_line(text, screen_width, center=center))

    def _print_centered_box(self, lines: Sequence[str]) -> None:
        screen_width, _, center = self._screen_layout()
        for line in lines:
            print(align_line(line, screen_width, center=center))

    def _print_branded_header(self, title: str, subtitle: str = "") -> int:
        clear_screen()
        _, box_width, _ = self._screen_layout()
        self._print_centered_text(style_text("Codex Session Toolkit", Ansi.BOLD, Ansi.CYAN))
        self._print_centered_text(style_text(title, Ansi.DIM))
        if subtitle:
            self._print_centered_text(style_text(subtitle, Ansi.DIM))
        print("")
        return box_width

    def _fit_lines_to_screen(self, lines: List[str]) -> List[str]:
        max_rows = max(12, term_height())
        if len(lines) <= max_rows:
            return lines
        return lines[: max(6, max_rows - 1)] + [style_text("... truncated ...", Ansi.DIM, Ansi.YELLOW)]

    def _await_input(self, prompt: str = "") -> str:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", prompt)
        leading_newlines = len(plain) - len(plain.lstrip("\n"))
        if leading_newlines:
            sys.stdout.write("\n" * leading_newlines)
            sys.stdout.flush()
            plain = plain.lstrip("\n")
        width = term_width()
        indent = max(0, (width - display_width(plain)) // 2)
        return builtins.input(" " * indent + plain)

    def _run_centered(self, runner: Callable[[], int]) -> int:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = runner()
        lines = buffer.getvalue().splitlines()
        if not lines:
            return result
        max_width = max(display_width(line) for line in lines)
        indent = max(0, (term_width() - max_width) // 2)
        for line in lines:
            print(" " * indent + line)
        return result

    def _prompt_value(
        self,
        *,
        title: str,
        prompt_label: str,
        help_lines: List[str],
        default: str = "",
        allow_empty: bool = True,
    ) -> Optional[str]:
        box_width = self._print_branded_header(title)
        self._print_centered_box(render_box(help_lines, width=box_width, border_codes=(Ansi.DIM, Ansi.BLUE)))
        suffix = f" (default: {default})" if default else ""
        value = self._await_input(style_text(f"{prompt_label}{suffix}: ", Ansi.BOLD, Ansi.CYAN)).strip()
        if value:
            return value
        if default:
            return default
        return "" if allow_empty else None

    def _confirm_toggle(
        self,
        *,
        title: str,
        question: str,
        yes_label: str,
        no_label: str,
        default_yes: bool = False,
    ) -> bool:
        default = yes_label if default_yes else no_label
        answer = self._prompt_value(
            title=title,
            prompt_label=f"{question} ({yes_label}/{no_label})",
            help_lines=[f"Enter {yes_label} or {no_label}."],
            default=default,
            allow_empty=False,
        )
        return str(answer).strip().lower() == yes_label.lower()

    def _show_detail_panel(
        self,
        title: str,
        lines: List[str],
        *,
        border_codes: Optional[Tuple[str, ...]] = None,
    ) -> None:
        box_width = self._print_branded_header(title)
        self._print_centered_box(render_box(lines, width=box_width, border_codes=border_codes or (Ansi.DIM, Ansi.BLUE)))
        self._await_input(style_text("\nPress Enter to return...", Ansi.DIM))

    def _open_session_browser(self, *, mode: str) -> None:
        args = ["list", "--limit", "20"] if mode == "view" else ["list", "--limit", "50"]
        self._run_toolkit(args)
        return None

    def _open_bundle_browser(self, *, mode: str, source_group: str = "all") -> None:
        args = ["list-bundles", "--limit", "20"]
        if source_group != "all":
            args.extend(["--source", source_group])
        self._run_toolkit(args)
        return None

    def _select_batch_bundle_import_scope(self) -> None:
        return None

    def _resolve_menu_action_request(self, menu_action: TuiMenuAction) -> Tuple[Optional[str], Optional[List[str]]]:
        if menu_action.action_id == "exit":
            return "Exit", None
        if menu_action.action_id == "list_sessions":
            self._open_session_browser(mode="view")
            return None, None
        if menu_action.action_id == "browse_bundles":
            self._open_bundle_browser(mode="view")
            return None, None
        if menu_action.action_id == "restore_backup":
            backup_dir = self._prompt_value(
                title="Restore backup",
                prompt_label="Backup directory",
                help_lines=["Paste a backup directory under repair_backups."],
                allow_empty=False,
            )
            return "Restore backup", ["restore-backup", str(backup_dir)] if backup_dir else (None, None)[1]
        if menu_action.action_id == "promote_session":
            session_id = self._prompt_value(
                title="Repair one session visibility",
                prompt_label="Session ID",
                help_lines=["Enter the session id to repair."],
                allow_empty=False,
            )
            if not session_id:
                return None, None
            detected = detect_session_provider(CodexPaths(), str(session_id)) or self.context.target_provider
            provider = self._prompt_value(
                title="Repair one session visibility",
                prompt_label="Provider",
                help_lines=["Use the provider that Desktop should show."],
                default=detected,
                allow_empty=False,
            )
            return f"Repair session visibility ({provider})", ["promote-session", str(session_id), str(provider)]
        if menu_action.action_id == "repair_session_history":
            session_id = self._prompt_value(
                title="Repair one session history",
                prompt_label="Session ID",
                help_lines=["Enter the session id to repair."],
                allow_empty=False,
            )
            if not session_id:
                return None, None
            detected = detect_session_provider(CodexPaths(), str(session_id)) or self.context.target_provider
            provider = self._prompt_value(
                title="Repair one session history",
                prompt_label="Provider",
                help_lines=["Use the provider that Desktop should show."],
                default=detected,
                allow_empty=False,
            )
            args = ["repair-session-history", str(session_id), str(provider)]
            if self._confirm_toggle(
                title="Repair one session history",
                question="Rebuild clean clone if needed",
                yes_label="yes",
                no_label="no",
                default_yes=False,
            ):
                args.append("--rebuild-clone")
            return f"Repair session history ({provider})", args

        args: List[str] = []
        for item in menu_action.cli_args:
            if item.startswith("<") and item.endswith(">"):
                value = self._prompt_value(
                    title=menu_action.label,
                    prompt_label=item.strip("<>"),
                    help_lines=[f"Enter value for {item}."],
                    allow_empty=False,
                )
                if not value:
                    return None, None
                args.append(value)
            else:
                args.append(item)
        return menu_action.label, args

    def _run_toolkit(self, args: Sequence[str]) -> int:
        return run_toolkit_cli(list(args))

    def _confirm_dangerous_action(self, cli_args: Sequence[str]) -> bool:
        box_width = self._print_branded_header("Dangerous action")
        self._print_centered_box(
            render_box(
                [
                    f"Command: {self._cli_preview(cli_args)}",
                    "Type DELETE to continue.",
                ],
                width=box_width,
                border_codes=(Ansi.DIM, Ansi.RED),
            )
        )
        return self._await_input(style_text("Confirm DELETE: ", Ansi.BOLD, Ansi.RED)).strip() == "DELETE"

    def _run_action(
        self,
        action_name: str,
        cli_args: Sequence[str],
        *,
        dry_run: bool,
        runner: Callable[[], int],
        danger: bool,
        preview_cmd: Optional[str] = None,
    ) -> None:
        box_width = self._print_branded_header("Running")
        color = Ansi.RED if danger and not dry_run else Ansi.YELLOW if dry_run else Ansi.CYAN
        self._print_centered_box(
            render_box(
                [
                    f"Action: {action_name}",
                    f"Command: {preview_cmd or self._cli_preview(cli_args)}",
                    f"Target provider: {self.context.target_provider}",
                    f"Sessions dir: {self.context.active_sessions_dir}",
                ],
                width=box_width,
                border_codes=(Ansi.DIM, color),
            )
        )
        print("")
        result = self._run_centered(runner)
        if result != 0:
            self._print_centered_text(style_text(f"Command returned {result}", Ansi.BOLD, Ansi.YELLOW))
        self._await_input(style_text("\nPress Enter to return...", Ansi.DIM))

    def _execute_menu_action(self, chosen_action: TuiMenuAction) -> None:
        action_name, cli_args = self._resolve_menu_action_request(chosen_action)
        if cli_args is None:
            return
        if chosen_action.is_dangerous and not chosen_action.is_dry_run:
            if not self._confirm_dangerous_action(cli_args):
                return
        self._run_action(
            action_name or chosen_action.label,
            cli_args,
            dry_run=chosen_action.is_dry_run,
            runner=lambda args=cli_args: self._run_toolkit(args),
            danger=chosen_action.is_dangerous,
        )

    def _render_home(self) -> None:
        if _WINDOWS_VT_OK:
            sys.stdout.write("\033[H")
        else:
            clear_screen()
        _, box_width, _ = self._screen_layout()
        lines = [
            style_text("Codex Session Toolkit", Ansi.BOLD, Ansi.CYAN),
            "",
            f"Target provider: {self.context.target_provider}",
            f"Sessions dir: {self.context.active_sessions_dir}",
            "",
        ]
        for action in self.menu_actions:
            if action.action_id == "exit":
                continue
            lines.append(f"[{action.hotkey}] {action.label}")
        lines.append("[0] Exit")
        self._print_centered_box(render_box(self._fit_lines_to_screen(lines), width=box_width, border_codes=(Ansi.DIM, Ansi.BLUE)))

    def run(self) -> int:
        hub_active = bool(os.environ.get("AIK_HUB_ACTIVE"))
        if _WINDOWS_VT_OK:
            if not hub_active:
                sys.stdout.write(self.screen_mode.enter_sequence)
                sys.stdout.flush()
        try:
            self._render_home()
            while True:
                key = read_key(timeout_ms=500 if os.name == "nt" else None)
                if key is None:
                    continue
                key_str = str(key).strip().lower()
                if key == "ESC" or key_str in {"0", "q", "quit", "exit"}:
                    return 0
                for action in self.menu_actions:
                    if action.hotkey == key_str:
                        self._execute_menu_action(action)
                        self._render_home()
                        break
        finally:
            if _WINDOWS_VT_OK:
                if not hub_active:
                    sys.stdout.write(self.screen_mode.exit_sequence)
            elif not _WINDOWS_VT_OK:
                clear_screen()
            sys.stdout.flush()


def run_tui(context: ToolkitAppContext) -> int:
    return ToolkitTuiApp(context).run()
