# Agent 订阅流程：Prompt 与 Skill

更新时间：2026-10-30。以下为通用接入说明，套餐、价格和日期以站点实时返回为准。

## 给 Agent 的 Prompt

公开地址：<https://quant.example.com/agent/prompt>

保持一句核心指令 + 服务网址，不把套餐、价格、支付方式或操作细节写进提示词：

> Please help me subscribe to Quant and connect this agent to receive future updates.
>
> https://quant.example.com

中文页面复制中文版本，英文页面复制英文版本；其余流程由 Skill/网页处理。

## Skill

公开地址：<https://quant.example.com/skill.md>

标准 Skill frontmatter（`name`/`description`）+ 接入选择规则；Skill 只负责引导，OAuth、MCP Events、配对、内容读取和附件校验等细节都在 <https://quant.example.com/agent/instructions>。`/llms.txt` 同时列出 Subscribe、Agent Skill、Agent Prompt 和 Protocol。

## 接入方式

按运行时实际支持的能力选择，不强制所有 Agent 采用同一种方式：

1. **MCP Events**：`quant.report.published`，服务端保存订阅并通过签名 webhook 推送；优先用于 ChatGPT Work/dots 等支持 MCP Events 的平台。
2. **MCP Tools**：同一 `/mcp` 端点提供按需读取和确认工具；没有后台定时能力。
3. **HTTPS Polling**：配对后由 Agent 安全保存凭据并自行定时读取；仅在运行时有安全凭据存储和持久调度时使用。
4. **Email**：独立交付通道，由 owner 在网页控制，不依赖 Agent 连接。

ChatGPT 首次连接通过 OAuth 完成：自定义 connector 指向 `https://quant.example.com/mcp`，浏览器内验证 owner 邮箱后自动完成首次授权，不需要再点同意。OAuth token 由平台保存，不进入对话上下文。详见 [MCP_CONNECTOR.md](MCP_CONNECTOR.md)。

## 当前套餐

Agent 必须调用 `GET /api/purchase-options` 或 MCP `get_subscription_status` 获取实时套餐；不要从文档硬编码价格、日期、支付方式或套餐 ID。站点返回的 `subscribe_url` 是 owner 手动购买时的备用入口。

## 语言规则

Agent 侧获取的 Quant 信息必须全英文，包括方案名、价格、日期、状态、错误、说明和摘要。

- 默认英文。
- 如果运行环境明确提供用户语言环境，可以只用该语言进行对话，但 Quant 的服务数据、价格、日期、状态和标识符保持英文。
- 没有语言环境时直接使用英文。

以上规则写入 `/skill.md` 和 `/agent/instructions`；`/agent/prompt` 只保留一句英文起始指令和网址，不承载完整规则。

## 交互逻辑

### OAuth / MCP 客户端（ChatGPT、Grok、Cursor 等）

1. 添加远程 MCP connector，URL 为 `https://quant.example.com/mcp`；让平台走 OAuth discovery + DCR，不手填 OIDC/Authorize/Token 地址。
2. 浏览器内验证 owner 邮箱。首次连接会自动完成授权，不需要再点同意；替换现有连接时才会再次确认。OAuth token 由平台保存，不进入对话上下文。
3. 若平台显示 `no_auth_link` 或无法弹授权卡片，不要声称已连接；立即回退到站点配对。首次连接在 owner 验证邮箱后自动完成，不需要再点配对授权。
4. 调 `get_subscription_status`；它返回实时套餐、可用支付方式、`terms_version`、`terms_url` 和 `subscribe_url`。Agent 在对话里说明套餐、价格、支付方式和条款链接，请 owner 明确确认。
5. Agent 调 `prepare_checkout`，传入 `plan_id`、`method`、`accept_terms=true`、`terms_version`（HTTP 客户端用 `POST /api/agent/checkout` 传同样 JSON）。服务端固定金额，不接受客户端金额或任意套餐。返回 PNG 二维码后直接把图片发给 owner；不要打开浏览器或云电脑，也不需要 owner 再去网页点授权按钮。客户端无法显示图片时用 `get_checkout_qr` / `GET /api/agent/checkout/qr` 下载后作为附件发送。
6. owner 扫码付款后，Agent 用 `get_checkout_status` 查询，`paid=true` 才算完成；轮询间隔不小于 5 秒。付款后确认订阅状态和 Agent delivery，再调 `events/subscribe`（需要 active subscription + agent delivery）。网页 `subscribe_url` 只作为备用手动流程。
7. 不支持 MCP Events 时，按 `get_scan_schedule` 的 `checks_new_york` 在每次扫描后的检查窗口内查 `list_events()`；每个扫描最多 3 次（扫描后 10/20/30 分钟），不要 24 小时每 10 分钟轮询。若运行时只能固定间隔执行，必须用 `check_window_start_new_york`/`check_window_end_new_york` 过滤，窗口外直接跳过；空列表不通知。

### 站点配对（HTTPS API 客户端）

1. `POST /api/agent/pair`，保存 `pair_id`/`device_secret`，让 owner 打开返回的确认链接；不需要手动输入配对码。
2. 轮询配对状态，通过后执行 `/api/agent/test`、下载测试文件、`/api/agent/test/complete`。
3. 读取 `/api/events?pending=true`，保存正文和附件、校验 SHA256、再 `/ack`。
4. 只有 `/api/agent/status` 返回 `ready=true` 才确认连接成功。
5. Agent 不执行付款、兑换或订阅修改。owner 在对话里确认套餐、价格、支付方式和条款后，Agent 才可创建该次订单并把二维码发给 owner；付款仍由 owner 本人扫码完成。
