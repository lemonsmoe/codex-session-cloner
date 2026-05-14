"""Provider resolution helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import ToolkitError
from ..paths import CodexPaths

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None


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


def _provider_from_openai_official(paths: CodexPaths, data: dict[str, Any], text: str) -> str:
    marketplaces = data.get("marketplaces")
    mentions_openai_bundle = isinstance(marketplaces, dict) and "openai-bundled" in marketplaces
    if not mentions_openai_bundle and "openai-bundled" not in text.lower():
        return ""

    auth_file = paths.code_dir / "auth.json"
    try:
        auth_data = json.loads(auth_file.read_text(encoding="utf-8")) if auth_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        auth_data = {}
    if isinstance(auth_data, dict) and _nonempty_str(auth_data.get("OPENAI_API_KEY")):
        return "OpenAI"
    return ""


def detect_provider(paths: CodexPaths, explicit: str = "") -> str:
    if explicit:
        return explicit

    config_file = paths.config_file
    if not config_file.exists():
        raise ToolkitError(f"Missing config file: {config_file}")

    data = _load_toml_data(config_file)
    provider = _nonempty_str(data.get("model_provider"))
    if provider:
        return provider

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

    raise ToolkitError("Could not detect model_provider from ~/.codex/config.toml")
