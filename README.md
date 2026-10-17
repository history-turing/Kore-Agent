# KoreAgent 

KoreAgent 是一套从零实现的本地 AI Agent Harness。它提供 Electron 桌面工作区、Python Host、JSON-RPC 通信、长期运行管理、审批、自动化、交付物、记忆、扩展能力，以及 macOS Computer Helper。

## 功能

- 对话与多步工具执行，支持流式回答、推理片段、工具状态、普通模式和无副作用计划模式
- 每个模型步骤的上下文预算检查、长工具结果压缩、滚动摘要，以及核心记忆/任务/Skill 上下文注入
- Run 生命周期、检查点、中断、恢复、用量统计、证据存档与完整 Trace；可按需查看持久化的模型输入和工具声明
- 敏感工具操作审批，电脑操作提供独立置顶审批窗口
- 一次性、间隔与 Cron 自动化
- 文件与 URL 交付物发布、预览和下载
- 长期记忆、核心记忆、运行后反思、容量维护、跨会话消息搜索，以及带步骤的持续任务
- Skill 安装、启停、删除、扩展预览导入和“重复成功任务 → 待审核 Skill 候选”学习闭环
- MCP Server 配置、启停、环境变量白名单、工作区约束和 macOS Seatbelt 策略；不支持隔离的平台默认拒绝启动需沙箱的进程
- OpenAI、DeepSeek、Qwen 和 Anthropic 模型配置
- macOS 应用观察、截图、可访问性元素树、元素引用点击、输入、按键、滚动、应用/窗口聚焦
- Computer Run 独占租约、Session 校验与过期 Observation 拒绝机制

Host 暴露 `GET /health`、`WS /rpc`、截图读取和 artifact 下载接口。业务操作均通过 JSON-RPC 完成。

## Windows 开发启动

需要 Python 3.11+、Node.js 20+ 和 npm。在 PowerShell 中执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\start.ps1
```

也可以分别启动：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m kore_next.server

cd ..\desktop
npm install
npm run electron:dev
```

首次运行在 `backend/.koreagent-next` 创建数据库、设置、记忆、技能和交付物目录。

如果 `backend/.env` 不存在，启动脚本会从 `.env.example` 创建一个空白文件，不会覆盖已有配置。Qwen 可在应用“设置”页面直接配置，也可以填写：

```dotenv
KORENEXT_PROVIDER=qwen
KORENEXT_MODEL=qwen-plus
KORENEXT_API_KEY=sk-你的密钥
KORENEXT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

## macOS Computer Helper

```bash
cd native/macos-computer-helper
swift build -c release
swift test
```

启用前设置 `KORENEXT_COMPUTER_ENABLED=true`，并在系统设置中授予辅助功能与屏幕录制权限。

Computer Helper 当前仅在 macOS 上可用。Windows 没有可靠工作区沙箱时，Shell 和默认 MCP 配置会拒绝启动；如确需运行 MCP，须明确将文件系统设为 `host`、网络设为 `allowed`，每次工具调用仍需审批。宿主机模式不提供隔离，切勿运行不可信 Server。

搜索默认使用 DuckDuckGo；设置 `KORENEXT_TAVILY_API_KEY` 后，`KORENEXT_SEARCH_PROVIDER=auto` 优先使用 Tavily，遇到可恢复错误时回退。可显式选择 `tavily` 或 `duckduckgo`。HTTP 工具默认拒绝私网地址及重定向到私网的目标；只有明确设置 `KORENEXT_HTTP_ALLOW_PRIVATE_NETWORK=true` 才放行。用户级 Skill 默认保存在 `~/.koreagent-next/skills`，可通过 `KORENEXT_USER_SKILLS_DIR` 指定。

## 验证

```powershell
cd backend
python -m pytest
ruff check .
python -m compileall -q kore_next

cd ..\desktop
npm test
npm run typecheck
npm run build
```

SWE-bench 的推理侧驱动与评分流程见 [docs/swebench.md](docs/swebench.md)。

## 数据隔离

默认运行数据位于 `backend/.koreagent-next`，工作文件位于 `backend/workspace`。可通过 `KORENEXT_DATA_DIR` 和 `KORENEXT_WORKSPACE_DIR` 覆盖。数据库升级前自动生成 `backup-v*` 快照并记录版本；旧的字段匹配授权规则保留但停用，需要重新审批。所有配置均使用 `KORENEXT_` 前缀。

模型密钥不会进入数据库导出或日志。桌面端保存密钥时使用独立的 KoreAgent Next 密钥命名空间；`.env`、数据库、虚拟环境、依赖与构建目录均被 Git 忽略。
