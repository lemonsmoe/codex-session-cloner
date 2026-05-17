"""Provider resolution helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..errors import ToolkitError
from ..paths import CodexPaths

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None


OPENAI_OFFICIAL_PROVIDER = "openai"


def _nonempty_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _load_toml_data(config_file) -> dict[str, Any]:
    if tomllib is None:
        return {}
    try:
        with config_file.open("rb") as fh:
            data = tomllib.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _provider_from_text(text: str) -> str:
    match = re.search(r"^\s*model_provider\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _provider_from_declared_models(data: dict[str, Any]) -> str:
    providers = data.get("model_providers")
    if not isinstance(providers, dict) or not providers:
        return ""

    provider_keys = [key.strip() for key in providers if isinstance(key, str) and key.strip()]
    if len(provider_keys) == 1:
        return provider_keys[0]

    openai_auth_keys: list[str] = []
    for key, value in providers.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(value, dict):
            continue
        if value.get("requires_openai_auth") is True:
            openai_auth_keys.append(key.strip())
    if len(openai_auth_keys) == 1:
        return openai_auth_keys[0]

    return ""


def _provider_from_openai_official(
    paths: CodexPaths,
    data: dict[str, Any],
    text: str,
) -> str:
    marketplaces = data.get("marketplaces")
    official_marketplaces = {"openai-bundled", "openai-primary-runtime"}
    mentions_official_marketplace = (
        isinstance(marketplaces, dict)
        and any(name in marketplaces for name in official_marketplaces)
    )
    lower_text = text.lower()
    if not mentions_official_marketplace and not any(name in lower_text for name in official_marketplaces):
        return ""
    return OPENAI_OFFICIAL_PROVIDER


def _provider_requires_openai_auth(data: dict[str, Any], provider_name: str) -> bool:
    providers = data.get("model_providers")
    if not isinstance(providers, dict) or not provider_name:
        return False
    provider = providers.get(provider_name)
    return isinstance(provider, dict) and provider.get("requires_openai_auth") is True


def _timestamp_score(value: Any, fallback: float) -> float:
    if isinstance(value, str) and value:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            pass
    return fallback


def _provider_from_recent_sessions(paths: CodexPaths) -> tuple[str, float]:
    candidates: list[tuple[float, str]] = []
    for root in (paths.sessions_dir, paths.archived_sessions_dir):
        if not root.exists():
            continue
        for session_file in root.rglob("*.jsonl"):
            try:
                mtime = session_file.stat().st_mtime
                with session_file.open("r", encoding="utf-8") as fh:
                    first_line = fh.readline()
                if not first_line.strip():
                    continue
                obj = json.loads(first_line)
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                provider = _nonempty_str(payload.get("model_provider"))
                if provider:
                    candidates.append((_timestamp_score(payload.get("timestamp") or obj.get("timestamp"), mtime), provider))
            except (OSError, json.JSONDecodeError):
                continue

    if not candidates:
        return "", 0.0
    candidates.sort(key=lambda item: item[0], reverse=True)
    mtime, provider = candidates[0]
    return provider, mtime


def detect_provider(paths: CodexPaths, explicit: str = "") -> str:
    if explicit:
        return explicit

    config_file = paths.config_file
    if not config_file.exists():
        raise ToolkitError(f"Missing config file: {config_file}")

    data = _load_toml_data(config_file)
    text = config_file.read_text(encoding="utf-8")
    config_provider = _nonempty_str(data.get("model_provider"))
    if config_provider:
        return config_provider

    provider = _provider_from_text(text)
    if provider:
        return provider

    official_provider = _provider_from_openai_official(paths, data, text)
    if official_provider:
        return official_provider

    provider = _provider_from_declared_models(data)
    if provider:
        return provider

    provider, _ = _provider_from_recent_sessions(paths)
    if provider:
        return provider

    raise ToolkitError("Could not detect model_provider from ~/.codex/config.toml")
