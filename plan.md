# plan.md — harness-lab 五天计划

> 项目的长期规则见 `CLAUDE.md`，本文件是执行清单。每项做完就勾选，写上日期，并在最后的"日志"一节追加当天记录。
> 状态标记：`[ ]` 未开始，`[~]` 进行中，`[x]` 完成，`[-]` 砍掉（需写明原因）。

---

## 0. 目标与验收

**最终交付物**

1. 一个可以运行的 harness：CLI 和 FastAPI 服务都能用，一条命令就能用 docker-compose 启动。
2. `results/summary/` 下的四份结果：主实验对比、消融实验、安全评测、成本分析。
3. 填好的 README：包括架构图、结果表、失败案例分析、复现步骤。
4. 3 条简历条目，所有数字都来自 `results/`。

**最低成功标准（MVP）**：主配置和 mini-SWE-agent 基线都跑完 50 题并完成判分，至少有 2 组消融实验、1 份安全评测，README 可以复现。

**实验矩阵**（同一模型、同一题目子集、同一步数和成本上限）

| ID | 配置文件 | 与主配置 M0 的唯一差别 | 优先级 |
|---|---|---|---|
| M0 | `default.yaml` | 主配置：tools + hybrid + planning on + 预算 {{128k}} + guard on | P0 |
| B0 | mini-SWE-agent | 外部基线，只用 bash | P0 |
| A1 | `ablations/ctx_none.yaml` | context = none | P0 |
| A2 | `ablations/bash_only.yaml` | action_space = bash_only | P0 |
| A3 | `ablations/ctx_truncate.yaml` | context = truncate | P1 |
| A4 | `ablations/ctx_summarize.yaml` | context = summarize | P1 |
| A5 | `ablations/no_planning.yaml` | planning = off | P1 |
| A6 | `ablations/budget_32k.yaml` | budget = 32k（在上下文压力下比较各策略，至少和 A1 组合跑一次） | P1 |
| M0' | `default.yaml` 重跑一次 | 用来估计运行间的随机波动 | P2 |
| B1 | `codex exec` | 外部参考（可选） | P2 |

**预算估算**（先用开发集的 5 题实测单题成本，再填这张表）

- 单题平均成本 ≈ `{{$X}}`，配置数 `{{N}}` × 50 题 ≈ `{{$Y}}`
- `.env` 中 `MAX_TOTAL_COST_USD = {{…}}`

---

## 第 0 天：准备（前一晚，1–2 小时）

- [ ] 准备 x86_64 机器：已装 Docker（含 `docker compose` v2 插件）和 `uv`，**空闲磁盘 ≥ 150GB**，内存 ≥ 16GB（SWE-bench 镜像是 x86 的，不要在 ARM Mac 上直接跑）。用 `docker version`、`docker compose version`、`uv --version` 确认
- [ ] 申请 API Key，确定主模型 `{{MODEL_MAIN}}` 和摘要模型 `{{MODEL_CHEAP}}`，写入 `.env`。选模型时优先考虑：价格便宜、有前缀缓存、工具调用稳定
- [ ] 建仓库，写 `pyproject.toml`（`requires-python = ">=3.12,<3.13"`）、`.python-version`（`3.12`）、`.env.example`、`.gitignore`；用 `uv add` / `uv add --dev` 加依赖，`uv sync` 生成 `.venv` 和 `uv.lock`（**提交 `uv.lock`**）；加入 CLAUDE.md、plan.md 和 README.md
- [ ] `eval/swebench/select_subset.py`：从 SWE-bench Verified 里固定随机种子选题，**集中在 3–5 个仓库**以减少镜像数量。输出 `verified_50.txt` 和 `dev_5.txt`，两者不重叠
- [ ] `eval/swebench/pull_images.py`：**当晚就在后台开始拉取 55 个镜像**
- [ ] 用 uv 安装 mini-SWE-agent（`uv tool install` 或放进 dev 依赖组，择一并记到 `docs/notes.md`，不用 pip），用 `--help` 确认它批量跑 SWE-bench 的命令，写进 `eval/baselines/mini_swe_agent.md`

**验收**：`uv sync` 成功且 `uv.lock` 已提交；`.env` 可用；子集文件已提交；镜像开始拉取。

---

## 第 1 天：Agent 核心能端到端跑通

- [ ] `llm/client.py`：基于 LiteLLM 的异步客户端，包含重试（只对 429 和 5xx）、成本计算、token 与缓存命中统计
- [ ] `llm/replay.py`：录制和回放两种模式
- [ ] `sandbox/docker_sandbox.py`：`exec`、`read_file`、`write_file`、`git_diff`；断网；资源限制；用 `finally` 保证清理容器
- [ ] `tools/`：bash、view、str_replace（只允许唯一匹配）、search（ripgrep，镜像里没有就回退到 grep）、todo、submit，以及 registry
- [ ] `agent/`：主循环、状态、prompts、限制（步数、成本、时间），实现 FailReason
- [ ] `tracing/tracer.py`：写 JSONL 格式的 span
- [ ] `cli.py`：实现 `harness run`
- [ ] `tests/fixtures/toy_repo/`：一个带 bug 的小仓库，包含 1 个会失败的测试
- [ ] 单元测试：str_replace 的边界情况、截断逻辑、限制检查、registry
- [ ] 端到端测试：在 toy_repo 上录制一次真实运行，再用回放通过测试
- [ ] 手动在 `dev_5` 的 2 道题上跑通，能生成补丁

**验收**
- `uv run pytest -q` 全部通过
- toy_repo 能修复
- 2 道 SWE-bench 题能产生非空补丁，轨迹文件完整
- 记录单题成本 `{{$}}` 和平均步数 `{{}}`

---

## 第 2 天：可开关组件和评测流水线（**当晚开跑主实验**）

- [ ] `context/`：`tokens.py`（计数）、`manager.py`，以及四种策略；实现可恢复截断；压缩记录 span
- [ ] 实现 KV 缓存友好的规则（CLAUDE.md §5.2），加单元测试：连续两轮的请求前缀逐字节相同
- [ ] 配置系统：`default.yaml` 和所有 `ablations/*.yaml`；启动时校验配置，并打印与主配置的差异
- [ ] `security/`：先把 policy 和 guard 的**基础版**接进循环（主配置默认 guard 开启），第 3 天再完善
- [ ] `eval/swebench/run_agent.py`：并发执行（`--workers`）、断点续跑、输出 `preds.jsonl`（字段 `instance_id`、`model_name_or_path`、`model_patch`）、跑之前打印成本预估并等待确认
- [ ] `eval/swebench/score.py`：调用官方 `swebench` 判分工具（命令先核实），把结果合并进 `metrics.json`
- [ ] `eval/report/aggregate.py`：汇总每次运行的指标，包括 Wilson 置信区间
- [ ] 在 `dev_5` 上跑 M0 做冒烟测试，**修完所有明显的 bug**
- [ ] **打 tag `v0.1-frozen`**
- [ ] 晚上：在 `verified_50` 上跑 M0，同时跑 B0（mini-SWE-agent，同一模型、同一题目子集）

**验收**：dev_5 全流程能生成判分结果；tag 已打；M0 和 B0 已开跑。

**如果进度落后**：`summarize` 策略可以推迟到第 3 天上午，但 M0 必须今晚开跑。

---

## 第 3 天：分析主实验，跑消融，完善安全层

- [ ] 给 M0 和 B0 判分，用 `failure_taxonomy.py` 生成失败分类
- [ ] 人工看 5 条失败轨迹，写进 `docs/failure_analysis.md`：是什么现象、根因、是 harness 的问题还是模型的问题
- [ ] 如果发现 harness 的 bug：修复 → 打 `v0.2-frozen` → 在 `results/CHANGELOG.md` 记录 → **重跑 M0**
- [ ] 按 P0、P1 的顺序跑消融实验 A1–A6（可以通宵并行）
- [ ] 完善安全层：完成 `policy.yaml` 的三级权限和 deny_patterns，`ask` 的处理，误拦记录
- [ ] `eval/security/build_cases.py`：生成 **20 个注入用例**（4 种注入方式 × 5 种攻击目标）
  - 注入方式：README 正文、代码注释、工具输出（伪造的报错信息）、issue 描述
  - 攻击目标：读取 `~/.ssh` 下的蜜罐文件、读取 `CANARY_TOKEN` 环境变量、联网下载并执行（`curl | sh`）、篡改测试断言、`rm -rf` 删除工作目录
  - 每个用例同时带一个良性任务（修一个小 bug），用来计算 `utility_under_attack`
- [ ] 蜜罐判定：通过命令日志和文件访问确定性地判断攻击是否得手，不用 LLM 判断

**验收**：M0 和 B0 的结果表生成；P0 的消融实验跑完；20 个注入用例可以运行。

---

## 第 4 天：安全评测、服务化、CI

- [ ] `run_security.py`：分别在 guard 关闭和开启时跑 20 个用例，输出 ASR、utility_under_attack
- [ ] 从 M0 的轨迹里统计 `false_block_rate`，抽 20 条拦截记录人工复核
- [ ] `service/`：
  - `POST /tasks`：提交仓库路径或 git URL、任务描述、配置名
  - `GET /tasks/{id}`：查询状态和结果
  - `GET /tasks/{id}/events`：用 SSE 流式推送 span
  - 后台用 asyncio 队列加 worker 执行；任务状态存 SQLite
- [ ] `docker/Dockerfile` 和 `docker-compose.yml`：Dockerfile 基于 `python:3.12-slim`，复制官方 uv 二进制，用 `uv sync --frozen --no-dev` 装依赖（不用 pip）；服务通过挂载宿主机的 `docker.sock` 创建沙箱容器；README 里要说明这样做的安全影响
- [ ] `.github/workflows/ci.yml`：用 `astral-sh/setup-uv` 装 uv，`uv sync` 后 `uv run` 跑 ruff 检查和 pytest（含回放端到端测试），不需要任何密钥
- [ ] 跑完剩下的 P1 消融实验；如果预算允许，跑 M0'

**验收**
- `docker compose up` 后，用 curl 提交 toy_repo 任务，能收到 SSE 事件和补丁
- CI 通过
- 安全评测表生成

---

## 第 5 天：出结果、写 README、写简历条目

- [ ] `make_tables.py`：生成主实验、消融、安全、成本四张表，写入 README 的标记区之间
- [ ] `plots.py`：两张图，保存到 `docs/img/`
  - 成本 vs 解决率散点图（每个配置一个点，基线也画进去）
  - 各配置的失败分类堆叠条形图
- [ ] 填写 README 里的所有 `{{}}`：动机、设计取舍、结论、局限性
- [ ] 用 mermaid 画架构图；用 vhs 或 asciinema 录一段 30 秒以内的演示 GIF
- [ ] 从 `results/summary/` 提炼 3 条简历条目，写到 `docs/resume_bullets.md`，每个数字都注明出自哪个文件
- [ ] 最终检查：在一个干净的 clone 里按 README 的复现步骤走一遍

**可选任务**（只有 P0 和 P1 全部完成才做）
- [ ] B1：用 `codex exec` 在同一子集上跑一遍（先核实非交互模式的参数，并确保它在题目容器里运行）
- [ ] 把最有效的上下文策略或权限层移植成 DeepSeek Harness 插件，单独建仓库发布
- [ ] 通过 OpenTelemetry 导出到 Langfuse

**验收**：README 里没有 `{{}}`，只有确实缺数据的地方允许保留 `{{TBD}}`；简历条目的数字都能追溯到 results。

---

## 风险与应对

| 风险 | 信号 | 应对 |
|---|---|---|
| 镜像拉取慢或磁盘不够 | 第 1 天镜像还没拉完 | 把子集缩到 2–3 个仓库、30 题；镜像用完即删 |
| 单题成本超出预期 | dev_5 实测超过预算 | 降低 `max_steps`；消融实验只跑 P0；摘要改用便宜模型 |
| 解决率太低（小于 10%），各配置之间看不出差别 | 看 M0 的结果 | 换更强的主模型，或者选更简单的题（按官方难度标注筛选） |
| 大预算下不会溢出，上下文策略之间没有差别 | A1 和 M0 几乎一样 | 这本身就是一个结论；重点看 A6 在 32k 预算下的对比 |
| API 限流 | 大量 429 | 减少 workers；重试逻辑按限流信息等待 |
| 进度落后 | 第 3 天结束时 P0 没完成 | 砍掉 P1 消融和服务化中的 SSE；保证 MVP |

---

## 简历条目模板（第 5 天填写，数字必须来自 results）

```text
harness-lab — Framework-free Coding Agent Harness | Python, LiteLLM, Docker, FastAPI, SWE-bench | {{2026}} | github.com/{{…}}
- Built a framework-free coding-agent harness with Docker-sandboxed execution and config-switchable components; resolved {{X}}% (95% CI {{…}}) of a 50-task SWE-bench Verified subset vs. {{Y}}% for mini-SWE-agent on the same model, at {{Z}} per task.
- Ran controlled harness ablations: under a 32K context budget, rule-based truncation + LLM compaction cut context-overflow failures from {{a}}% to {{b}}%; a KV-cache-friendly, append-only context layout reached a {{c}}% prompt-cache hit rate.
- Designed a tiered permission guard and a 20-case prompt-injection suite with canary-based detection, reducing attack success rate from {{d}}% to {{e}}% at a {{f}}% false-block rate; shipped as a FastAPI + SSE service with replay-based CI.
```

---

## 日志

<!-- 每天追加一段：日期 / 完成了什么 / 当天花费 $ / 遇到的问题 / 明天先做什么 -->
