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

- [x] 准备 x86_64 机器（2026-10-04）：已装 Docker（含 `docker compose` v2 插件）和 `uv`，**空闲磁盘 ≥ 150GB**，内存 ≥ 16GB（SWE-bench 镜像是 x86 的，不要在 ARM Mac 上直接跑）。用 `docker version`、`docker compose version`、`uv --version` 确认
  - 环境：Windows + WSL2（Ubuntu 22.04，x86_64），不使用 Docker Desktop，细节见 `docs/learning/docker_related.md`
  - [x] Docker Engine 29.1.3（Ubuntu 自带源 `docker.io`）、`docker compose` 2.40.3（v2 插件）、`docker buildx` 0.30.1，均在 2026-10-04 实测可用；已开启 systemd 并设为开机自启；用户已加入 `docker` 组，免 sudo
  - [x] `uv` 0.9.13，装在 `~/.local/bin/uv`（注意：Claude Code 的 shell 里 `which uv` 找不到，需确认该目录在 PATH 中）
  - [x] 空闲磁盘：当前 149GB 可用（总 251GB），略低于 150GB 的要求；**按用户决定暂不考虑**，拉镜像时再留意
  - [x] 内存：`.wslconfig` 限制 WSL 为 12GB / 10 核 / 8GB swap，低于 16GB 的要求；**按用户决定暂不考虑**，但并行 worker 数和容器内存上限设置时仍要保守
  - [x] 沙箱限制实测通过（2026-10-04，用 `alpine` 镜像，命令见 `docs/learning/docker_related.md` §4）：
    - cgroup 文件：`memory.max=67108864`、`cpu.max=50000 100000`、`pids.max=20`
    - `--network none`：容器里只有 `lo`，`wget example.com` 报 `bad address`
    - `--pids-limit 20`：起第 21 个以上进程报 `can't fork: Resource temporarily unavailable`
    - `--memory 32m --memory-swap 32m`：写入 100MB 被 OOM 杀掉，退出码 137
- [x] 申请 API Key，确定主模型 `{{MODEL_MAIN}}` 和摘要模型 `{{MODEL_CHEAP}}`，写入 `.env`。选模型时优先考虑：价格便宜、有前缀缓存、工具调用稳定（2026-10-04）
  - `.env` 已创建且被 `.gitignore` 忽略，未被 git 跟踪；服务商为 DeepSeek。2026-10-05 检查：`MODEL_MAIN=deepseek/deepseek-v4-pro`、`MODEL_CHEAP=deepseek/deepseek-flash`（已带 `deepseek/` 前缀），但密钥和地址仍是 `OPENAI_API_KEY`、`OPENAI_API_BASE`
  - `MAX_TOTAL_COST_USD` 仍是 `.env.example` 里的占位值 20，待 dev_5 实测单题成本后重填
  - 已离线核实（2026-10-05，详见 `docs/notes.md` §5）：`deepseek/` 前缀能路由、能计价（含缓存命中价）；裸名报 `LLM Provider NOT provided`；`openai/` 前缀能路由但 `completion_cost` 抛 `ModelNotMappedError`，所以应当用 `deepseek/`
  - [x] `.env` 的变量名已改成 `DEEPSEEK_API_KEY`、`DEEPSEEK_API_BASE=https://api.deepseek.com`（原先的 `OPENAI_*` 在 `deepseek/` 前缀下读不到，`get_llm_provider` 得到 `api_key_resolved=False`，地址落到默认的 `/beta`）。2026-10-05 核实：格式正确，LiteLLM 解析出提供商、密钥、地址，本机假服务器上请求形状和用量/成本解析都对（详见 `docs/notes.md` §5）
  - [ ] `.env.example` 还是 `OPENAI_*` 的旧模板，与 `.env` 的键不一致，需要同步；`.env` 权限是 644，建议 `chmod 600`
  - [ ] **仍未验证**（还没有发起过真实调用）：模型名在服务商那边是否真实存在、思考模式的默认状态、`reasoning_content` 是否需要回传、LiteLLM 内置价格表是否等于官方价格。验证计划（9 步，第 1 步起会花钱）见 `docs/notes.md` §5，最迟在写 `llm/client.py` 之前做完步骤 1–3
- [~] 建仓库，写 `pyproject.toml`（`requires-python = ">=3.12,<3.13"`）、`.python-version`（`3.12`）、`.env.example`、`.gitignore`；用 `uv add` / `uv add --dev` 加依赖，`uv sync` 生成 `.venv` 和 `uv.lock`（**提交 `uv.lock`**）；加入 CLAUDE.md、plan.md 和 README.md
  - [x] `.python-version`（3.12）、`pyproject.toml`（`requires-python` 已锁 `>=3.12,<3.13`）、`.venv`（Python 3.12.12）、`uv.lock`（已提交）、plan.md、README.md 已就绪
  - [x] 依赖已用 `uv add` / `uv add --dev` 加入并提交（`e1aff8e`，121 个包）：运行时 litellm、docker、pydantic、pyyaml、fastapi、uvicorn、aiosqlite、python-dotenv、swebench；dev 组 ruff、pytest、pytest-asyncio、httpx、mini-swe-agent
  - [x] `pyproject.toml` 加了 `[tool.pytest.ini_options]`（`testpaths=["tests"]`、`pythonpath=["eval/swebench"]`，让测试能按模块名 import `eval/swebench/` 里的脚本）（2026-10-04，未提交）
  - [ ] `pyproject.toml` 的 `name = "harness-demo"`（项目名是 harness-lab）和 `description` 还没改，也还没有 `[build-system]` / `[project.scripts]`（第 1 天写 `cli.py` 时一起配）
  - [x] `.env.example` 已创建（2026-10-04）：OpenAI 兼容格式，含 `OPENAI_API_KEY`、`OPENAI_API_BASE`、`MODEL_MAIN`、`MODEL_CHEAP`、`MAX_TOTAL_COST_USD`（其中 `MAX_TOTAL_COST_USD=20` 只是占位值，待 dev_5 实测后重填）
  - [x] `CLAUDE.md` 已从 `.gitignore` 移除并提交（`e1aff8e`）
  - [x] `.gitignore` 已加入 `.env`（2026-10-04，`git check-ignore` 确认生效）
  - [x] `.gitignore` 已加入 `results/runs/`、`.mswea/`（mini-swe-agent 的全局配置目录），并补了文件末尾换行（2026-10-04，未提交）
  - [ ] 本次新增的改动**还没提交**：`.gitignore`、`pyproject.toml`、`docs/notes.md`、`docs/learning/docker_related.md`（§2.7）、`configs/subsets/`、`eval/`、`tests/`
- [x] `eval/swebench/select_subset.py`：从 SWE-bench Verified 里固定随机种子选题，**集中在 3–5 个仓库**以减少镜像数量。输出 `verified_50.txt` 和 `dev_5.txt`，两者不重叠（2026-10-04；**配额待你确认，子集文件未冻结**）
  - 种子 42；数据集固定在 revision `78f471bf655a3137b2e8a75af1501690ec009ec3`；输出同时写 `configs/subsets/subsets_meta.json`（种子、revision、配额、实际计数、难度分布，无时间戳）
  - 配额 django 20 / sympy 15 / sphinx 10 / scikit-learn 10（共 55，只按各仓库题数定，不涉及其他理由）；`verified_50` 实际是 django 18 / sympy 14 / sklearn 9 / sphinx 9，`dev_5` 是 5 道 `<15 min fix`（django 2、sklearn 1、sphinx 1、sympy 1），两者不重叠
  - 已验证：用固定 revision 重跑，三个文件**字节相同**；再次运行默认拒绝覆盖（`--force` 才覆盖）；12 个单元测试（假数据集，不联网）
  - [ ] 你确认配额后再提交并视为冻结（CLAUDE.md §2.4）；想改配额用 `--quota repo=N --force`
- [~] `eval/swebench/pull_images.py`：拉取 55 个镜像（**时机已放宽**，原计划是"当晚就在后台开始拉取"；2026-10-05 与你商定：配额确认并冻结子集后，在第 1 天写代码的空闲时间后台拉，**最晚第 2 天白天开始**，第 2 天晚上开跑主实验前必须拉完）
  - [x] 脚本完成并测试（2026-10-04）：镜像名读数据集 `image` 字段（只断言命名规则，不手拼）；并发拉取、失败重试、429 限速时停止；断点续跑（状态在 `results/runs/pull_images/status.json`）；可用空间低于 `--min-free-gb`（默认 20）时停下；`--verify` 在断网的一次性容器里校验 `/testbed`；`--dry-run`；超过 10 个镜像要 `--yes`；12 个单元测试（假 docker 客户端）
  - [x] 5 个 dev 镜像已拉取并通过 `--verify`（`base_commit` 一致、工作区干净）。镜像里**没有 `rg`**，第 1 天的 search 工具必须回退到 grep；镜像的 `HEAD` 是 base_commit 之上一个空提交，所以校验比对的是 `HEAD^`（详见 `docs/notes.md` §2）
  - [x] 磁盘实测见 `docs/notes.md` §7：5 个镜像共占 15.4GB，每个新增 1.8–5.4GB；镜像是"共享基础层 + 每个 (repo, version) 一个环境层 + 每个镜像一个实例层"，55 题共 22 个 (repo, version)
  - [ ] **剩余 50 个镜像还没拉，不急着拉**：拉取不依赖任何功能；第 1 天只用 `dev_5`（已就绪），第 2 天晚上才用 `verified_50`；前置条件是先确认配额、冻结子集（否则改配额会让已拉的镜像白占磁盘）。粗略估计约 45–60GB（假设见 `docs/notes.md` §7，样本只有 5 个），当前可用约 143.6GB，放得下，**不需要扩大虚拟磁盘**；Docker Hub 匿名额度每小时 100 次，剩余 92，够用
  - [ ] 建议的命令（后台）：`mkdir -p results/runs/pull_images && nohup uv run python eval/swebench/pull_images.py --yes --verify --workers 2 --min-free-gb 35 > results/runs/pull_images/pull.log 2>&1 &`（`--min-free-gb 35` 是因为底线只在每次开始拉取前检查，并发在途的镜像可能再多占十几 GB）；之后用 `--dry-run` 和 `results/runs/pull_images/status.json` 看进度
- [x] 用 uv 安装 mini-SWE-agent（`uv tool install` 或放进 dev 依赖组，择一并记到 `docs/notes.md`，不用 pip），用 `--help` 确认它批量跑 SWE-bench 的命令，写进 `eval/baselines/mini_swe_agent.md`（2026-10-04）
  - 选择放进 dev 依赖组（`uv add --dev mini-swe-agent`，2.4.6，锁进 `uv.lock`，不与其他依赖冲突），理由记在 `docs/notes.md` §8；`tests/unit/test_no_agent_frameworks.py` 保证 `src/harness/` 不 import 它
  - `eval/baselines/mini_swe_agent.md` 含命令模板、参数说明、与 M0 的差异清单（网络默认联网、无资源上限、命令超时 60 秒等）、输出结构、判分命令和 `--help` 原文；`-c environment.run_args=[...]` 能解析成列表已离线验证
  - [ ] 冒烟测试（1 题，会产生少量 LLM 花费）**等你确认后再跑**；文档里的 `{{STEP_LIMIT}}`、`{{COST_LIMIT}}`、容器资源上限要等 `configs/default.yaml` 创建后才能填

**验收**：`uv sync` 成功且 `uv.lock` 已提交；`.env` 可用；子集文件已提交；镜像开始拉取。
（2026-10-05 调整：镜像拉取不再是第 0 天的验收项，改为最晚第 2 天白天开始；`dev_5` 的 5 个镜像已就绪。）

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

### 2026-10-04（第 0 天，进行中）

**完成了什么**
- 确定 Docker 方案：WSL2 内原生 Docker Engine，不用 Docker Desktop；开启 systemd，`docker.io` + `docker-compose-v2` + `docker-buildx` 用 Ubuntu 自带源（本机 apt 已配清华镜像，不访问 `download.docker.com`）
- 环境实测：Docker 29.1.3、Compose 2.40.3、buildx 0.30.1、uv 0.9.13；cgroup v2（systemd 驱动）、overlayfs、runc；免 sudo 可用；`hello-world` 拉取并运行成功（说明 Docker Hub 可达，大镜像的拉取速度还没测）
- 写了 `.wslconfig`：12GB 内存 / 10 核 / 8GB swap
- 写了学习笔记 `docs/learning/docker_related.md`（项目所需 Docker 特性 → 原理 → WSL2 配置与排错）

**当天花费**：暂记 $0（harness 还没写出来，没有调用记录）

**补充（同日，后续）**
- 申请了 DeepSeek 的 API Key，创建 `.env`（已被 `.gitignore` 忽略）；"申请 API Key"一项已勾选，模型名的真实存在性等仍未验证，见该项下的说明
- `.gitignore` 加入 `.env`；`CLAUDE.md` 已 `git add`；上述改动都在暂存区，尚未提交
- 磁盘和内存两项按用户决定暂不考虑，"准备机器"已勾选完成
- 已拉取 `alpine` 镜像（13MB），沙箱限制（断网、内存、CPU、pids）实测通过，结果见上方勾选项
- 创建 `.env.example`（OpenAI 兼容格式）；`CLAUDE.md` 已移出 `.gitignore`
- 在 scratchpad 的副本里用 `uv add --no-sync` 试解析了依赖，在 Python 3.12 下全部可解析、互不冲突（项目本身的 `pyproject.toml` 和 `uv.lock` 没动）

**遇到的问题**
- `docker buildx` 报 `unknown command`：`docker.io` 不带 buildx，它是单独的包，装 `docker-buildx` 解决
- 加入 `docker` 组后当前 shell 报 `permission denied`：用户组要重新登录才生效
- 磁盘（149GB 可用）和内存（12GB）都略低于第 0 天的要求，见上方勾选项
- 仓库底座还有两处与 plan 不一致：`pyproject.toml` 项目名和依赖未填（依赖还没 `uv add`）、`.gitignore` 缺 `results/runs/`（后续已解决，见下方"实现"一节）
- `.env` 里的模型名没有提供商前缀，LiteLLM 无法识别（后续已改成 `deepseek/` 前缀，变量名问题见 2026-10-05 的日志）

**实现（同日，晚）**
- 依赖已加入并提交（`e1aff8e`）；`pyproject.toml` 加了 pytest 配置；`.gitignore` 加了 `results/runs/`、`.mswea/`
- 新增 `docs/notes.md`：swebench 5.0.2、mini-swe-agent 2.4.6 等外部接口的核实记录（带源码行号），回忆来的内容单独标为"未核实"
- 新增 `eval/swebench/select_subset.py` 并生成 `configs/subsets/{verified_50,dev_5}.txt` 和 `subsets_meta.json`（固定 revision，重跑字节相同）
- 新增 `eval/swebench/pull_images.py`；拉取并校验了 5 个 dev 镜像，实测磁盘占用（15.4GB）
- 新增 `eval/baselines/mini_swe_agent.md`；安装方式选 dev 依赖组
- 新增测试：`test_select_subset.py`（12 个）、`test_pull_images.py`（12 个）、`test_no_agent_frameworks.py`（1 个），`ruff check`、`ruff format --check` 和 `pytest` 全部通过
- `CLAUDE.md` 移出 `.gitignore` 后，ruff 0.16 会格式化 markdown 里的 Python 代码块，`ruff format --check .` 因此报 `CLAUDE.md` 要重排；已在 `pyproject.toml` 的 `[tool.ruff]` 里 `extend-exclude = ["CLAUDE.md"]`，没有改动规则文件本身
- 花费：仍是 $0（没有调用 LLM；只访问了 HF、Docker Hub 和 PyPI）

**实现中的发现**
- 镜像里 `HEAD` 是 base_commit 之上的一个空提交，最初按 `HEAD == base_commit` 校验会误判，已改为比对 `HEAD^`
- 镜像里没有 `rg`（有 `grep`），第 1 天的 search 工具必须回退
- 磁盘占用远大于压缩体积（django-11066 一例约 3.6 倍），但镜像按层共享：55 题只有 22 个 (repo, version) 环境层，剩余 50 个镜像粗略估计 45–60GB，当前 143.6GB 放得下（最初我按 5 个样本的平均值外推出"约 154GB 放不下"，那个估算有误，已更正）
- mini-swe-agent 默认联网、无资源上限，与 M0 的沙箱不一致，已在 `mini_swe_agent.md` 列出对齐方案
- `swebench eval verified` 不支持固定数据集 revision，判分前要核对最新 sha 与 `subsets_meta.json` 一致

**明天先做（按顺序）**
1. 你确认子集配额（django 20 / sympy 15 / sphinx 10 / sklearn 10）。确认后提交并冻结子集，再用上面的命令后台拉取剩余 50 个镜像
2. 用一次最小的 LiteLLM 调用验证 `.env` 的模型名、前缀和价格数据（建议改用 `deepseek/` 前缀；会产生极小花费，记入日志）
3. 你确认后跑 mini-swe-agent 的 1 题冒烟测试
4. 修 `pyproject.toml` 的项目名和描述，提交本次所有改动（提交信息按 CLAUDE.md §8 的格式）
5. 开始第 1 天：`llm/client.py`、`sandbox/docker_sandbox.py` 等

### 2026-10-05（第 0 天收尾：与 plan 的差距检查）

**完成了什么**
- `docs/notes.md` §5 补上 LiteLLM 与 DeepSeek 的离线核实结果：`deepseek/` 前缀能路由也能计价，`openai/` 前缀计价失败，裸名无法识别；DeepSeek 默认 `api_base` 是 `/beta`；思考模式需要回传 `reasoning_content`
- 按商定放宽了镜像拉取的时机（见第 0 天的拉取一项）
- 检查了 `.env`（没有输出密钥的值）：先发现变量名还是 `OPENAI_*`，LiteLLM 读不到；之后你改成了 `DEEPSEEK_*`，重新核实通过（见"申请 API Key"一项）
- `docs/learning/python_feature.md` 是你新增的笔记，未跟踪，不在 plan 里；它里面的 Python 代码块让 `ruff format --check .` 失败（ruff 0.16 会格式化 markdown 里的代码块），已把 `pyproject.toml` 的 ruff 排除从只排除 `CLAUDE.md` 改成排除所有 `*.md`，没有改你的笔记

**当天花费**：$0

**与 plan 的差距**

| 第 0 天验收项 | 状态 |
|---|---|
| `uv sync` 成功且 `uv.lock` 已提交 | 达成（`uv sync --frozen` 通过，`uv.lock` 在 `e1aff8e` 里） |
| `.env` 可用 | **部分达成**：格式和 LiteLLM 解析已核实（`DEEPSEEK_*` 已改好）；没发起过真实调用；`MAX_TOTAL_COST_USD` 还是占位值 20；`.env.example` 未同步 |
| 子集文件已提交 | **未达成**：文件已生成但未跟踪，配额还没确认，所以不算冻结 |
| 镜像开始拉取 | 已调整：5/55（dev 的 5 个）已就绪，其余按新时机拉 |

- 第 0 天 6 个顶层项：4 个完成、2 个进行中（建仓库：`pyproject.toml` 的项目名和描述没改；拉取：50 个未拉）。另外 mini-swe-agent 的冒烟测试没跑
- 第 1–5 天共 43 个顶层项：**0 个开始**。`src/` 目录不存在；`configs/` 只有 `subsets/`（没有 `default.yaml`、`policy.yaml`、`ablations/`）；`eval/swebench/` 只有 2 个文件（缺 `run_agent.py`、`score.py`）；`eval/report/`、`eval/security/`、`docker/`、`.github/` 都不存在
- 自 `e1aff8e` 之后的改动都**没有提交**：`.gitignore`、`pyproject.toml`、`plan.md`、`docs/notes.md`、`docs/learning/`、`configs/`、`eval/`、`tests/`
- 总花费 $0，距离预算上限还很远

**明天先做（按顺序）**
1. 你确认子集配额（django 20 / sympy 15 / sphinx 10 / sklearn 10）；确认后提交并冻结子集
2. 同步 `.env.example`，按 `docs/notes.md` §5 的验证计划做步骤 1–3 的真实调用（花费不到 0.001 美元量级，记入日志）
3. 修 `pyproject.toml` 的项目名和描述，提交所有改动（提交信息按 CLAUDE.md §8 的格式）
4. 开始第 1 天：`llm/client.py`、`llm/replay.py`、`sandbox/docker_sandbox.py`；写代码的间隙后台拉取剩余 50 个镜像
5. mini-swe-agent 的冒烟测试等 `configs/default.yaml` 确定、`.env` 可用后再跑
