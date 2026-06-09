---
domain: desktop-visibility
current_status: stable
last_event_id: EVT-2026-06-09-1755-context-baseline
last_event_time: 2026-06-09T17:55:53+08:00
last_recorded_at: 2026-06-09T17:55:53+08:00
owners:
  - 主 agent
active_branches:
  - codex/new-arch-migration
---

# desktop-visibility

## Current State

- Desktop 可见性不是单一文件决定的，至少涉及：
  - `~/.codex/session_index.jsonl`
  - 最新 `~/.codex/state_*.sqlite` 的 `threads` 等表
  - `~/.codex/.codex-global-state.json`
- `services/repair.py` 负责扫描会话并编排修复；实际 index、SQLite、workspace roots、thread hints 和 permissions 写入集中在 stores。
- 默认 repair 只注册目标 provider 且 Desktop 兼容的 active 会话，不会把其他 provider 全部 retag。
- `--retag-provider` 或 `switch-provider` 才允许原地改写 provider。
- clone 没有有效标题时，优先继承父线程标题，再使用会话首条用户消息或工作区信息生成预览。
- CLI 会话只有显式 `--include-cli` 时才转换为 Desktop 元数据。

## Active Work

- 暂无未完成实现。
- Desktop 新版本若增加状态字段，应优先扩展 `stores/desktop_state.py`，保持 service 只做编排。

## Blockers

- Codex Desktop 运行期间可能并发改写状态文件，真实修复前建议退出 Desktop。
- Desktop 侧边栏还可能受缓存和折叠工作区影响，写入后通常需要完整重启 Desktop 验证。

## Next Actions

- 修复前先 dry-run，确认目标 provider、扫描数量和待写入范围。
- 验收时同时检查 index、threads、global state 和 Desktop 侧边栏，不以文件存在作为唯一成功条件。

## Key Paths

- `src/ai_cli_kit/codex/services/repair.py`
- `src/ai_cli_kit/codex/stores/desktop_state.py`
- `src/ai_cli_kit/codex/stores/index.py`
- `src/ai_cli_kit/codex/stores/history.py`

## Recent Event Links

- [EVT-2026-06-09-1755-context-baseline](../../02_events/2026/06/EVT-2026-06-09-1755-context-baseline.md)
