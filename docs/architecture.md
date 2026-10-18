# KoreAgent Next 后端架构

后端在 `application.py` 中保持稳定的 JSON-RPC 合约，并把具体行为放入 `model/`、`tools/` 和 `services/` 三个包。`Application` 只负责组装对象、持有共享运行状态和转发 RPC 方法。

## 模块地图

```text
kore_next/
  application.py          组合根与 JSON-RPC facade
  server.py               WebSocket / HTTP 入口
  rpc.py                  RpcFault 与参数校验
  context.py              上下文预算、工具结果压缩与摘要注入
  runtime_policy.py       运行预算、重复调用保护与终止原因
  evidence.py             工具证据存档、检索与旧格式标记
  checkpoint.py           Run 检查点
  context_snapshots.py    上下文压缩物化快照
  context_offload.py      大工具结果的内容寻址归档与有界读取
  resources.py            跨 Run 的文件租约、串行 Actor 与并发配额
  database.py             SQLite schema、升级与备份
  model/
    client.py             重试策略、凭据与协议分派
    types.py              ModelReply 与 usage 归一化
    presets.py            Provider 标签、官方地址与 API 风格
    openai_chat.py        OpenAI-compatible Chat Completions
    openai_responses.py   OpenAI Responses API
    anthropic_messages.py Anthropic Messages API
  tools/
    definitions.py        工具 schema
    catalog.py            能力分类、别名与有界检索
    projection.py         每个模型步骤的不可变工具可见性投影
    validation.py         工具参数 JSON Schema 校验
    errors.py             可回传模型的结构化工具错误
    executor.py           工具预检、审批、派发、文件、Shell 与网络调用
    shell.py              Shell argv 构造
  services/
    conversations.py      会话持久化与搜索
    runs.py               Run 记录、事件写入、恢复与 Trace
    runtime_events.py     Append-only 事件封套、序号与 digest 链
    runtime_projections.py 消息与终态投影
    run_engine.py         Agent 主循环
    run_postprocessing.py 上下文摘要、反思与计划提取
    model_settings.py     模型配置与后台角色
    approvals.py          人工审批与持久授权规则
    artifacts.py          文件与 URL 交付物
    automations.py        调度、occurrence 抢占与重启恢复
    skills.py             Skill 发现与生命周期
    mcp_settings.py       MCP 配置与运行时视图
    extensions.py         扩展导入与能力总览
    tool_router.py        服务型工具的路由
```

桌面端按页面拆分：

```text
desktop/src/
  App.tsx                 应用外壳、导航、命令面板与事件订阅
  ui.tsx                  共用页面组件、状态文案与格式化函数
  views/                  对话、运行、自动化、审批、交付物、电脑、记忆
  PreferencesView.tsx     模型、Skill 与 MCP 设置
  views/ExtensionManager.tsx  Skill 与 MCP 管理面板
  RunExplorer.tsx         Run Trace 与用量视图
```

## 主要调用链

```text
WebSocket RPC
  -> Application.methods()
  -> feature service

conversation.send
  -> ConversationService.add_message
  -> RunStore.create
  -> RunEngine.execute
  -> ContextManager.prepare / prepare_step
  -> ModelClient.complete
  -> protocol adapter
  -> ToolExecutor.execute
  -> ServiceToolRouter.call
  -> RunStore.event（events + runtime_events 双写）
  -> CheckpointStore.save

automation tick
  -> AutomationService.loop
  -> AutomationService.claim_occurrence
  -> Application._execute_run
  -> RunEngine.execute
```

`Application` 持有共享运行状态（`running`、审批 waiter、广播函数和后台任务），再通过回调注入服务。服务和测试可以替换设置、模型客户端或运行执行函数，而不需要重建整个对象图。

## 边界约定

- `model/` 只负责协议请求、流式响应和 usage 归一化。
- `tools/` 只负责工具声明、参数校验和单次执行，不直接写业务表。
- `services/` 拥有各自的数据表和业务状态；跨服务调用通过构造参数暴露的明确依赖完成。
- 工具调用按 `preflight -> T1 dispatch -> execute -> T2 result` 排序；参数、可见性和人工审批全部通过后才写入 `dispatched`。
- `ToolProjection` 在每次模型请求前冻结当前可见工具集合；`tool_catalog` 的激活只影响下一个模型步骤。
- 新代码直接导入 `kore_next.model`、`kore_next.tools` 和 `kore_next.services`，不再保留额外的旧路径兼容层。
- 前后端共享的是 RPC 方法名和 JSON 字段，不共享 Python 或 TypeScript 对象。

## 性能边界

模块拆分只影响导入时的少量文件解析，不会给每次模型调用或工具调用增加序列化、网络或数据库开销。热路径的性能由以下边界决定：

- `ContextManager` 在进入模型请求前完成预算检查与压缩。
- `RunBudget` 在请求前后各检查一次，避免超限后继续调用。
- `EvidenceStore.search` 只遍历一次结果集，并在达到 limit 后停止。
- `RunStore.event` 在同一个短事务里写入兼容 Trace 和 `runtime_events`；事件序号按 Run 单调递增，并维护 `prev_digest → digest` 链。
- 恢复优先使用检查点中的阶段、待执行工具和 `tool_executions` 状态；存在已派发但未记录结果的工具时，要求人工核对，不自动重放。
- 前端页面在构建时静态打包，生产环境不会因文件拆分产生额外请求。

## 运行事实与投影

`runtime_events` 是新增的运行事实日志，记录 `turn_id`、`run_id`、`invocation_id`、`event_seq` 和 digest。现有 `events` 表暂时保留用于兼容 Trace；UI 增量读取和 Trace 已经优先使用事件日志。

`runtime_projections.py` 提供纯函数式的消息与终态投影。投影只读取事件，不修改事件历史；工具执行状态由 `tool_executions` 表持久化，派发和结果分别提交。

检查点现在同时记录 `through_event_seq` 和前缀 digest。恢复时先校验事件前缀，再只应用水位线之后的模型可见事件；恢复出的新 Run 拥有独立事件序列，继承的是物化状态而不是旧 Run 的序号。检查点与日志不一致时会拒绝自动恢复。

工具可见性按模型步骤记录在 `tool_activation_steps` 中，避免同一批次里 `tool_catalog` 激活后立即调用尚未看到 Schema 的工具。参数在派发前按 JSON Schema 校验；校验失败或人工拒绝都不会写入 `dispatched`，`tool_executions.error_code` 记录稳定错误码。

`tool_catalog` 不带参数时返回能力分组和示例工具，不激活任何 Schema；带 `query`、`category` 或 `names` 时按名称、别名、标签和描述的加权评分返回有界结果。MCP 工具按 `mcp:<server>` 分组，宽泛的类别查询会激活该组内有界数量的工具，具体工具名查询只激活排名靠前的少量工具。

同一 Assistant Step 的多个只读工具会并发派发，结果按模型原始顺序组装回上下文；只要批次中出现写操作、人工审批、MCP 或桌面操作，整批按模型顺序串行执行。`ResourceAuthority` 在 Application 级别共享，用 canonical path 读写租约、Shell 工作区独占锁、桌面串行 Actor、MCP Server 与网络并发配额约束跨 Run 的真实资源竞争。

并发批次只在结束后提交一次完整 checkpoint；批次执行期间，每个调用的 T1/T2 状态和 `tool_result` 事件已经独立落盘。恢复时根据 `tool_executions` 的 `completed_call_ids` 过滤已完成调用，而不是假设调用按索引连续完成；已经投影到上下文的 tool messages 会按模型原始请求顺序重排。

上下文压缩会把摘要作为 `context_snapshots` 物化快照持久化，记录覆盖的消息数量、源消息 digest 和摘要 hash；原始消息历史不被改写。超过阈值（默认 64KB）的工具结果先写入内容寻址归档，校验写入后模型只看到包含 `sha256`、大小和预览的占位符，需要细节时通过 `tool_result_inspect` / `tool_result_read` 有界读取。

前端和 Trace 通过 `run.events(after_seq, limit)` 增量读取 `runtime_events`；`trace.get` 也优先使用事件日志，旧 `events` 表只在历史 Run 没有迁移数据时回退。

## 未知工具结果与后台维护

如果中断发生在工具派发和结果提交之间，Run 会进入 `tool_unknown`，不会自动重放。用户可以通过 `run.resolve_tool` 选择 `retry`（明确确认后重新派发）或 `abandon`（写入占位 tool result、修复会话结构并终止该 Run）。桌面端会在会话任务面板和运行详情页提供对应操作。

消息全文索引只覆盖 `user` / `assistant` 消息，避免工具大结果同步触发 FTS 写入。证据索引改为运行后异步写入，并由 `EvidenceStore.backfill_from_runtime_events` 在启动时从事件日志补建。`MaintenanceService` 按保留策略清理旧兼容 Trace、证据索引和无人引用的归档文件；`runtime_events`、检查点和会话消息不会被自动删除。
