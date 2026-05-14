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
    *,
    require_chatgpt_auth: bool = False,
) -> str:
    marketplaces = data.get("marketplaces")
    mentions_openai_bundle = isinstance(marketplaces, dict) and "openai-bundled" in marketplaces
    if not mentions_openai_bundle and "openai-bundled" not in text.lower():
        return ""

    auth_file = paths.code_dir / "auth.json"
    try:
        auth_data = json.loads(auth_file.read_text(encoding="utf-8")) if auth_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        auth_data = {}
    if not isinstance(auth_data, dict):
        return ""

    auth_mode = _nonempty_str(auth_data.get("auth_mode")).lower()
    tokens = auth_data.get("tokens")
    has_chatgpt_tokens = isinstance(tokens, dict) and any(
        _nonempty_str(tokens.get(key)) for key in ("id_token", "access_token", "refresh_token")
    )
    if auth_mode == "chatgpt" and has_chatgpt_tokens:
        return OPENAI_OFFICIAL_PROVIDER
    if require_chatgpt_auth:
        return ""
    if _nonempty_str(auth_data.get("OPENAI_API_KEY")):
        return OPENAI_OFFICIAL_PROVIDER
    return ""


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
    official_provider = _provider_from_openai_official(paths, data, text, require_chatgpt_auth=True)
    if official_provider:
        return official_provider

    if config_provider:
        return config_provider

    text = config_file.read_text(encoding="utf-8")
    provider = _provider_from_text(text)
    if provider:
        return provider

    provider = _provider_from_declared_models(data)
    if provider:
        return provider

    provider = _provider_from_openai_official(paths, data, text)
    if provider:
        return provider

    provider, _ = _provider_from_recent_sessions(paths)
    if provider:
        return provider

    raise ToolkitError("Could not detect model_provider from ~/.codex/config.toml")
