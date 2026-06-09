---
domain: provider-switching
current_status: stable
last_event_id: EVT-2026-06-09-1755-context-baseline
last_event_time: 2026-06-09T17:55:53+08:00
last_recorded_at: 2026-06-09T17:55:53+08:00
owners:
  - 主 agent
active_branches:
  - codex/new-arch-migration
---

# provider-switching

## Current State

- `services/provider.py` 负责 provider 上下文解析，输出 `ProviderContext` 和稳定 fingerprint。
- 显式 `model_provider` 优先；OpenAI official 可由 `auth_mode = "chatgpt"` 与 token 结构辅助判断。
- 不能仅凭 `OPENAI_API_KEY` 或 `openai-bundled` 判断为 OpenAI official。
- CcSwitch 上下文来自 `~/.cc-switch/settings.json` 与 `cc-switch.db`，可识别 `custom` 指向的真实 provider。
- `switch-provider` 通过 `repair_desktop(..., retag_provider=True)` 原地切换 Desktop 兼容会话，并复用统一备份和可见性重建。
- `restore-backup` 在恢复前会为当前目标文件再建立 rollback 备份。

## Active Work

- 暂无未完成实现。
- 新 provider 接入应优先扩展 `ProviderContext`，不要在 clone、repair 或 TUI 中复制判断条件。

## Blockers

- CcSwitch 数据库字段和 Codex config 结构属于外部格式，升级后可能变化。
- official 与自定义 OpenAI-compatible API 可能共享认证环境变量，必须依赖配置上下文而非单一环境变量。

## Next Actions

- provider 误判时先运行 `debug-provider`，只记录脱敏后的 fingerprint、host 和 provider 名称。
- 新增识别规则时补充 `tests/test_core_workflows.py` 的 provider 测试。

## Key Paths

- `src/ai_cli_kit/codex/services/provider.py`
- `src/ai_cli_kit/codex/services/switching.py`
- `src/ai_cli_kit/codex/commands.py`

## Recent Event Links

- [EVT-2026-06-09-1755-context-baseline](../../02_events/2026/06/EVT-2026-06-09-1755-context-baseline.md)
