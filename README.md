# KoreAgent Next

KoreAgent Next 是一套本地 AI Agent Harness，包含 Electron 桌面工作区、Python Host、JSON-RPC 通信、运行与审批管理、长期记忆、扩展系统，以及 macOS Computer Helper。

## 功能

- 多步工具执行，支持流式回答、推理片段、普通模式和无副作用计划模式
- 上下文预算、工具结果压缩、滚动摘要，以及记忆、任务和 Skill 上下文注入
- Run 生命周期、检查点、中断恢复、用量统计、证据存档与完整 Trace
- 敏感操作审批，以及独立的桌面置顶审批窗口
- 一次性、间隔与 Cron 自动化
- 文件与 URL 交付物发布、预览和下载
- 长期记忆、运行后反思、容量维护、跨会话搜索和持续任务
- Skill 安装与学习闭环、MCP Server 配置与沙箱策略
- OpenAI、DeepSeek、Qwen 和 Anthropic 模型配置
- macOS 应用观察、截图、可访问性元素树和电脑操作

Host 暴露 `GET /health`、`WS /rpc`、截图读取和 artifact 下载接口；业务操作统一通过 JSON-RPC 完成。

## 项目结构

```text
backend/kore_next/
  application.py      组合根与稳定的 JSON-RPC facade
  model/              模型客户端与各协议适配器
  tools/              工具声明、执行器与 Shell 参数构造
  services/           对话、运行、审批、自动化、扩展等业务服务
  context.py          上下文预算与压缩
  evidence.py         证据存档与检索
  memory.py           长期记忆
  runtime_policy.py   运行预算与防循环策略

desktop/src/
  App.tsx             应用外壳、导航与命令面板
  views/              按页面拆分的业务视图
  ui.tsx              共用页面组件与状态展示
  electron/           主进程与 preload

native/macos-computer-helper/
  Sources/            Computer Helper 与通信协议
```

更完整的模块关系和调用链见 [docs/architecture.md](docs/architecture.md)。

## Windows 开发启动

需要 Python 3.12+、Node.js 20+ 和 npm。在 PowerShell 中执行：

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

首次运行会在 `backend/.koreagent-next` 创建数据库、设置、记忆、技能和交付物目录。如果 `backend/.env` 不存在，启动脚本会从 `.env.example` 创建一个空白文件，不会覆盖已有配置。

Qwen 可在应用“设置”页面直接配置，也可以填写：

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

## 网络与数据

搜索默认使用 DuckDuckGo；设置 `KORENEXT_TAVILY_API_KEY` 后，`KORENEXT_SEARCH_PROVIDER=auto` 会优先使用 Tavily，遇到可恢复错误时回退。HTTP 工具默认拒绝私网地址及重定向到私网的目标，只有明确设置 `KORENEXT_HTTP_ALLOW_PRIVATE_NETWORK=true` 才放行。

默认运行数据位于 `backend/.koreagent-next`，工作文件位于 `backend/workspace`，可通过 `KORENEXT_DATA_DIR` 和 `KORENEXT_WORKSPACE_DIR` 覆盖。数据库升级前会自动生成 `backup-v*` 快照并记录版本。所有配置均使用 `KORENEXT_` 前缀。

模型密钥不会进入数据库导出或日志。桌面端保存密钥时使用独立的 KoreAgent Next 密钥命名空间；`.env`、数据库、虚拟环境、依赖与构建目录均被 Git 忽略。

## 验证

```powershell
cd backend
python -m pytest
ruff check .
ruff format --check .
python -m compileall -q kore_next

cd ..\desktop
npm test
npm run typecheck
npm run format:check
npm run build
```

SWE-bench 的推理侧驱动与评分流程见 [docs/swebench.md](docs/swebench.md)。

## 关于模块拆分

按职责拆分模块不会改变 Agent 热路径：每次运行仍然只包含上下文准备、模型请求、工具执行和持久化。额外的开销仅来自进程启动时多打开几个 Python 文件，以及前端静态导入的模块解析；Vite 会把这些模块合并进同一份生产包，不会产生额外的网络请求。

真正的性能风险来自重复序列化、无界查询和每步重复构造大对象，而不是文件数量。本项目的性能相关逻辑集中在 `context.py`、`runtime_policy.py`、`evidence.py` 和 `services/runs.py`，测试会对压缩、预算、证据检索和 Run 恢复进行覆盖。
