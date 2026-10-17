# 用 SWE-bench 评测 KoreAgent

SWE-bench 分两步：先生成 patch，再独立评分。KoreAgent 负责第一步，官方 harness 负责第二步。两者之间只通过 `predictions.json` 交接。

## 前置条件

- Docker Desktop 或 Linux Docker，能运行 `x86_64` Linux 容器
- Python 3.11+
- 可用的模型 API Key
- 官方镜像每个约 1–2GB，完整跑 Verified 建议预留 100GB 以上磁盘

模型配置优先使用桌面端已保存的 provider / model / 密钥；没有保存配置时再回退到 `.env`。也可以用 `--provider`、`--model`、`--base-url` 显式覆盖。密钥只从 DPAPI / Keychain 或环境变量读取，不写入 `predictions.json`。

## 运行

```powershell
cd backend
.\.venv\Scripts\python.exe -m kore_next.bench.swebench_driver --limit 10
```

常用参数：

```text
--dataset SWE-bench/SWE-bench_Verified   数据集，默认 Verified
--split test
--instance-ids astropy__astropy-12907    只跑指定题目
--limit 10                               未指定 instance 时取前 N 条
--model gpt-4.1-mini
--max-steps 40                           单题模型步数预算
--timeout 1800                           单题墙钟超时（秒）
--run-dir <path>                         输出目录
--keep-workspaces / --keep-containers    保留现场用于排查
--dry-run                                只加载题目、打印镜像，不启动容器
```

## 产物

```text
<run-dir>/
  predictions.json             官方 harness 的输入
  summary.json                 每题状态、耗时、用量、patch 大小
  run.json                     本次运行配置
  patches/<instance>.patch     原始 diff
  logs/<instance>.events.jsonl 工具调用与运行事件
  workspaces/<instance>/       题目工作区（默认跑完后删除）
```

## 分批与断点续跑

不需要一次跑完。每题的 patch、事件日志和 `predictions.json` 都会即时落盘。

```powershell
# 第一批：第 0-99 条
python -m kore_next.bench.swebench_driver --offset 0 --limit 100 --run-dir .koreagent-next/bench/verified

# 第二批：第 100-199 条
python -m kore_next.bench.swebench_driver --offset 100 --limit 100 --run-dir .koreagent-next/bench/verified

# 补跑：在候选集合里跳过已有非空 patch 的题目
python -m kore_next.bench.swebench_driver --limit 200 --run-dir .koreagent-next/bench/verified --resume
```

注意 `--limit` / `--offset` 决定本次的候选集合，`--resume` 只在这个集合内跳过已完成项。空 patch 或失败的题目会重试。`predictions.json` 和 `summary.json` 会合并已有结果，不会覆盖其他题目的记录。

官方评分也支持只评子集：

```powershell
swebench eval verified -p <run-dir>\predictions.json -i <instance_id> -i <instance_id> --run-id batch-1
```

`predictions.json` 不必包含全部题目。注意官方 harness 会按 `(run_id, instance_id)` 缓存，patch 变化后要换一个新的 `run_id`。

## 评分

```powershell
pip install swebench
swebench eval verified -p <run-dir>\predictions.json --run-id kore-001 -j 2
```

结果写入 `logs/evaluation/<run-id>/results.json`。不要复用同一个 `run-id` 评测不同的 patch，harness 会命中缓存。

如果使用较新的 v5 CLI，可以显式指定报告目录：

```powershell
swebench eval verified -p <run-dir>\predictions.json --run-id kore-001 -j 2 --report-dir <run-dir>\eval
```

## 实现说明

- driver 用数据集里的 `image` 字段启动官方镜像，把 `/testbed` 绑定到宿主工作区。
- 文件工具直接读写宿主工作区；`shell` 工具通过 `docker exec` 在容器内执行，并自动激活 `testbed` conda 环境。
- 评测运行使用 `KORENEXT_AUTO_APPROVE` 等价的内部配置，跳过人工审批；显式 deny 规则仍然生效。
- patch 通过 `git add -A` + `git diff --cached <base_commit>` 生成，已过滤缓存和构建产物。
- 官方镜像的 HEAD 不一定是题目的 `base_commit`，driver 会在启动 Agent 前显式 checkout。
- 评测默认 `--network none` 且禁用 `web_search` / `http_request`，避免 Agent 搜到原 PR；调试时可加 `--container-network bridge`。
- 容器是隔离边界；不要把这个模式用于不受信任的本地任务。

### Docker / 网络

如果 Windows 上的 Docker Desktop CLI 指向未启动的引擎、但 WSL 里有可用的 daemon，driver 会自动尝试 `wsl.exe -e docker`。也可以用 `KORENEXT_DOCKER_COMMAND` 显式指定。

如果 daemon 无法访问 Docker Hub（常见于只给 shell 配了代理），可以先在能联网的环境拉取镜像再 `docker load`：

```bash
crane pull --format=legacy --platform linux/amd64 <image> image.tar
docker load -i image.tar
```

然后加 `--no-pull` 运行 driver。Windows + WSL 的 bind mount 在首次 `docker cp` 时较慢，批量评测建议放在 Linux 主机上。

## 限制

- 分数是 “KoreAgent scaffold + 模型” 的组合分，报告时需要写清模型、步数和超时。
- 只覆盖仓库内的代码修改；容器里临时安装的依赖不会进入 patch。
- 官方数据集的题目来自 12 个 Python 仓库，不覆盖 Electron、桌面 UI 和 macOS native 能力。
- 如果要评测 KoreAgent 自己的 issue，需要另外构造 `FAIL_TO_PASS` / `PASS_TO_PASS` 题库。

## 已验证

`astropy__astropy-12907` 已完成一次离线端到端验证：容器使用 `--network none`，联网工具关闭；KoreAgent 在容器内定位并修改 `astropy/modeling/separable.py`，driver 产出 504B patch，官方 harness 判定 `resolved 1/1`。这是一次链路烟测，不代表整体分数。

对应的预测、patch 和评分报告位于 `backend/.koreagent-next/bench/smoke-offline/`。

## 初步样本（非随机，仅供趋势参考）

2026-10-17 用 `deepseek/deepseek-flash` 跑了一组跨仓库的 Verified 样本（每个仓库取第一条，离线、禁用联网工具）：

| instance | 结果 | 步数上限 |
|---|---|---|
| astropy__astropy-12907 | resolved | 20 |
| pytest-dev__pytest-10051 | resolved | 20 |
| psf__requests-1142 | resolved | 40 |
| pallets__flask-5014 | resolved | 40 |
| sympy__sympy-11618 | unresolved | 40 |

汇总：**4/5 resolved（80%）**，总计约 1.0M tokens。报告在 `backend/.koreagent-next/bench/sample-results/eval/`，合并后的预测在 `backend/.koreagent-next/bench/sample-results/predictions.json`。

失败案例：sympy-11618 的 Agent 选择了对不同维度抛 `ValueError`，而题目期望把缺失维度补 0 后计算 `sqrt(5)`。这说明当前 scaffold 能定位到正确代码，但语义判断仍会出错。

这 5 条是“每个仓库的第一条”，存在明显选择偏差，不能外推成 Verified 500 的全量分数。要判断 harness 的真实水平，需要跑随机分层样本（例如 30–100 条）并统一步数预算。
