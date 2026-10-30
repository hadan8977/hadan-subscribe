# Quant 管理 MCP（用户名单）

更新时间：2026-10-30。新增独立的本地管理 MCP，用于手动添加和管理订阅用户、登录白名单、邮件/Agent 开关、到期时间和邮件屏蔽。

## 运行方式

- systemd：`hadan-mcp.service`
- 监听：`127.0.0.1:8098/mcp`（仅本机，不经过 Caddy，不暴露公网）
- 用户：`hadan`，与 web/main/source 使用同一数据库，但只开放下面列出的管理工具
- 代码：`src/hadan/mcp_server.py`
- 可选：在 Claude 全局配置 `~/.claude.json` 中加入：
  ```json
  "hadan": {"type": "http", "url": "http://127.0.0.1:8098/mcp"}
  ```
  新开的 Claude 会话会加载 `mcp__hadan__*` 工具；已运行的会话需要重启。

## 工具

只读：

| 工具 | 用途 |
| --- | --- |
| `list_members(query, status, expiring_within_days)` | 列出用户/订阅、到期时间、邮件/Agent 开关、是否在 DB 白名单 |
| `get_member(email)` | 单个用户的订阅、白名单、屏蔽、最近投递和 Agent 绑定 |
| `list_deliveries(email, limit)` | 最近投递记录 |
| `list_suppressions()` | 本地退信/投诉/手动屏蔽列表 |
| `list_allowlist()` | 数据库登录白名单 |

写入（全部需要 `confirm=true`；不传时只返回 dry-run 预览，不写库）：

| 工具 | 用途 |
| --- | --- |
| `add_member(email, days=30, until=None, product_id="quantgt", email_consent=False, note=None)` | 创建用户、订阅和登录白名单；`email_consent` 默认 false，只允许登录，不发内容邮件 |
| `extend_member(email, days=30, until=None)` | 从“现在/原到期时间中较晚者”起延长 |
| `set_expiry(email, until)` | 设置绝对到期时间，必须带时区 |
| `set_email_enabled(email, enabled)` | 开关内容邮件 |
| `set_agent_enabled(email, enabled)` | 开关 Agent 通道 |
| `remove_member(email)` | 软移除：结束订阅、关闭邮件/Agent、撤销会话和绑定、删登录白名单；保留用户和审计历史 |
| `add_allowlist(email, note)` / `remove_allowlist(email)` | 只管理登录白名单，不创建订阅 |
| `suppress_email(email, reason)` / `unsuppress_email(email)` | 管理本地屏蔽表 |

## 登录白名单

应用原先只读取环境变量 `HADAN_ALLOWED_EMAILS`。现在登录检查为：

1. `HADAN_ALLOWED_EMAILS` 为空：公开注册；
2. 邮箱在 `HADAN_ALLOWED_EMAILS` 中：允许；
3. 邮箱在数据库 `access_allowlist` 表中：允许；
4. 否则拒绝（“当前仅向受邀账号开放”）。

`add_member` 和 CLI `grant` 会写入 `access_allowlist`；`remove_member` 会删除。环境变量白名单是静态的，MCP 的 `remove_allowlist` 不会修改它；如果邮箱仍在环境变量中，删除 DB 白名单后仍可登录。

## 安全边界

- MCP 只绑定 loopback；不要把它加到公网 Caddy 路由。
- 所有写入都写 `audit(actor="mcp")`；`confirm` 是防止误操作的唯一门槛，调用前先用 `list_members/get_member` 确认对象。
- `remove_member` 不删除用户/订阅行，避免破坏历史投递和审计；如需彻底删除，走数据库备份/人工流程。
- MCP 进程不读取 SMTP 凭证（`Settings.smtp_required=false`），不具备发信能力；发信仍由 main worker 执行。
