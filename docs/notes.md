# docs/notes.md — 外部接口核实记录

> CLAUDE.md §8 要求：外部接口（swebench、mini-swe-agent、codex、Docker 等）的命令和参数先核实再写，把核实结果记在这里，**不凭记忆写**。
> 每条注明核实日期、版本和依据（源码 `文件:行号` 或命令输出）。依据是回忆而非核实的，单独放在 §9，并标明"未核实"。
> 缺数据的地方保留 `{{TBD}}`，实验指标不估算；基于少量实测样本的外推会明确标注假设和范围。

核实日期：2026-10-04。版本（`uv.lock`）：swebench 5.0.2、mini-swe-agent 2.4.6、docker (docker-py) 7.2.0、litellm 1.104.0。

---

## 1. SWE-bench Verified 数据集（HuggingFace）

- 数据集 id：`SWE-bench/SWE-bench_Verified`，split：`test`，共 **500** 行。
- 数据集 revision（sha）：`78f471bf655a3137b2e8a75af1501690ec009ec3`（2026-10-04 由 `select_subset.py` 解析，写入 `configs/subsets/subsets_meta.json`，选题和拉取都以它为准）。
- 字段（`load_dataset_builder(...).info.features`）：`base_commit, created_at, difficulty, environment_setup_commit, eval_type, image, instance_id, log_parser, repo, version, patch, test_patch, eval_script, problem_statement, hints_text, FAIL_TO_PASS, PASS_TO_PASS`。
- `image` 字段 500 行全部非空且互不相同，仓库前缀全是 `swebench`，tag 全是 `latest`。
- 仓库分布（题数）：django/django 231、sympy/sympy 75、sphinx-doc/sphinx 44、matplotlib/matplotlib 34、scikit-learn/scikit-learn 32、astropy/astropy 22、pydata/xarray 22、pytest-dev/pytest 19、pylint-dev/pylint 10、psf/requests 8、mwaskom/seaborn 2、pallets/flask 1。
- 难度分布：`<15 min fix` 194、`15 min - 1 hour` 261、`1-4 hours` 42、`>4 hours` 3。

## 2. 镜像命名与拉取行为

- **镜像名直接读数据集的 `image` 字段，不手拼。** 例：`astropy__astropy-12907` → `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`。
- 命名规则（`swebench/image_builder/image_spec.py` 的 `name` 属性，约 36–44 行）：`<namespace>/sweb.eval.<arch>.<instance_id>:<tag>`，`amd64` 写作 `x86_64`，**`__` 替换为 `_1776_`**（Docker Hub 不允许双下划线），整体小写。
- mini-swe-agent 的规则（`minisweagent/run/benchmarks/swebench.py:68-76`）：优先取实例的 `image_name` 或 `docker_image` 字段；数据集没有这两个字段时按同样规则拼成 `docker.io/swebench/sweb.eval.x86_64.<id>:latest`。新数据集用的字段叫 `image`，所以 mini-swe-agent 走的是拼接分支，结果与数据集的 `image` 一致（只多了 `docker.io/` 前缀）。
- Docker Hub 上抽查的 3 个镜像存在（`django-11099`、`django-11133`、`sympy-20590`），manifest 是 index：`linux/amd64` 加一个 `unknown/unknown`（attestation）。
- 抽查的压缩体积：django-11099 1152MB、django-11133 1152MB、sympy-20590 1089MB。**层共享**：django 两个镜像 10 层中有 8 层相同；django 与 sympy 前 6 层相同。所以集中在少数仓库减少的是下载量和磁盘，不是镜像数量。
- **镜像内部（5 个 dev 镜像实测，`docker image inspect` 与容器内命令）**：`Entrypoint=null`、`Cmd=["/bin/bash"]`、`WorkingDir=/testbed/`、用户为 root；`/testbed` 的 `HEAD` 是 base_commit 之上一个**空的** `SWE-bench` 提交（`git show --stat` 无文件改动，`HEAD^` == 数据集的 `base_commit`），工作区干净；**镜像里没有 `rg`**（有 `grep`），第 1 天的 search 工具要回退到 grep；django 镜像的 Python 是 3.6.13（Anaconda）。因此校验 `base_commit` 要比对 `HEAD^`（或 `HEAD`），不能只比 `HEAD`。
- 本地实际磁盘占用见 §7。
- **缺镜像时会自动拉取**，两处源码都确认：
  - `docker/models/containers.py:879`：`containers.run` 遇到 `ImageNotFound` 就 `images.pull`。
  - `swebench/harness/run_evaluation.py:91-99`：先 `images.get`，找不到就 `images.pull`。
  - mini-swe-agent 的 `DockerEnvironmentConfig.pull_timeout` 默认 120 秒（`environments/docker.py:36`）。

## 3. swebench 判分（5.0.2）

- 新 CLI：`swebench eval <DATASET> -p <preds> --run-id <id> -j <workers> [-t <timeout>] [-i <instance_id>...] [--split test] [--report-dir DIR]`。`DATASET` 别名 `verified` → `SWE-bench/SWE-bench_Verified`（`swebench/cli/_datasets.py`）。`--gold` 评测官方参考补丁。
- 旧入口 `python -m swebench.harness.run_evaluation` 仍在，参数是 `-d/--dataset_name`（**默认是 SWE-bench_Lite**，用于 Verified 必须显式传）、`-p`、`-id`、`--max_workers`（默认 4）、`-t`（默认 1800 秒）。
- 预测文件（`swebench/harness/utils.py:65-75`）：`.json`（列表，或 `{instance_id: 预测}` 字典，字典会取 values）或 `.jsonl`。每条要有 `instance_id`、`model_name_or_path`、`model_patch`。**mini-swe-agent 输出的 `preds.json` 可以直接传给 `-p`。**
- 其他子命令：`swebench report`、`swebench images build|check|clean|push`、`swebench dataset ...`（`images check` 面向 task repo，不用于本项目）。

## 4. mini-swe-agent（2.4.6）

入口：`mini-extra swebench`（批量）、`mini-extra swebench-single`、`mini-extra inspect`、`mini-extra config`。

| 项 | 事实（依据） |
|---|---|
| 数据集 | `--subset` 默认 `lite`；别名 `verified` 映射到 **`princeton-nlp/SWE-Bench_Verified`**（`swebench.py:53-62`），与 swebench 用的 id 不同。**要显式传 `--subset SWE-bench/SWE-bench_Verified`。** |
| split | `--split` 默认 **`dev`**，Verified 要传 `--split test`。 |
| 选题 | 没有读文件的参数。`--filter` 用 **`re.match`**（`swebench.py:189`，前缀匹配，需自己加 `^...$`）；`--slice` 按位置；`--shuffle` 无随机种子，**不用**。 |
| 默认配置 | `-c` 一旦传入就**替换**默认配置，必须带上 `-c swebench.yaml`。`swebench.yaml` 的默认值：`step_limit: 250`、`cost_limit: 3.0`、`environment.timeout: 60`、`cwd: /testbed`、`model_name: anthropic/claude-sonnet-4-5-20250929`。 |
| 沙箱 | `DockerEnvironmentConfig`（`environments/docker.py:15-42`）：`run_args` 默认 `["--rm"]`（**默认联网，无内存、pids 限制**），`executable` 取 `MSWEA_DOCKER_EXECUTABLE` 或 `docker`，`pull_timeout: 120`，`container_timeout: 2h`。 |
| 输出 | `<output>/preds.json`（`{instance_id: {model_name_or_path, instance_id, model_patch}}`）、每题 `<instance_id>.traj.json`、`exit_statuses_<ts>.yaml`、`minisweagent.log`。重跑跳过 `preds.json` 里已有的题，`--redo-existing` 才重做。 |
| 全局配置目录 | `minisweagent/__init__.py:26-27`：`MSWEA_GLOBAL_CONFIG_DIR`，否则 `~/.config/mini-swe-agent`，**import 时就 `mkdir`**，并读取其中的 `.env`。本项目把它指到仓库内被忽略的 `.mswea/`。 |
| 计价 | `MSWEA_COST_TRACKING=ignore_errors` 可忽略缺价格的报错，但会让 `cost_limit` 失效，仅作最后手段（`models/litellm_model.py:36`）。 |
| `-c key=value` 的解析 | `config/__init__.py:39-43`：值先按 JSON 解析，失败才当字符串。所以 `-c 'environment.run_args=["--rm","--network","none"]'` 得到列表（已离线验证），`-c agent.step_limit=100` 得到整数。 |
| `run_args` 的用法 | `environments/docker.py:75-96`：拼成 `docker run -d --name <名> -w <cwd> <run_args…> <image> sleep <container_timeout>`，整个命令受 `pull_timeout`（120 秒）限制；`run_args` 会整体替换默认的 `["--rm"]`，所以 `--rm` 要自己保留。 |
| 每题成本字段在轨迹里的位置 | `{{TBD}}`（冒烟测试后核实） |

## 5. LiteLLM（1.104.0）与 DeepSeek

核实日期 2026-10-05，全部是离线核实（读源码、`LITELLM_LOCAL_MODEL_COST_MAP=True` 下调用 `get_llm_provider` 和 `completion_cost`、本机假服务器），**没有调用过 DeepSeek 的 API**。

**路由**（`litellm_core_utils/get_llm_provider_logic.py:723-727`）

| 模型名 | 结果 |
|---|---|
| `deepseek/deepseek-v4-pro` | 提供商 `deepseek`；密钥读 `DEEPSEEK_API_KEY`；`api_base` 取参数，其次 `DEEPSEEK_API_BASE`，**再没有就是 `https://api.deepseek.com/beta`**（注意是 `/beta`） |
| `deepseek-v4-pro`（裸名） | **报错** `LLM Provider NOT provided`。目前 `.env` 里的 `MODEL_MAIN`、`MODEL_CHEAP` 就是裸名，会失败 |
| `openai/deepseek-v4-pro` + `api_base=https://api.deepseek.com` | 能路由（走 OpenAI 兼容协议），但**计价失败**，见下 |

**计价**（`completion_cost`）

- `deepseek/` 前缀：能算，并且按缓存命中的价格单独计费。用 `prompt_tokens=10000`、其中 8000 缓存命中、`completion_tokens=500` 构造的假响应：v4-pro 算出 0.004972，不带缓存信息时 0.015180；与手算一致。
- `openai/deepseek-v4-pro`：抛 `ModelNotMappedError`（`custom_llm_provider=openai` 下查不到）。**所以 `openai/` 前缀会让成本统计和预算控制失效。**
- DeepSeek 的缓存字段 `prompt_cache_hit_tokens` 会被映射到 `usage.prompt_tokens_details.cached_tokens`（`types/utils.py:1907-1911`），`cache_hit_rate = Σcached / Σprompt_tokens` 可以直接用。

**LiteLLM 本地价格表里的条目**（`deepseek/deepseek-v4-pro` 与裸名 `deepseek-v4-pro` 相同，`deepseek-flash` 同理）。这是 LiteLLM 内置表的内容，**不一定等于 DeepSeek 官方当前的价格，开跑前要对照官方价格页**：

| | `deepseek/deepseek-v4-pro` | `deepseek/deepseek-flash` |
|---|---|---|
| 输入（每 token） | 1.32e-06 | 3e-07 |
| 输出（每 token） | 3.96e-06 | 1.2e-06 |
| 缓存命中输入（每 token） | 4.4e-08 | 6e-09 |
| 最大输入 / 输出 token | 1000000 / 393216 | 1000000 / 393216 |
| 支持函数调用、reasoning、prompt caching、tool_choice | 是 | 是 |

缓存命中价格约为未命中的 3%（v4-pro）和 2%（flash），所以 KV 缓存友好的上下文规则（CLAUDE.md §5.2）对成本影响很大。

**思考模式**（`llms/deepseek/chat/transformation.py`）

- 只有调用时显式传 `thinking={"type": "enabled"}`（或 `reasoning_effort` 不是 `"none"`）且模型支持 reasoning，LiteLLM 才会补 `reasoning_content`（`_thinking_mode_active`，约 214-225 行）。
- 开启思考模式时，DeepSeek 要求多轮对话里每条 assistant 消息都带回 `reasoning_content`，否则报错。LiteLLM 会从 `provider_specific_fields` 里找回；找不到就塞一个空格占位并打警告，**可能悄悄降低多轮质量**（源码注释的说法）。
- **未核实**：DeepSeek v4 模型默认是否开启思考模式。需要一次最小调用确认；harness 的消息历史要保存模型返回的 `reasoning_content`，压缩上下文时也要考虑它。

**异常与调用**

- `litellm.acompletion` 是协程函数；`RateLimitError`、`InternalServerError`、`APIConnectionError`、`Timeout`、`BadRequestError`、`AuthenticationError`、`ContextWindowExceededError` 都存在，可用于 `llm/client.py` 的"只对 429 和 5xx 重试"。
- LiteLLM 自带重试（`num_retries` 通过 kwargs 传入，`main.py` 里有读取）。CLAUDE.md §5.1 要求重试放在自己的 `llm/client.py` 里并记录次数，所以调用时应显式传 `num_retries=0`，避免两层重试叠加。

**当前 `.env` 的核实结果（2026-10-05，没有任何外部流量）**

`.env` 现在是 `DEEPSEEK_API_KEY`、`DEEPSEEK_API_BASE=https://api.deepseek.com`、`MODEL_MAIN=deepseek/deepseek-v4-pro`、`MODEL_CHEAP=deepseek/deepseek-flash`、`MAX_TOTAL_COST_USD`。

- **格式**：UTF-8、LF、无 CRLF、无行尾空格、有末尾换行；5 个键，没有引号，`=` 两侧没有空格；`python-dotenv` 能解析出全部 5 个键；`DEEPSEEK_API_KEY` 以 `sk-` 开头、长度 35（只比较了布尔值和长度，没有输出内容）；文件权限 644（建议 `chmod 600 .env`）。
- **LiteLLM 解析**：两个模型都路由到 `deepseek` 提供商，`api_key` 解析到 `DEEPSEEK_API_KEY`，`api_base` 是 `https://api.deepseek.com`（不再是 `/beta`）。
- **请求形状**：在本机 `127.0.0.1` 起一个假的 DeepSeek 服务器，用真实 `.env` 调 `litellm.completion`。LiteLLM 发出的是 `POST /chat/completions`，`Authorization` 等于 `Bearer <DEEPSEEK_API_KEY>`，请求体里 `model` 是去掉前缀的 `deepseek-v4-pro`，只有 `max_tokens`、`messages`、`model` 三个键（**没有发送 `thinking` 参数**，所以思考模式的默认状态由服务端决定）。
- **响应解析**：假响应里的 `prompt_cache_hit_tokens=8000` 被解析为 `usage.prompt_tokens_details.cached_tokens=8000`，`completion_cost` 算出 0.004972，与前面手算一致。
- **仍然不一致**：`.env.example` 还是 `OPENAI_*` 的模板，与 `.env` 的键不一致，需要同步（`MAX_TOTAL_COST_USD=20` 仍是占位值）。

**验证计划（步骤 1 起会花钱，需要你确认；花费用 LiteLLM 内置价格表估算，不是官方价格）**

| 步骤 | 做什么 | 要确认的事 | 预计花费 |
|---|---|---|---|
| 0（已完成，免费） | 格式检查 + 本地假服务器 | 密钥/地址/路径/请求体/用量解析/计价 | 0 |
| 1 | 对 `deepseek-flash` 发一条 `ping`，`max_tokens=64`，`num_retries=0` | 密钥有效、模型名真实存在、不走 `/beta`、`usage` 的字段（是否有 `prompt_cache_hit_tokens`）、响应里有没有 `reasoning_content`（即思考模式的默认状态） | 不到 0.001 美元 |
| 2 | 对 `deepseek-v4-pro` 重复步骤 1 | 主模型同样可用 | 不到 0.001 美元 |
| 3 | 带一个最小的 `bash` 工具定义发请求（`tool_choice` 自动） | `tool_calls` 能解析、`arguments` 是合法 JSON、两个模型都支持函数调用 | 不到 0.001 美元 |
| 4 | 思考模式：分别显式传 `thinking={"type":"disabled"}` 和 `{"type":"enabled"}`，做一次带工具结果的两轮对话 | 开启时是否要求回传 `reasoning_content`；决定所有配置统一用哪个设置（对比实验要保持一致），写进 `configs/default.yaml` | 几美分以内 |
| 5 | 同一段较长的前缀连续发两次 | 第二次是否出现 `prompt_cache_hit_tokens>0`，以及缓存生效的最短前缀；对应 §5.2 的缓存规则 | 几美分以内 |
| 6 | 到 DeepSeek 控制台核对：步骤 1–5 的实际扣费与 LiteLLM 的 `completion_cost` 之和是否一致 | **LiteLLM 内置价格表是否等于官方当前价格**，决定 `max_cost_per_task_usd` 和 `MAX_TOTAL_COST_USD` 能否信任 | 0 |
| 7 | 故意用错误的密钥调一次 | 抛出的是 `AuthenticationError`，不会被当成可重试错误 | 0 |
| 8 | 用本地假服务器返回 429、500、畸形 JSON | `llm/client.py` 的"只对 429 和 5xx 重试、记录重试次数"逻辑（第 1 天的单元测试，不花钱） | 0 |
| 9 | `mini-extra swebench` 对 1 道 dev 题（`-m deepseek/deepseek-flash`，很小的 `cost_limit`） | B0 的命令、`--network none` 下能否工作、轨迹里的成本字段位置 | 少量 |

步骤 1–3 通过后才有必要做 4–5；步骤 6 通过前，不要信任任何成本数字。每一步的结果和花费记进 `plan.md` 的日志，并回填本节。

## 6. Docker 与 uv 环境

- Docker Engine 29.1.3（`docker.io`）、Compose 2.40.3、buildx 0.30.1；cgroup v2（systemd 驱动）、runc。
- **存储驱动 `overlayfs`，`driver-type: io.containerd.snapshotter.v1`**：用的是 containerd 镜像存储，会同时保存压缩的内容 blob 和解包后的快照，单个镜像的磁盘占用大于压缩体积。
- **未登录 Docker Hub**（没有 `~/.docker/config.json`），也没有 registry mirror（没有 `/etc/docker/daemon.json`）：匿名拉取受 Docker Hub 的速率限制。用文档里的 `ratelimitpreview/test` 接口（HEAD 请求，不消耗额度）查到：`ratelimit-limit: 100;w=3600`（每小时 100 次），拉了 5 个镜像后 `ratelimit-remaining: 92`。
- WSL 发行版 `Ubuntu` 的虚拟磁盘在 `D:\WSL\Ubuntu_2204.1.7.0_x64`，D 盘空闲约 331GB（`Get-PSDrive`，2026-10-04）；WSL 内 `/` 显示 251GB 容量、约 149GB 可用，**以这个 149GB 为准**。C 盘空闲只有约 71GB，但与 Docker 数据无关。
- `uv` 0.9.13 在 `~/.local/bin/uv`，不在 Claude Code 的 Bash 工具的 PATH 里，需要写完整路径或先 `export PATH`。

## 7. 磁盘实测（2026-10-04，containerd 镜像存储）

按顺序逐个拉取 5 个 dev 镜像，每次记录 `df`（可用空间，`/var/lib/docker` 所在的 `/`）的前后差值：

| 顺序 | 镜像 | 单独的 `docker images` 大小 | 实际新增磁盘 | 说明 |
|---|---|---|---|---|
| 1 | django-11066 | 4.18GB | 4.18GB | 第一个，没有可共享的层 |
| 2 | django-15930 | 4.40GB | 2.23GB | 与 1 同仓库，共享了一部分 |
| 3 | scikit-learn-13779 | 5.43GB | 5.35GB | 新仓库，几乎没共享 |
| 4 | sphinx-7889 | 4.06GB | 1.80GB | 新仓库，但共享了基础层 |
| 5 | sympy-16886 | 3.97GB | 1.80GB | 同上 |

- 5 个镜像共新增 **15.4GB**（可用空间 158.96GB → 143.57GB，与 `docker system df` 的 15.39GB 一致）。django-11066 一例：压缩约 1.15GB，磁盘占用 4.18GB，约 3.6 倍（containerd 镜像存储同时保留压缩 blob 和解包后的快照）；其他镜像没有单独测过这个比例。
- **层结构**（`docker history` 的非空层，解包大小）：
  - 所有镜像共有的基础层：Ubuntu 87.7MB + apt 666MB + miniconda 762MB，约 1.5GB。
  - **每个 (repo, version) 一个环境层**：django 1.36GB（3.0）、1.5GB（4.2），scikit-learn 2.16GB，sphinx 1.38GB，sympy 1.27GB。
  - **每个镜像一个实例层**：django 149–169MB，scikit-learn 255MB，sphinx 58.6MB，sympy 99.7MB。
  - 所以同一个 (repo, version) 的镜像只多一个实例层；新的 (repo, version) 才多一个环境层。
- **55 个题里共 22 个不同的 (repo, version)**（django 7、scikit-learn 3、sphinx 6、sympy 6；`version` 列，固定 revision），5 个 dev 镜像已覆盖其中 5 个，**其余 50 个镜像涉及 17 个新的 (repo, version)**。
- **粗略估计（不是预测）：剩余 50 个约 45–60GB**，远小于当前可用的 143.6GB。假设：17 个新环境对每个的新增占用不超过已测值（django 2.23GB、sphinx/sympy 1.80GB，scikit-learn 最坏按 5.35GB 计，共约 44GB）；其余 33 个同环境镜像每个只新增一个实例层，保守按 0.4GB 计（约 13GB）。依据只有 5 个样本，scikit-learn 的基础层与 django 似乎不共享（它新增了 5.35GB，几乎等于整个镜像），其余版本的环境层大小也未实测，拉取时以实际为准。
- 之前（写在本节早期版本里）"按平均值外推约 154GB、放不下"的说法**不成立**：它把每个仓库第一个镜像的一次性开销（基础层、新环境层）当成了每个镜像都要付的成本。已更正。
- `pull_images.py` 在可用空间低于 `--min-free-gb` 时会停下，已拉的保留，剩余标为 pending；底线只在每次开始拉取前检查，并发的 worker 会各自在途，所以全量拉取建议 `--workers 2 --min-free-gb 35`。
- 拉取耗时（单 worker）：84–210 秒/个。

## 8. 决策记录

**为什么要写预拉脚本（`eval/swebench/pull_images.py`）。** Docker SDK 和 swebench 缺镜像时都会自动拉取（§2），所以预拉不是技术必需，而是为了让度量可信：
1. 自动拉取发生在某道题的计时区间内，`latency` 的 P50/P95 会混进下载时间，还可能撞 `time_limit`。
2. mini-swe-agent 的 `pull_timeout` 默认 120 秒，冷拉 1GB 以上的镜像很容易超时，会让 B0 因环境问题而不是模型能力失败。
3. 多个 worker 同时遇到同一个未缓存镜像会重复拉取。
4. 磁盘预检、断点续跑（按 `instance_id`，CLAUDE.md §8）、`base_commit` 校验。

脚本保持精简，只做这几件事。

**为什么用 SWE-bench Verified。** 项目要交付的是 harness 的可信实验结论，实验对象是 harness（上下文策略、动作空间、规划、预算、权限层），不是被修的库。需要的是：用仓库自带测试判分（客观、不靠 LLM 评判）、任务够长（上下文压力才会显现）、有同生态的基线（mini-swe-agent）和官方判分器（`swebench`）、环境可复现（官方单题镜像）。我们取 50 题是为了控制成本，配置有 M0、B0 和多个消融。

**为什么 mini-swe-agent 放进 dev 依赖组**（`uv add --dev mini-swe-agent`），而不是 `uv tool install`：与 litellm 等依赖解析不冲突（121 个包），版本锁进 `uv.lock` 便于复现。`src/harness/` 里不得 import 它（CLAUDE.md §2.1），由 `tests/unit/test_no_agent_frameworks.py` 检查。

**局限（写 README 时用）**：只覆盖 Python 缺陷修复；django 占比大（231/500），子集的仓库配额会影响结论；公开热门仓库可能出现在模型的训练数据里，绝对解决率可能偏高，消融是同一模型的相对比较，受影响较小。

## 9. 背景（回忆，**未核实**）

以下内容来自记忆，没有在本次会话里核实，引用前要先查证：
- SWE-bench 由 Princeton 团队提出，从 12 个热门 Python 仓库收集真实 issue 与修复 PR。（12 个仓库与上面 §1 的数据一致，Princeton 一说未核实。）
- Verified 是 500 题的子集，据说经过人工标注者复核，剔除描述不清或测试有问题的题。
