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
  - [x] 内存：`.wslconfig` 原先限制 WSL 为 12GB / 10 核 / 8GB swap，低于 16GB 的要求；**2026-10-12 `docker info` 实测为 15.6GB / 10 核**（配置显然已变，以实测为准）；**按用户决定暂不考虑**，但并行 worker 数和容器内存上限设置时仍要保守
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
  - [x] `.env.example` **不同步**（你的决定，2026-10-05）：保持适用范围最广的 `OPENAI_*` 格式。它是通用模板，`.env` 是你自己的 DeepSeek 配置（`DEEPSEEK_*`、`deepseek/` 前缀），两者的键允许不同
  - 走通用的 `openai/<模型名>` 路线时，已核实的差异（2026-10-05，本机假服务器，离线）：① `thinking=` 关键字参数会抛 `UnsupportedParamsError`；如果设了 `drop_params=True` 则会**静默丢掉**，思考模式就失控。要改用 `extra_body={"thinking": {...}}`，请求体里能正确带上。mini-swe-agent 的 `swebench.yaml` 默认 `drop_params: True`，所以 B0 的 `model_kwargs.thinking` 在这条路线上会被静默丢掉；② `completion_cost` 抛 `ModelNotMappedError`：我们自有的价格表不受影响，但 mini-swe-agent 的成本统计会失败（要么给覆盖文件加 `openai/` 的键，要么 `ignore_errors`，后者会让 `cost_limit` 失效）；③ `reasoning_content`、缓存命中 token、思考 token 的解析与 `deepseek/` 路线相同
  - [ ] `.env` 权限是 644，建议 `chmod 600`（未处理）
  - [x] 验证计划步骤 1–3 已完成（2026-10-05，真实调用）：密钥有效，`deepseek/deepseek-flash` 和 `deepseek/deepseek-v4-pro` 都存在，响应能解析，两个模型都支持函数调用；**不传 `thinking` 时服务端默认开启思考模式**（响应带 `reasoning_content`）；`usage` 里有 `prompt_cache_hit_tokens`。4 次调用合计 $0.001159（LiteLLM 价格表估算），详见 `docs/notes.md` §5
  - [x] 验证计划步骤 4 已完成（2026-10-05）：`reasoning_content` 回传**没有被强制**（去掉后单轮、3 轮工具链都正常），且回传内容不计 prompt token；`thinking: disabled` 可用；默认思考模式下 `max_tokens` 太小会得到空内容和 `finish_reason=length`；两种模式的 prompt 模板不同，一次运行内不要切换。30 次调用 $0.005940（LiteLLM 价格表估算），详见 `docs/notes.md` §5
  - [x] 验证计划步骤 7–9 已完成（2026-10-05，详见 `docs/notes.md` §5 末尾）：① 错误密钥抛 `BadRequestError` 但 `status_code=401`（不是 `AuthenticationError`），所以重试要**按 `status_code` 分类**，不能按类名；不存在的模型名返回支持的名字 `deepseek-flash`、`deepseek-v4-pro`；② 本机假服务器：429/500/502/503/断连/缺 `choices` 都映射到 429 或 5xx 的状态码，`Timeout` 是 408，畸形的 200 是 `APIError(200)`，**LiteLLM 没有隐藏重试**，`Retry-After` 在 `e.litellm_response_headers` 里；③ mini-swe-agent 冒烟成功（见下面 mini-swe-agent 一项）
  - [ ] 步骤 5（缓存命中和最短前缀）、步骤 6（对账）**暂不做**（你的决定：有成熟文档可参考）。**风险**：LiteLLM 的 `completion_cost` 按**计算时的当前时间**使用错峰价（标准价的 50%），不对账的情况下尤其要靠固定费率的自有价格表，详见下面的待决策项
  - [x] **成本口径已决定**（2026-10-05，你的决定，以最后一次为准）：**同时保留 token 指标和美元成本**（`tokens_in` 拆成未命中和命中缓存、`tokens_out` 含思考 token、`cache_hit_rate`、`steps`、`latency`，以及 `cost_per_task`）；**美元成本用固定的高峰价（标准价）按记录的 token 数计算，不依赖时钟**；预算保护仍按美元用同一个标准价，**CLAUDE.md 不需要改**。公式与 LiteLLM 的标准价结果完全一致（冒烟轨迹 0.016967）。B0 通过 `LITELLM_MODEL_REGISTRY_PATH=eval/baselines/litellm_registry.json` 把错峰价设成与标准价相同，已离线验证。详见 `docs/notes.md` §5 末尾
  - [x] 价格覆盖文件 `eval/baselines/litellm_registry.json` 和 4 个单元测试 `tests/unit/test_price_registry.py`、`tests/conftest.py`（固定 `LITELLM_LOCAL_MODEL_COST_MAP=True`）已完成（2026-10-05，已提交）：覆盖文件的标准价与 LiteLLM 一致；不用覆盖文件时成本随错峰判断变化；用了之后无论是否错峰都是标准价。**第 1 天写 `llm/cost.py` 时**：用同一个公式和一张标准价表，不调用 `completion_cost`，每个 `llm_call` 记录原始 token 和成本，`run_start` 记录价格表的哈希
  - [x] **重试策略已决定**（2026-10-05，你的决定：方案 B）：除 429 和 5xx 外，也重试**客户端超时**（`Timeout`）和**不可用的 200**（响应体不是合法 JSON 或缺 `choices`）；总共最多 3 次尝试，指数退避加抖动，429 遵守 `Retry-After`；按（异常类型、状态码、响应形状）判断；4xx 不重试；每次记录次数和 `retry_reason`；用尽后以 `env_error` 终止；重试等待计入墙钟时间和延迟。**CLAUDE.md §5.1 和 §5.7 已按此修改**；问题清单和策略详见 `docs/notes.md` §5
  - [x] **输出上限、超时、流式、截断、失败尝试已决定**（2026-10-06，你的决定：全部按我的方案；调研数据和依据见 `docs/notes.md` §5，CLAUDE.md §5.1、§5.3、§5.7 已修改）：① 当前只用非流式，基本功能完成后再支持流式；② 思考开 `max_tokens=65536`、`timeout_s=900`，思考关 `max_tokens=16384`、`timeout_s=300`，成对设置，超时必须大于顶满上限的耗时；③ `finish_reason=length` 不重试，半截工具调用不执行，连续 3 次截断记为 `format_error`（详情 `output_truncated`），另设 `truncated_outputs` 计数器；④ `insufficient_system_resource` 和 `aborted` 加入重试类别并丢弃半截内容；⑤ 重试保持最多 3 次尝试；⑥ 主指标只统计成功调用，失败尝试另记并估算浪费；⑦ `context.budget_tokens` 只算输入；⑧ B0 在关闭思考时显式设 `max_tokens=16384` 和超时
  - [ ] 仍待验证：真实 DeepSeek 上的 429、5xx、超时表现和失败请求的计费；`aborted` 的触发场景；速度数据每项只有 1 个样本，要用第 1 天 `dev_5` 的真实延迟校准超时；B0 的 `model_kwargs.max_tokens` 和 `timeout` 是否在 mini-swe-agent 里生效（没实测）
  - [x] **`thinking` 默认开**（你的决定，2026-10-07：参考主流 harness）。每次调用都显式传，所有配置统一。**强度 `reasoning_effort` 临时定为 `high`，等你确认**：调研和依据见 `docs/notes.md` §11.1（要点：LiteLLM 会丢掉 `reasoning_effort` 关键字，必须走 `extra_body`；`high` 与 B0 的默认一致；官方数字里 `max` 只比 `high` 高约 1 个点，50 题分辨不出来）。强度已是配置项并进了 `config_hash`、回放键和 `run_start`，改成 `max` 要同时重新核对成对的 `max_tokens`/`timeout_s`
- [~] 建仓库，写 `pyproject.toml`（`requires-python = ">=3.12,<3.13"`）、`.python-version`（`3.12`）、`.env.example`、`.gitignore`；用 `uv add` / `uv add --dev` 加依赖，`uv sync` 生成 `.venv` 和 `uv.lock`（**提交 `uv.lock`**）；加入 CLAUDE.md、plan.md 和 README.md
  - [x] `.python-version`（3.12）、`pyproject.toml`（`requires-python` 已锁 `>=3.12,<3.13`）、`.venv`（Python 3.12.12）、`uv.lock`（已提交）、plan.md、README.md 已就绪
  - [x] 依赖已用 `uv add` / `uv add --dev` 加入并提交（`e1aff8e`，121 个包）：运行时 litellm、docker、pydantic、pyyaml、fastapi、uvicorn、aiosqlite、python-dotenv、swebench；dev 组 ruff、pytest、pytest-asyncio、httpx、mini-swe-agent
  - [x] `pyproject.toml` 加了 `[tool.pytest.ini_options]`（`testpaths=["tests"]`、`pythonpath=["eval/swebench"]`，让测试能按模块名 import `eval/swebench/` 里的脚本）（2026-10-04，已提交）
  - [ ] （**暂不处理**，你的决定，2026-10-05）`pyproject.toml` 的 `name = "harness-demo"`（项目名是 harness-lab）和 `description` 还没改。`[build-system]`、`[project.scripts]`、`[tool.uv.build-backend]` 已在第 1 天（2026-10-06）补上
  - [x] `.env.example` 已创建（2026-10-04）：OpenAI 兼容格式，含 `OPENAI_API_KEY`、`OPENAI_API_BASE`、`MODEL_MAIN`、`MODEL_CHEAP`、`MAX_TOTAL_COST_USD`（其中 `MAX_TOTAL_COST_USD=20` 只是占位值，待 dev_5 实测后重填）
  - [x] `CLAUDE.md` 已从 `.gitignore` 移除并提交（`e1aff8e`）
  - [x] `.gitignore` 已加入 `.env`（2026-10-04，`git check-ignore` 确认生效）
  - [x] `.gitignore` 已加入 `results/runs/`、`.mswea/`（mini-swe-agent 的全局配置目录），并补了文件末尾换行（2026-10-04，已提交）
  - [x] 截至 2026-10-05，上述改动都已提交（最近一次 `aafd242`），工作区干净
- [x] `eval/swebench/select_subset.py`：从 SWE-bench Verified 里固定随机种子选题，**集中在 3–5 个仓库**以减少镜像数量。输出 `verified_50.txt` 和 `dev_5.txt`，两者不重叠（2026-10-04；**配额已于 2026-10-05 确认，保持当前值**；真正的冻结标签 `v0.1-frozen` 在第 2 天主实验前打）
  - 种子 42；数据集固定在 revision `78f471bf655a3137b2e8a75af1501690ec009ec3`；输出同时写 `configs/subsets/subsets_meta.json`（种子、revision、配额、实际计数、难度分布，无时间戳）
  - 配额 django 20 / sympy 15 / sphinx 10 / scikit-learn 10（共 55，只按各仓库题数定，不涉及其他理由）；`verified_50` 实际是 django 18 / sympy 14 / sklearn 9 / sphinx 9，`dev_5` 是 5 道 `<15 min fix`（django 2、sklearn 1、sphinx 1、sympy 1），两者不重叠
  - 已验证：用固定 revision 重跑，三个文件**字节相同**；再次运行默认拒绝覆盖（`--force` 才覆盖）；12 个单元测试（假数据集，不联网）
  - [x] 配额已确认（2026-10-05，你的决定：保持 django 20 / sympy 15 / sphinx 10 / scikit-learn 10）；子集文件已提交（`04f6a4a`）。冻结标签在第 2 天主实验前打（CLAUDE.md §2.4）；之后如要改配额，用 `--quota repo=N --force` 重新生成，已跑的配置要全部重跑
- [~] `eval/swebench/pull_images.py`：拉取 55 个镜像（**时机已调整两次**，原计划是"当晚就在后台开始拉取"；2026-10-05 你决定：**配额已确认，但不现在拉，等主 harness 实现后再动手**；仍须在第 2 天晚上开跑主实验前拉完，所以最晚第 2 天白天开始）
  - [x] 脚本完成并测试（2026-10-04）：镜像名读数据集 `image` 字段（只断言命名规则，不手拼）；并发拉取、失败重试、429 限速时停止；断点续跑（状态在 `results/runs/pull_images/status.json`）；可用空间低于 `--min-free-gb`（默认 20）时停下；`--verify` 在断网的一次性容器里校验 `/testbed`；`--dry-run`；超过 10 个镜像要 `--yes`；12 个单元测试（假 docker 客户端）
  - [x] 5 个 dev 镜像已拉取并通过 `--verify`（`base_commit` 一致、工作区干净）。镜像里**没有 `rg`**，第 1 天的 search 工具必须回退到 grep；镜像的 `HEAD` 是 base_commit 之上一个空提交，所以校验比对的是 `HEAD^`（详见 `docs/notes.md` §2）
  - [x] 磁盘实测见 `docs/notes.md` §7：5 个镜像共占 15.4GB，每个新增 1.8–5.4GB；镜像是"共享基础层 + 每个 (repo, version) 一个环境层 + 每个镜像一个实例层"，55 题共 22 个 (repo, version)
  - [ ] **剩余 50 个镜像还没拉，等主 harness 实现后再拉**（你的决定，2026-10-05）：拉取不依赖任何功能；第 1 天只用 `dev_5`（已就绪），第 2 天晚上才用 `verified_50`；配额已确认，没有别的前置条件。粗略估计约 45–60GB（假设见 `docs/notes.md` §7，样本只有 5 个），当前可用约 143.6GB，放得下，**不需要扩大虚拟磁盘**；Docker Hub 匿名额度每小时 100 次，剩余 92，够用
  - [ ] 建议的命令（后台）：`mkdir -p results/runs/pull_images && nohup uv run python eval/swebench/pull_images.py --yes --verify --workers 2 --min-free-gb 35 > results/runs/pull_images/pull.log 2>&1 &`（`--min-free-gb 35` 是因为底线只在每次开始拉取前检查，并发在途的镜像可能再多占十几 GB）；之后用 `--dry-run` 和 `results/runs/pull_images/status.json` 看进度
- [x] 用 uv 安装 mini-SWE-agent（`uv tool install` 或放进 dev 依赖组，择一并记到 `docs/notes.md`，不用 pip），用 `--help` 确认它批量跑 SWE-bench 的命令，写进 `eval/baselines/mini_swe_agent.md`（2026-10-04）
  - 选择放进 dev 依赖组（`uv add --dev mini-swe-agent`，2.4.6，锁进 `uv.lock`，不与其他依赖冲突），理由记在 `docs/notes.md` §8；`tests/unit/test_no_agent_frameworks.py` 保证 `src/harness/` 不 import 它
  - `eval/baselines/mini_swe_agent.md` 含命令模板、参数说明、与 M0 的差异清单（网络默认联网、无资源上限、命令超时 60 秒等）、输出结构、判分命令和 `--help` 原文；`-c environment.run_args=[...]` 能解析成列表已离线验证
  - [x] 冒烟测试已完成（2026-10-05，`django__django-11066`，`deepseek/deepseek-flash`，`--network none`）：退出 `Submitted`，99 秒，25 次调用，生成 767 个字符的补丁；`swebench eval` 判分命令也验证通过（34 秒，`resolved: 1`；dev 题的冒烟，不是实验结果）。LiteLLM 记录的成本 $0.008483 是错峰价（标准价是 $0.016967），详见 `mini_swe_agent.md` §7
  - [ ] 文档里的 `{{STEP_LIMIT}}`、`{{COST_LIMIT}}`、容器资源上限要等 `configs/default.yaml` 创建后才能填；`-c 'model.model_kwargs.thinking={"type":"disabled"}'` 在 mini-swe-agent 里是否生效还没测

**验收**：`uv sync` 成功且 `uv.lock` 已提交；`.env` 可用；子集文件已提交；镜像开始拉取。
（2026-10-05 调整：镜像拉取不再是第 0 天的验收项，改为主 harness 实现后再拉、最晚第 2 天白天开始；`dev_5` 的 5 个镜像已就绪；配额已确认。）

---

## 第 1 天：Agent 核心能端到端跑通

**实现顺序（2026-10-06 与你商定；下面的清单是勾选用的，不规定顺序，实际顺序如下）**

1. **类型、最小配置、追踪、成本**：先建所有层都要用的纯逻辑（响应对象、配置模型、JSONL 追踪、固定标准价成本公式）。追踪最先建，因为每一层都要写 span，晚建就得回头补埋点。
2. **沙箱**：最容易踩坑的外部系统，早暴露问题（`exec` 没有超时、`git diff` 默认不含新建文件）。集成测试用 `python:3.12-slim` 加 git 的小镜像，不调用 LLM。
3. **LLM 客户端和回放**：把已核实的结论落成代码（重试分类、成对的上限和超时、截断、失败尝试的记录）；测试复用之前的本机假服务器场景。
4. **工具和注册表**：每个工具独立，输出处理要确定。
5. **主循环、状态、限制、提示词**：先定 LLM 和沙箱两个窄接口，主循环对着接口写，用脚本化的假 LLM 和假沙箱把所有终止原因测完（零花费）。
6. **CLI**：只负责装配。
7. **toy_repo 和端到端测试**：用 flash 在 toy_repo 上做第一次真实调用并录制，再切回放；然后在 `dev_5` 里挑 2 道题（`django__django-11066`，加一道 sympy、sphinx 或 scikit-learn 的）手动跑，验收。

**与清单的差别**：① 单元测试不集中放最后，每个模块写完就测（CLAUDE.md §8）；② 追踪最先建；③ 沙箱放在 LLM 客户端前面；④ 提前了两处第 2 天的内容：**最小配置模型**（主循环需要 `thinking`、`max_tokens`、`timeout_s` 和各类上限，并要记在 `run_start`；消融 YAML 和差异打印仍在第 2 天）和 **`llm/cost.py`**；⑤ 上下文管理和权限层只放接口占位（上下文"什么都不做"、权限层"全部放行"），第 2 天直接接上。

**已定的做法**：自己写简洁的 system prompt，不照搬 mini-swe-agent；对话历史只追加，动态信息（步数、剩余预算）在消息创建时就写进去并永久保存；模型只回文字、不调用工具按格式错误计数；调试阶段用 flash，最后的验收用 v4-pro；单题花费很小，不触发"超过 10 题要确认"的规则。

- [x] `llm/client.py`（2026-10-06）：基于 LiteLLM 的异步客户端，包含重试（429、5xx、`Timeout`、不可用的 200；最多 3 次尝试，指数退避加抖动，429 遵守 `Retry-After`，记录 `retry_reason`，用尽后 `env_error`，见 CLAUDE.md §5.1）、成本计算、token 与缓存命中统计
  - 2026-10-06 决定的调用参数（见 `docs/notes.md` §5）：非流式；`thinking`、`max_tokens`、`timeout_s` 来自配置并记在 `run_start`（思考开 65536 / 900 秒，思考关 16384 / 300 秒）；`finish_reason=length` 不重试、半截工具调用不执行、计入 `truncated_outputs`，连续 3 次记为 `format_error`（详情 `output_truncated`）；`insufficient_system_resource` 和 `aborted` 重试并丢弃半截内容（`retry_reason` 取值 `interrupted`）；失败的尝试也写 span（`usage` 为空），另报 `failed_attempts`、`retry_rate`、`wasted_prompt_tokens_est`，预算保护把估计也算进去；每次尝试之前要检查任务的墙钟时间上限（最坏情况 3 次 × 900 秒 = 45 分钟）；`context.budget_tokens` 只算输入；退避初始值基数 4 秒、倍数 3、上限 60 秒、加抖动
  - 已核实的约束（见 `docs/notes.md` §5）：重试按（异常类型、状态码、响应形状）判断，不能只看状态码或类名（错误密钥是 `BadRequestError` 但状态码 401，畸形 200 的状态码是 200，缺 `choices` 的 200 被映射成 500）；LiteLLM 没有隐藏重试；`Retry-After` 在 `e.litellm_response_headers`；每次调用显式传 `thinking`；`finish_reason=length` 且内容为空要当成失败；成本用固定的标准价公式计算，不依赖时钟，不调用 `completion_cost`；固定 `LITELLM_LOCAL_MODEL_COST_MAP=True`
- [x] `llm/replay.py`（2026-10-06）：录制和回放两种模式
  - 请求的键由模型、`thinking`、`max_tokens`、工具和**除工具结果内容外**的全部消息决定；工具输出另做规范化摘要比对（去掉耗时、临时路径、地址、时间戳、`ls` 日期），不一致只报告到 `ReplayClient.mismatches`，严格模式才报错。原因：`find`、`ls` 的输出顺序取决于宿主机文件系统，换机器（如 CI）会变；端到端测试断言真实结果（沙箱里项目自己的测试通过）兜底
  - 加载仓库时固定文件修改时间和基础提交的日期，使同一仓库两次加载逐字节相同（否则 `git log` 的哈希每次不同）
- [x] `sandbox/docker_sandbox.py`（2026-10-06）：`exec`、`read_file`、`write_file`、`git_diff`；断网；资源限制；用 `finally` 保证清理容器
  - 实测并处理的坑：`exec` 没有超时（用容器内的 `timeout`）；超时后残留僵尸进程占 `pids_limit`（容器加 `init=True`）；`git diff` 默认不含新建文件，且同大小、同一秒内的覆盖写入会被 git 的 stat 缓存误判为未变化（每次从 HEAD 重建临时 index）；`python:3.12-slim` 没有 git（`tests/fixtures/toy_image/Dockerfile` 加 git，**在本机网络下构建用了 9 分 22 秒**，apt 很慢）；在真实的 django 镜像上验证了补丁含新建和修改的文件并能应用到干净检出，耗时 2–3 秒
- [x] `tools/`（2026-10-06）：bash、view、str_replace（只允许唯一匹配）、search（ripgrep，镜像里没有就回退到 grep）、todo、submit，以及 registry
- [x] `agent/`（2026-10-06）：主循环、状态、prompts、限制（步数、成本、时间），实现 FailReason
- [x] `tracing/tracer.py`（2026-10-06）：写 JSONL 格式的 span（每条立即落盘；另加了 `message` 类型，每条消息一个 span，崩溃也留得下完整对话，供人工分析失败轨迹）
- [x] `cli.py`（2026-10-06）：实现 `harness run`（`--repo` 本地仓库或 `--instance` SWE-bench 实例，`--record` / `--replay`；没有创建 `configs/default.yaml`，那是第 2 天的内容，不带 `--config` 时用代码里的开发默认值）
- [x] `tests/fixtures/toy_repo/`（2026-10-06）：一个带 bug 的小仓库，包含 1 个会失败的测试（测试只用标准库 `unittest`，因为沙箱断网）
- [x] 单元测试（2026-10-06，全套 195 个）：str_replace 的边界情况、截断逻辑、限制检查、registry、重试分类（用本机假服务器覆盖 429、5xx、超时、断连、非 JSON/截断/缺 `choices` 的 200，`insufficient_system_resource` / `aborted` 的 200（重试并丢弃半截内容），以及 4xx 和 `finish_reason=length` 不重试且半截工具调用不执行）
- [x] 端到端测试（2026-10-06）：在 toy_repo 上用 flash 录制了一次真实运行（5 步，约 $0.001），再用回放通过测试，并断言沙箱里项目自己的测试从失败变成通过
- [x] 手动在 `dev_5` 的 2 道题上跑通，能生成补丁（2026-10-06：`django__django-11066` 和 `sympy__sympy-16886`，flash 和 v4-pro 各跑一遍；补丁都是非空的，用官方 `swebench eval` 判分都是 resolved，**这是 dev 题的开发检查，不是实验结果，不进报告**）

**验收（2026-10-06 全部达成）**
- [x] `uv run pytest -q` 全部通过（195 个测试；2026-10-07 复核为 194 通过、1 跳过，跳过的是只在 `HARNESS_STRICT_REPLAY=1` 时运行的严格回放测试）；`ruff check` 和 `ruff format --check` 通过
- [x] toy_repo 能修复（回放测试断言项目自己的测试通过）
- [x] 2 道 SWE-bench 题能产生非空补丁，轨迹文件完整（每个工具调用恰好对应一条 tool 消息，`run_start` 到 `run_end` 俱全，`llm_call` 字段齐全）
- [x] 记录单题成本和平均步数：用默认配置（主模型 v4-pro，思考开，标准价）跑 `dev_5` 的 2 题，`django__django-11066` 15 步 / $0.0279，`sympy__sympy-16886` 11 步 / $0.0177，**平均 13 步、$0.0228**（只有 2 个样本，是开发检查，不是实验结果）；用 flash 跑同样两题是 11 步 / $0.0046 和 9 步 / $0.0020

---

## 第 2 天：可开关组件和评测流水线（**当晚开跑主实验**）

**范围（2026-10-07 与你商定）**：先做 `src/harness/` 的核心部分（上下文、权限层、配置、客户端参数）；`eval/` 下的流水线（`run_agent.py`、`score.py`、`aggregate.py`）、dev_5 冒烟、打 tag、开跑主实验放在核心之后；**镜像拉取不做**（你的决定，之后再开）。

- [x] `context/`（2026-10-07）：`tokens.py`（本地计数 + 服务端实报值锚定，误差实测 −2.2% 到 +7.3%，见 `docs/notes.md` §11.3）、`manager.py`（持有"视图"，`state.messages` 永不修改）、四种策略（`strategies/{none,truncate,summarize,hybrid}.py`，另有共享类型 `types.py`）；高低水位线一次压缩到位以保住缓存前缀；压缩不拆开工具调用和回复；摘要失败回退到清理输出；压缩成本计入 `cost_usd` 和预算保护；`compaction` span（压缩前后 token、动作、成本）
  - 可恢复截断：`truncate` 清理旧输出前先把模型当时看到的内容存到 `/tmp/harness/ctx_<n>.txt`（已被限长器存过的指向原来的 `out_<n>.txt`），占位符 `[output cleared: N chars, saved at <path>]`
  - 压缩触发有滞后：压缩后仍高于高水位线时，下一次压缩要等提示词增长一个水位带，上限为预算，避免每轮重跑摘要；摘要调用带任务的墙钟 `deadline`；`run_start` 记录实际摘要模型
  - 摘要走独立的 `LLMClient`（`MODEL_CHEAP`、思考固定关、`llm_call` span 带 `purpose=summary`）
  - 测试时发现并修了一个真 bug：回合数不足 `keep_recent` 时受保护区间算反，会让所有消息都可清理
- [x] KV 缓存友好的规则（2026-10-07）：单元测试覆盖连续两轮请求的前缀逐字节相同（管理器层和真实循环层各一个），工具定义排序且不变；压缩只在越过高水位线时发生
- [x] `no_submit` 的用法（2026-10-07 完成）：连续 3 次纯文字回复以 `no_submit` 终止（详情 `no_tool_call`），不计入 `format_errors`；纯文字回合打断连续格式错误的计数，带工具调用的回合重置纯文字计数。改动在 `agent/loop.py`、`agent/state.py`，`tests/unit/test_loop.py` 补了 4 个测试，全套 197 通过、1 跳过
- [ ] 回放客户端按录制的 usage 补写 `llm_call` span：**延后到第 4 天，不是砍掉**（CI 冒烟才用；`aggregate.py` 处理的是真实运行，真实运行本来就有这些 span）
- [x] 配置系统（2026-10-07）：`configs/default.yaml`；`configs/ablations/` 8 个文件（`ctx_none`、`bash_only`、`ctx_truncate`、`ctx_summarize`、`no_planning`、`budget_32k`、`budget_32k_none`（A6 与 A1 的组合）、`no_guard`（第 4 天安全评测用）），用 `extends:` 继承主配置；测试保证每个消融与主配置恰好只差预期的叶子键；`harness run` 启动时校验并打印与主配置的差异；沙箱资源上限进了配置（`sandbox.*`），因此进 `config_hash`
  - **步数、成本、墙钟上限是临时值**（100 步 / $1.0 / 1800 秒），要在 dev_5 冒烟后确认再冻结；B0 用同样的值
- [x] `security/`（2026-10-07，**按你选的 (a) 定稿**，不是"基础版"）：`configs/policy.yaml`（三级权限、`deny` 和 `ask` 规则、写入根目录、测试文件规则）、`security/policy.py`、`security/guard.py`；`guard_decision` span 带 `level`，`run_start` 带 `policy_hash`；83 个测试，其中 35 条是**必须放行**的日常命令（防误拦）。复核时发现并修了两处误拦：测试文件规则原先会拦根目录或 `/tmp` 下的 `test_repro.py`，且能跨行扫 heredoc 正文，现在只管 `tests/` 目录和 `conftest.py`、不跨行、豁免 `/tmp`。M0 用的就是这份文件，第 3 天不再改它（改了就要换 tag 并重跑 M0）；`ask` 在批量运行里按 `deny`，CLI 交互确认还没做
- [x] 客户端强度参数（2026-10-07）：`llm.reasoning_effort`，经 `extra_body` 传（见 `docs/notes.md` §11.2）；进回放键，所以 toy 录制重录了一次（flash，5 步，$0.0015）
- [x] **summarize 重做**（2026-10-11 起，调研与更正见 `docs/notes.md` §12；全部在 `v0.1-frozen` 之前完成，因为会改变 M0 的行为）
  - [x] 阶段 0：视图拆成对齐的 `view`（发给模型的）和 `original`（agent 当时看到的）两个列表；`truncate` 只改 `view`，`summarize` 在两处同步替换
  - [x] 阶段 1：摘要读 `original` 全文（修了 `hybrid` 摘要读到占位符的缺陷）；去掉写死的 1500/1000 裁剪；`reasoning_content` 进摘要输入（`summary_include_reasoning`，默认开）；仅在 `context_overflow`/`timeout` 时按 `summary_clip_chars`（4000）× `summary_clip_scale`（0.5）逐次收紧并重试（`summary_retries`=3）；响应不可用时也计入成本；`compaction` span 记录输入字符数、尝试次数、是否收紧；三处变异检查均变红
  - [x] 阶段 2（2026-10-11）：摘要提示重写（交接框架，7 节：Tried so far / Failed attempts / Current hypothesis / **Next steps** / **Tests** / Files modified / Useful facts，字数两档 700 / 200）；摘要消息末尾由代码拼"状态块"（新文件 `context/restore.py`，**不在 CLAUDE.md §4 的目录树里**）：`git diff --stat`（由补丁文本自己算，始终保留）+ diff 正文（≤`summary_diff_chars`）、todo、已落盘输出索引、读过或改过的文件路径（只给路径，不重读内容）。
    - 分隔与时效：状态块用固定标记 `## State as of step N` 与摘要正文隔开并注明"生成于第 N 步、可能过时"；**下一次摘要的输入不含旧状态块**（`summary_text_of`）；`compaction` span 里 `summary`（只含摘要模型的文字）与 `restored`（状态块各项）分开存放，所以 `summary_file_recall` 不会被状态块里的 diff stat 抬高
    - 索引有上限（你提的问题）：最多 `summary_index_entries`=20 条、每条 ≤120 字符，其余折叠成一行并提示 `ls /tmp/harness`；索引与读过的文件跨压缩累积在 `RestoreIndex` 里，摘要真正生效才提交；状态块整体受 `restore_tokens_cap()` 约束，超出时按固定顺序降级（路径 → 索引 → diff 正文 → todo，stat 始终保留）；最小模式只留 stat（和放得下的 todo）
    - 共用格式：`truncate` 的占位符与限长器的提示一样，改为共用一份定义加解析函数（`cleared_stub` / `cleared_output`，`saved_output`），索引从 `original` 取限长器提示路径、从 `view` 取 `ctx_N` 占位符路径
    - 28 个新测试；六处变异检查均变红（其中一处最初存活，原因是我的测试没有构造出区分两种实现的数据，已补；另一处最初因为编辑没有匹配上而静默失败，已发现并重做）
    - 真实核实（flash，一次）：用 `dev-pro-1` 的 django 轨迹强制压缩一次，摘要输入 10517 token、全文未裁剪，输出 1200 token，$0.0046；摘要含全部 7 节，状态块 339 token（上限 1000）；摘要里 3 个"读过的文件"和 diff 文件都能对上。n=1，只证明链路可用
  - [x] 阶段 3（2026-10-11，先于阶段 2 完成，顾问建议的顺序）：`keep_recent` 改为 `keep_recent_min_turns`（2）+ `keep_recent_ratio`（0.25，占 `budget_tokens` 的比例，**上限**，可配置）；每次压缩前先做规划（head、尾部、余量、五级：normal / tail_shrunk / minimal / unreachable / infeasible），head 在首次调用后取服务端实报值；`min_gain_ratio`（0.1），**超出预算时豁免**；压缩尝试被跳过时也更新滞后基准；`infeasible` 且已超预算时不调用摘要、直接 `context_overflow` 并写明原因；摘要字数两档固定值（700 / 200 词）；摘要和状态块的有效上限为"绝对值与比例取小"（128k：4096 / 2500；32k：2560 / 1600），配置加载时校验两者之和低于 `low_water × budget`；`compaction` span 增加 `plan_level`、`target_unreachable`、`head_tokens`、`tail_tokens`、`room_tokens`。同时修了一个已有缺陷：本地计数把回传的 `reasoning_content` 算进提示词大小，而服务端不计。21 个新测试，六处变异检查均变红
  - [x] 阶段 4（2026-10-11）：`tool_call` 和 `message` span 加 `step`（`compaction` span 的 `summary`、`restored` 阶段 2 已加）；新建 `eval/report/aggregate.py`（纯函数 + 命令行，写 `results/runs/<id>/metrics.json`）：CLAUDE.md §6 里能从轨迹算出的指标（成本均值和 P50、token、`cache_hit_rate`、延迟 P50/P95、步数、`overflow_rate`、`fail_taxonomy`）、`resolve_rate` 及 Wilson 区间（已解决的题号从 `<run_dir>/resolved.json` 或 `--resolved` 读，score.py 写好后对接），以及压缩指标 `steps_after_compaction`、`post_compaction_repeat_rate`、`summary_file_recall`（口径：压缩前用 `str_replace` 改过的文件里摘要正文提到的占比，而不是最终补丁里的文件）、`compaction_cost_share`；`--baseline` 做同题配对对比（只看发生过压缩的题，重复率与基线在同一步数处比较，解决情况四格表）。离线探针 `eval/report/summary_probe.py`（强制压缩一次，用真实摘要模型）。23 个新测试；五处变异检查均变红（其中一处最初存活：没有测试覆盖"正好在边界步的调用"，已补）。同时发现并删掉两处恒真断言（`or True`）
    - 探针在 `dev-pro-1` 的两条真实轨迹上各跑一次（共约 $0.004）：django 与 sympy 的摘要都提到了改过的文件，文件召回率 1.0；两个任务都只改了一个文件，**区分度不够，只证明链路可用**
  - [x] 阶段 6（2026-10-12）：`docs/notes.md` §12 已写；`CLAUDE.md` 已按你确认的 diff 更新（§4、§5.1、§5.2、§5.3、§5.6、§5.7、§5.8、§6、§7、§8），应用前对着代码重新核对了一遍（配置键与默认值、span 字段、规划等级、两条命令能运行、两个行为陈述），全部一致；新增"待同步清单"（见下一节），§8 加了登记规则
  - [-] 阶段 5（摘要模型对照、始终开启的遮蔽窗口、`summarize` 落盘）：**你决定暂时不做**
- [x] `eval/swebench/run_agent.py`（2026-10-12）：顺序运行；按 `instance_id` 断点续跑（`preds.jsonl` 里有这一行才算做完，半截轨迹先挪开再重跑）；一个 `run_id` 只对应一个配置（`config_hash` 不一致就拒绝）；工作区有未提交改动时拒绝（`--allow-dirty` 仅限开发运行并记录）；开跑前检查镜像在不在（不拉取）、打印预计与最坏花费，超过 10 题要确认，累计花费到 `MAX_TOTAL_COST_USD` 的 80% 自动停；`env_error` 记入并标记，`--retry-env-errors` 重跑它们；每题的装配抽成 `cli.run_one`，`harness run` 与批量运行器共用。**并行是 TODO**（`--workers` 大于 1 报错并打印本机上限）。输出 `preds.jsonl`、`status.jsonl`、`run_meta.json`、`metrics.json`
- [x] `eval/swebench/score.py`（2026-10-12）：调用 `swebench eval verified`（`-p preds.jsonl -i <我们的题号> --report-dir .`，在运行目录里跑，`logs/` 因此落在被 gitignore 的目录）；判分前核对数据集最新 revision 与 `subsets_meta.json` 一致，不一致就不判；报告按 `*.<run_id>.json` 找；写出 `resolved.json`、`score.json`（每个题号按判分器的结论归类）并更新 `metrics.json`。**判分器没法判的题**（基础设施失败、出错、空补丁）与"判了但没修好"分开，`fail_taxonomy` 里是 `scoring_error` / `submitted_empty_patch`，不并入 `submitted_but_failed`。测试用 B0 冒烟的真实报告做固定数据，假 `swebench` 跑通整条流程；还没有在真实运行上端到端跑过（等 dev_5 冒烟）
- [x] `eval/report/aggregate.py`：汇总每次运行的指标，包括 Wilson 置信区间（见 summarize 重做的阶段 4；B0 的轨迹格式不同，读不了，见下面 B0 一项）
- [x] B0 对齐（2026-10-12）：新增 `eval/baselines/run_b0.py`（**不在 CLAUDE.md §4 的目录树里**，已登记待同步）：从 `configs/default.yaml` 生成 `mini-extra swebench` 命令，步数 / 成本 / 墙钟 / 命令超时 / 容器上限 / 思考 / `reasoning_effort` / `max_tokens` / 请求超时都来自那份配置，不再手写；`mini_swe_agent.md` 的占位符已清掉。**`model_kwargs` 是否生效已实测**：用 mini-swe-agent 自己的 `LitellmModel` 对着本机假服务器，请求体里确有 `thinking`、`reasoning_effort`（经 `extra_body`）、`max_tokens`，`timeout` 在 `litellm.completion` 的参数里。已核实 mini-swe-agent 的"一步"= 一次模型调用（`n_calls`）。`convert` 把 mini-swe-agent 的输出（`mini/preds.json` + `*.traj.json`）转成 `preds.jsonl`、`status.jsonl` 和只含 `run_start` / `run_end` 的轨迹，所以 `score.py`、`aggregate.py` 和花费统计都读得了 B0；B0 的成本按它记录的 token 用我们自己的标准价重算（mini-swe-agent 自己的数字另存为 `b0_reported_cost`）。已知差异：B0 的 `wall_seconds` 是第一次到最后一次响应的间隔，略短；B0 没有逐步 span，所以压缩指标不适用
- [x] dev_5 冒烟（2026-10-12，**开发运行，不是实验结果，不进报告**；`--allow-dirty`，因为工作区有你的学习笔记没提交）：M0（`M0-dev`）、B0（`B0-dev`）各跑全部 5 题，都经 `score.py` 官方判分，**都是 5/5 解决**，没有基础设施故障 / 空补丁 / 出错。M0：$0.5453，平均 37.8 步，缓存命中率 97.9%，提示词峰值 6.8k 到 61.9k；B0：$0.2666（按 token 重建，与 mini-swe-agent 自己报的数 5 题都在 4 位小数内一致），平均 21 步。另做两次真实压缩冒烟：`smoke-compaction`（`configs/smoke/compaction.yaml`，预算 10k）5 题全解决，4 题共发生 30 次压缩，没有一次回退或摘要失败；`smoke-32k`（A6 配置）2 题，`django-15930` 压缩 5 次、全是 `normal` 级别的清理，没有摘要。完整状态块的真实样例来自探针（`django-15930` 第 70 步：44167 → 3665 token，$0.0177）。**这一步发现并改了一个配置**：`django-15930` 在 10k 预算下用了 109 步（M0 是 79 步），超过原来的 100 步上限，所以 `max_steps` 改为 150（依据见 `default.yaml` 注释）；预算表和 `MAX_TOTAL_COST_USD` 见下面的日志。没有发现需要修的 harness bug
- [x] **摘要大小与提示词估计的修复**（2026-10-13 至 14，都在冻结前；实测与更正见 `docs/notes.md` §17）
  - 摘要按档位实测的 token 数预留（full 1950 / minimal 1050），字数、预计 token、minimal 档状态块上限都做成配置键；从文件加载的配置要求摘要上限 ≥ full 档预计值（提交 `8af8fea`、`43ea787`）
  - 回传的推理计入 `prompt_tokens`（真实对照：2064 字符多出 492 token），本地计数改为计入；压缩后的估计误差从 −16%~+42% 降到 −8%~+5%，有锚的平均绝对误差 1.42% → 0.79%（`2e8e26b`）
  - 压缩流程：规划按预计值预留；先生成状态块，门槛用它的实际大小；hybrid 清理后重新判断是否超预算；跳过的尝试写 `compaction_skipped` span；`compaction` span 记录比例并标出异常（`43ea787`）
  - 报告：压缩后的大小用下一次请求的服务端数字，带来源标记；`monitoring` 一节（`30f1920`）
  - 新增 24k 压缩冒烟配置（`efbded7`）；跳过档位过渡区和 `tail_cap` 依据两项记入"后续 TODO"
- [x] **M0 基本符合预期**（2026-10-13 与你商定的验收标准，满足后才拉镜像、打 tag；2026-10-14 全部满足）
  - [x] 工作区干净，在候选冻结提交上重跑 dev_5 的 M0（`M0-dev-v2`，提交 `ed6625a`，配置哈希 `6ea20eaecc69`，$0.458）：官方判分 5/5 解决，没有 `env_error`、`format_error`、输出截断或失败重试；权限层 0 次拦截（全部放行，没有可复核的拦截；`search` 的放行不带 `rule_id`，记为小问题）。128k 下没有触发压缩（提示词峰值 8.0k–60.6k），压缩路径由 10k / 24k 冒烟覆盖
  - [x] 压缩路径在真实运行里都走过：minimal（10k 冒烟）和 normal 带完整状态块（24k 冒烟），没有摘要失败或回退（2026-10-14，`docs/notes.md` §17.5）
  - [x] 摘要预留与实际的差距可解释（§17.1）；压缩后大小的偏差已解释并修复，报告改用服务端数字（§17.2–17.4）
  - [x] `run_b0.py` 的两道保护（`docs/notes.md` §16.C1；2026-10-14，`4d9011a`）：拒绝有未提交改动的工作区（开发运行可用 `--allow-dirty` 并记录），同一 `run_id` 的配置哈希必须一致；用同一份配置跑 B0-dev 能对上。（B0 运行中的花费进预算守卫是另一件事，按你的决定和并行化一起记入"后续 TODO"）
- [ ] **打 tag `v0.1-frozen`**
- [ ] 晚上：在 `verified_50` 上跑 M0，同时跑 B0（mini-swe-agent，同一模型、同一题目子集）；先打印预计花费并等你确认；本机实测 15.6GB 内存（不是原来写的 WSL 12GB），worker 数 × 单容器内存上限（现在 4g）加判分容器不能超，M0 和 B0 是否并行要先定
- [~] 拉 `verified_50` 的镜像（2026-10-15 你的决定：先拉按仓库分层抽出的一半 `verified_25`）：**25/25 完成**，每个都在断网容器里核对过 `/testbed` 在基准提交、工作区干净；约 9 分钟，没有被限流；可用空间 141.6 → 113.4 GB（约 28GB，比按 §7 估的 45–50GB 少，同仓库共享的层更多）。剩下 25 个待定。原记录：拉剩余 45 个镜像（**你的决定**：M0 基本符合预期之前不拉；`verified_50` 与 `dev_5` 不重合，本地只有 5 个 dev 镜像；按 §7 的实测约需 90–100GB，现有 133GB 可用）

**验收**：dev_5 全流程能生成判分结果；tag 已打；M0 和 B0 已开跑。

**如果进度落后**：`summarize` **不能推迟**：M0 用的是 `hybrid`，缺了 `summarize` 就不是 M0，冻结后再补要重跑 M0（2026-10-07 你的决定，原来写的是"可以推迟到第 3 天上午"）。M0 今晚必须开跑。

---

## 第 3 天：分析主实验，跑消融，完善安全层

- [ ] 给 M0 和 B0 判分，用 `failure_taxonomy.py` 生成失败分类
- [ ] 人工看 5 条失败轨迹，写进 `docs/failure_analysis.md`：是什么现象、根因、是 harness 的问题还是模型的问题
- [ ] 如果发现 harness 的 bug：修复 → 打 `v0.2-frozen` → 在 `results/CHANGELOG.md` 记录 → **重跑 M0**
- [ ] 按 P0、P1 的顺序跑消融实验 A1–A6（可以通宵并行）
- [-] 完善安全层：**移除**，原因：已在第 2 天定稿（你选 (a)，见第 2 天 `security/` 一项）；第 3 天只做注入用例和蜜罐，不再改 `policy.yaml`，改了要换 tag 并重跑 M0
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
  - 第 1 天遗留给这一项的事：CI 要先构建 `harness-toy` 镜像（`tests/fixtures/toy_image/Dockerfile`，本机网络下 apt 用了约 9 分钟，CI 网络通常快得多，但仍要留出时间，最好缓存），沙箱的集成测试和回放的端到端测试都需要它；`ReplayClient` 目前不写 `llm_call` span，回放的轨迹只有 `run_end` 的合计，见 `docs/notes.md` §10
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

## 后续 TODO（不在当前冻结范围内）

**注意**：凡是改 harness 行为的项，如果在 `v0.1-frozen` 之后做，受影响的配置都要换 tag 重跑，并记入 `results/CHANGELOG.md`（CLAUDE.md §2 第 4 条）。

- [ ] **并行化**（2026-10-13 记录）：`run_agent.py --workers` 大于 1 现在报错。一并处理：
  - B0 运行中的花费不在 80% 预算守卫里（`docs/notes.md` §16.C6）：开跑前从余量里先扣掉 B0 的最坏花费（`--reserve-usd`），或在运行中读取 B0 的花费
  - 资源上限：`max_sandboxes` 按单沙箱内存和 CPU 上限计算，判分容器另留余量（`docs/notes.md` §13）
- [ ] **摘要档位的过渡区**（2026-10-13 记录，效果优化时再考虑）：`_plan` 只有长版和短版两档。最短尾部一超过长版的门槛，摘要就从 700 词直接降到 200 词；而在过渡区（短版放得下、长版放不下）里，剩下的空间既给不了尾部（已超过 `tail_cap`），也给不了摘要，就这样空着
  - 过渡区大小（按实测预留，head 取 1573）：128k 是最短尾部 57977–61177，M0 实际碰不到；32k 是 10877–13177，浪费 2300 → 0 token，**主要影响 A6**
  - 候选方案：A 维持两档；B 摘要大小连续变化（字数换算 token 不可靠，不推荐）；**C 固定阶梯**（例如 700 / 450 / 200 词，每档有自己的实测预计值，选放得下的最大一档；中间档的预计值要用探针实测，约 $0.03）；D 先把最少轮数从 2 降到 1（没有证据，不推荐）
  - 收益未经验证：先看 A6 的结果里按档位统计的压缩前后表现，再决定
- [x] **摘要预计值的标定**（2026-10-14 记录，同日完成，A2 提前做了）：探针有了 `calibrate`（多条轨迹 × 多个字数，拟合并输出配置值）；预计值改为按字数的直线；标定条件（摘要模型、提示哈希、字数范围）记在配置里，开跑前核对。见 `docs/notes.md` §19
- [ ] **`keep_recent_ratio`（`tail_cap`）的 0.25 没有依据**（2026-10-13 记录）：阶段 3 定的设计值，没有记录理由。可以用真实轨迹里最近几轮的实际大小分布重新定，或者在注释里写明"未经验证的设计值"

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

## 待同步清单（设计或代码改了、CLAUDE.md 还没跟上的）

**用法**：改了设计或代码，且影响 CLAUDE.md 里写到的规则、名字或字段时，**当场**在下表登记一行；同步时先把 diff 给你看，应用后勾掉并写日期。陈述以代码为准（读代码，不凭对话记忆）。CLAUDE.md 只写规则和影响实验的配置键，字段清单以 `config.py` 和代码为准。

| 登记日期 | 变更 | CLAUDE.md 位置 | 状态 |
|---|---|---|---|
| 2026-10-07 | `llm.reasoning_effort`，必须经 `extra_body` 传 | §5.1、§5.3 表 | [x] 2026-10-12 |
| 2026-10-07 | 回放键含 `reasoning_effort` | §5.8 | [x] 2026-10-12 |
| 2026-10-07 | 沙箱资源上限进配置（`sandbox.*`） | §5.3 表 | [x] 2026-10-12 |
| 2026-10-11 | 视图与原文两个等长列表、规划五级、滞后基准 | §5.3 | [x] 2026-10-12 |
| 2026-10-11 | `summarize` 读原文（含推理，可关）、失败后收紧重试；`hybrid` 的摘要读清理前原文 | §5.3 | [x] 2026-10-12 |
| 2026-10-11 | 摘要末尾的状态块（代码生成，与摘要正文分开） | §5.3 | [x] 2026-10-12 |
| 2026-10-11 | `keep_recent` → `keep_recent_min_turns` + `keep_recent_ratio`；`min_gain_ratio`；`min_clear_chars`；水位线 | §5.3 表 | [x] 2026-10-12 |
| 2026-10-11 | 提示词大小不计回传的 `reasoning_content` | §5.3 | [x] 2026-10-12 |
| 2026-10-11 | CI 回放必须用不压缩的配置 | §5.8 | [x] 2026-10-12 |
| 2026-10-11 | span 新字段（`step`、`purpose`、`level`、`plan_level` 等、`compaction` 的摘要字段） | §5.7 | [x] 2026-10-12 |
| 2026-10-11 | 四个压缩指标；`tokens_in` 不含压缩调用 | §6 | [x] 2026-10-12 |
| 2026-10-11 | 新文件 `restore.py`、`strategies/types.py`、`summary_probe.py` | §4 | [x] 2026-10-12 |
| 2026-10-11 | `aggregate.py`、`summary_probe.py` 的命令 | §7 | [x] 2026-10-12 |
| 2026-10-12 | 漂移：`deny_patterns` 实际是 `deny` / `ask` 两组规则 | §5.6 | [x] 2026-10-12 |
| 2026-10-12 | 漂移：`cached_input_tokens` 实际叫 `cached_tokens` / `tokens_cached` | §5.2 | [x] 2026-10-12 |
| 2026-10-12 | 漂移：回放键字段列表漏了 `reasoning_effort` | §5.8 | [x] 2026-10-12 |
| 2026-10-12 | 登记规则本身 | §8 | [x] 2026-10-12 |
| 2026-10-12 | `run_agent.py` 的示例命令不再有 `--workers 4`（并行是 TODO，大于 1 会报错） | §7 | [x] 2026-10-12 |
| 2026-10-12 | 新文件 `eval/baselines/run_b0.py`（B0 命令生成与输出转换）；B0 的命令示例 | §4 目录树、§7 | [x] 2026-10-12 |
| 2026-10-12 | 新目录 `configs/smoke/`（冒烟配置，不是消融）；`aggregate.py` 的 `--baseline` 用法已验证 | §4 目录树 | [x] 2026-10-12 |
| 2026-10-12 | **瘦身**：CLAUDE.md 只留规则、不变量和指标名字；实现细节搬到代码的模块说明（`tracer.py` 的 span 字段目录、`tokens.py` 的估计误差、`docker_sandbox.py` 的约定）；默认数值改为指向 `config.py`；§8 加了"往本文件加内容之前先问"的标准 | §5.1、§5.3、§5.4、§5.5、§5.6、§5.7、§6、§7、§8 | [x] 2026-10-12（26 973 → 24 761 字节，约 8.2%，比预期小：文件的大头本来就是规则；理由和取舍见 `docs/notes.md` §15） |
| 2026-10-15 | 新子集 `verified_25.txt` + `verified_25.meta.json`（从 `verified_50` 按仓库分层派生，种子 42，`select_subset.py derive`） | §4 目录树的 `subsets/` | [x] 2026-10-15 |
| 待办 | CLI 交互确认（`ask`）实现后 | §5.6 删掉"尚未实现" | [ ] |
| 待办 | 回放补写 `llm_call` span 后（第 4 天） | §5.8 删掉"待做" | [ ] |
| 待办 | `score.py` 写好后：`resolved.json` 约定（现在只在 `docs/notes.md` §12.6） | §6 或 §7 | [ ] |
| 待办 | 冻结前定下 `default.yaml` 的步数 / 成本 / 墙钟上限（现在是临时值） | 不进 CLAUDE.md，只记 plan.md | [x] 2026-10-12（150 步 / $2 / 1800 秒，依据在 `default.yaml` 注释） |
| 2026-10-13 | 提示词估计的误差：有锚时约 ±8% 仍成立（实测更小），压缩过程中没有锚；回传的推理计入 `prompt_tokens`，本地计数也计入（`docs/notes.md` §17.2–17.3） | §5.3 不变量里"约 ±8% 的误差（§11.3）" | [x] 2026-10-14 |
| 2026-10-13 | 新 span 类型 `compaction_skipped`（什么都没做的压缩尝试，不计入压缩次数） | §5.7 span 类型列表 | [x] 2026-10-14 |
| 2026-10-13 | `compaction` span 里压缩前后的 token 数是估计值；报告里压缩后的大小用下一次请求的服务端 `prompt_tokens`（`aggregate.py`，`tokens_after_source`） | §5.7 `compaction` 一条 | [x] 2026-10-14 |

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

**当天花费**：$0.015582（LiteLLM 价格表估算，59 次成功的 DeepSeek 调用：步骤 1–3 共 4 次 $0.001159，步骤 4 共 30 次 $0.005940，两者是高峰时段的标准价；mini-swe-agent 冒烟 25 次 $0.008483，是错峰价（标准价是 $0.016967）；另有 2 次被拒绝的调用（错误密钥、不存在的模型名）不计费。**混合了两种费率，不能直接相加比较**；没有和 DeepSeek 控制台对账）

**LiteLLM + DeepSeek 验证（步骤 1–3，同日稍后）**
- 全部通过，结果表见 `docs/notes.md` §5：密钥有效，模型名存在，不走 `/beta`，函数调用能解析（`arguments` 是合法 JSON）
- 发现：**思考模式默认开启**，`reasoning_content` 随响应返回，思考 token 计入 `completion_tokens` 并按输出价计费；`usage` 同时带 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`
- 对 `llm/client.py` 的影响：消息历史要保留 `reasoning_content`；`max_tokens` 要给思考留余量；是否统一关闭思考要等步骤 4 决定
- 验证脚本放在 scratchpad，没有进仓库

**LiteLLM + DeepSeek 验证（步骤 4，同日稍后）**
- `reasoning_content` 回传没有被强制：默认思考模式下去掉它、LiteLLM 注入空格占位、3 轮连续工具链，共 4 条链和 7 组两轮对话都没有报错；回传的内容不计 `prompt_tokens`（**2026-10-13 已被推翻：计入，见 `docs/notes.md` §17.2**）
- 默认思考模式并不总是思考（reasoning token 0–58 波动）；`thinking: disabled` 可用；默认模式下 `max_tokens=16` 得到 `finish_reason=length` 且内容为空
- 两种模式的 `prompt_tokens` 不同（flash 308 / 284，v4-pro 371 / 293），推断缓存不能跨模式共享，一次运行内不要切换；`cached_tokens` 都是 128 的倍数
- 对 `llm/client.py` 的影响：每次调用显式传 `thinking`（配置项）；保留 `reasoning_content` 但压缩时可以丢；`length` 且内容为空要识别成失败；`thinking` 默认开还是关等 dev_5 上的对比再定

**LiteLLM + DeepSeek 验证（步骤 7–9，同日稍后）**
- 步骤 7：错误密钥 → `BadRequestError`（`status_code=401`）；不存在的模型名 → `BadRequestError`（400），消息列出支持的名字
- 步骤 8（本机假服务器）：LiteLLM 没有隐藏重试（每种情形服务器只收到 1 个请求）；429/500/502/503/断连/缺 `choices` 都映射到 429 或 5xx 的状态码；`Timeout` 是 408；畸形的 200 是 `APIError(200)`；`Retry-After` 在 `e.litellm_response_headers`
- 步骤 9：mini-swe-agent 对 `django__django-11066` 冒烟成功，`--network none` 下正常工作，`swebench eval` 判分命令验证通过；轨迹里的成本和 `usage` 字段位置已核实；容器清理是异步的，没有残留
- **意外发现**：LiteLLM 的 `completion_cost` 会按**当前 UTC 时间**使用错峰价（标准价的 50%，工作日 UTC 10:00–24:00 等时段和整个周末）；冒烟的成本因此恰好是按标准价手算的一半，且同一条记录在不同时间重算会得到不同金额。这会让不同时段跑的配置之间的 `cost_per_task` 偏差最多 2 倍，要在 `llm/cost.py` 里用固定费率的自有价格表解决
- 步骤 5、6 按你的决定暂不做

**决定：成本口径（同日，提交之后；先决定只比 token，随后改为两者都保留，以后者为准）**
- 你决定**同时保留 token 指标和美元成本，美元成本用固定的高峰价，去掉峰谷影响**。这样 CLAUDE.md 的预算规则、`cost_per_task`、`FailReason.cost_limit` 都保持有效，不需要改
- 实现：自有 harness 用公式 + 标准价表（第 1 天的 `llm/cost.py`，不调用 `completion_cost`）；B0 用 `LITELLM_MODEL_REGISTRY_PATH` 指向 `eval/baselines/litellm_registry.json`，让 mini-swe-agent 自己的成本也是标准价。公式、覆盖文件都已离线验证，并有单元测试（29 个测试全过）
- 提醒：不同模型的 token 计数口径可能不同（同一句 ping，flash 31、v4-pro 84），跨模型对比 token 数时优先用比率类指标

**与 plan 的差距**

| 第 0 天验收项 | 状态 |
|---|---|
| `uv sync` 成功且 `uv.lock` 已提交 | 达成（`uv sync --frozen` 通过，`uv.lock` 在 `e1aff8e` 里） |
| `.env` 可用 | **部分达成**：格式和 LiteLLM 解析已核实（`DEEPSEEK_*` 已改好）；没发起过真实调用；`MAX_TOTAL_COST_USD` 还是占位值 20；`.env.example` 未同步 |
| 子集文件已提交 | **未达成**：文件已生成但未跟踪，配额还没确认，所以不算冻结 |
| 镜像开始拉取 | 已调整：5/55（dev 的 5 个）已就绪，其余按新时机拉 |

- 第 0 天 6 个顶层项：4 个完成、2 个进行中（建仓库：`pyproject.toml` 的项目名和描述没改；拉取：50 个未拉）。mini-swe-agent 的冒烟测试已跑通
- 第 1–5 天共 43 个顶层项：**0 个开始**。`src/` 目录不存在；`configs/` 只有 `subsets/`（没有 `default.yaml`、`policy.yaml`、`ablations/`）；`eval/swebench/` 只有 2 个文件（缺 `run_agent.py`、`score.py`）；`eval/report/`、`eval/security/`、`docker/`、`.github/` 都不存在
- 自 `e1aff8e` 之后的改动都**没有提交**：`.gitignore`、`pyproject.toml`、`plan.md`、`docs/notes.md`、`docs/learning/`、`configs/`、`eval/`、`tests/`
- 总花费 $0，距离预算上限还很远

**明天先做（按顺序）**
1. （已完成，2026-10-05）子集配额已确认，保持当前值
2. （`.env.example` 不同步，你的决定）验证计划的步骤 5、6 暂不做；重试策略已定为方案 B（见 2026-10-05 的重试策略补充）
3. （`pyproject.toml` 的项目名和描述暂不管，你的决定；改动已全部提交）
4. 开始第 1 天：`llm/client.py`、`llm/replay.py`、`sandbox/docker_sandbox.py`；镜像拉取等主 harness 实现后再动手
5. 等 `configs/default.yaml` 确定后，回填 `mini_swe_agent.md` 里的 `{{STEP_LIMIT}}`、`{{COST_LIMIT}}` 和容器资源上限

### 2026-10-05（补充：配额与拉取时机）

**决定（你的）**
- 子集配额**保持当前值**（django 20 / sympy 15 / sphinx 10 / scikit-learn 10），已记入第 0 天的选题一项。这只是锁定决定，不是打标签；`v0.1-frozen` 仍在第 2 天主实验前打
- 剩余 50 个镜像**不现在拉，等主 harness 实现后再拉**。约束不变：第 2 天晚上开跑主实验前必须拉完（按单个 84–210 秒的实测粗估，用 2 个 worker 大约一小时多，网络状况会变），所以最晚第 2 天白天开始；建议在第 1 天验收（harness 能在 `dev_5` 上端到端跑通）之后就开始

**当天花费**：无新增

### 2026-10-05（补充：提交与 .env、pyproject 的决定）

- 已提交：`aafd242`（配额确认、拉取推迟）；工作区干净
- 决定（你的）：`pyproject.toml` 的项目名和描述**暂不处理**；`.env` 和 `.env.example` **不同步**，`.env.example` 保持适用范围最广的 `OPENAI_*` 格式
- 影响：走通用 `openai/` 路线时 `thinking` 参数要用 `extra_body` 传，`drop_params=True` 会静默丢掉它；`completion_cost` 对 `openai/` 前缀失败，只影响 mini-swe-agent 的成本统计；已写在"申请 API Key"一项下面
- `Timeout`（408）和畸形 200 是否重试：问题清单已列出；你随后决定采用方案 B（也重试这两类），见同日后面的补充

**当天花费**：无新增

### 2026-10-05（补充：重试策略决定）

- 你决定采用**方案 B**：除 429 和 5xx 外，也重试客户端超时和不可用的 200。CLAUDE.md §5.1（重试规则）和 §5.7（`llm_call` span 多一个重试原因）已修改；`docs/notes.md` §5 记录了问题清单、策略和还没定的细节
- 策略：总共最多 3 次尝试；指数退避加抖动；429 遵守 `Retry-After`；按（异常类型、状态码、响应形状）判断；4xx 不重试；记录重试次数和 `retry_reason`；用尽后 `env_error`；重试等待计入墙钟时间和延迟
- 其中"重试等待计入墙钟时间和延迟"是我取的默认做法（那是真实流逝的时间），不是你明确说过的，不同意的话再改
- 还没定：`llm.timeout_s` 的取值、是否用流式输出
- 第 1 天写 `llm/client.py` 时要补重试分类的单元测试（已加到第 1 天的单元测试一项）

**当天花费**：无新增

### 2026-10-06（补充：输出上限、超时、流式和失败尝试的决定）

**完成了什么**
- 调研了主流 harness（SWE-agent、mini-swe-agent、Aider、Claude Code、Codex、OpenHands，以及 OpenAI 和 Anthropic 的 SDK）的超时、重试、流式、输出上限和失败尝试的 token 处理，并做了几次很小的真实调用测截断行为和生成速度；数据、来源和可信度记在 `docs/notes.md` §5
- 你决定全部按我的方案，已写进 CLAUDE.md（§5.1 调用参数、截断、失败尝试，§5.3 的 `context.budget_tokens` 定义，§5.7）、`docs/notes.md` §5、本文件第 1 天的 `llm/client.py` 一项、`mini_swe_agent.md` 和 `swe_bench.md` 里的 B0 对齐表

**当天花费**：约 $0.0107（标准价，按记录的 token 数手算：flash 的 5 次调用输入 1075、输出 3633，v4-pro 1 次调用输入 40、输出 1500；缓存命中按 0 计，没有对账）。这些调用是为测截断和生成速度做的，不属于任何实验

**发现**
- 被 `max_tokens` 截断的工具调用，参数是不完整的 JSON，会被误当成"格式错误"；而且重试只会再被截断一次，所以它不属于可重试的范围，要单独处理
- 非流式下超时必须大于顶满上限的耗时，否则失控的生成会被超时重试 3 次，白白浪费 3 倍；我先前建议的"180 秒对 16384"在 v4-pro 上余量只有 1.6 倍，是我的疏漏，已按新的成对方案取代
- DeepSeek 的 `finish_reason` 还有 `insufficient_system_resource` 和 `aborted`（状态码 200，但生成被打断）；`aborted` 官方只有一句话，没有触发场景

**明天先做**：无变化，按第 1 天的任务开始写 `llm/client.py` 等；`llm/client.py` 的单元测试要覆盖上面的截断和可重试 `finish_reason`

### 2026-10-06（第 1 天：Agent 核心端到端跑通）

**完成了什么**
- 按"类型/配置/追踪/成本 → 沙箱 → LLM 客户端和回放 → 工具 → 主循环 → CLI → toy 端到端 → dev_5"的顺序实现，第 1 天的清单全部勾选；`src/harness/` 约 2400 行，测试 195 个（单元 191，端到端 4），ruff 和 pytest 全过，没有泄漏的容器
- 主循环对着窄接口写，用脚本化的假 LLM 和假沙箱把所有终止原因测完；沙箱用真实 Docker（toy 镜像和 django 镜像）测；LLM 客户端用本机假服务器测（重试分类、`Retry-After`、超时、断连、坏 200、被打断的 `finish_reason`、4xx 不重试、墙钟时间限制）
- `dev_5` 的 2 道题：用 flash 和 v4-pro 都跑通，补丁相同（`django-11066` 767 个字符，`sympy-16886` 462 个字符），官方判分都是 resolved（dev 检查，不进报告）

**当天花费**：harness 的真实运行 $0.0545（标准价，按轨迹里记录的 token 算：toy 录制两次 $0.0023，flash 的两题 $0.0066，v4-pro 的两题 $0.0456）；消息配对的 API 检查 8 次调用，token 没有记录，量级约 $0.001（估算）。另有上午的 `max_tokens` 与速度实验 $0.0107，见前一天的记录。没有和控制台对账

**遇到的问题和发现**（接口行为已记入 `docs/notes.md` §10）
- **LiteLLM 会把不认识的 `finish_reason` 悄悄改成 `stop`**（`aborted`、`insufficient_system_resource`），只打一条警告，原始值在 `choice.provider_specific_fields["native_finish_reason"]`；不处理的话，我们决定要重试的两种中断会被当成正常完成
- LiteLLM 的 `RateLimitError`、`InternalServerError`、`Timeout` 等继承自 **openai 的 `APIError`**，不是 `litellm.exceptions.APIError`；一开始 `classify` 没认出它们，把可重试的错误当成了"我们自己的 bug"直接抛出，测试抓到了
- DeepSeek 的消息格式：每个 `tool_call` 必须恰好有一条 tool 回复（缺回复、少回复、回复不存在的 id 都是 400）；参数不是合法 JSON 的调用放在历史里**不会被拒绝**
- 沙箱：超时后的僵尸进程、git stat 缓存、`python:3.12-slim` 没有 git 等（见上面沙箱一项）
- 回放：第一次录制后测试失败，原因是**测试自己先跑了一遍测试，生成了 `__pycache__`，改变了 agent 看到的环境**；进一步发现 `find`、`ls` 的顺序依赖宿主机文件系统，所以键不再包含工具输出内容

**与 CLAUDE.md 不一致或值得你决定的地方**（我没有改 CLAUDE.md）
- §3.2 还写着"成本用 `litellm.completion_cost` 计算"，但 §5.1 和实际实现用的是固定标准价的自有公式（`completion_cost` 随时钟用错峰价）。此前我说过 CLAUDE.md 不需要改，**漏看了这一处**（2026-10-07 已改 CLAUDE.md §3.2）
- §5.7 的 span 清单里没有 `message`，实现里为了崩溃后仍保留完整对话加了这一类（2026-10-07 已补入 §5.7，同时补了 `truncation`）
- `FailReason.no_submit` 目前没有使用：只回文字的回复按格式错误计数，连续 3 次记为 `format_error`（2026-10-07 已改：现在连续 3 次纯文字回复记为 `no_submit`，见第 2 天一项）
- `pyproject.toml` 除了你说的项目名和描述之外，我加了 `[build-system]`、`[project.scripts]`、`[tool.uv.build-backend]`（让 `uv run harness` 可用，样板由 `uv init --package` 生成）、pytest 的 `asyncio_mode`、标记、`norecursedirs` 和 `tests` 的 `pythonpath`
- 步数、成本、墙钟上限只是**开发默认值**（50 步、$0.5、1800 秒），真正的实验值要在第 2 天创建 `configs/default.yaml` 时定
- 连接错误（没有响应，如 DNS、拒绝连接）我归入 `timeout` 类重试：规则里没有明说，是我的判断

**明天先做（第 2 天）**
1. `context/`：`tokens.py`（真正的 token 计数，取代现在用上一次提示词大小的近似）、`manager.py`、四种策略、可恢复截断
2. 配置系统：`default.yaml` 和消融 YAML，启动时打印与主配置的差异；定下实验用的步数、成本、墙钟上限
3. `eval/swebench/run_agent.py`、`score.py`；权限层的基础版
4. 开跑 50 题之前还要拉剩余 50 个镜像（你决定等 harness 实现后再拉，现在可以开始），并先打印预计花费等你确认


### 2026-10-07（第 2 天：harness 核心部分）

**你的决定**：`thinking` 默认开（强度另说）；guard 选 (a)，第 2 天定稿 `policy.yaml`；`summarize` 不推迟；不拉镜像，先做核心。

**完成了什么**（`src/harness/` 的核心，详见第 2 天清单）
- 思考强度：调研主流 harness 并核实接口，记在 `docs/notes.md` §11.1–11.2；发现 **LiteLLM 会丢掉 `reasoning_effort` 关键字**，改走 `extra_body`；`reasoning_effort` 进配置、回放键、`run_start`；**临时定 `high`，等你确认**
- 上下文：`tokens.py`、`manager.py`、四种策略、压缩 span、成本计入；token 估计用 4 条真实轨迹校准（§11.3）
- 权限层定稿：`configs/policy.yaml` + `policy.py` + `guard.py`，83 个测试（含 35 条必须放行的日常命令）
- 配置：`default.yaml`、8 个消融文件（`extends` 继承）、启动时打印与主配置的差异、沙箱上限进配置
- 测试：全套 335 通过、1 跳过；`ruff check` 和 `ruff format --check` 通过
- 真实 DeepSeek 核实（几次很小的调用）：`reasoning_effort` 的 `high`/`max` 都被接受；压缩后的历史（连续两条 user 消息）被接受，包括 M0 设置（v4-pro、思考开、带 tools、保留带 `reasoning_content` 的 assistant 消息）

**当天花费**：约 $0.0040（标准价，按记录的 token 算：toy 重录 5 步 $0.0015，真实核实：flash 3 次调用 $0.0011，M0 设置（v4-pro）2 次调用 $0.0014；没有对账）

**遇到的问题和发现**
- 测试抓到一个真 bug：回合数不足 `keep_recent` 时，受保护区间的下界返回 `len(view)`，意思反了，会让所有消息都变得可清理；改为返回 `HEAD`，补了测试
- 本地计数偏低 8%–21%（工具定义约 800 token 也不在 LiteLLM 的 `token_counter(tools=…)` 里：它遇到 `todo` 工具缺 `items` 子 schema 会抛 `KeyError`，原因已在 `docs/notes.md` §11.3 更正），所以用"服务端实报值锚定 + 比例"，并给水位线留余量
- 一开始用 YAML 折叠标量 `>-` 写多行正则，折叠会补空格，把 `|` 前的空格变成正则的一部分；改成 `|-` 字面块
- 在我自己的测试里把字符数当成 token 数，规模算错了几处（约 5 字符一个 token），已按实测比例重算

**与 CLAUDE.md 不一致或值得你决定的地方**（没有改 CLAUDE.md）
- §5.1 写"每次调用显式传 `thinking`"，现在还要显式传 `reasoning_effort`；§5.3 的配置键表没有 `llm.reasoning_effort`、`sandbox.*`、`context.high_water/low_water/keep_recent`、`guard.policy_path`。等你确认强度后一起补
- §5.7 的 `llm_call` span 现在多一个 `purpose`（`agent`/`summary`），`compaction` span 和 `guard_decision.level` 是新字段
- 步数 / 成本 / 墙钟上限（100 / $1.0 / 1800 秒）是**临时值**，要在 dev_5 冒烟后定

**待你决定**
1. `reasoning_effort`：`high`（我的建议）还是 `max`？是否另加一个 P2 实验用 `max` 重跑 M0 来量化影响？
2. 上限值，以及 worker 数、M0 和 B0 并行还是串行（WSL 12GB）

**明天先做**
1. `eval/swebench/run_agent.py`、`score.py`、`aggregate.py`
2. 回填 `mini_swe_agent.md` 并实测 B0 的 `model_kwargs`
3. 在 dev_5 全部 5 题上跑 M0 和 B0 冒烟，定上限值和预算；然后打 `v0.1-frozen`
4. 镜像拉取等你说开始

### 2026-10-11 / 12（第 2 天续：summarize 重做、聚合与规则同步）

**完成了什么**（详见第 2 天"summarize 重做"一项）
- 阶段 0 到 4 和阶段 6 全部完成：视图与原文双列表；摘要读原文全文（含推理）、仅在输入过大时收紧重试；摘要消息末尾的状态块（带上限的索引）；自适应尾部与规划五级；`tool_call` / `message` span 加 `step`；`eval/report/aggregate.py` 和 `summary_probe.py`；`CLAUDE.md` 同步并新增本"待同步清单"
- 测试 429 通过、1 跳过；`ruff` 通过。新增约 110 个测试，对每个新不变量做了变异检查
- 调研（`docs/notes.md` §12）：主流 harness 的摘要输入、遮蔽与 `truncate` 的关系、Claude Code 与 Codex 压缩后的状态恢复；其中对我前面几处错误说法做了更正

**当天花费**：约 $0.01（标准价，按记录的 token 算：阶段 2 的真实核实 $0.0046，探针三次共约 $0.005；没有对账）

**提交**（2026-10-12，按功能切成 5 个，每个功能只出现在一个提交里；每个提交单独检出后 `ruff check`、`ruff format --check` 和 `pytest` 都通过，测试数依次为 223 / 306 / 404 / 429 / 429，各自另有 1 个跳过）
1. `8393438` feat(config)：配置层与 LLM 请求参数（`reasoning_effort`、`sandbox.*`、上下文设置、消融配置、回放重录）
2. `5030c7e` feat(security)：权限层（`policy.yaml`、`policy.py`、`guard.py`）
3. `b27dee4` feat(context)：视图与原文、四种策略、带状态块的摘要、规划，以及接进主循环和 CLI 的部分
4. `4865a92` feat(eval)：逐步追踪、`aggregate.py`、`summary_probe.py`
5. docs：`CLAUDE.md`、`plan.md`、`notes.md`
`loop.py` 和 `cli.py` 同时承载了多个功能，为了不让一个功能横跨提交，按功能拆成了中间版本（各提交时刻的内容）；`test_loop_context.py` 末尾那个属于评测功能的测试挪到了 `test_trace_steps.py`。

**遇到的问题和发现**
- `hybrid` 的摘要读到的是 `[output cleared]` 占位符而不是原文；本地 token 计数把回传的 `reasoning_content` 算进提示词大小，而服务端不计。两者都是已有缺陷，现已修复
- 变异检查时三次栽在同一个坑：用 `sed` 或字符串替换改文件，但文件已被格式化换行，替换没匹配上又没报错，变异体因此"存活"，另有两次是编辑本身静默失败。之后改成带断言的替换；还发现并删掉两处恒真断言（`or True`）
- CLAUDE.md 与代码出现 11 处不一致：7 处是我凭对话记忆写草稿造成的，4 处是 CLAUDE.md 写于第 0 天、之后字段名和结构有了更具体的决定却没回头改。根因是没有机制强制同步，已加"待同步清单"和 §8 的登记规则
- 一条回复被安全分类器拦截，我不知道是哪一部分；没有重复它

**明天先做**
1. ~~先按"review 路线"把改动切成提交~~（2026-10-12 已完成，见上面"提交"一条）
2. `eval/swebench/run_agent.py`、`score.py`；回填 `mini_swe_agent.md` 并实测 B0 的 `model_kwargs`
3. dev_5 全部 5 题跑 M0 和 B0 冒烟，定上限值和预算，再打 `v0.1-frozen`
4. 镜像拉取等你说开始

### 2026-10-12（修正：`todo` 工具的 schema）

- 复盘 `tokens.py` 里一条注释时发现，之前"LiteLLM 的 `token_counter` 对 DeepSeek 不计 `tools`"的结论是**错的**：真实原因是我们的 `todo` 工具的数组参数没有 `items` 子 schema，`token_counter(tools=…)` 对所有模型都抛 `KeyError`（更正记录在 `docs/notes.md` §11.3）
- 按你的决定补全了 `todo` 的 `items` 子 schema；新增 4 个测试（所有数组参数必须声明元素类型、LiteLLM 能数四种工具集合、`todo` schema 与运行时校验一致、失败请求的估计不再回退），旧 schema 下全部变红；toy 回放重录（$0.0011）；真实核实 3 次调用 $0.0009。**M0 的工具定义因此变了，必须在 `v0.1-frozen` 之前**（已满足）
- 受影响：`wasted_prompt_tokens_est`（失败请求的估计）此前在 M0 的工具集下悄悄回退到"字符数 // 4"，现在走分词器；dev 阶段没有失败请求，没有已记录的数据受影响
- 未提交（`tokens.py`、`todo.py`、`test_tools.py`、`test_llm_client.py`、`test_context.py`、`toy_fix.jsonl`、`notes.md`、`plan.md`）

### 2026-10-12（第 2 天续：评测流水线）

- **决定（你的）**：`reasoning_effort` 定为 `high`；步数 / 成本 / 墙钟上限做成配置，数值参考主流 harness 和现有数据；并行先只算资源，实现是 TODO；加一次真实压缩冒烟；先不拉 50 个镜像；暂不打 `v0.1-frozen`，先做核心功能
- 批量运行器 `run_agent.py` 完成（见第 2 天对应一项）；每题的装配抽成 `cli.run_one`
- 本机资源（`docker info`）：15.6GB 内存、10 核；沙箱上限 4g / 2 核时，按用满上限算最多同时 3 个（内存限制出 3 个，CPU 允许 5 个）；上限是封顶不是预留，实际可以更多，但这是不会撑爆内存的数
- B0 对齐完成（`run_b0.py`）；限制值定为 100 步 / $2.0 / 1800 秒（`default.yaml` 的注释写明了依据和我的算术）
- **dev_5 冒烟完成**（开发运行，不进报告）：M0 与 B0 各 5/5；真实压缩冒烟 30 次压缩无失败。数据与观察见 `docs/notes.md` §14
- **花费**：今天累计约 $1.8（`results/runs/` 下所有已完成轨迹的标准价合计 $1.7765，含 B0；另有零星的核实调用约 $0.03；没有和控制台对账），占 `MAX_TOTAL_COST_USD`（$20，仍是占位值）的约 9%
- **预算表（用 dev_5 的 M0 实测填，样本只有 5 道简单题，偏低）**：M0 平均 $0.109 / 题；若 verified_50 与 dev_5 相当，一个配置 50 题约 $5.5；更难的题会更贵，所以这个数应当当作下限。预估公式的教训：运行前的预估 $0.11（依据是之前 2 道最简单的题）比实际 $0.545 低了约 5 倍。`MAX_TOTAL_COST_USD` 要等你按"配置数 × 每配置成本"重定，我的建议在下面"待你决定"里
- **CLAUDE.md 瘦身**已应用（未提交）：实现细节搬到代码的模块说明，只改说明文字、代码逐字不变（AST 比对确认）；体积只降了约 8.2%，因为文件的大头是规则；分层的思想和理由写在 `docs/notes.md` §15。不加"CLAUDE.md 标识符必须在代码里存在"的一致性测试（你的决定）

### 2026-10-13 / 14（第 2 天续：摘要大小、推理计数与压缩流程）

**你的决定**：`MAX_TOTAL_COST_USD` 暂定 $40；M0 基本符合预期之前不拉镜像，验收标准见第 2 天"M0 基本符合预期"一项；摘要的大小、字数和状态块上限都要可配置；第 0 天"推理不计入"的矛盾不再复现，直接按实测更正；摘要档位的过渡区和 `tail_cap` 的依据先记入"后续 TODO"；B0 运行中的花费守卫和并行化一起做；跳过的压缩要记录，hybrid 清理后要重新判断是否超预算。

**完成了什么**（提交按功能切分，每个都通过 ruff 和全套测试）
1. `8af8fea` 摘要按档位实测的 token 数预留，代替"每词 1.5 token"
2. `2e8e26b` 回传的推理计入提示词大小（删掉 `strip_reasoning`）
3. `43ea787` 摘要大小可配置；先生成状态块，门槛用它的实际大小；hybrid 清理后重新判断是否超预算；`compaction_skipped` span；比例写入 span 并标出异常
4. `30f1920` `aggregate.py`：压缩后的大小用服务端数字（带来源标记）；`monitoring` 一节
5. `efbded7` 24k 压缩冒烟配置；plan 的"后续 TODO"
6. `4d9011a` `run_b0.py` 的两道保护（§16.C1），并修了 `convert` 先写文件、后核对配置的问题
7. 文档：`docs/notes.md` §17（实测与更正），§5、§11.3、§14、§16 里被推翻的结论已在原文旁标注

测试 524 通过、1 跳过（开始时 487）。每个新行为都做了变异检查，共 33 个变异体，全部被对应的测试抓到；其中 4 个第一次存活，原因都是测试没覆盖到，补了测试后被抓到。

**花费**：探针 $0.025，推理计数的真实对照 $0.005，10k 冒烟 $0.305，24k 冒烟 $0.255，合计约 $0.59。运行器统计的累计花费 $2.33（不含 B0 的 $0.27），占 $40 的约 6%。

**遇到的问题和发现**
- 我最初说"摘要不遵守字数"，判断错了：词数基本受控，错的是换算假设
- 回传的推理计入 `prompt_tokens`，推翻了第 0 天和 10-11 的结论。没有锚的估计因此偏差 −16% 到 +42%；修复后离线验收在 −8% 到 +5%，真实 10k 冒烟在 −9% 到 +10%。残余偏差有规律（只清理时低估、带摘要时高估），是不同内容的分词差异造成的；报告已改用服务端数字
- 验收脚本出过错：漏算工具定义，制造出 +146% 的假离群点。教训：离群点要先怀疑测量方法
- 我把你推迟的"B0 花费守卫"错记成了"`run_b0.py` 的保护"，已更正
- 24k 冒烟第一次启动时被运行器以"工作区不干净"拒绝（我在 10k 冒烟进行时改了文件），保护按设计起作用，没有花钱
- 两次真实冒烟（§17.5）：都提交了补丁，没有回退或摘要失败；完整状态块第一次在真实运行里出现；full 档摘要的实测最大值刷新为 1822（预计 1950）
- **新发现，未修，等你决定**（§17.6）：滞后机制的实现和说明不一致。清理后已经降到高水位线以下的压缩，下一次触发线也可能被推到预算本身，提示词会贴着预算走
- hybrid 原来会做"亏本摘要"：清理后已经回到预算内，仍被当成最后机会跳过门槛，导致摘要放回的比省下的还多。有 4 个测试依赖了这个行为，已改

**补充（2026-10-14）**：滞后机制按你确认的方案修复（`6b9bb5d`，新配置项 `context.hysteresis_cap`，默认 0.5）。重跑 24k 冒烟（$0.272）：开始时已超预算的压缩 1 → 0，压缩 5 → 7 次，缓存命中率 92.2% → 89.8%。讨论结论写进 `docs/notes.md` §18（简明版）；"后续 TODO"新增"摘要预计值的标定"。今天累计花费约 $0.86。

**你的决定（2026-10-14，本条只记录，未实施）**
- A1 同意 CLAUDE.md 的修改（§5.3 估计误差的写法；§5.7 新增 `compaction_skipped`，`compaction` 的 token 数注明是估计值）。下次动手时应用，并勾掉待同步清单的 3 行
- A2 摘要的预计 token 数改为**根据字数动态估计**，不再按档位写死。实现方案等你确认（见当天的对话；要点：截距 + 每词斜率的线性模型，两个系数可配置，用至少三档字数标定）。**会改变 M0 的行为，必须在冻结前做**
- A3 同意在候选冻结提交上重跑 dev_5 的 M0（约 $0.55）。**顺序**：A2 实施之后再跑，否则候选提交不是最终版本
- A4 摘要档位的过渡区、`tail_cap` 的依据、摘要预计值的标定工具，放到冻结之后（标定工具可能会提前，因为 A2 需要它）

**A2 已实施（2026-10-14）**：摘要预计 token = (225.0 + 1.757 × 字数) × 1.4，三个系数和标定条件都是配置项；标定花费 $0.013；10k 冒烟配置的 `summary_max_ratio` 调到 0.21；A1 的 CLAUDE.md 修改已应用。测试 539 通过、1 跳过；8 个变异体全部被抓到。

**A3 完成（2026-10-14）**：`M0-dev-v2` 5/5 解决，$0.458，平均 33 步，缓存命中率 97.5%，见上面"M0 基本符合预期"一项。验收标准全部满足。今天累计花费约 $1.33；运行器统计的累计花费 $3.07（不含 B0 的 $0.27），占 $40 的约 8%。

**明天先做**
1. 等你确认后：拉剩余 45 个镜像（约 90–100GB），打 `v0.1-frozen`（候选提交 `ed6625a` 之后只有文档改动）
2. 定 M0 和 B0 在 verified_50 上先后跑还是同时跑，打印预计花费后等你确认

### 2026-10-15

- notes §20.1 换成读源码核实的详细版本（Codex、OpenHands、Aider、Claude Code 官方文档、SWE-agent、mini-swe-agent），更正了"OpenHands 摘要调用不设 `max_tokens`"
- 新子集 `verified_25`（`select_subset.py derive`，从 `verified_50` 按仓库分层，种子 42：django 9、sympy 7、scikit-learn 5、sphinx 4），镜像 25/25 拉完并核对
- 待你决定：摘要空间用"硬上限"还是"按字数估计"（notes §20），定了之后在新的候选提交上重跑 A3 再冻结
- **你的决定（2026-10-15）**：给摘要留空间采用**方案 A**（现状：两档摘要 + 按字数的估计值做规划 + 两档共用宽松的输出上限），不改代码。notes §20.2–20.4 已按 A / B / C 分开重写，更正了"方案 B 和主流思路一致"的错误说法。`ed6625a` 之后 `src/` 和实验配置都没有改动，A3 的结论对当前提交仍然有效
- 方案 A 仍需你确认：① 标定的覆盖面（200 词那档的 13 个样本里有 10 个来自同一道题）；② 余量 1.4（正好盖住已见过的最大值，没有额外余地）；③ 冻结后，正式实验里摘要实际超出预计时怎么处理
- **你的决定（2026-10-15）**：方案 A 的三项都按建议：① 标定接受现状，靠 `monitoring.summary_size` 发现估低；② 余量保持 1.4；③ 冻结后正式实验中摘要超出预计时不中途改参数，如实报告，频繁的话下一个版本重新标定并按规则重跑、记入 `results/CHANGELOG.md`。CLAUDE.md §4 已同步 `verified_25`。打 `v0.1-frozen`；先在 `verified_25` 上跑 M0 和 B0 看结果
- **你的决定（2026-10-15）**：冻结后的实验在 worktree `/home/yuhe/code/labs/harness_demo-frozen`（固定在 `v0.1-frozen`）里跑，结果用 `--results-dir` 写回主目录的 `results/runs/`；master 上照常写文档。做法和核对结果见 `docs/notes.md` §21。`M0`（verified_25）在主目录启动，当时主目录正好在 tag 的提交上、工作区干净，等价于在 tag 上跑。B0 先不跑，看完 M0 的结果再定
- **M0 在 verified_25 上完成**（2026-10-15，run-id `M0`，提交 `ff11f3b`，配置哈希 `6ea20eaecc69`；数字出自 `results/runs/M0/metrics.json` 和 `score.json`）：官方判分 22/25；25 题都提交了补丁；没有格式错误、输出截断、失败重试，也没有溢出；128k 下一次压缩都没发生（提示词峰值最高 108111，离触发线 108800 很近）；花费 $4.36（每题平均是 dev 的约 1.75 倍）
  - 权限层拦截 2 次，都是 `deny.test_edit_bash`：agent 在 `tests/` 下**新建**临时复现文件（sphinx-8269、sphinx-8621，两题最后都解决了），不是改已有的断言，人工复核判为**误拦**。`policy.yaml` 与 M0 一起冻结，不改；以后报误拦率时如实计入
  - 没解决的 3 题：django-11087（89 步）、scikit-learn-14983（35 步）、sphinx-9602（106 步），都没有压缩，留到第 3 天做失败分析
- **B0 在 verified_25 上完成**（2026-10-15，run-id `B0`，在冻结版本的 worktree 里跑，提交 `ff11f3b`，配置哈希 `6ea20eaecc69`，与 M0 相同；数字出自 `results/runs/B0/metrics.json`、`score.json`）：官方判分 22/25，**没解决的与 M0 是同样 3 题**；花费 $3.16（mini-swe-agent 自报的数与我们按 token 重算的一致）。逐题配对：M0 有 20 题更贵、21 题步数更多，花费中位数比 1.34、步数中位数比 1.42；只有 25 题，置信区间很宽，只描述不下结论
- 累计花费：运行器统计约 $7.43（不含 B0），加上 B0 的 $3.43（dev 时的 $0.27 和这次的 $3.16），合计约 $10.9，占 $40 的约 27%
