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
| 每题成本字段在轨迹里的位置 | 已核实（冒烟测试，2026-10-05）：`info.model_stats.instance_cost`、`info.model_stats.api_calls`，每条 assistant 消息的 `extra.cost`，原始响应在 `extra.response`（含 `usage`）。**注意成本随当前时间变化（错峰价），见 §5** |

## 5. LiteLLM（1.104.0）与 DeepSeek

核实日期 2026-10-05。前面的路由、计价、思考模式源码部分是离线核实（读源码、`LITELLM_LOCAL_MODEL_COST_MAP=True` 下调用 `get_llm_provider` 和 `completion_cost`、本机假服务器）；验证计划的步骤 1–3 起有真实的 DeepSeek 调用，结果见 §5 末尾。

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
- **已核实（真实调用，2026-10-05）**：不传 `thinking` 参数时，两个模型的响应都带 `reasoning_content`，且 `completion_tokens_details.reasoning_tokens > 0`，说明**服务端默认开启思考模式**。多轮是否必须回传 `reasoning_content`：步骤 4 实测**没有被强制**（见 §5 末尾的步骤 4 结果），但样本很小。

**异常与调用**

- `litellm.acompletion` 是协程函数；`RateLimitError`、`InternalServerError`、`APIConnectionError`、`Timeout`、`BadRequestError`、`AuthenticationError`、`ContextWindowExceededError` 都存在，可用于 `llm/client.py` 的重试判断（策略见 §5 的"重试策略"）。
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
| 8 | 用本地假服务器返回 429、500、畸形 JSON | `llm/client.py` 的重试逻辑（429、5xx、超时、不可用的 200，记录次数和原因；第 1 天的单元测试，不花钱） | 0 |
| 9 | `mini-extra swebench` 对 1 道 dev 题（`-m deepseek/deepseek-flash`，很小的 `cost_limit`） | B0 的命令、`--network none` 下能否工作、轨迹里的成本字段位置 | 少量 |

步骤 1–3 通过后才有必要做 4–5；步骤 6 通过前，不要信任任何成本数字。每一步的结果和花费记进 `plan.md` 的日志，并回填本节。

**步骤 1–3 的结果（2026-10-05，真实调用，`num_retries=0`，`timeout=90`，脚本在 scratchpad，不在仓库里）**

| 调用 | 模型 | prompt / completion tokens（其中 reasoning） | 结果 | 成本（LiteLLM 内置价格表） |
|---|---|---|---|---|
| 1 ping | `deepseek/deepseek-flash` | 31 / 21（15） | `finish_reason=stop`，content `pong 🏓` | $0.000034 |
| 2 ping | `deepseek/deepseek-v4-pro` | 84 / 24（21） | `stop`，content `pong` | $0.000206 |
| 3 bash 工具 | `deepseek/deepseek-flash` | 298 / 53（15） | `finish_reason=tool_calls`，`bash` 的 `arguments` 是合法 JSON `{"command": "echo hello"}`，有 `id` | $0.000153 |
| 3 bash 工具 | `deepseek/deepseek-v4-pro` | 361 / 73（29） | 同上 | $0.000766 |

四次调用合计 **$0.001159**（LiteLLM 价格表的估算，没有和 DeepSeek 控制台对账；调用发生在约 09:52 UTC 的高峰时段，按标准价，见 §5 末尾的错峰价说明）。结论：

- **通过**：密钥有效，两个模型名真实存在，走的是 `/chat/completions`（不是 `/beta`），响应能被 LiteLLM 解析，两个模型都支持函数调用。
- 响应里的模型名是 `deepseek-flash`、`deepseek-v4-pro`（没有版本后缀）。
- **`usage` 里同时有** `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`，并被映射到 `prompt_tokens_details.cached_tokens`；这 4 次都是 0（没有重复的前缀）。
- **思考 token 算在 `completion_tokens` 里**（例如 21 个 completion token 里有 15 个是 reasoning），按输出价计费；`max_tokens` 设得太小时，思考可能把预算用光，导致 `content` 为空、`finish_reason=length`（这次 64 和 256 都没触发，但 harness 的 `max_tokens` 要给思考留余量）。
- 同样的 `ping`，flash 报 31 个 prompt token，v4-pro 报 84 个，原因没有查；两个模型的 token 数不能直接比较。
- 步骤 4、7–9 的结果见下；**5（缓存命中）、6（对账）暂不做**（你的决定：有成熟文档可参考）。注意 §5 末尾的"成本会随当前时间变化"这一发现，在不对账的情况下尤其要靠固定费率的自有价格表。

**步骤 4 的结果（2026-10-05，真实调用，两个模型各自独立测；脚本在 scratchpad）**

实验：带 `bash` 工具的多轮对话；`thinking` 参数分别是不传（服务端默认）、显式 `enabled`、显式 `disabled`；第二轮及之后的 assistant 消息分别"带回 `reasoning_content`"和"去掉 `reasoning_content`"；另有 3 轮连续工具调用的链；再加一个 `max_tokens` 太小的对照。线上请求体用 LiteLLM 的 `log_pre_api_call` 钩子抓取并核对（发出去的 `reasoning_content` 长度、有没有 `thinking` 字段）。

| 问题 | 结果 |
|---|---|
| **回传是否被强制？** | **否。** 默认思考模式下，第二轮去掉 `reasoning_content`（发出的长度为 0）不报错；显式 `enabled` 时 LiteLLM 注入的单空格占位也不报错；3 轮连续工具调用的链（flash 和 v4-pro，各一条带回传、一条去掉）全部正常完成，答案都是 alpha/beta/gamma。共 4 条链加 7 组两轮对话，没有出现 400。**局限**：链很短（最多 3 次工具调用），不能证明任何情况下都不会报错；LiteLLM 源码注释说 API 会拒绝缺失的 `reasoning_content`，我们没有复现。 |
| 回传的内容算不算 prompt token？ | **不算。** 同一个第二轮请求，带回传（发出 73 / 92 个字符）和去掉，`prompt_tokens` 完全相同（flash 377 / 377，v4-pro 452 / 452）。 |
| 默认情况下是否一定会思考？ | **不一定。** 默认模式下 reasoning token 在 0–58 之间波动，后几轮经常是 0；显式 `thinking: enabled` 的 flash 第一轮也出现过 reasoning 为 0（只有 1 个样本）。 |
| `disabled` 能用吗？ | 能。两个模型都正常返回工具调用和最终答案，响应里没有 `reasoning_content`，`reasoning_tokens` 为空；`max_tokens=16` 也能正常得到 `pong`。 |
| `max_tokens` 太小会怎样？ | 默认思考模式下 `max_tokens=16`：`finish_reason='length'`、`content` 为空，16 个 token 全被思考用掉（已复现）；`disabled` 时正常。 |
| 两种模式的 prompt 一样吗？ | **不一样。** 同样的请求，`prompt_tokens` 在默认模式是 flash 308 / v4-pro 371，在 `disabled` 是 284 / 293。说明服务端对两种模式套的模板不同；`disabled` 第一次调用 `cached_tokens=0`，之后同模式下的第二轮才有命中。推断（未单独验证）：**缓存很可能不能跨模式共享，一次运行内不要切换思考模式**。 |
| 缓存的观察（步骤 5 的前置数据） | 所有 `cached_tokens` 都是 128 的倍数（0、128、256、384），提示命中以 128 token 为块。 |

步骤 4 的 30 次调用（两轮对话实验 14 次、工具链 16 次）共花费 **$0.005940**（LiteLLM 价格表估算，同样是高峰时段的标准价），步骤 1–4 累计 $0.007099。

**对 harness 的结论**
1. **每次调用都显式传 `thinking`**（来自配置），不依赖服务端默认值，否则服务端以后改默认值会让实验不可复现。
2. **一次运行内保持同一个 `thinking` 设置**（缓存规则，CLAUDE.md §5.2）；所有对比配置用同一个设置。
3. **保留并回传 `reasoning_content` 是低成本的**（不计 prompt token），能避开 LiteLLM 文档里提到的报错；但上下文压缩（truncate、summarize）时丢掉旧的 `reasoning_content` 是可行的（去掉也被接受），可以减少体积。
4. `max_tokens` 要给思考留余量，`finish_reason=length` 且 `content` 为空要当成可识别的失败，不能当成空回答。
5. **默认用开还是关，要靠实验决定**：关闭更便宜、更可控、没有 `reasoning_content` 的处理；开启可能提高解决率，代价是 reasoning token 计入输出价。建议等第 1 天的循环能跑后，在 `dev_5` 上各跑一次再定（见 plan.md），不要现在凭感觉选。
6. B0 要与 M0 对齐：mini-swe-agent 可以用 `-c 'model.model_kwargs.thinking={"type":"disabled"}'`（`-c key=value` 按 JSON 解析，`model_kwargs` 会透传给 LiteLLM），这个写法在 mini-swe-agent 里实际是否生效还没测。

**步骤 7–9 的结果（2026-10-05）**

*步骤 7（真实调用，不花钱）*

| 输入 | LiteLLM 抛出的异常 | 说明 |
|---|---|---|
| 错误的密钥 | **`BadRequestError`，但 `status_code=401`** | 响应体 `type=authentication_error`，消息 `Authentication Fails, Your api key: ****0000 is invalid`（密钥被服务端打码）。**类名不是 `AuthenticationError`**（本机假服务器返回 401 时才是），所以 `llm/client.py` 要**按 `status_code` 分类，不能按类名**。不属于重试范围 |
| 不存在的模型名 | `BadRequestError`，`status_code=400` | 消息直接列出当前支持的名字：`The supported API model names are deepseek-flash, deepseek-v4-pro` |

*步骤 8（本机假服务器，不花钱）：LiteLLM 如何映射错误*（`litellm.acompletion`，`api_base` 指向本机）

| 假服务器的行为 | 异常类（`status_code`） | 服务器收到的请求数 |
|---|---|---|
| 429（带 `Retry-After: 7`） | `RateLimitError`（429） | 1 |
| 500 | `InternalServerError`（500） | 1 |
| 502 | `BadGatewayError`（502） | 1 |
| 503 | `ServiceUnavailableError`（503） | 1 |
| 401 | `AuthenticationError`（401） | 1 |
| 400，消息含 `maximum context length` | `ContextWindowExceededError`（400） | 1 |
| 400，其他消息 | `BadRequestError`（400） | 1 |
| 200 但响应体不是 JSON | `APIError`（**200**） | 1 |
| 200 但 JSON 被截断 | `APIError`（**200**） | 1 |
| 200 但 JSON 里没有 `choices` | `APIError`（**500**） | 1 |
| 服务器直接断开连接 | `InternalServerError`（**500**，不是 `APIConnectionError`） | 1 |
| 响应比 `timeout=1` 慢 | `Timeout`（**408**，是 `APIConnectionError` 的子类） | 1 |

- **没有隐藏重试**：每种情形服务器都只收到 1 个请求；不传任何重试参数、传 `num_retries=0`、再加 `max_retries=0` 结果相同。500 之后第二次本来会成功的"先 500 再 200"，在 `num_retries=0` 下也直接失败。所以重试要完全由我们自己的 `llm/client.py` 实现。
- **`Retry-After` 能拿到，但位置不是 `e.response.headers`**（那里是空的），而是 **`e.litellm_response_headers`**（一个包含 `retry-after` 的字典，`e.headers` 也有）。
- **只按 `status_code` 判断是不够的**：`status_code == 429 or status_code >= 500` 能覆盖 429、500、502、503、断连（映射成 500）和缺 `choices` 的 200（映射成 500），但覆盖不了超时（408）和非 JSON、被截断的 200（状态码是 200）。最终的重试策略见下面的"重试策略"。
- 原先 CLAUDE.md §5.1 的"只对 429 和 5xx 重试"没覆盖的两类：`Timeout`（408，客户端超时）和 `APIError` 且 `status_code=200`（响应体畸形）。**你已决定采用方案 B，也重试它们**，下面是问题清单和策略。
- 局限：429、5xx 只在本机假服务器上测过，没有在真实 DeepSeek 上触发；DeepSeek 真实的上下文超限文案没测（`ContextWindowExceededError` 靠消息匹配，这里用了 OpenAI 风格的文案）；本项目的 `context_overflow` 以我们自己的 token 计数为准，不依赖它。

**Timeout 与 HTTP 200 的问题清单和重试策略（2026-10-05，你的决定：方案 B）**

*已核实的事实*（本机假服务器，没有在真实 DeepSeek 上触发过）：响应比 `timeout=1` 慢 → `Timeout`、`status_code=408`、服务器只收到 1 个请求；服务器断开连接 → `InternalServerError`、500；200 但不是 JSON、200 但 JSON 被截断 → `APIError`、`status_code=200`；200 但 JSON 里没有 `choices` → `APIError`、`status_code=500`。

*Timeout 的问题*
1. **按原先的字面规则不重试，一次超时就让整个任务失败。** 一个任务有几十次 LLM 调用（mini-swe-agent 冒烟是 25 次），单次的小概率会累积。纯算术示例，不是数据：每次调用 0.1% 概率超时，25 次就有约 2.5% 的任务受影响，100 次约 9.5%。这些失败是基础设施噪声，会混进 `resolve_rate`。
2. **和断连的处理不一致**：连接被重置映射成 500 会被重试，超时同样是暂时性的网络问题却不重试。
3. **超时时长没有依据**：只测过 1 秒的假场景，真实调用里思考和长上下文会让延迟波动。
4. **超时后的计费和用量未知**：服务端可能仍在处理甚至计费，我们拿不到 `usage`，token 统计和预算保护都会漏算；如果重试，还可能重复计费。
5. **重试的细节原先没定义**：次数、退避、抖动、`Retry-After`、等待时间是否计入墙钟时间和 `latency`。
6. 是否使用流式输出还没决定，它会改变超时的语义。

*HTTP 200 的问题*（状态码 200 但响应体不可用）
1. **三种坏响应被映射成不同的异常**：非 JSON → `APIError(200)`，截断 → `APIError(200)`，缺 `choices` → `APIError(500)`。根因相同，按状态码判断只有第三种会被重试。
2. **异常的状态码是 200，反直觉**：任何"状态码 ≥ 400 才是错误"或"200 就是成功"的写法都会误判，必须同时看异常类型。
3. **容易和"模型格式错误"混淆**：CLAUDE.md §5.1 的 `format_errors` 指模型给出的工具调用格式不对（回给模型，连续 3 次终止）；传输层的坏 200 是模型根本没答，**不能计入 `format_errors`，也不能回给模型**。
4. **有一类坏响应根本不抛异常**：默认思考模式下 `max_tokens` 太小会得到 200、`finish_reason='length'`、内容为空（步骤 4 复现过）；内容为空、没有工具调用、`finish_reason='stop'` 也属于这类。要靠我们自己检查。
5. 重试本身是安全的，因为 harness 状态在调用成功前不会改变。
6. 回放模式（`llm/replay.py`）不会录到这些错误，只能用假服务器测。

*共同的缺口*：429、5xx、408 从没在真实 DeepSeek 上触发过；DeepSeek 真实的错误文案没测；失败的调用没有 `usage`，会让 token 和成本统计偏低；凭记忆（**未核实**）DeepSeek 在高负载时可能长时间保持连接而不是直接报错，如果属实，表现为长时间等待和超时，而不是坏的 200。

*可选方案*

| 方案 | 做法 | 代价 |
|---|---|---|
| A 严格 | 完全按原先的字面，超时和坏 200 都不重试，直接 `env_error` | 最简单，但基础设施噪声直接污染结果 |
| **B（已采用）** | 重试超时和不可用的 200，与 429、5xx 共用上限，按（异常类型、状态码、响应形状）判断，记录重试原因 | 要改 CLAUDE.md §5.1 |
| C 折中 | 只重试超时，坏 200 不重试 | 部分覆盖 |

*重试策略（已写进 CLAUDE.md §5.1 和 §5.7）*

| 项 | 取值 |
|---|---|
| 次数 | 总共最多 3 次尝试（首次加最多 2 次重试） |
| 退避 | 指数退避，加随机抖动 |
| 429 | 响应带 `Retry-After` 时按它等待（在 `e.litellm_response_headers["retry-after"]`） |
| 可重试 | 429；5xx（LiteLLM 映射出的 500、502、503、断连，和缺 `choices` 的 200）；`Timeout`；不可用的 200（非 JSON、被截断，或 `finish_reason` 为 `insufficient_system_resource` / `aborted`，后者丢弃半截内容） |
| 不重试 | 4xx，含 400（包括 `ContextWindowExceededError`）、401（注意错误密钥是 `BadRequestError` 但状态码 401）、404；以及 `finish_reason=length` 且内容为空这类**不是传输故障**的失败 |
| 判断依据 | （异常类型、状态码、响应形状），不能只看状态码，也不能只看异常类名 |
| 记录 | 每次重试的次数和 `retry_reason`（建议取值：`rate_limit`、`server_error`、`timeout`、`bad_response`、`interrupted`）写进 `llm_call` span；失败的尝试没有 `usage`，统计时要标注可能漏算 |
| 用尽之后 | 以 `env_error` 终止任务 |
| 时间 | 重试的等待时间计入墙钟时间和延迟（我取的默认做法，不是你明确说过的） |

*后续已决定*：`llm.timeout_s`、`max_tokens`、流式、截断和失败尝试的处理，见下面"输出上限、超时、流式和失败尝试的调研与决定"。仍未验证：真实 DeepSeek 上的 429、5xx、超时的实际表现。第 1 天写 `llm/client.py` 时，用本机假服务器覆盖上面的每一类（包括 4xx 和 `finish_reason=length` 不重试）。

**输出上限、超时、流式和失败尝试的调研与决定（2026-10-06，你的决定：全部按我的方案）**

*实测（真实调用，每项只有 1 个样本，网络平静时；`deepseek/` 前缀，`num_retries=0`）*

| 实验 | 结果 |
|---|---|
| 要求用 `write_file` 写 40 行注释，`max_tokens=60`，关闭思考 | `finish_reason='length'`；工具调用的 `arguments` 只有 61 个字符，**不是合法 JSON**（停在 `"content": "# line 1: some filler words here`） |
| 同一个请求，`max_tokens=900` | 实际用 453 个 token，`arguments` 1422 个字符，合法 JSON |
| 默认思考，`max_tokens=120` | 120 个 token **全是思考**，没有内容，也没有工具调用 |
| 约 1500 token 的代码答案，`max_tokens=1500` | 两个模型都是 `finish_reason='length'`（被截断） |
| 生成速度（含预填充，小提示词），关闭思考 | flash：1500 token / 5.4 秒 ≈ 278 token/秒；v4-pro：1500 token / 10.4 秒 ≈ 145 token/秒 |
| 生成速度，flash，思考开（1500 个 token 全是思考） | 7.4 秒 ≈ 203 token/秒 |

按这个速度，**输出刚好顶到上限时**的耗时（纯算术，没有算负载变慢）：

| 上限 | v4-pro（145/秒） | flash（278/秒） |
|---|---|---|
| 8192 | 56 秒 | 29 秒 |
| 16384 | 113 秒 | 59 秒 |
| 32768 | 226 秒 | 118 秒 |
| 65536 | 452 秒 | 236 秒 |

*DeepSeek 官方文档*：并发上限 flash 2500、v4-pro 500，超过返回 429；排队时非流式返回空行、流式返回 `: keep-alive`；**推理 10 分钟内没开始**服务端就关闭连接（指排队，不限制生成总时长）；`max_tokens` 默认关闭思考 8K、思考 64K（`reasoning_effort=max` 时 128K），范围 1 到 393216；`finish_reason` 取值有 `stop`、`length`、`content_filter`、`tool_calls`、`insufficient_system_resource`（推理系统资源不足被打断）、`aborted`（生成被中断）。**`aborted` 官方只有这一句话，没有触发场景、计费和是否重试的说明**；中断或超时的请求怎么计费，官方也没写。

*主流 harness*（来源性质不同，可信度已标）

| 工具 | 单次请求超时 | 重试 | 流式 | 输出上限 | 失败尝试的 token | 来源 |
|---|---|---|---|---|---|---|
| OpenAI Python SDK 2.54.0 | 600 秒，连接 5 秒 | 2 次，退避 0.5–8 秒 | — | — | — | 本机源码 |
| LiteLLM 1.104.0 | 默认 6000 秒 | `DEFAULT_MAX_RETRIES=2`（我们的实测中每次失败只发 1 个请求） | — | — | — | 本机源码 |
| Anthropic Python SDK | 10 分钟 | 2 次，含连接错误、408、409、429、≥500，超时也重试 | 长请求建议流式；非流式预计超过约 10 分钟会抛 `ValueError` | `max_tokens` 必填 | — | 官方文档 |
| Claude Code | `API_TIMEOUT_MS` 默认 600000；流式空闲看门狗 600000 毫秒，响应体空闲 5 分钟 | `CLAUDE_CODE_MAX_RETRIES` 默认 10 | 是（有看门狗，从设置推断） | `CLAUDE_CODE_MAX_OUTPUT_TOKENS`，未知模型默认 32000、上限 128000 | 没查到 | 官方文档 |
| Codex CLI | `stream_idle_timeout_ms` 默认 300000 | `request_max_retries` 4，`stream_max_retries` 5 | 是（推断） | 没查到 | 没查到 | **二手**（搜索结果引用的文档副本，没取到原文） |
| Aider | `--timeout` 默认 None | — | `--stream` 默认 True | 用模型默认；`finish_reason=length` 时，模型支持助手预填充就自动续写，否则报错 | 没查到 | 源码 |
| SWE-agent | 没有显式设置 | 20 次，等待 10–120 秒 | 否 | `max_output_tokens` 默认 None（从 LiteLLM 价格表查）；**不专门处理 `length`** | **只在成功响应后更新统计** | 源码 |
| OpenHands | V0 文档默认 0 | 8 次，等待 15–120 秒，倍数 2.0 | 没查到 | 没查到 | 没查到 | 官方文档，但是 **V0 旧版** |
| mini-swe-agent 2.4.6 | 没有显式设置 | 10 次，指数退避（4–60 秒） | 否 | 不设，用服务商默认 | **成本只在重试循环结束后、对成功的响应计算** | 本机源码 |

规律：面向人的交互工具（Aider、Claude Code、Codex）流式，批量评测用的 harness（SWE-agent、mini-swe-agent）不流式；超时要么是 SDK 级 10 分钟，要么不设；没有人设很小的输出上限；失败的尝试几乎都不计入统计。服务商侧：取消的流式请求输入 token 仍然全额计费、已生成的输出也计费（第三方博客和社区帖，**可信度低**）。

*决定*

| 项 | 决定 |
|---|---|
| 流式 | **当前只用非流式**，基本功能全部完成后再支持流式。M0 和 B0 都不流式，和 SWE-agent、mini-swe-agent 一致；非流式的 `usage` 在最终响应里，回放按请求哈希找响应也更简单。重新评估的触发条件：真实运行里超时超过 1%，或 p99 延迟超过 120 秒。改用流式前要先验证 LiteLLM 对 DeepSeek 的流式 `usage` |
| 输出上限和超时（成对设置，写成配置项并记在 `run_start`） | **思考开：`max_tokens=65536`、`timeout_s=900`**；**思考关：`max_tokens=16384`、`timeout_s=300`**。思考开的值与 DeepSeek 默认值、B0 默认值一致。超时按"最慢模型 v4-pro 顶满上限的耗时 × 2"算（452 秒 × 2 ≈ 900 秒；113 秒 × 2 ≈ 226 秒，取整到 300 秒，并给预填充留余量） |
| 设置原则 | **超时必须大于顶满上限的耗时**，让失控的生成先撞上上限，得到 `finish_reason=length`（不重试），而不是先撞上超时（会被重试 3 次，把同样的失控生成重复 3 遍） |
| 截断（`finish_reason=length`） | 不重试；半截的工具调用**绝不执行**；给模型一条"输出被长度限制截断，请改小"的反馈；计入 `truncated_outputs`，**不计入** `format_errors`；连续 3 次就终止任务，记为 `format_error`，详情字段写 `output_truncated`（不改变 CLAUDE.md 的终止原因取值表） |
| 可重试的 `finish_reason` | `insufficient_system_resource`、`aborted`（它们是状态码 200 的响应，算"不可用的 200"，**丢弃半截内容**后重试）；`retry_reason` 新增取值 `interrupted`。`aborted` 的触发场景官方没写，所以单独记录，第一次在真实运行里遇到时再看是什么场景 |
| 重试次数和退避 | 总共最多 3 次尝试；退避初始值是基数 4 秒、倍数 3、上限 60 秒、加抖动（和 mini-swe-agent 的 4–60 秒同量级，可调）；主流 harness 的次数是 10–20 次、等待 4–120 秒，我们偏少，真实运行中如果经常重试用尽再调大 |
| 失败尝试的 token | 主指标（`tokens_*`、`cost_per_task`）**只统计成功的调用**，与 SWE-agent、mini-swe-agent 一致，各配置之间可比；**额外**为每次失败尝试写一条 span（`outcome`、`retry_reason`、延迟，`usage` 为空），单独报告 `failed_attempts`、`retry_rate`、`wasted_prompt_tokens_est`（用 `litellm.token_counter` 估算输入）；预算保护把这个估计也算进去，但不混进上面的指标 |
| `context.budget_tokens` | **只算发给模型的提示词（输入）**，不含输出上限；否则消融 A6（32K）装不下 64K 的输出上限，M0（128K）也会被扣掉一半。API 的真实限制是输入加输出不超过 100 万，永远不会触顶 |
| B0 对齐 | 思考开时 mini-swe-agent 的默认（不设上限，用 DeepSeek 默认 64K）与我们一致；思考关时它的默认是 8K，我们是 16K，所以**显式**加 `-c model.model_kwargs.max_tokens=16384`，超时同理加 `-c model.model_kwargs.timeout=...`（`model_kwargs` 会透传给 `litellm.completion`，`litellm_model.py:66-71`；在 mini-swe-agent 里实际是否生效**尚未实测**） |

*仍然未知或未验证*：真实 DeepSeek 上的 429、5xx、超时的实际表现；失败请求的计费；`aborted` 的触发场景；非流式长调用的连接空闲风险（输出接近上限时连接会空闲数分钟，有些网络会丢弃空闲连接；实际输出很短，观测最大 1755 个 token，所以只会发生在失控时）；速度数据每项只有 1 个样本，要用第 1 天在 `dev_5` 上记录的真实延迟校准这些数值；"每次尝试之前检查任务的墙钟时间上限"要写进 `llm/client.py`（最坏情况下 3 次尝试 × 900 秒 = 45 分钟）。

来源：[DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)、[DeepSeek 限流与连接](https://api-docs.deepseek.com/quick_start/rate_limit)、[Anthropic Python SDK](https://platform.claude.com/docs/en/api/sdks/python)、[Claude Code 环境变量](https://code.claude.com/docs/en/env-vars)、[OpenHands V0 配置](https://docs.openhands.dev/openhands/usage/v0/advanced/V0_configuration-options.md)、[SWE-agent models.py](https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/sweagent/agent/models.py)、[Aider args.py](https://raw.githubusercontent.com/Aider-AI/aider/main/aider/args.py) 和 [base_coder.py](https://raw.githubusercontent.com/Aider-AI/aider/main/aider/coders/base_coder.py)、[Codex 配置文档副本（二手）](https://fossies.org/linux/codex-rust/docs/config.md)、[第三方：DeepSeek finish_reason 说明](https://chat-deep.ai/docs/deepseek-finish-reason/)。

*步骤 9（mini-swe-agent 冒烟，真实调用）*

命令：`mini-extra swebench --subset SWE-bench/SWE-bench_Verified --split test --filter '^django__django-11066$' -w 1 -m deepseek/deepseek-flash -c swebench.yaml -c agent.step_limit=30 -c agent.cost_limit=0.3 -c 'environment.run_args=["--rm","--network","none"]' -o results/runs/B0_smoke`（环境变量来自 `.env`，数据集 revision 核对通过；比文档里原先写的多了 `step_limit=30` 来限制时间）。

| 项 | 结果 |
|---|---|
| 退出 | 退出码 0，状态 `Submitted`，用时 99 秒，没有报错 |
| 调用量 | 25 次 API 调用，`step_limit=30` 和 `cost_limit=0.3` 都没触发 |
| `--network none` | agent 在断网容器里正常完成任务，生成了 767 个字符的补丁 |
| 轨迹里的成本字段 | `info.model_stats.instance_cost` 和 `.api_calls`；每条 assistant 消息有 `extra.cost`；`extra.response` 是完整的原始响应（含 `usage`）；还有 `info.exit_status`、`info.submission`、`info.config` |
| 思考模式 | 25 条 assistant 消息里 23 条带 `reasoning_content`（默认开启），mini-swe-agent 把它存在消息里 |
| 本次运行的 token（只是一次冒烟，不是实验结果） | Σprompt 272,239，Σ缓存命中 260,864（命中率 0.958），Σcompletion 9,991（其中思考 6,275）；prompt 从第 1 次的 1,752 涨到最后的 18,725 |
| 容器清理 | mini-swe-agent 用后台的 `docker stop` 清理，运行刚结束时 `docker ps -a` 还能看到容器，几秒后消失，没有残留 |
| 判分 | 在 `results/runs/B0_smoke/` 下运行 `swebench eval verified -p preds.json --run-id B0_smoke -j 1 -i django__django-11066 --report-dir .`，用时 34 秒，`Instances resolved: 1`；报告文件名是 `deepseek__deepseek-flash.B0_smoke.json`；**会在当前目录下建 `logs/`**，所以要在被 gitignore 的目录里运行。这是 dev 题的冒烟，不是实验结果，不进报告 |

**意外发现：LiteLLM 的成本会随当前时间变化（错峰价）**

冒烟的 `instance_cost` 是 $0.008483；按 `usage` 和标准价手算是 $0.016967，**每一次调用都恰好是 2 倍**。原因：

- LiteLLM 价格表里这两个模型都有 `off_peak_pricing`，错峰价是标准价的 **50%**（输入、输出、缓存命中都是）。
- 错峰窗口（UTC）：周一到周五 `00:00–01:00`、`04:00–06:00`、`10:00–24:00`；周六、周日全天。（北京时间的高峰期是工作日 09:00–12:00 和 14:00–18:00。）
- **`completion_cost` 用的是计算时的当前 UTC 时间**（`datetime.now(timezone.utc)`），**不是响应里的 `created`**：我把同一个响应的 `created` 改成高峰、错峰、周末，结果都一样。所以今天稍晚或明天重算，同一条记录会得到不同的金额。
- 实际时间线：步骤 1–4 在约 09:52 UTC（高峰，标准价），冒烟在 10:04 UTC（错峰，半价）。两批数字**不能直接比较**。
- **DeepSeek 实际是否这样计费没有核实**（步骤 6 暂不做）；这里只能说 LiteLLM 内置价格表是这么写的。

**已决定（2026-10-05，以最后一次为准）：同时保留 token 指标和美元成本；美元成本用固定的高峰价（标准价），去掉峰谷影响**

*两套指标都报告*
1. **token 指标**：`tokens_in`（拆成未命中缓存和命中缓存）、`tokens_out`（含思考 token，思考部分单列）、`cache_hit_rate = Σ缓存命中 / Σprompt`、`steps`、`latency`，以及压缩调用自己的 token。
2. **美元成本**（`cost_per_task`）：由每次调用记录的 token 数和**固定的标准价**算出，**不依赖时钟**：

   `cost = (prompt − 缓存命中) × 输入价 + 缓存命中 × 缓存命中价 + completion × 输出价`（`completion` 已包含思考 token，按输出价计）

   标准价（每 token）取 LiteLLM 1.104.0 本地价格表里的标准字段：`deepseek-flash` 输入 3e-07、输出 1.2e-06、缓存命中 6e-09；`deepseek-v4-pro` 输入 1.32e-06、输出 3.96e-06、缓存命中 4.4e-08。这是"高峰期价格"（LiteLLM 里的错峰价是它的 50%）。**不代表 DeepSeek 实际账单**（没有对账），只是一个固定、可比较的口径。
3. 预算保护（CLAUDE.md §2.5 的 `max_cost_per_task_usd`、`MAX_TOTAL_COST_USD`、80% 规则）**仍按美元，用同一个标准价**，偏保守（实际错峰时更便宜）。**CLAUDE.md 不需要修改。**
4. 跨模型比较：成本按各模型自己的标准价，token 数则要小心（同一句 `ping`，flash 报 31 个 prompt token，v4-pro 报 84 个，原因没查，不同模型的计数口径可能不同），**比率类指标**（缓存命中率、相对 M0 的变化）更可靠。

*已验证（2026-10-05，离线，用冒烟轨迹里 25 次调用的 `usage`）*

| 计算方式 | 合计 |
|---|---|
| LiteLLM `completion_cost`，按当前时钟（当时是错峰） | 0.008483 |
| 上面的公式 + 标准价 | **0.016967** |
| LiteLLM `completion_cost`，把错峰判断强制关掉（高峰） | **0.016967** |

公式与 LiteLLM 的标准价结果完全一致，可以直接作为 `llm/cost.py` 的实现依据。

*落地方式*
- **我们自己的 harness**（第 1 天的 `llm/cost.py`）：用上面的公式和一张价格表，**不调用 `completion_cost`**，所以不受时钟影响；每个 `llm_call` 的 span 同时记录原始 token 数和算出的成本；`run_start` 记录价格表的版本（内容的哈希）。要加一个单元测试，对照 LiteLLM 的标准字段，防止价格表和 LiteLLM 漂移（`tests/unit/test_price_registry.py` 里已经有同样的检查，针对 B0 用的覆盖文件）。
- **B0（mini-swe-agent）**：它内部用 `litellm.completion_cost`，成本随时钟变化，而它的 `cost_limit` 也按这个成本触发。解决办法：`export LITELLM_MODEL_REGISTRY_PATH=$PWD/eval/baselines/litellm_registry.json`。这个文件把两个模型的**错峰价设成与标准价相同**，mini-swe-agent 初始化时用 `litellm.utils.register_model` 加载（`litellm_model.py:61-62`）。已离线验证：不设这个变量时，mini-swe-agent 自己的 `_calculate_cost` 对 25 次调用合计 0.008483，设了之后是 0.016967。（只传 `off_peak_pricing: null` 没用，合并时被忽略，必须把错峰价写成与标准价相同。）
- 单元测试 `tests/unit/test_price_registry.py`（4 个）：覆盖文件的标准价与 LiteLLM 一致；不用覆盖文件时，成本确实随错峰判断变化（错峰是高峰的一半）；用了覆盖文件后，无论是否错峰，两个模型的成本都等于标准价。它依赖 LiteLLM 内部的 `_is_off_peak`，升级 LiteLLM 时要留意。
- **`LITELLM_LOCAL_MODEL_COST_MAP=True`**：测试里用 `tests/conftest.py` 固定；harness 运行时也应该固定，价格表就来自 `uv.lock` 锁定的 LiteLLM 版本，不在每次导入时从 GitHub 抓取（国内可能不通，也不可复现）。
- 仍然不知道的：DeepSeek 实际是否按错峰价计费（步骤 6 没做）。在这个口径下不影响对比。

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

## 10. 第 1 天实现中核实的接口行为（2026-10-06）

**DeepSeek 的消息格式**（真实调用，flash，关闭思考，8 种历史）

| 历史 | 结果 |
|---|---|
| 1 个调用 + 1 条回复 | 正常 |
| 调用的参数不是合法 JSON（`{"command": "ls`）+ 一条错误回复 | **接受** |
| 调用的参数是空字符串 + 回复 | 接受 |
| 有 `tool_calls`，后面是 user 消息，没有回复 | **400**：`An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'` |
| 两个调用，只回复了第一个 | **400**（同上） |
| 两个调用，都回复了 | 正常 |
| tool 回复指向不存在的调用 id | **400**：`Messages with role 'tool' must be a response to a preceding message with 'tool_calls'` |
| 纯文字的助手回复，后面是 user 提示 | 正常 |

结论：每个 `tool_call` 必须恰好有一条 tool 消息，所以即使调用没被执行（被权限层拒绝、提交后的多余调用、格式错误）也要回复；被 `max_tokens` 截断的回复不存进历史，只追加一条 user 反馈。

**LiteLLM 1.104.0**
- 不认识的 `finish_reason`（`aborted`、`insufficient_system_resource`）被改成 `stop`，只打一条警告（`core_helpers.py:256`）；**原始值在 `choice.provider_specific_fields["native_finish_reason"]`**。
- `RateLimitError`、`InternalServerError`、`Timeout`、`ContextWindowExceededError` 等继承自 **openai 的 `APIError`**；LiteLLM 自己的 `APIError`（响应体畸形）是另一个类，两族都要处理。

**Docker 沙箱**
- Docker SDK 的 `exec_run` 没有超时：用容器内的 `timeout -k 5 N bash -lc ...`，它会向整个进程组发信号。
- 容器的 1 号进程如果不是 init，被杀掉的孤儿进程会变成僵尸并占用 `pids_limit`：创建容器时加 `init=True`。
- `git diff` 不含未跟踪的新文件；而复制出来的 index 带着 stat 缓存，同大小、同一秒内的覆盖写入会被误判为未变化。做法：每次用 `git read-tree HEAD` 重建临时 index，再 `git add -A` 和 `git diff --cached HEAD`，不动真正的 index。
- `put_archive` 默认保留宿主机的 uid，会让 git 报"dubious ownership"：tar 条目设 uid、gid 为 0，并在容器里加 `safe.directory '*'`。
- `python:3.12-slim` 没有 git。`tests/fixtures/toy_image/Dockerfile` 用 apt 加 git，**在本机网络下构建用了 9 分 22 秒**（拉基础镜像只要 49 秒）。
- 在真实的 django 镜像上，`bash -lc` 里是实例自己的 conda 环境（Python 3.6），重建 index 并取 diff 约 2–3 秒。

**回放的确定性**
- 同一仓库两次加载，`git log` 的哈希和 `ls -l` 的日期每次都不同（提交时间和文件修改时间）；固定文件修改时间（2000-01-01）和提交日期后逐字节相同。
- 工具输出里的耗时、临时路径、地址、时间戳、`ls` 日期用规范化处理。
- `find`、`ls` 的输出顺序取决于宿主机文件系统，无法规范化，所以请求的键不含工具输出内容，只比较规范化后的摘要并报告差异。
- 端到端测试里先跑一遍项目的测试会生成 `__pycache__`，改变 agent 看到的环境，导致回放对不上：测试前置检查要放在另一个容器里。
- **录制必须用显式设了 `llm.model` 的配置**：键里用的是 `cfg.model or $MODEL_MAIN`，CI 没有 `.env`，靠环境变量取模型的录制会对不上。
- 严格比对工具输出的测试（`test_on_the_recording_machine_...`）只在设了 `HARNESS_STRICT_REPLAY=1` 时运行，因为 `find`、`ls` 的顺序依赖宿主机文件系统，换机器会失败。
- **回放的轨迹没有 `llm_call` span**（`ReplayClient` 不写）：回放的运行只有 `run_end` 里的合计，没有逐次调用的 token、缓存命中和延迟。第 4 天 CI 的冒烟测试和 `aggregate.py` 如果要和真实运行同形状，需要由回放根据录制的 usage 补写这些 span（小改动，还没做）。

**另一个历史形状**（真实调用，flash）：内容为空、没有 `tool_calls` 的助手回复（例如只有思考就 `stop`）放进历史后，后面接一条 user 提示，三种情况（思考关、思考开且带 `reasoning_content`、思考开不带）**都被接受**，所以循环不需要特殊处理空的助手回复。

