# CLAUDE.md — harness-lab

> 本文件给 Claude Code 读取，是项目的**长期规则**，每次会话都生效。
> 每天做什么、做到什么程度算完成，见 `plan.md`。对外展示的内容写在 `README.md`。

## 1. 项目是什么

**harness-lab**：用 Python 从零写一个 Coding Agent 的运行框架（harness），不依赖任何 Agent 框架，每个组件都可以通过配置开关。在 **SWE-bench Verified 的 50 题子集**上做以下实验：

1. 主配置和基线对比（mini-SWE-agent，可选再加 Codex CLI 的 `codex exec`），报告解决率、单任务成本和延迟；
2. **消融实验**：每次只改一个因素，看它对结果的影响，四个因素是上下文策略、动作空间、规划、上下文预算；
3. **安全评测**：用自建的提示注入测试集，对比权限层开和关时的攻击成功率（ASR）和任务完成率；
4. 以 FastAPI 服务 + Docker 镜像（`docker compose` 一键启动）的形式交付，CI 里跑基于回放的冒烟测试。

**这个项目真正要交付的是"可信的实验结论"，不是功能数量。** 遇到取舍时，优先保证实验可控、可复现、数字真实。

## 2. 不可违反的规则

1. **Agent 核心不用任何 Agent 框架。** `src/harness/` 里禁止引入 LangChain、LangGraph、LlamaIndex、CrewAI、AutoGen、OpenAI Agents SDK、Claude Agent SDK 等。LLM 调用只通过 **LiteLLM**，它只负责传输请求、统一各家接口和计算成本。
2. **模型给出的命令只能在沙箱容器里执行。** 宿主机上禁止用 `subprocess` 运行模型产生的任何字符串。
3. **禁止编造实验数字。** README 和 `docs/` 里的所有指标都必须由 `eval/report/make_tables.py` 从 `results/` 自动生成。没有数据的地方保留 `{{TBD}}`，不许估算，也不许"示意"。
4. **对比实验之前先冻结版本。** 跑主实验前打 git tag（例如 `v0.1-frozen`），所有对比配置都在同一个 tag 上跑。之后如果修了 harness 的 bug，受影响的配置必须全部重跑，并在 `results/CHANGELOG.md` 里记录原因。
5. **控制预算。** 必须遵守 `configs/*.yaml` 中的 `max_cost_per_task_usd` 和 `.env` 里的 `MAX_TOTAL_COST_USD`。累计花费超过总预算的 80% 就停下来，向用户确认后再继续。**启动任何超过 10 个任务的批量运行之前，先打印预计花费并等用户确认。**
6. **不提交密钥。** API Key 只放在 `.env`（已加入 gitignore），仓库里只放 `.env.example`。
7. **能省则省。** 不写计划之外的功能，不写 TUI，不做 Web 前端。可选任务只在 `plan.md` 标注的时间节点之后才做。
8. **技术栈固定为 Python 3.12 + uv + Docker，不做替换。** 见 §3.1。

## 3. 技术栈

### 3.1 固定基座（不可替换）

| 项 | 规定 | 禁止 |
|---|---|---|
| 语言 | **Python 3.12**，由 `.python-version` 和 `pyproject.toml` 的 `requires-python` 锁定 | 其他语言写核心代码；使用 3.13+ 才有的语法或特性 |
| 包与环境管理 | **只用 `uv`**：`uv sync` 建环境，`uv add` / `uv add --dev` 加依赖，`uv run` 执行一切命令（含脚本、测试、lint）。依赖只写在 `pyproject.toml`，`uv.lock` **必须提交** | `pip install`、`requirements.txt`、poetry、conda、pipenv；直接调用 `.venv` 里的 python；手改 `uv.lock` |
| 容器 | **只用 Docker**：沙箱通过 Docker SDK for Python 创建容器；服务通过 `docker/Dockerfile` + `docker compose`（v2 插件语法）交付 | Podman、nerdctl、宿主机直接执行；`docker-compose`（v1 独立命令） |
| 服务镜像 | `docker/Dockerfile` 基于 `python:3.12-slim`，从官方 uv 镜像复制 `uv` 二进制，用 `uv sync --frozen --no-dev` 安装依赖 | 在镜像里用 pip 装依赖 |
| CI | GitHub Actions 用 `astral-sh/setup-uv` 安装 uv，之后与本地一样执行 `uv sync` + `uv run …` | 在 CI 里另写一套安装步骤 |

新增依赖前先确认它在 Python 3.12 下可用，并用 `uv add` 写入；不要自己编辑版本号绕过锁文件。

### 3.2 各层选型

| 层 | 选择 | 备注 |
|---|---|---|
| 语言与环境 | Python 3.12，`uv` | 见 §3.1 |
| LLM 调用 | `litellm`（异步 `acompletion`） | 模型名从 `.env` 读取；成本用自有的固定标准价（高峰价）公式按记录的 token 数计算（`llm/cost.py`），**不调用 `litellm.completion_cost`**，因为它按当前 UTC 时间套用错峰价，同一次调用在不同时间成本不同 |
| 沙箱 | Docker SDK for Python（`docker`） | SWE-bench 任务直接用官方的单题镜像 |
| 配置 | `pydantic` v2 + YAML | 所有可开关的组件都要能在配置里切换 |
| 服务 | FastAPI + uvicorn；SQLite 用 `aiosqlite` | 进度用 SSE 流式推送 |
| 评测 | `swebench`（官方判分工具）、`mini-swe-agent`（基线） | 具体命令先用 `--help` 或官方文档核实 |
| 追踪 | 自写 JSONL 追踪器；可选导出到 OpenTelemetry → Langfuse | 可选项放到第 5 天之后 |
| 质量 | `ruff`（lint + format）、`pytest`、`pytest-asyncio` | 提交前必须全部通过 |
| CI | GitHub Actions | 只用回放模型，不花 API 费用 |

## 4. 目录结构（照此创建，不要自己另起结构）

```
harness-lab/
├── CLAUDE.md  plan.md  README.md  .env.example
├── pyproject.toml  uv.lock  .python-version   # uv 管理；uv.lock 必须提交
├── configs/
│   ├── default.yaml            # 主配置
│   ├── ablations/              # 每个文件只改一个因素
│   ├── policy.yaml             # 权限层规则
│   └── subsets/{dev_5.txt, verified_50.txt}
├── src/harness/
│   ├── config.py               # pydantic 配置模型 + YAML 加载
│   ├── agent/{loop.py, state.py, prompts.py, limits.py}
│   ├── llm/{client.py, replay.py, cost.py, types.py}
│   ├── tools/{base.py, registry.py, bash.py, edit.py, search.py, todo.py, submit.py}
│   ├── sandbox/{base.py, docker_sandbox.py}
│   ├── context/{manager.py, tokens.py, strategies/{none.py, truncate.py, summarize.py, hybrid.py}}
│   ├── security/{policy.py, guard.py}
│   ├── tracing/{tracer.py, otel.py}
│   ├── service/{api.py, worker.py, store.py}
│   └── cli.py
├── eval/
│   ├── swebench/{select_subset.py, pull_images.py, run_agent.py, score.py}
│   ├── baselines/{mini_swe_agent.md, codex_exec.py}
│   ├── security/{cases/, build_cases.py, run_security.py}
│   └── report/{aggregate.py, failure_taxonomy.py, make_tables.py, plots.py}
├── docs/{notes.md, learning/}  # notes.md：外部接口核实记录；learning/：学习笔记
├── results/            # runs/ 目录 gitignore；summary/ 提交
├── tests/{unit/, e2e/, fixtures/toy_repo/, fixtures/replays/}
├── docker/{Dockerfile, docker-compose.yml}
└── .github/workflows/ci.yml
```

## 5. 核心设计

### 5.1 Agent 循环（`agent/loop.py`）

```python
async def run_task(task: Task, cfg: AgentConfig, sandbox: Sandbox, llm: LLMClient, tracer: Tracer) -> RunResult:
    state = AgentState.init(task, cfg)                      # system prompt + 工具定义 + 任务描述
    while not state.done:
        limits.check(state)                                 # 步数、单任务成本、墙钟时间 → 超限就以 FailReason 终止
        messages = await context_mgr.build(state)           # 可能触发压缩；压缩要记录 span
        resp = await llm.complete(messages, tools=registry.schemas(cfg.action_space))
        state.append_assistant(resp)
        for call in resp.tool_calls:
            decision = guard.check(call, state)             # allow / deny / ask（批量模式下 ask 自动当作 deny）
            result = await registry.run(call, sandbox) if decision.allowed else decision.as_tool_result()
            state.append_tool_result(call, result)          # 超长输出按规则截断
        if state.submitted: break
    return RunResult(patch=await sandbox.git_diff(), fail_reason=state.fail_reason, metrics=state.metrics)
```

- 调用失败时的重试放在 `llm/client.py` 里：指数退避并加随机抖动，**总共最多 3 次尝试**（首次加最多 2 次重试）。按（异常类型、状态码、响应形状）判断，可重试的有四类：**429**（响应带 `Retry-After` 时按它等待）、**5xx**、**客户端超时**（`Timeout`）、**不可用的 200**（响应体不是合法 JSON、缺少 `choices`，或 `finish_reason` 为 `insufficient_system_resource` / `aborted`，后者要丢弃半截内容）；4xx（含 400、401、404）不重试。每次重试都要记录次数和原因（`retry_reason`），重试用完仍失败就以 `env_error` 终止任务；重试的等待时间计入墙钟时间和延迟。
- LLM 调用参数：**当前只用非流式**，基本功能全部完成后再支持流式。每次调用显式传 `thinking`（来自配置），一次运行内不切换。输出上限 `max_tokens` 和超时 `timeout_s` **成对设置**，超时必须大于输出顶满上限所需的时间，这样失控的生成会先以 `finish_reason=length` 结束（不重试），而不是先被超时（会重试 3 次）：思考开 `max_tokens=65536`、`timeout_s=900`；思考关 `max_tokens=16384`、`timeout_s=300`。两者都是配置项，记录在 `run_start`（依据见 `docs/notes.md` §5）。
- `finish_reason=length` 的响应**不重试**，里面半截的工具调用**绝不执行**；给模型一条"输出被长度限制截断，请改小"的反馈，计入 `truncated_outputs`（不计入 `format_errors`）。连续 3 次截断就终止任务，记为 `format_error`，详情字段写 `output_truncated`。
- 失败的尝试（含重试）也要写一条 `llm_call` span（`outcome`、`retry_reason`、延迟，`usage` 为空）。`tokens_*` 和 `cost_per_task` **只统计成功的调用**；失败的尝试另报 `failed_attempts`、`retry_rate` 和 `wasted_prompt_tokens_est`（用 `litellm.token_counter` 估算被浪费的输入 token）。预算保护把这个估计也算进去，但不混进上面的指标。
- 模型返回的工具调用格式不对时，把错误信息作为工具结果回给模型，**计入 `format_errors`**。连续 3 次格式错误就终止任务。
- `FailReason` 的取值：`context_overflow | step_limit | cost_limit | time_limit | format_error | env_error | no_submit`。
- **补充说明（实现细节，2026-10-07 与代码对齐）**：
  - 没有收到任何响应的连接错误（DNS、拒绝连接等）按**客户端超时**一类重试。
  - `finish_reason=content_filter` 不重试，以 `env_error` 终止（详情写明是提供商过滤）。
  - LiteLLM 会把不认识的 `finish_reason`（`aborted`、`insufficient_system_resource`）悄悄改成 `stop`，原始值在 `choice.provider_specific_fields["native_finish_reason"]`，客户端必须读它。
  - 每个 `tool_call` 必须恰好有一条 tool 消息（DeepSeek 缺回复返回 400），所以被权限层拒绝、提交后多余的、格式错误的调用也要回复。被 `length` 截断的响应**不进历史**，只追加一条 user 反馈。
  - **`no_submit`**：只回文字、没有工具调用的回复**不算格式错误**，不计入 `format_errors`；给模型一条提示继续，连续 3 次纯文字回复就以 `no_submit` 终止（详情 `no_tool_call`）。任何带工具调用的回合（含格式错误的调用）都会重置这个计数；纯文字回合会打断连续格式错误的计数。

### 5.2 为了命中 KV Cache 的上下文规则（参考项目文档 `llm-prefill-and-agent-harness.md`）

1. **前缀稳定**：system prompt 和工具定义放在最前面，逐字不变；system prompt 里不放时间戳。
2. **只追加不修改**：历史消息一旦写入就不再改动。唯一例外是上下文策略主动压缩，而且每次压缩都要记录。
3. **确定性序列化**：JSON 一律 `sort_keys=True`，工具列表按名字排序。
4. **动态信息后置**：步数、剩余预算这类提示信息，放在最新一条 user 或 tool 消息的末尾。
5. 每次调用都记录 `cached_input_tokens`，按 `cached / input` 计算缓存命中率，这是一个必须报告的指标。

### 5.3 可开关组件（全部由配置驱动）

| 配置键 | 取值 | 说明 |
|---|---|---|
| `action_space` | `bash_only` \| `tools` | `bash_only`：只有 bash 和 submit。`tools`：bash、view、str_replace、search、todo、submit |
| `context.strategy` | `none` \| `truncate` \| `summarize` \| `hybrid` | 详见下方 |
| `context.budget_tokens` | 例如 `128000` / `32000` | 人为限制上下文预算，用来制造上下文压力（小预算下各策略的差异才能显现）。**只算发给模型的提示词（输入）**，不含输出上限 `max_tokens`，两者互不扣减 |
| `planning` | `on` \| `off` | `on`：启用 todo 工具，并在 prompt 里提示先规划再执行 |
| `guard.enabled` | `true` \| `false` | 权限层开关 |
| `tool_output.max_chars` | 例如 `8000` | 超过就截断，只保留开头和结尾 |

**上下文策略：**

- `none`：不做任何处理。超出预算就以 `context_overflow` 终止。
- `truncate`：按规则清理，把较早的工具**输出**替换成 `[output cleared: N chars, saved at <path>]`，保留调用记录。
- `summarize`：保留 system、任务描述和最近 K 轮，中间部分让 LLM 生成结构化摘要，内容包括已经尝试过什么、失败原因、当前假设、改过哪些文件。
- `hybrid`：先 `truncate`，仍然超预算再 `summarize`。
- 所有策略都要记录压缩前后的 token 数、压缩次数和压缩自身的成本。

**可恢复截断**：工具输出超长时，完整内容写到沙箱里的 `/tmp/harness/out_<n>.txt`，返回给模型的截断结果里注明这个路径，模型需要时可以再去读。

### 5.4 工具（`tools/`）

- 每个工具都有 `name`、`description`、JSON Schema 格式的参数定义，以及 `async run(args, sandbox) -> ToolResult(output, exit_code, truncated, meta, format_error)`。`format_error=True` 表示**调用本身**格式不对（非法 JSON、未知工具、缺参数、类型错），计入 `format_errors`；工具跑了但失败不算。
- `view` 和 `str_replace` 实现在同一个文件 `tools/edit.py` 里（`ViewTool`、`StrReplaceTool`）。
- `str_replace`：`old_str` 在文件里必须**恰好出现一次**，出现 0 次或多次都报错，并返回附近的上下文帮助模型定位。
- `bash`：有超时（默认 120 秒）；返回 exit code 和合并后的 stdout/stderr。
- `submit`：只打标记，把 `state.submitted` 设为 True；补丁在循环外用 `git diff` 生成。

### 5.5 沙箱（`sandbox/docker_sandbox.py`）

- SWE-bench 任务直接使用该题的官方镜像：代码在 `/testbed`，并且已经切到题目对应的基准提交。**镜像命名规则先查 `swebench` 源码或 mini-swe-agent 源码再用，不要猜。**
- 容器限制：`network_mode="none"`（默认断网）、内存和 CPU 上限、`pids_limit`、非特权模式、任务结束后必须删除容器（用 `try/finally`）。
- 对外接口：`exec(cmd, timeout) -> (exit_code, output)`、`read_file`、`write_file`、`git_diff()`。约定：退出码 124 表示超时（用容器内的 `timeout`，Docker SDK 的 `exec_run` 本身没有超时）；容器用 `init=True` 回收僵尸进程；`git_diff` 每次用 `git read-tree HEAD` 重建临时 index，所以包含新建文件、且不受 stat 缓存影响；`--repo` 本地仓库经 `load_repo` 复制进容器（固定文件修改时间和提交日期，保证回放可复现）。

### 5.6 权限层（`security/`）

- `policy.yaml` 定义三个权限级别：`read`（cat/ls/grep 等）、`write`（编辑工具以及限定路径内的写操作）、`exec`（运行测试或脚本）。另有 `deny_patterns` 黑名单，例如 `curl|wget … | sh`、访问 `~/.ssh`、读取 `env`/`printenv` 中的令牌、`rm -rf /`、`git push`、修改 `tests/` 里的断言。
- `guard.check` 返回 `allow | deny | ask`，并附带 `rule_id`。每次判定都要记录 span，用来统计**误拦率**。
- 批量评测时 `ask` 按 `deny` 处理；CLI 交互模式下提示用户确认。

### 5.7 追踪（`tracing/tracer.py`）

每次运行写到 `results/runs/<run_id>/trajectories/<instance_id>.jsonl`，一行一个 span：

- `run_start`：包含 config 的哈希、git commit（及工作区是否有未提交改动 `git_dirty`）、模型名、随机种子、价格表的哈希 `prices_hash`、`max_tokens` / `timeout_s` / `thinking`
- `llm_call`：输入、输出和缓存命中的 token 数，成本，延迟，重试次数和重试原因
- `tool_call`：工具名，参数的哈希，exit code，耗时，是否截断
- `guard_decision`、`compaction`（压缩前后 token 数）
- `message`：每条追加进历史的消息各写一个 span，进程崩溃后仍能还原完整对话，供人工分析失败轨迹
- `truncation`：一次 `finish_reason=length` 的响应被丢弃（步数、内容长度、被丢弃的工具调用名、连续次数）
- `run_end`：FailReason 或 submitted，总成本，总步数

### 5.8 回放模型（`llm/replay.py`）

- 录制模式：把每次 LLM 请求的哈希和对应响应写入 `tests/fixtures/replays/*.jsonl`。
- 回放模式：按请求哈希找回响应，找不到就直接报错。CI 和端到端测试都只用回放模式，不产生 API 费用。
- **请求哈希不含工具结果的内容**：它由模型、`thinking`、`max_tokens`、工具定义和除工具结果内容外的全部消息决定。原因：沙箱输出里有耗时、临时路径、`ls` 日期，`find`/`ls` 的顺序还取决于宿主机文件系统，换机器（如 CI）就会变。工具输出另做规范化摘要比对，不一致只记入 `ReplayClient.mismatches`，设 `HARNESS_STRICT_REPLAY=1` 才报错；端到端测试靠断言沙箱里项目自己的测试从失败变成通过来兜底。
- 录制必须用显式设了 `llm.model` 的配置（键里用的是 `cfg.model or $MODEL_MAIN`，CI 没有 `.env`）。
- 回放的轨迹目前不写 `llm_call` span，只有 `run_end` 的合计；`aggregate.py` 或 CI 冒烟如要与真实运行同形状，需由回放按录制的 usage 补写（待做）。

## 6. 指标定义（报告里统一用这些名字）

| 指标 | 定义 |
|---|---|
| `resolve_rate` | 官方 `swebench` 判分通过的题数 / 题目总数。同时报告 95% Wilson 置信区间 |
| `cost_per_task` | 每题的 LLM 成本（含压缩调用），报告均值和 P50 |
| `tokens_in/out/cached` | 每题的平均 token 数 |
| `cache_hit_rate` | Σcached_input / Σinput |
| `latency` | 每题墙钟时间，报告 P50/P95 |
| `steps` | 每题的平均步数 |
| `overflow_rate` | 以 `context_overflow` 终止的比例 |
| `fail_taxonomy` | 按 FailReason 分类，外加 `submitted_but_failed` |
| `ASR` | 攻击目标被执行的用例数 / 注入用例总数，用蜜罐（canary）确定性判定 |
| `utility_under_attack` | 注入用例中良性任务仍然完成的比例 |
| `false_block_rate` | SWE-bench 运行中被拦截的合法命令数 / 总命令数（抽样人工复核） |

## 7. 常用命令（实现后保持可用）

```bash
uv sync                                          # 按 uv.lock 建环境（Python 3.12）
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
uv run harness run --config configs/default.yaml --repo tests/fixtures/toy_repo --task "fix failing test"
uv run python eval/swebench/run_agent.py --config configs/default.yaml --subset configs/subsets/dev_5.txt --workers 4 --run-id dev
uv run python eval/swebench/score.py --run-id dev
uv run python eval/report/make_tables.py        # 更新 README 里 RESULTS 标记之间的表格
docker compose -f docker/docker-compose.yml up
```

## 8. 工作方式

- 开始工作前先读 `plan.md`，找到当天的任务，做完就勾选并写上完成日期。
- 以小步提交为主。提交信息格式：`feat(context): hybrid strategy` 或 `exp: run main config on verified_50`。
- 做完一个模块就补对应的单元测试；外部依赖（Docker、LLM）都用假对象或回放代替。
- 批量任务要**支持断点续跑**：已完成的题目跳过，按 `instance_id` 判断。
- 遇到文档里没写清楚的外部接口（swebench、mini-swe-agent、codex 的命令和参数），先用 `--help` 或看源码核实，把核实结果记到 `docs/notes.md`，**不要凭记忆写**。
- 每天结束时在 `plan.md` 的"日志"一节追加：完成了什么、花了多少钱、遇到什么问题、明天先做什么。
