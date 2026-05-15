"""Desktop state and SQLite helpers."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..errors import ToolkitError
from ..stores.history import first_history_messages
from ..stores.session_files import build_session_preview, is_placeholder_thread_name
from ..support import atomic_write, file_lock, iso_to_epoch, lock_path_for, long_path, nearest_existing_parent


def _sqlite_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _string_field(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def desktop_visible_path(value: str) -> str:
    """Strip Windows long-path prefixes before writing Desktop state JSON."""
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def merge_ordered_strings(existing: object, additions: Iterable[str]) -> list[str]:
    result = [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
    seen = set(result)
    for item in additions:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def merge_string_mapping(existing: object, updates: Mapping[str, str]) -> dict[str, str]:
    result = (
        {key: value for key, value in existing.items() if isinstance(key, str) and isinstance(value, str)}
        if isinstance(existing, dict)
        else {}
    )
    result.update({key: value for key, value in updates.items() if key and value})
    return result


def workspace_write_permission(workspace_root: str) -> dict:
    return {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandboxPolicy": {
            "type": "workspaceWrite",
            "writableRoots": [workspace_root],
            "excludeSlashTmp": False,
            "excludeTmpdirEnvVar": False,
            "networkAccess": False,
        },
    }


def session_workspace_roots(cwd: str) -> list[str]:
    if not cwd:
        return []
    visible_cwd = desktop_visible_path(cwd)
    roots: dict[str, bool] = {}
    nearest = nearest_existing_parent(visible_cwd) or visible_cwd
    for candidate in (nearest, visible_cwd):
        if candidate:
            roots[desktop_visible_path(candidate)] = True
    parent = str(Path(nearest).parent) if nearest else ""
    if parent and parent != nearest:
        roots[desktop_visible_path(parent)] = True
    return list(roots)


def _is_subpath(child: Path, parent: Path) -> bool:
    """Case-insensitive subpath check (Win NTFS / macOS APFS-default).

    On case-insensitive filesystems, ``Path.relative_to`` does a literal string
    compare and would treat case-variant roots (``C:\\Users\\Foo`` vs
    ``c:\\users\\foo``) as distinct. ``os.path.normcase`` normalises that
    difference; on Linux it is the identity function so POSIX behavior is
    unchanged.
    """
    try:
        child_norm = Path(os.path.normcase(str(child)))
        parent_norm = Path(os.path.normcase(str(parent)))
        child_norm.relative_to(parent_norm)
        return True
    except ValueError:
        return False


def _paths_equal_ci(a: Path, b: Path) -> bool:
    """Case-insensitive path equality (matches ``_is_subpath`` semantics)."""
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def merge_desktop_visibility_state(
    state_data: object,
    *,
    workspace_roots: Iterable[str],
    visible_thread_ids: Iterable[str],
    thread_workspace_hints: Mapping[str, str],
    thread_permissions: Mapping[str, dict],
    expand_workspace_roots: Iterable[str] = (),
) -> dict:
    data = dict(state_data) if isinstance(state_data, dict) else {}
    saved_roots = [item for item in data.get("electron-saved-workspace-roots", []) if isinstance(item, str)]
    project_order = [item for item in data.get("project-order", []) if isinstance(item, str)]

    normcased_order = {os.path.normcase(item) for item in project_order}
    for root in workspace_roots:
        if not root:
            continue
        root_path = Path(root)
        covered = any(_is_subpath(root_path, Path(existing)) for existing in saved_roots)
        if not covered:
            saved_roots.append(root)
        if os.path.normcase(root) not in normcased_order:
            project_order.append(root)
            normcased_order.add(os.path.normcase(root))

    data["electron-saved-workspace-roots"] = saved_roots
    data["active-workspace-roots"] = list(saved_roots)
    data["project-order"] = project_order
    data["projectless-thread-ids"] = merge_ordered_strings(
        data.get("projectless-thread-ids"),
        list(visible_thread_ids),
    )
    data["thread-workspace-root-hints"] = merge_string_mapping(
        data.get("thread-workspace-root-hints"),
        thread_workspace_hints,
    )

    atom_state = data.setdefault("electron-persisted-atom-state", {})
    if isinstance(atom_state, dict):
        collapsed = atom_state.get("sidebar-collapsed-groups")
        if isinstance(collapsed, dict):
            for root in expand_workspace_roots:
                collapsed.pop(root, None)
        atom_state["projectless-thread-ids"] = merge_ordered_strings(
            atom_state.get("projectless-thread-ids"),
            list(visible_thread_ids),
        )
        atom_state["thread-workspace-root-hints"] = merge_string_mapping(
            atom_state.get("thread-workspace-root-hints"),
            thread_workspace_hints,
        )
        existing_permissions = atom_state.get("heartbeat-thread-permissions-by-id")
        if not isinstance(existing_permissions, dict):
            existing_permissions = {}
        merged_permissions = dict(existing_permissions)
        for session_id, permission in thread_permissions.items():
            if session_id and permission:
                merged_permissions[session_id] = permission
        atom_state["heartbeat-thread-permissions-by-id"] = merged_permissions

    return data


def upsert_thread_entries(
    state_db: Path | None,
    entries: list[dict],
    *,
    provider: str,
    dry_run: bool = False,
) -> tuple[int, list[str]]:
    warnings: list[str] = []
    if not state_db or not state_db.exists():
        return 0, warnings

    count = 0
    with closing(sqlite3.connect(long_path(state_db), timeout=30)) as conn, conn:
        cur = conn.cursor()
        row = cur.execute("select name from sqlite_master where type='table' and name='threads'").fetchone()
        if not row:
            return 0, [f"threads table not found in {state_db}"]

        columns = [r[1] for r in cur.execute("pragma table_info(threads)").fetchall()]
        for entry in entries:
            data = {
                "id": entry["id"],
                "rollout_path": str(entry["session_file"]),
                "created_at": iso_to_epoch(entry["created_iso"]),
                "updated_at": iso_to_epoch(entry["updated_iso"]),
                "source": (entry["source"] if isinstance(entry["source"], str) and entry["source"] else "vscode"),
                "model_provider": entry.get("model_provider") or provider,
                "cwd": entry.get("cwd", ""),
                "title": entry.get("thread_name", entry["id"]),
                "sandbox_policy": entry.get("sandbox_policy", "{}"),
                "approval_mode": entry.get("approval_mode", "on-request"),
                "tokens_used": 0,
                "has_user_event": 1,
                "archived": entry.get("archived", 0),
                "archived_at": iso_to_epoch(entry["updated_iso"]) if entry.get("archived") else None,
                "cli_version": entry.get("cli_version", ""),
                "first_user_message": entry.get("first_user_message", entry.get("thread_name", entry["id"])),
                "memory_mode": "enabled",
                "model": entry.get("model"),
                "reasoning_effort": entry.get("reasoning_effort"),
            }
            insert_cols = [name for name in data if name in columns]
            placeholders = ", ".join("?" for _ in insert_cols)
            col_list = ", ".join(insert_cols)
            update_cols = [name for name in insert_cols if name != "id"]
            update_sql = ", ".join(f"{name}=excluded.{name}" for name in update_cols)
            values = [_sqlite_value(data[name]) for name in insert_cols]
            sql = f"insert into threads ({col_list}) values ({placeholders}) on conflict(id) do update set {update_sql}"
            if not dry_run:
                cur.execute(sql, values)
            count += 1

        if not dry_run:
            conn.commit()

    return count, warnings


def ensure_desktop_workspace_root(workspace_dir: str, state_file: Path) -> bool:
    if not state_file.exists():
        print(f"Warning: Codex Desktop state file not found: {state_file}", file=sys.stderr)
        return False

    # Hold the canonical state.json lock for the full read-modify-write so concurrent
    # repair_desktop() (which also rewrites state.json) cannot clobber our addition,
    # and vice versa. lock_path_for() ensures we share the same lock-file path.
    with file_lock(lock_path_for(state_file)):
        with state_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        saved = list(data.setdefault("electron-saved-workspace-roots", []))
        project_order = list(data.setdefault("project-order", []))

        covered = False
        workspace_path = Path(workspace_dir)
        for root in saved:
            existing = Path(root)
            if _paths_equal_ci(workspace_path, existing) or _is_subpath(workspace_path, existing):
                covered = True
                break

        if not covered:
            saved.append(workspace_dir)
            normcased_order = {os.path.normcase(item) for item in project_order}
            if os.path.normcase(workspace_dir) not in normcased_order:
                project_order.append(workspace_dir)

        data["electron-saved-workspace-roots"] = saved
        data["active-workspace-roots"] = list(saved)
        data["project-order"] = project_order

        with atomic_write(state_file) as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write("\n")
    return True


def prepare_session_for_import(
    source_session: Path,
    prepared_session: Path,
    *,
    auto_desktop_compat: bool,
    session_kind: str,
    target_desktop_model_provider: str,
) -> None:
    # newline="" preserves LF line endings across platforms — critical on Windows
    # where text-mode write would translate \n → \r\n and break byte-comparison
    # with the existing target_session in importing.py (read_bytes-based diff).
    with source_session.open("r", encoding="utf-8", newline="") as in_fh, \
            prepared_session.open("w", encoding="utf-8", newline="") as out_fh:
        saw_session_meta = False
        for raw in in_fh:
            line = raw.rstrip("\n")
            if not line:
                out_fh.write(raw)
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out_fh.write(raw)
                continue

            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                if saw_session_meta:
                    obj = dict(obj)
                    obj["type"] = "session_meta_embedded"
                    out_fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
                    continue
                saw_session_meta = True
                payload = dict(obj["payload"])
                if auto_desktop_compat and session_kind == "cli":
                    payload["source"] = "vscode"
                    payload["originator"] = "Codex Desktop"
                if target_desktop_model_provider:
                    payload["model_provider"] = target_desktop_model_provider

                obj = dict(obj)
                obj["payload"] = payload
                out_fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
                auto_desktop_compat = False
                target_desktop_model_provider = ""
                continue

            out_fh.write(raw)


def upsert_threads_table(
    state_db: Path,
    session_file: Path,
    history_file: Path,
    target_rollout: Path,
    *,
    session_id: str,
    thread_name: str,
    updated_at: str,
    session_cwd: str,
    session_source: str,
    session_originator: str,
    session_kind: str,
    classify_session_kind,
) -> bool:
    if not state_db or not state_db.is_file():
        return False

    meta: dict = {}
    turn_context: dict = {}
    last_timestamp = ""

    with session_file.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except Exception as exc:
                raise ToolkitError(f"Failed to parse prepared session file at line {line_number}: {exc}") from exc
            timestamp = obj.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                last_timestamp = timestamp
            if obj.get("type") == "session_meta" and not meta and isinstance(obj.get("payload"), dict):
                meta = obj.get("payload", {})
            elif obj.get("type") == "turn_context" and not turn_context and isinstance(obj.get("payload"), dict):
                turn_context = obj.get("payload", {})

    history_preview = first_history_messages(history_file).get(session_id, "")

    source_name = _string_field(session_source) or _string_field(meta.get("source", ""))
    originator_name = _string_field(session_originator) or _string_field(meta.get("originator", ""))
    effective_kind = session_kind or classify_session_kind(source_name, originator_name)
    cwd = _string_field(session_cwd) or _string_field(meta.get("cwd", ""))
    first_user_message = build_session_preview(history_preview, session_file, cwd)
    created_iso = _string_field(meta.get("timestamp")) or last_timestamp or updated_at
    updated_iso = updated_at or last_timestamp or created_iso
    title = (
        first_user_message
        if is_placeholder_thread_name(thread_name, session_id)
        else thread_name or first_user_message or session_id
    )
    sandbox_policy = json.dumps(turn_context.get("sandbox_policy", {}), ensure_ascii=False, separators=(",", ":"))
    approval_mode = _sqlite_value(turn_context.get("approval_policy", "on-request"))
    model_provider = _string_field(meta.get("model_provider", ""))
    cli_version = _string_field(meta.get("cli_version", ""))
    model = _sqlite_value(turn_context.get("model"))
    reasoning_effort = _sqlite_value(turn_context.get("effort"))
    archived = 1 if "archived_sessions" in target_rollout.parts else 0
    archived_at = iso_to_epoch(updated_iso) if archived else None

    # long_path() prefixes \\?\ on Windows so sqlite3 (CreateFileW under the
    # hood) can open paths > MAX_PATH. POSIX no-op.
    with closing(sqlite3.connect(long_path(state_db), timeout=30)) as conn, conn:
        cur = conn.cursor()
        row = cur.execute("select name from sqlite_master where type='table' and name='threads'").fetchone()
        if not row:
            return False

        columns = [r[1] for r in cur.execute("pragma table_info(threads)").fetchall()]
        data = {
            "id": session_id,
            "rollout_path": str(target_rollout),
            "created_at": iso_to_epoch(created_iso),
            "updated_at": iso_to_epoch(updated_iso),
            "source": source_name or ("vscode" if effective_kind == "desktop" else "cli" if effective_kind == "cli" else "unknown"),
            "model_provider": model_provider,
            "cwd": cwd,
            "title": title,
            "sandbox_policy": sandbox_policy,
            "approval_mode": approval_mode,
            "tokens_used": 0,
            "has_user_event": 1,
            "archived": archived,
            "archived_at": archived_at,
            "cli_version": cli_version,
            "first_user_message": first_user_message or title,
            "memory_mode": "enabled",
            "model": model,
            "reasoning_effort": reasoning_effort,
        }

        insert_cols = [c for c in data if c in columns]
        placeholders = ", ".join("?" for _ in insert_cols)
        col_list = ", ".join(insert_cols)
        update_cols = [c for c in insert_cols if c != "id"]
        update_sql = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        values = [_sqlite_value(data[c]) for c in insert_cols]

        sql = f"insert into threads ({col_list}) values ({placeholders}) on conflict(id) do update set {update_sql}"
        cur.execute(sql, values)
        conn.commit()
    return True
