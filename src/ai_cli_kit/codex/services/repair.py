"""Desktop repair service."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import OrderedDict
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ToolkitError
from ..models import RepairResult
from ..paths import CodexPaths
from ..services.provider import detect_provider
from ..stores.history import first_history_messages
from ..stores.index import load_existing_index
from ..stores.session_files import (
    build_session_preview,
    is_placeholder_thread_name,
    iter_session_files,
    parse_jsonl_records,
)
from ..support import (
    atomic_write,
    backup_file,
    classify_session_kind,
    file_lock,
    iso_to_epoch,
    lock_path_for,
    long_path,
    nearest_existing_parent,
    normalize_iso,
    prune_old_backups,
)


def _string_field(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _sqlite_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _desktop_visible_path(value: str) -> str:
    """Strip Windows long-path prefixes before writing Desktop state JSON."""
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _merge_ordered_strings(existing: object, additions: list[str]) -> list[str]:
    result = [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
    seen = set(result)
    for item in additions:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _merge_string_mapping(existing: object, updates: dict[str, str]) -> dict[str, str]:
    result = (
        {key: value for key, value in existing.items() if isinstance(key, str) and isinstance(value, str)}
        if isinstance(existing, dict)
        else {}
    )
    result.update({key: value for key, value in updates.items() if key and value})
    return result


def _workspace_write_permission(workspace_root: str) -> dict:
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


def repair_desktop(
    paths: CodexPaths,
    *,
    target_provider: str = "",
    dry_run: bool = False,
    include_cli: bool = False,
    retag_provider: bool = False,
) -> RepairResult:
    if not paths.code_dir.is_dir():
        raise ToolkitError(f"Missing Codex data directory: {paths.code_dir}")

    provider = detect_provider(paths, explicit=target_provider)
    backup_parent = paths.code_dir / "repair_backups"
    if not dry_run:
        prune_old_backups(backup_parent, keep_last=20)
    backup_root = backup_parent / f"visibility-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    backed_up: set[str] = set()
    warnings: list[str] = []

    history_first_messages = first_history_messages(paths.history_file)
    existing_index = load_existing_index(paths.index_file)
    state_db = paths.latest_state_db()

    entries: list[dict] = []
    changed_sessions: list[str] = []
    skipped_sessions: list[str] = []
    workspace_candidates: "OrderedDict[str, bool]" = OrderedDict()
    visible_thread_ids: list[str] = []
    thread_workspace_hints: dict[str, str] = {}
    thread_permissions: dict[str, dict] = {}
    desktop_retagged = 0
    cli_converted = 0

    for session_file in iter_session_files(paths):
        try:
            records = parse_jsonl_records(session_file)
        except ToolkitError as exc:
            warnings.append(f"Skipped invalid session file: {exc}")
            skipped_sessions.append(str(session_file))
            continue

        session_meta = None
        session_meta_index = -1
        embedded_session_meta_indices: set[int] = set()
        turn_context: dict = {}
        last_timestamp = ""

        for idx, (raw, obj) in enumerate(records):
            if not obj:
                continue
            timestamp = obj.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                last_timestamp = timestamp
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                if session_meta is None:
                    session_meta_index = idx
                    session_meta = dict(obj["payload"])
                else:
                    embedded_session_meta_indices.add(idx)
            elif obj.get("type") == "turn_context" and not turn_context and isinstance(obj.get("payload"), dict):
                turn_context = dict(obj["payload"])

        if not session_meta:
            warnings.append(f"Skipped session without session_meta: {session_file}")
            skipped_sessions.append(str(session_file))
            continue

        session_id = session_meta.get("id")
        if not isinstance(session_id, str) or not session_id:
            warnings.append(f"Skipped session without payload.id: {session_file}")
            skipped_sessions.append(str(session_file))
            continue

        source_name = _string_field(session_meta.get("source"))
        originator_name = _string_field(session_meta.get("originator"))
        session_kind = classify_session_kind(source_name, originator_name)
        desktop_like = session_kind == "desktop"
        convert_cli = include_cli and session_kind == "cli"

        updated_meta = dict(session_meta)
        changed = False
        sanitize_embedded_meta = False

        if retag_provider and desktop_like and provider and updated_meta.get("model_provider") != provider:
            updated_meta["model_provider"] = provider
            changed = True
            desktop_retagged += 1

        if convert_cli:
            if updated_meta.get("source") != "vscode":
                updated_meta["source"] = "vscode"
                changed = True
            if updated_meta.get("originator") != "Codex Desktop":
                updated_meta["originator"] = "Codex Desktop"
                changed = True
            if provider and updated_meta.get("model_provider") != provider:
                updated_meta["model_provider"] = provider
                changed = True
            if changed:
                cli_converted += 1
            source_name = updated_meta.get("source", source_name)
            originator_name = updated_meta.get("originator", originator_name)
            session_kind = "desktop"
            desktop_like = True

        if embedded_session_meta_indices and desktop_like and provider and updated_meta.get("model_provider") == provider:
            sanitize_embedded_meta = True

        if changed or sanitize_embedded_meta:
            changed_sessions.append(str(session_file))
            if not dry_run:
                backup_file(paths.code_dir, backup_root, backed_up, session_file, enabled=True)
                with atomic_write(session_file) as fh:
                    for idx, (raw, obj) in enumerate(records):
                        if not obj:
                            fh.write(raw)
                            continue
                        if idx == session_meta_index and obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                            patched = dict(obj)
                            patched["payload"] = updated_meta
                            fh.write(json.dumps(patched, ensure_ascii=False, separators=(",", ":")) + "\n")
                        elif idx in embedded_session_meta_indices and obj.get("type") == "session_meta":
                            patched = dict(obj)
                            patched["type"] = "session_meta_embedded"
                            fh.write(json.dumps(patched, ensure_ascii=False, separators=(",", ":")) + "\n")
                        else:
                            fh.write(raw)

        session_meta = updated_meta
        created_iso = normalize_iso(str(session_meta.get("timestamp", ""))) or normalize_iso(last_timestamp)
        updated_iso = (
            normalize_iso(last_timestamp)
            or created_iso
            or existing_index.get(session_id, {}).get("updated_at")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        cwd = session_meta.get("cwd", "") if isinstance(session_meta.get("cwd", ""), str) else ""
        preview_title = build_session_preview(history_first_messages.get(session_id, ""), session_file, cwd)
        existing_thread_name = existing_index.get(session_id, {}).get("thread_name", "")
        cloned_from = session_meta.get("cloned_from")
        parent_thread_name = (
            existing_index.get(cloned_from, {}).get("thread_name", "")
            if isinstance(cloned_from, str) and cloned_from
            else ""
        )
        thread_name = (
            parent_thread_name or preview_title
            if is_placeholder_thread_name(existing_thread_name, session_id)
            else existing_thread_name or parent_thread_name or preview_title or session_id
        )
        if cwd:
            desktop_cwd = _desktop_visible_path(cwd)
            candidate = nearest_existing_parent(desktop_cwd) or desktop_cwd
            if candidate and candidate not in workspace_candidates:
                workspace_candidates[candidate] = True

        archived = 1 if "archived_sessions" in session_file.parts else 0
        if desktop_like and provider and session_meta.get("model_provider") == provider and not archived:
            if cwd:
                desktop_cwd = _desktop_visible_path(cwd)
                workspace_root = _desktop_visible_path(nearest_existing_parent(desktop_cwd) or desktop_cwd)
                if workspace_root:
                    thread_workspace_hints[session_id] = workspace_root
                if workspace_root.lower().startswith(str(paths.code_dir).lower()):
                    visible_thread_ids.append(session_id)
                thread_permissions[session_id] = _workspace_write_permission(workspace_root)

        entries.append(
            {
                "id": session_id,
                "thread_name": thread_name,
                "updated_at": updated_iso,
                "session_file": session_file,
                "source": source_name,
                "originator": originator_name,
                "kind": session_kind,
                "cwd": cwd,
                "created_iso": created_iso or updated_iso,
                "updated_iso": updated_iso,
                "first_user_message": preview_title or thread_name,
                "sandbox_policy": json.dumps(turn_context.get("sandbox_policy", {}), ensure_ascii=False, separators=(",", ":")),
                "approval_mode": turn_context.get("approval_policy", "on-request"),
                "model_provider": session_meta.get("model_provider", "") if isinstance(session_meta.get("model_provider", ""), str) else "",
                "cli_version": session_meta.get("cli_version", "") if isinstance(session_meta.get("cli_version", ""), str) else "",
                "model": turn_context.get("model"),
                "reasoning_effort": turn_context.get("effort"),
                "archived": archived,
            }
        )

    entries_scanned_count = len(entries)
    entries.sort(key=lambda item: (iso_to_epoch(item["updated_at"]), item["id"]), reverse=True)
    unique_entries: list[dict] = []
    seen_entry_ids: set[str] = set()
    for entry in entries:
        if entry["id"] in seen_entry_ids:
            continue
        unique_entries.append(entry)
        seen_entry_ids.add(entry["id"])
    entries = unique_entries

    if not dry_run:
        backup_file(paths.code_dir, backup_root, backed_up, paths.index_file, enabled=True)
        # Serialise against concurrent upsert/remove on session_index.jsonl.
        with atomic_write(paths.index_file, lock_path=lock_path_for(paths.index_file)) as fh:
            for entry in entries:
                obj = {
                    "id": entry["id"],
                    "thread_name": entry["thread_name"],
                    "updated_at": entry["updated_at"],
                }
                fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    state_lock_path = lock_path_for(paths.state_file)
    # Hold the state.json lock for the entire read-modify-write so concurrent
    # ensure_desktop_workspace_root() (or other repair runs) cannot clobber
    # the workspace-roots merge we are about to compute.
    state_context = nullcontext() if dry_run else file_lock(state_lock_path)
    with state_context:
        try:
            state_data = json.loads(paths.state_file.read_text(encoding="utf-8")) if paths.state_file.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"Warning: failed to read state file {paths.state_file}: {exc}")
            state_data = {}

        saved_roots = list(state_data.get("electron-saved-workspace-roots", []))
        project_order = list(state_data.get("project-order", []))

        # On case-insensitive filesystems (Windows NTFS, macOS APFS-default)
        # `relative_to` is a string compare and would treat `C:\Users\Foo`
        # vs `c:\users\foo` as distinct, allowing duplicate workspace entries
        # to accumulate. Compare under normcase so case-only variants dedupe.
        def _is_subpath_ci(child: Path, parent: Path) -> bool:
            try:
                child_norm = Path(os.path.normcase(str(child)))
                parent_norm = Path(os.path.normcase(str(parent)))
                child_norm.relative_to(parent_norm)
                return True
            except ValueError:
                return False

        normcased_existing = {os.path.normcase(item) for item in saved_roots}
        normcased_order = {os.path.normcase(item) for item in project_order}
        for root in workspace_candidates:
            root_path = Path(root)
            covered = any(
                _is_subpath_ci(root_path, Path(existing)) for existing in saved_roots
            )
            if not covered:
                saved_roots.append(root)
                normcased_existing.add(os.path.normcase(root))
                if os.path.normcase(root) not in normcased_order:
                    project_order.append(root)
                    normcased_order.add(os.path.normcase(root))

        if not dry_run:
            state_data["electron-saved-workspace-roots"] = saved_roots
            state_data["active-workspace-roots"] = list(saved_roots)
            state_data["project-order"] = project_order
            state_data["projectless-thread-ids"] = _merge_ordered_strings(
                state_data.get("projectless-thread-ids"),
                visible_thread_ids,
            )
            state_data["thread-workspace-root-hints"] = _merge_string_mapping(
                state_data.get("thread-workspace-root-hints"),
                thread_workspace_hints,
            )
            atom_state = state_data.setdefault("electron-persisted-atom-state", {})
            if isinstance(atom_state, dict):
                atom_state["projectless-thread-ids"] = _merge_ordered_strings(
                    atom_state.get("projectless-thread-ids"),
                    visible_thread_ids,
                )
                atom_state["thread-workspace-root-hints"] = _merge_string_mapping(
                    atom_state.get("thread-workspace-root-hints"),
                    thread_workspace_hints,
                )
                existing_permissions = atom_state.get("heartbeat-thread-permissions-by-id")
                if not isinstance(existing_permissions, dict):
                    existing_permissions = {}
                merged_permissions = dict(existing_permissions)
                for session_id, permission in thread_permissions.items():
                    merged_permissions.setdefault(session_id, permission)
                atom_state["heartbeat-thread-permissions-by-id"] = merged_permissions
        else:
            state_data = dict(state_data)
            state_data["active-workspace-roots"] = list(saved_roots)

        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, paths.state_file, enabled=True)
            with atomic_write(paths.state_file) as fh:
                json.dump(state_data, fh, ensure_ascii=False, separators=(",", ":"))
                fh.write("\n")

    threads_updated = 0
    if state_db and state_db.exists():
        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, state_db, enabled=True)
        # long_path() prefixes \\?\ on Windows when the path exceeds MAX_PATH
        # (260 chars); sqlite3 ultimately uses CreateFileW which honours that
        # prefix. No-op on POSIX where it just returns the original string.
        with closing(sqlite3.connect(long_path(state_db), timeout=30)) as conn, conn:
            cur = conn.cursor()
            row = cur.execute("select name from sqlite_master where type='table' and name='threads'").fetchone()
            if row:
                columns = [r[1] for r in cur.execute("pragma table_info(threads)").fetchall()]
                updatable_entries = [entry for entry in entries if entry["kind"] == "desktop"]
                for entry in updatable_entries:
                    data = {
                        "id": entry["id"],
                        "rollout_path": str(entry["session_file"]),
                        "created_at": iso_to_epoch(entry["created_iso"]),
                        "updated_at": iso_to_epoch(entry["updated_iso"]),
                        "source": (entry["source"] if isinstance(entry["source"], str) and entry["source"] else "vscode"),
                        "model_provider": entry["model_provider"] or provider,
                        "cwd": entry["cwd"],
                        "title": entry["thread_name"],
                        "sandbox_policy": entry["sandbox_policy"],
                        "approval_mode": entry["approval_mode"],
                        "tokens_used": 0,
                        "has_user_event": 1,
                        "archived": entry["archived"],
                        "archived_at": iso_to_epoch(entry["updated_iso"]) if entry["archived"] else None,
                        "cli_version": entry["cli_version"],
                        "first_user_message": entry["first_user_message"],
                        "memory_mode": "enabled",
                        "model": entry["model"],
                        "reasoning_effort": entry["reasoning_effort"],
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
                    threads_updated += 1

                if not dry_run:
                    conn.commit()
            else:
                warnings.append(f"threads table not found in {state_db}")

    return RepairResult(
        provider=provider,
        dry_run=dry_run,
        include_cli=include_cli,
        retag_provider=retag_provider,
        entries_scanned=entries_scanned_count,
        desktop_retagged=desktop_retagged,
        cli_converted=cli_converted,
        skipped_sessions=skipped_sessions,
        workspace_roots_count=len(state_data.get("active-workspace-roots", [])),
        threads_updated=threads_updated,
        visible_thread_ids_count=len(visible_thread_ids),
        workspace_hints_count=len(thread_workspace_hints),
        backup_root=(None if dry_run else backup_root),
        changed_sessions=changed_sessions,
        warnings=warnings,
    )
