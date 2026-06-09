# Codex Session Toolkit Maintenance

This document describes the maintenance workflow for this fork. The goal is to keep local provider-switching fixes easy to verify and easy to merge with upstream changes.

新 agent 接手前先阅读 [项目当前状态](context/00_current/index.md)，再按任务选择对应领域文档；多 agent 协作规范见 [项目上下文与协作运行手册](runbooks/context-management.md)。

## Branch Policy

- Keep `main` close to the GitHub upstream branch.
- Keep durable local work on `codex/new-arch-migration`.
- Use short-lived task branches for risky changes, then merge or cherry-pick back after tests pass.
- Rebase or merge from upstream frequently. Small conflicts are cheaper than one large recovery session.

Recommended sync rhythm:

```powershell
git fetch origin
git status --short --branch
git rebase origin/main
```

If the branch carries local commits that must not be rewritten for sharing, use merge instead of rebase:

```powershell
git fetch origin
git merge origin/main
```

## Safety Rules

- Run dry-run commands before any operation that edits `~/.codex`.
- Do not run real `dedupe-clones` until the dry-run output has been reviewed.
- Do not delete real session files without a backup under `~/.codex/repair_backups`.
- Do not print or commit `auth.json`, API keys, tokens, or provider secrets.
- Keep real Desktop recovery commands separate from code tests.

## Verification

Use the repository verification script before merging or committing session-repair changes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_codex_toolkit.ps1
```

The script runs:

- `tests/test_core_workflows.py`
- `py_compile` for core Codex service modules
- `dedupe-clones openai --dry-run`
- `repair-desktop openai --dry-run`

The dry-run commands read real Codex session data but should not write or delete files.

## Provider Switching Workflow

Preferred workflow when switching between providers:

```powershell
codex-session-toolkit.cmd switch-provider openai --dry-run
codex-session-toolkit.cmd switch-provider openai
codex-session-toolkit.cmd repair-desktop openai --dry-run
codex-session-toolkit.cmd repair-desktop openai
```

Use `restore-backup` if a switch or repair produces bad visibility state:

```powershell
codex-session-toolkit.cmd restore-backup C:\Users\22796\.codex\repair_backups\<backup-dir> --dry-run
codex-session-toolkit.cmd restore-backup C:\Users\22796\.codex\repair_backups\<backup-dir>
```

## Dedupe Workflow

The intended dedupe behavior is one visible Desktop conversation per logical lineage. The current implementation only deletes a lineage member when its normalized JSONL content is:

- exactly the same as the selected representative, or
- a prefix of the selected representative.

Diverged lineage content is skipped. Review dry-run output before running the real command:

```powershell
codex-session-toolkit.cmd dedupe-clones openai --dry-run
codex-session-toolkit.cmd dedupe-clones openai
```

Expected dry-run reasons:

- `same_content_keep_latest_representative`
- `prefix_content_keep_latest_representative`

If a duplicate appears in Desktop but is not listed in dry-run, inspect whether the session content diverged.

## Single Session Visibility Repair

When one known session exists on disk but is not visible in Desktop, use:

```powershell
codex-session-toolkit.cmd promote-session <session_id> openai --dry-run
codex-session-toolkit.cmd promote-session <session_id> openai
```

For the currently recovered toolkit conversation, the known id is:

```text
0e814ee0-3f0c-4e4e-aac3-cd1a10503242
```

## Refactor Direction

Keep repair logic concentrated in a few stable modules:

- `stores/session_files.py`: rollout JSONL parsing and identity handling.
- `stores/desktop_state.py`: Desktop state, index, and SQLite thread registration.
- `services/dedupe.py`: lineage graph and content-safety decisions.
- `services/repair.py`, `services/promote.py`, `services/switching.py`: orchestration only.

Avoid copying index/state/thread-writing logic into new commands. Add shared helpers first, then call them from services.
