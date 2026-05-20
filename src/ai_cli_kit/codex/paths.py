"""Path helpers for Codex session data and local bundle workspaces."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_STATE_DB_NAME_RE = re.compile(r"^state_(\d+)\.sqlite$")


@dataclass(frozen=True)
class CodexPaths:
    home: Path = field(default_factory=Path.home)
    cwd: Path = field(default_factory=Path.cwd)

    @property
    def code_dir(self) -> Path:
        return self.home / ".codex"

    @property
    def sessions_dir(self) -> Path:
        return self.code_dir / "sessions"

    @property
    def archived_sessions_dir(self) -> Path:
        return self.code_dir / "archived_sessions"

    @property
    def history_file(self) -> Path:
        return self.code_dir / "history.jsonl"

    @property
    def index_file(self) -> Path:
        return self.code_dir / "session_index.jsonl"

    @property
    def state_file(self) -> Path:
        return self.code_dir / ".codex-global-state.json"

    @property
    def config_file(self) -> Path:
        return self.code_dir / "config.toml"

    @property
    def local_bundle_workspace(self) -> Path:
        return self.cwd / "codex_sessions"

    @property
    def default_bundle_root(self) -> Path:
        return self.local_bundle_workspace

    @property
    def default_desktop_bundle_root(self) -> Path:
        return self.local_bundle_workspace

    @property
    def legacy_bundle_root(self) -> Path:
        return self.local_bundle_workspace / "bundles"

    @property
    def legacy_desktop_bundle_root(self) -> Path:
        return self.local_bundle_workspace / "desktop_bundles"

    def latest_state_db(self) -> Optional[Path]:
        """Return the newest Desktop state DB using a stable, refreshable key.

        Older revisions memoized the first lookup and picked the lexicographic
        last filename. That fails in two real cases:
        1. ``state_10.sqlite`` sorts before ``state_9.sqlite`` lexicographically.
        2. A long-lived TUI session keeps returning a stale DB after Desktop
           rotates from ``state_0001.sqlite`` to ``state_0002.sqlite``.

        Prefer the numeric suffix when present, then mtime, then filename as a
        deterministic tie-breaker. Recompute on every call so callers see
        rotation that happens after process start.
        """
        candidates: list[tuple[int, int, str, Path]] = []
        for candidate in self.code_dir.glob("state_*.sqlite"):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            match = _STATE_DB_NAME_RE.match(candidate.name)
            numeric_rank = int(match.group(1)) if match else -1
            candidates.append((numeric_rank, stat.st_mtime_ns, candidate.name, candidate))
        if not candidates:
            return None
        return max(candidates)[-1]
