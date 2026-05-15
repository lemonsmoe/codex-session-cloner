"""Provider switch and backup restore services."""

from __future__ import annotations

from pathlib import Path

from ..errors import ToolkitError
from ..models import RestoreBackupResult, SwitchResult
from ..paths import CodexPaths
from ..services.repair import repair_desktop
from ..support import atomic_write, backup_file, safe_copy2


def switch_provider(
    paths: CodexPaths,
    *,
    target_provider: str = "",
    dry_run: bool = False,
    include_cli: bool = False,
) -> SwitchResult:
    """Retag Desktop-compatible sessions in-place to the target provider.

    This is intentionally a thin, explicit wrapper around repair_desktop's
    retag path. It gives users a safer command name for the in-place migration
    workflow while retaining the existing backup and Desktop visibility rebuild.
    """
    result = repair_desktop(
        paths,
        target_provider=target_provider,
        dry_run=dry_run,
        include_cli=include_cli,
        retag_provider=True,
    )
    return SwitchResult(provider=result.provider, dry_run=dry_run, repair_result=result)


def restore_repair_backup(
    paths: CodexPaths,
    backup_root: Path,
    *,
    dry_run: bool = False,
) -> RestoreBackupResult:
    backup_root = Path(backup_root).expanduser()
    if not backup_root.is_dir():
        raise ToolkitError(f"Backup directory not found: {backup_root}")

    restore_candidates: list[tuple[Path, Path]] = []
    for backup_file_path in backup_root.rglob("*"):
        if not backup_file_path.is_file():
            continue
        relative = backup_file_path.relative_to(backup_root)
        restore_candidates.append((backup_file_path, paths.code_dir / relative))

    restored: list[Path] = []
    errors: list[tuple[Path, str]] = []
    if dry_run:
        return RestoreBackupResult(
            backup_root=backup_root,
            dry_run=True,
            files_found=len(restore_candidates),
            files_restored=[target for _, target in restore_candidates],
        )

    rollback_backup_root = paths.code_dir / "repair_backups" / f"pre-restore-{backup_root.name}"
    backed_up: set[str] = set()
    for source, target in restore_candidates:
        try:
            backup_file(paths.code_dir, rollback_backup_root, backed_up, target, enabled=target.exists())
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix in {".jsonl", ".json", ".toml", ".env"}:
                with source.open("r", encoding="utf-8", newline="") as in_fh:
                    content = in_fh.read()
                with atomic_write(target) as out_fh:
                    out_fh.write(content)
            else:
                safe_copy2(source, target)
            restored.append(target)
        except Exception as exc:
            errors.append((target, str(exc)))

    return RestoreBackupResult(
        backup_root=backup_root,
        dry_run=False,
        files_found=len(restore_candidates),
        files_restored=restored,
        errors=errors,
    )
