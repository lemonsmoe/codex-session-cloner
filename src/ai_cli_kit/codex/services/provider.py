"""Provider resolution helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..errors import ToolkitError
from ..paths import CodexPaths

DEFAULT_MODEL_PROVIDER = "openai"

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None


OPENAI_OFFICIAL_PROVIDER = "openai"


@dataclass(frozen=True)
class ProviderContext:
    model_provider: str
    model: str = ""
    provider_key: str = ""
    provider_name: str = ""
    base_url: str = ""
    base_url_host: str = ""
    wire_api: str = ""
    requires_openai_auth: bool = False
    is_openai_official: bool = False
    is_ccswitch: bool = False
    ccswitch_provider_id: str = ""
    ccswitch_provider_name: str = ""
    ccswitch_api_format: str = ""
    is_local_route: bool = False
    legacy_profile_detected: bool = False
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def label(self) -> str:
        return self.ccswitch_provider_name or self.provider_name or self.provider_key or self.model_provider


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


def _load_toml_text(text: str) -> dict[str, Any]:
    if tomllib is None or not text.strip():
        return {}
    try:
        data = tomllib.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _provider_from_text(text: str) -> str:
    match = re.search(r"^\s*model_provider\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _provider_config(data: dict[str, Any], provider_key: str) -> dict[str, Any]:
    providers = data.get("model_providers")
    if not isinstance(providers, dict) or not provider_key:
        return {}
    provider = providers.get(provider_key)
    return provider if isinstance(provider, dict) else {}


def _host_from_url(base_url: str) -> str:
    if not base_url:
        return ""
    parsed = urlparse(base_url if "://" in base_url else "https://" + base_url)
    return (parsed.hostname or "").lower()


def _is_local_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _legacy_profile_detected(text: str) -> bool:
    return bool(
        re.search(r"^\s*profile\s*=\s*['\"][^'\"]+['\"]", text, re.MULTILINE)
        or re.search(r"^\s*\[profiles\.", text, re.MULTILINE)
    )


def _auth_has_chatgpt_tokens(paths: CodexPaths) -> bool:
    try:
        data = json.loads((paths.code_dir / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("auth_mode") != "chatgpt":
        return False
    tokens = data.get("tokens")
    return isinstance(tokens, dict) and any(tokens.get(key) for key in ("id_token", "access_token", "refresh_token"))


def _ccswitch_root(paths: CodexPaths) -> Path:
    return paths.home / ".cc-switch"


def _read_ccswitch_current_provider(paths: CodexPaths, warnings: list[str]) -> str:
    settings_file = _ccswitch_root(paths) / "settings.json"
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"Warning: failed to read CcSwitch settings: {exc}")
        return ""
    if not isinstance(data, dict):
        return ""
    return _nonempty_str(data.get("currentProviderCodex"))


def _read_ccswitch_provider(paths: CodexPaths, provider_id: str, warnings: list[str]) -> dict[str, Any]:
    db_file = _ccswitch_root(paths) / "cc-switch.db"
    if not provider_id or not db_file.exists():
        return {}
    conn = None
    try:
        conn = sqlite3.connect(db_file, timeout=2)
        columns = [row[1] for row in conn.execute("pragma table_info(providers)").fetchall()]
        if not columns:
            return {}
        select_cols = [col for col in ("id", "name", "settings_config", "app_type") if col in columns]
        rows = conn.execute(f"select {', '.join(select_cols)} from providers").fetchall()
    except sqlite3.Error as exc:
        warnings.append(f"Warning: failed to read CcSwitch provider DB: {exc}")
        return {}
    finally:
        if conn is not None:
            conn.close()

    for row in rows:
        item = dict(zip(select_cols, row))
        app_type = _nonempty_str(item.get("app_type"))
        if app_type and app_type.lower() != "codex":
            continue
        row_id = _nonempty_str(item.get("id"))
        row_name = _nonempty_str(item.get("name"))
        if provider_id not in {row_id, row_name}:
            continue
        return item
    return {}


def _ccswitch_config_context(paths: CodexPaths, warnings: list[str]) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    provider_id = _read_ccswitch_current_provider(paths, warnings)
    provider_row = _read_ccswitch_provider(paths, provider_id, warnings)
    settings_config = provider_row.get("settings_config")
    settings_data: dict[str, Any] = {}
    if isinstance(settings_config, str) and settings_config.strip():
        try:
            parsed = json.loads(settings_config)
            settings_data = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError as exc:
            warnings.append(f"Warning: failed to parse CcSwitch provider settings: {exc}")
    config_text = _nonempty_str(settings_data.get("config"))
    return provider_id, _nonempty_str(provider_row.get("name")), settings_data, _load_toml_text(config_text)


def _context_fingerprint(
    model_provider: str,
    *,
    provider_name: str,
    base_url_host: str,
    is_openai_official: bool,
    is_ccswitch: bool,
    ccswitch_provider_id: str,
) -> str:
    if is_openai_official:
        return OPENAI_OFFICIAL_PROVIDER
    if is_ccswitch and ccswitch_provider_id:
        return f"ccswitch:{ccswitch_provider_id}"
    if model_provider == "custom" and (provider_name or base_url_host):
        return f"custom:{provider_name or 'unknown'}:{base_url_host or 'unknown'}"
    return model_provider or DEFAULT_MODEL_PROVIDER


def _provider_context_from_data(
    paths: CodexPaths,
    data: dict[str, Any],
    text: str,
    *,
    explicit: str = "",
) -> ProviderContext:
    warnings: list[str] = []
    legacy_profile = _legacy_profile_detected(text)
    if legacy_profile:
        warnings.append("Warning: legacy profile config detected; this toolkit reads it but does not modify it.")

    model_provider = explicit or _nonempty_str(data.get("model_provider")) or _provider_from_text(text)
    if not model_provider:
        if _auth_has_chatgpt_tokens(paths):
            model_provider = OPENAI_OFFICIAL_PROVIDER
        else:
            model_provider = _provider_from_declared_models(data) or _provider_from_recent_sessions(paths)[0] or DEFAULT_MODEL_PROVIDER

    ccswitch_provider_id, ccswitch_provider_name, ccswitch_settings, ccswitch_toml = _ccswitch_config_context(paths, warnings)
    ccswitch_data = ccswitch_toml if ccswitch_toml else {}
    effective_data = ccswitch_data if ccswitch_data else data
    effective_provider = _nonempty_str(effective_data.get("model_provider")) or model_provider
    if ccswitch_data and effective_provider == model_provider:
        data = effective_data
    provider_cfg = _provider_config(effective_data, effective_provider)

    model = _nonempty_str(effective_data.get("model") or data.get("model"))
    provider_name = _nonempty_str(provider_cfg.get("name")) or ccswitch_provider_name
    base_url = _nonempty_str(provider_cfg.get("base_url"))
    base_url_host = _host_from_url(base_url)
    wire_api = _nonempty_str(provider_cfg.get("wire_api"))
    requires_openai_auth = provider_cfg.get("requires_openai_auth") is True
    is_openai_official = effective_provider == OPENAI_OFFICIAL_PROVIDER or (
        not explicit and effective_provider == OPENAI_OFFICIAL_PROVIDER and _auth_has_chatgpt_tokens(paths)
    )
    is_ccswitch = bool(ccswitch_provider_id and ccswitch_data)
    api_format = ""
    meta = ccswitch_settings.get("meta")
    if isinstance(meta, dict):
        api_format = _nonempty_str(meta.get("apiFormat"))
    fingerprint = _context_fingerprint(
        effective_provider,
        provider_name=provider_name,
        base_url_host=base_url_host,
        is_openai_official=is_openai_official,
        is_ccswitch=is_ccswitch,
        ccswitch_provider_id=ccswitch_provider_id,
    )
    return ProviderContext(
        model_provider=effective_provider,
        model=model,
        provider_key=effective_provider,
        provider_name=provider_name,
        base_url=base_url,
        base_url_host=base_url_host,
        wire_api=wire_api,
        requires_openai_auth=requires_openai_auth,
        is_openai_official=is_openai_official,
        is_ccswitch=is_ccswitch,
        ccswitch_provider_id=ccswitch_provider_id,
        ccswitch_provider_name=ccswitch_provider_name,
        ccswitch_api_format=api_format,
        is_local_route=_is_local_host(base_url_host),
        legacy_profile_detected=legacy_profile,
        warnings=warnings,
        fingerprint=fingerprint,
    )


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


def detect_session_provider(paths: CodexPaths, session_id: str) -> str:
    """Return the model_provider stored in a specific session file, if present."""
    from ..stores.session_files import find_session_file, read_session_payload

    session_file = find_session_file(paths, session_id)
    if session_file is None:
        return ""
    try:
        payload = read_session_payload(session_file)
    except ToolkitError:
        return ""
    return _nonempty_str(payload.get("model_provider"))


def session_provider_fingerprint(payload: dict[str, Any]) -> str:
    for key in ("target_provider_fingerprint", "provider_fingerprint", "original_provider_fingerprint"):
        value = _nonempty_str(payload.get(key))
        if value:
            return value
    provider = _nonempty_str(payload.get("model_provider"))
    if provider == OPENAI_OFFICIAL_PROVIDER:
        return OPENAI_OFFICIAL_PROVIDER
    label = _nonempty_str(payload.get("provider_context_label"))
    host = _nonempty_str(payload.get("provider_base_url_host"))
    if provider == "custom" and (label or host):
        return f"custom:{label or 'unknown'}:{host or 'unknown'}"
    return provider


def session_matches_provider(payload: dict[str, Any], provider: str, fingerprint: str = "") -> bool:
    if _nonempty_str(payload.get("model_provider")) != provider:
        return False
    session_fingerprint = session_provider_fingerprint(payload)
    return not fingerprint or not session_fingerprint or session_fingerprint == fingerprint


def detect_provider_context(paths: CodexPaths, explicit: str = "") -> ProviderContext:
    config_file = paths.config_file
    if not config_file.exists():
        if explicit:
            return ProviderContext(
                model_provider=explicit,
                provider_key=explicit,
                fingerprint=explicit,
                is_openai_official=explicit == OPENAI_OFFICIAL_PROVIDER,
            )
        raise ToolkitError(f"Missing config file: {config_file}")

    data = _load_toml_data(config_file)
    text = config_file.read_text(encoding="utf-8")
    return _provider_context_from_data(paths, data, text, explicit=explicit)


def detect_provider(paths: CodexPaths, explicit: str = "") -> str:
    return detect_provider_context(paths, explicit=explicit).model_provider
