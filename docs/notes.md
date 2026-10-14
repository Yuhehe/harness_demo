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
| 回传的内容算不算 prompt token？ | ~~**不算。**~~ **已被推翻（2026-10-13，见 §17.2）：计入。** 原始数据保留：同一个第二轮请求，带回传（发出 73 / 92 个字符）和去掉，`prompt_tokens` 完全相同（flash 377 / 377，v4-pro 452 / 452）。与 §17.2 的实测（M0 设置，2064 字符的推理多出 492 token）矛盾，原因没有查清（当时用的是默认思考模式、推理很短，脚本已不在）；以 §17.2 为准。 |
| 默认情况下是否一定会思考？ | **不一定。** 默认模式下 reasoning token 在 0–58 之间波动，后几轮经常是 0；显式 `thinking: enabled` 的 flash 第一轮也出现过 reasoning 为 0（只有 1 个样本）。 |
| `disabled` 能用吗？ | 能。两个模型都正常返回工具调用和最终答案，响应里没有 `reasoning_content`，`reasoning_tokens` 为空；`max_tokens=16` 也能正常得到 `pong`。 |
| `max_tokens` 太小会怎样？ | 默认思考模式下 `max_tokens=16`：`finish_reason='length'`、`content` 为空，16 个 token 全被思考用掉（已复现）；`disabled` 时正常。 |
| 两种模式的 prompt 一样吗？ | **不一样。** 同样的请求，`prompt_tokens` 在默认模式是 flash 308 / v4-pro 371，在 `disabled` 是 284 / 293。说明服务端对两种模式套的模板不同；`disabled` 第一次调用 `cached_tokens=0`，之后同模式下的第二轮才有命中。推断（未单独验证）：**缓存很可能不能跨模式共享，一次运行内不要切换思考模式**。 |
| 缓存的观察（步骤 5 的前置数据） | 所有 `cached_tokens` 都是 128 的倍数（0、128、256、384），提示命中以 128 token 为块。 |

步骤 4 的 30 次调用（两轮对话实验 14 次、工具链 16 次）共花费 **$0.005940**（LiteLLM 价格表估算，同样是高峰时段的标准价），步骤 1–4 累计 $0.007099。

**对 harness 的结论**
1. **每次调用都显式传 `thinking`**（来自配置），不依赖服务端默认值，否则服务端以后改默认值会让实验不可复现。
2. **一次运行内保持同一个 `thinking` 设置**（缓存规则，CLAUDE.md §5.2）；所有对比配置用同一个设置。
3. **保留并回传 `reasoning_content` 是低成本的**（不计 prompt token；**已被推翻，见 §17.2：回传的推理计入 `prompt_tokens`**），能避开 LiteLLM 文档里提到的报错；但上下文压缩（truncate、summarize）时丢掉旧的 `reasoning_content` 是可行的（去掉也被接受），可以减少体积。
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


## 11. 第 2 天实现中核实的接口行为（2026-10-07）

### 11.1 思考强度（`reasoning_effort`）：主流 harness 怎么用

| 来源 | 做法 | 可信度 |
|---|---|---|
| DeepSeek 官方文档（`api-docs.deepseek.com/guides/thinking_mode`，已抓取） | 思考默认开，强度默认 `high`；取值 `low/high/max`（OpenAI 格式用 `reasoning_effort`，思考开关用 `thinking.type`）；带 `tools` 时必须回传所有历史轮的 `reasoning_content` | 高（官方页面） |
| 同上，搜索摘要 | `low`/`medium` 在服务端映射为 `high`；智能体场景官方建议 `max` | 中（摘要，抓取的页面正文里没有"建议 max"这句，用前再核对） |
| DeepSeek-V4 公布的 SWE-bench Verified（搜索摘要，来自论文/模型卡） | Pro：不思考 73.6 / high 79.4 / max 80.6；Flash：73.7 / 78.6 / 79.0；官方用自己的最小 harness 在 `max` 下评测，第三方 OpenCode 的一次运行没设变体、用 API 默认 `high` | 中（二手摘要，没读原文） |
| Codex CLI（官方配置参考，已抓取） | `model_reasoning_effort`，取值 low / medium / high / xhigh 等随模型而定；不设就用模型自己的默认 | 高 |
| Claude Code（第三方博客，非官方） | 默认强度随模型：Opus 4.7 为 `xhigh`，4.6 为 `high`（订阅档位更低时为 `medium`）；自适应思考 | 低到中 |
| mini-swe-agent 2.4.6（本机源码，已核实） | `swebench.yaml` 里**没有**任何思考或强度设置，只有 `drop_params: true`、`parallel_tool_calls: true`；所以 B0 跑在服务端默认上，即 DeepSeek 的思考开 + `high` | 高 |
| SWE-agent | 没有核实 | — |

**结论**：没有统一做法。通用 harness 多数不设，用模型默认；DeepSeek 默认是 `high`，官方自己的评测用 `max`。官方数字里 `max` 比 `high` 高 1.2 个点（Pro）或 0.4 个点（Flash），而 50 题的 95% Wilson 区间宽约 22–25 个点（70% 时 56%–81%），**这个差别在我们的样本量下分辨不出来**。

**取值**：M0 和全部消融用 `high`（临时决定，等你确认）。理由：① 与 B0 的默认完全一致，对比不需要额外配置；② 强度不是消融因素，所有配置统一即可；③ `max` 要重新核对成对的 `max_tokens`/`timeout_s`（DeepSeek 在 `max` 下默认输出上限是 128K，我们的成对默认是 65536/900 秒，`length` 截断会更常见）；④ 成本：同一道很小的题上 flash 的思考 token 是 278（high）对 388（max），**只有 1 个样本，不是证据**。想量化强度的影响，用一个 P2 实验（`max` 重跑 M0），不要换主配置。

### 11.2 `reasoning_effort` 怎么传：LiteLLM 会丢掉它

- LiteLLM 1.104.0 的 DeepSeek 转换（`llms/deepseek/chat/transformation.py:36-60`）把 `reasoning_effort` 关键字弹出，只剩 `thinking` 的开或关：`high`、`max` 都到不了服务端。本机假服务器核实：`reasoning_effort="max"` 关键字 → 请求体里 `reasoning_effort` 为空；`extra_body={"reasoning_effort": "max"}` → 请求体里是 `"max"`。
- 所以 `llm/client.py` 一律放进 `extra_body`（`deepseek/` 和通用 `openai/` 两条路线都测了，`tests/unit/test_llm_client.py`）。
- **推论**：第 1 天的所有真实运行（含 `dev-*`、toy 录制）都没传强度，跑在服务端默认 `high` 上，与现在显式传 `high` 等价。
- 真实 DeepSeek 核实（flash，各 1 次）：`high` 和 `max` 都被接受，`finish_reason=stop`。

### 11.3 token 估计（`context/tokens.py`）的校准

- 工具定义约 800 token，所以把序列化后的工具定义当文本单独计数。
- **更正（2026-10-12）**：这里原来写的是"LiteLLM 的 `token_counter` 对 DeepSeek 不计 `tools`（带不带结果一样）"，**这个原因是错的**。重新测了：`token_counter(..., tools=工具列表)` 对**所有模型**（`gpt-4o`、`deepseek/…` 都一样）都抛 `KeyError: 'items'`，因为我们的 `todo` 工具的数组参数 `{"type": "array"}` 没有写 `items` 子 schema，而 LiteLLM 的 `_format_object_parameters`（`token_counter.py`）会去读它。去掉 `todo` 后，其余 5 个工具都被正常计入（对 DeepSeek 是 +421 token）。当时看到的"8 对 8"，是我自己的兜底分支（`len(json.dumps(messages)) // 4`）在两次调用里都被触发。当时只做了实测，没有读源码确认原因。结论里**文本计数工具定义**这一做法仍然成立（它不经过 LiteLLM 的格式化函数），只是理由要改。
- **已处理（2026-10-12，按方案 1）**：给 `todo` 的 `items` 补上元素子 schema（`{text: string, status: enum}`，`additionalProperties: false`，`status` 的取值复用运行时校验用的同一个 `STATUSES`）。补完后四种工具集合（`tools` / `bash_only` × 规划开 / 关）LiteLLM 的 `token_counter(tools=…)` 都能正常计入（+507 / +421 / +137 / +137 token；完整工具集的文本计数是 799，两者口径不同，仍以文本计数加服务端校准为准）。真实 DeepSeek（flash）核实：服务端接受嵌套 schema，3 次调用里 2 次给出的 `todo` 参数通过校验（样本太小，只说明可用，不说明比旧 schema 更好）。工具定义变了，toy 回放已重录（5 步，$0.0011）。下面描述的是修复前的情况：`llm/client.py` 的 `_estimate_prompt_tokens`（失败请求浪费的输入 token 估计）把 `tools` 传给了 `token_counter`，M0 的工具集里有 `todo`，所以会抛异常并悄悄回退到 `字符数 // 4`，既不含工具定义，也不是分词器的结果。影响限于 `wasted_prompt_tokens_est` 和预算保护里对失败请求的估计。两种修法：给 `todo` 的 `items` 补上子 schema（对 OpenAI 的严格校验也更合规，但会改工具定义，toy 回放要重录），或让这个估计也走文本计数。
- 用第 1 天 `dev-flash-1`、`dev-pro-1` 的 4 条轨迹（44 个调用对）对照服务端的 `prompt_tokens`：本地计数偏低 8%–21%（每条消息的模板开销和分词差异），比例在一条轨迹内很稳定。
- **更正（2026-10-11，已被推翻，见 §17.2；`strip_reasoning` 已删除）**：本地计数器会把回传的 `reasoning_content` 算进去（同一条消息 14 → 2026 token），而服务端不计，所以计算提示词大小前先去掉这个字段（`strip_reasoning`）。去掉之后在同样的 4 条轨迹上重新测得：误差平均 −0.1%，范围 −8.4% 到 +7.8%（42 个对）；dev 轨迹里推理文字很短，真实任务里推理更长，不去掉会被严重高估。下面一条是更正前的数字。
- 采用"上一次服务端实报值 + 新增消息的本地计数 × 比例"，误差：平均 +0.6%，范围 −2.2% 到 +7.3%（只用本地计数 × 比例是 +0.5%，−2.3% 到 +7.2%，两者几乎一样）。**所以水位线要留余量**：`high_water=0.85`；预算硬判断用估计值，可能有约 7% 的偏差。
- 溢出由估计值判定，估计值有约 7% 的偏差，所以 A1 的 `overflow_rate` 可能比"真实超出"早一点。所有配置共用同一个估计器，相互之间可比；报告里要注明。
- 压缩触发有滞后：压缩后若仍高于高水位线（受保护的尾部本身就很大），下一次压缩要等提示词增长一个完整水位带（`high_water − low_water`）以后，上限是预算本身。否则每一轮都会再调用一次摘要、每次都让缓存前缀失效（有测试：去掉滞后时该测试失败）。
- 压缩之后没有锚点，下一次请求前用本地计数 × 上次的比例，请求完成后重新锚定。

### 11.4 压缩后的历史形状（真实 DeepSeek）

`system, user(任务), user(摘要), assistant(tool_calls), tool(已清理的占位)` 这样**连续两条 user 消息**的历史被接受，工具调用正常返回。两种设置各核实 1 次：flash、思考关；以及 **M0 的设置**（v4-pro、思考开、`reasoning_effort=high`、带 tools，保留的那条 assistant 消息带 `reasoning_content` 和 `tool_calls`）。各只有 1 次，是"没有被 400 拒绝"的证据，不是稳定性证据。

### 11.5 权限层的边界

`configs/policy.yaml` 靠正则匹配命令文本，是纵深防御，不是证明：引号拆分、脚本内部做的事（`python -c` 里写测试文件、`open(..., 'w')`）拦不住。已知取舍：`tests/` 只匹配复数目录名，因为 `django/test/`、`sympy/testing/` 是库代码；`git stash`、`git checkout -- <file>` 常被模型用来回退，所以放行，只有 `reset --hard`、`clean -f`、`checkout .`、`stash drop/clear` 走 `ask`（批量运行里按 `deny`）。真正的边界是沙箱（断网、资源上限）；第 3 天的注入用例量的是这一层在沙箱之上多拦了多少。

## 12. summarize 的调研与更正（2026-10-11）

### 12.1 主流实现对摘要输入的做法

| 来源 | 摘要模型看到什么 | 依据 |
|---|---|---|
| OpenHands | 被遗忘的每个事件用 `str(event)` 全文；正常路径不截断，只在"硬重置重试"时才限制长度（首次重试不缩，之后每次乘 0.8） | `llm_summarizing_condenser.py` 源码（已读） |
| Complexity Trap 论文（arXiv 2508.21433v3） | 摘要由"上一份摘要 + 待压缩轮次"递归生成；混合变体里传给摘要的是**未遮蔽的完整上下文**；N=21、M=10，摘要温度 0，摘要与 agent 同一个模型 | arXiv HTML（已读；摘要模型是否逐配置相同没写清） |
| Codex CLI | 完整历史 + 压缩提示；只有上下文溢出时才从头部丢条目重试 | `codex-rs/core/src/compact.rs`（已读） |
| Claude Code | 官方文档只写"用结构化摘要替换对话"；摘要输入细节没有一手资料 | code.claude.com 文档 |

**因此我们的旧实现是偏离的**：`summarize.py` 把每条工具结果裁到 1500 字符、参数裁到 1000，而且 `hybrid` 先清理再摘要，摘要模型读到的是 `[output cleared]` 占位符。已改为：读 `original`（agent 当时看到的全文，含 `reasoning_content`），不预先裁剪，只有摘要调用因输入过大失败（`context_overflow`、`timeout`）时才逐次收紧再重试。

### 12.2 对我之前说法的更正

- "Claude Code、OpenCode 和论文混合变体都是先清理再摘要"：**没有一手依据**。Claude Code 的"先清理旧工具输出"只来自一篇第三方指南；Claude API 的 context editing 文档没有说它与压缩的先后顺序，也没提 Claude Code；OpenCode 的 gist 只列了 `prune` 的参数，没说它在摘要之前还是之后，也没说摘要看到的是裁剪前还是裁剪后（源码链接两次 404，没读到）。有一手证据的几个系统，摘要模型看到的都是原文。
- "Claude Code 和 Codex 都靠重读文件恢复状态"：Claude Code 官方文档（经搜索结果引用）说压缩后重读最近读过或改过的至多 5 个文件，超过 5000 token 的只给路径引用，并**重新读取 git 状态**；Codex 的 `compact.rs` 里没有读文件，也没有注入 git 状态，只重新注入初始上下文和最近的用户消息。"重读 5 个文件、50k token"对 Codex 来自第三方博客，**未能确认**。两家都没有公开解释为什么不用 diff。

### 12.3 遮蔽（observation masking）与 `truncate`

论文的遮蔽：只把早于滚动窗口（M=10 轮）的环境观察换成占位符（如 `Previous 8 lines omitted for brevity`），动作和推理保留，**每轮都生效**，与上下文大小无关。`truncate` 是同一类方法的变体，但不是复现：我们只在越过高水位线时触发、保护最近 6 轮、占位符带可读回的路径、短于 400 字符的不清、批量清理并带滞后。论文里"遮蔽把成本减半"来自一直开着的窗口，所以 128k 预算下 A3 很可能与 A1、M0 几乎一样，差别主要出现在 A6（32k）。

### 12.4 论文数据（SWE-bench Verified，SWE-agent，n=500，arXiv HTML 提取）

Qwen3-Coder 480B：原始 / 遮蔽 / 摘要的解决率 53.4 / 54.8 / 53.8%，单题成本 $1.29 / $0.61 / $0.64；摘要使轮数比原始多约 15%；摘要调用占单题成本 0.65% 到 7.2%。Gemini 2.5 Flash 思考模式是反例：遮蔽 −9.9%、摘要 −22.3%。我们用的也是思考模式，不要预设 A3、A4 一定优于 M0。局限：只覆盖 SWE-agent，触发条件固定，OpenHands 上只是初步验证。

### 12.5 状态块与阶段 2 的真实核实（2026-10-11）

- Claude Code 官方文档（经搜索结果引用）：压缩后重读最近读过或改过的至多 5 个文件，超过 5000 token 的只给路径引用，并重新读取 git 状态；Codex 的 `compact.rs` 里没有读文件。我们的状态块用沙箱自己的 `git diff`（补丁就是最终要判分的东西），不重读文件内容，只给读过的文件路径。两种做法哪种更好没有数据，要看阶段 4 的"压缩后重复率"。
- 状态块不进下一次摘要的输入，也不进 `compaction` span 的 `summary` 字段；否则 `summary_file_recall` 永远是 100%（状态块里本来就有 diff stat）。
- 摘要模型的输入是把工具输出拼进一条 **user** 消息，回放键"不含工具结果内容"的规则管不到它。录制一次带压缩的运行，换机器回放会对不上。**CI 的回放要继续用不压缩的配置**（toy 录制用的是 `strategy: none`，不受影响）。
- 真实核实（flash，1 次，n=1）：`dev-pro-1` 的 django 轨迹强制压缩一次。摘要输入 10517 token（全文，未裁剪），摘要输出 1200 token（提示要求至多 700 词，实际略超：字数只是软约束，硬上限是 `summary_max_tokens`），$0.0046。摘要含 7 节，状态块 339 token，补丁文件在摘要正文里被提到。

### 12.6 aggregate.py 的接口约定与探针结果（2026-10-11）

- `aggregate.py` 从 `results/runs/<run_id>/trajectories/*.jsonl` 算指标，写 `metrics.json`。**是否解决由官方判分给出，不在轨迹里**：已解决的 `instance_id` 列表放 `<run_dir>/resolved.json`（或 `--resolved`），没有时 `resolve_rate` 为 null、`fail_taxonomy` 不区分 `submitted_but_failed`。`score.py` 写好后按这个约定对接（它原计划是"把结果合并进 metrics.json"）。
- `tool_call` 和 `message` span 在这次之前没有 `step`，所以更早的运行（`dev-*`）只能算不依赖步数的指标；它们也都没有发生过压缩，不受影响。
- `summary_file_recall` 的口径：压缩之前用 `str_replace` 改过的文件里，摘要正文（只含摘要模型的文字）提到路径或文件名的占比。已知盲区：通过 `bash`（`sed -i`、重定向）做的修改看不到。没用"最终补丁里的文件"，因为压缩之后才改的文件不该出现在摘要里。
- 探针（`summary_probe.py`，flash 摘要，n=2，易题）：django-11066 在第 14 步压缩，提示 10255 → 3417 token，摘要输入 8643 token，$0.0008；sympy-16886 在第 10 步压缩，6250 → 2238，摘要输入 6040 token，$0.0034；两个摘要都提到了改过的唯一文件（召回率 1.0）。两个任务都只改了一个文件，**区分度不够**，真正的比较要等 dev_5 之后的、改多个文件的题。探针里摘要客户端的输出上限用的是默认配置（4096），不是探针自己缩小的预算，所以 sympy 的摘要输出 1304 token 超过了按小预算折算的上限；这是探针的简化，不影响真实运行。

## 13. 批量运行器与本机资源（2026-10-12）

- **本机**：`docker info` 报告 `MemTotal` 16 772 939 776 字节（15.6GB）、10 核。plan.md 里原来写的"WSL 12GB"（`.wslconfig`）与此不符，配置显然已变，以实测为准。
- **同时能跑几个沙箱**：`min((总内存 − 2GB 宿主机预留) // 单沙箱内存上限, 核数 // 单沙箱 CPU 上限)`。4g / 2 核的沙箱：内存限制出 3 个，CPU 允许 5 个，所以最多 3 个。上限是封顶不是预留，实际用量通常低得多，所以这个数偏保守，但它是不会撑爆内存的数；判分时 `swebench eval` 自己还要起容器，所以留了 2GB。并行目前是 TODO，`run_agent.py --workers` 大于 1 会报错并打印这个数。
- **步数 / 成本上限的参考**：mini-swe-agent 核心代码默认 `step_limit=0`（不限）、`cost_limit=3.0`，它的 SWE-bench 配置覆盖为 250 步 / $3；SWE-agent 每题成本上限默认 $3.0、调用次数不限，文档里的批量命令多显式设成 $2.00；OpenHands 的 SWE-bench 文档里常用 `--max-iterations 100`（远程运行用 500，100 是示例值，不是硬默认）。均来自文档和搜索结果，没有逐个读源码；"一步"在 mini-swe-agent 里是否等于一次模型调用还没核实。
- **续跑的约定**：一道题"做完"的标志是 `preds.jsonl` 里有它的行；这一行在题目跑完之后才写。半截的轨迹在重跑前改名为 `<id>.jsonl.staleN`（不被 `*.jsonl` 匹配，聚合不会把两次运行加在一起）。`env_error`（容器起不来等）记入并标记，不算"没做"，除非加 `--retry-env-errors`。
- **花费统计的范围**：`spent_so_far` 汇总 `results/runs/` 下所有已完成轨迹的 `run_end.cost_usd`，**不含 B0**（mini-swe-agent 的轨迹格式不同，`aggregate.py` 和这里都读不了）。80% 预算的判断因此漏掉 B0 的花费，B0 的适配器见 plan.md。
- **判分产物**（`swebench` 5.0.2，`swebench eval verified -p … --report-dir .`）：报告文件名是 `<model_name_or_path，斜杠换成 __>.<run_id>.json`，字段有 `resolved_ids`、`unresolved_ids`、`empty_patch_ids`、`error_ids`、`infra_failure_ids`、`ambiguous_failure_ids`、`incomplete_ids` 等。`swebench` 是控制台脚本（`.venv/bin/swebench`），没有 `__main__`，不能 `python -m swebench`。`score.py` 按 `*.<run_id>.json` 找报告，不自己拼文件名。
- **B0 的轨迹格式**（mini-swe-agent 2.4.6，`*.traj.json`，`trajectory_format: mini-swe-agent-1.1`）：`info.model_stats` 有 `instance_cost`、`api_calls`；`info.exit_status`（`Submitted`、`LimitsExceeded`、`TimeExceeded`、`RepeatedFormatError` 等）；每条 assistant 消息的 `extra.response.usage` 有 `prompt_tokens`、`completion_tokens`、`prompt_tokens_details.cached_tokens`、`completion_tokens_details.reasoning_tokens`，`extra.timestamp` 是 `time.time()`。`run_b0.py convert` 据此重建 `run_end`。mini-swe-agent 还有 `agent.wall_time_limit_seconds`（默认 0，不限），已用来对齐墙钟上限。

## 14. dev_5 冒烟的数据与观察（2026-10-12，开发运行，**不是实验结果，不进报告**）

五道 dev 题都是"<15 分钟修复"。`--allow-dirty` 运行（工作区有未提交的学习笔记）。标准价，v4-pro，思考开，`reasoning_effort=high`。

| 运行 | 配置 | 解决 | 花费 | 平均步数 | 缓存命中率 | 说明 |
|---|---|---|---|---|---|---|
| `M0-dev` | `default.yaml`（128k，hybrid） | 5/5 | $0.5453 | 37.8（10 / 79 / 32 / 56 / 12） | 97.9% | 提示词峰值 9.4k / 61.9k / 25k / 42.7k / 6.8k，没有压缩，没有被权限层拦截 |
| `B0-dev` | mini-swe-agent，同一组限制 | 5/5 | $0.2666 | 21.0（9 / 29 / 17 / 39 / 11） | 95.3% | 成本是按 token 用我们的标准价重建的，与 mini-swe-agent 自己报的数 5 题都在 4 位小数内一致 |
| `smoke-compaction` | `configs/smoke/compaction.yaml`（预算 10k） | 5/5 | $0.5114 | 38.0 | 84.0% | 4 题发生 30 次压缩（清理 24 次、其中 6 次又加摘要），无回退、无摘要失败；压缩成本占 5.9%；压缩后重复率 4.2%，不压缩的同题在同一步数处是 0%（4 道题，样本太小） |
| `M0-dev-v2`（2026-10-14，候选冻结提交 `ed6625a`） | `default.yaml`（本节之后的全部修复） | 5/5 | $0.4583 | 33.0（8 / 58 / 16 / 64 / 19） | 97.5% | 提示词峰值 8.0k–60.6k，没有压缩，没有拦截、格式错误、截断或重试 |
| `smoke-32k` | `ablations/budget_32k.yaml` | 2/2（未判分） | $0.3961 | 55.5 | — | `django-15930` 压缩 5 次，全是 `normal` 级别的清理，没有摘要；`sphinx-7889` 没越过触发线 |

**观察（都只是 n=5 的描述，不是结论）**
- 缓存命中率 97.9% 比我之前估算成本时假设的 89% 高；压缩把它拉到 84.0%，这是压缩的真实代价之一。
- 同样是 5/5，M0 的步数约为 B0 的 1.8 倍，成本约 2 倍。看了 `django-15930`：M0 用了 83 次工具调用（51 次 `bash`、24 次 `view`、4 次 `search`、2 次 `todo`、1 次 `str_replace`），B0 只有 29 次 `bash`；没有精确重复的调用、没有格式错误、没有输出被截断，所以不是 bug，更像是 `view` 的 400 行窗口让读文件变碎。这正是 A2（`bash_only` 对 `tools`）要检验的因素。
- 10k 预算下压缩的每一次都是 `minimal` 或 `unreachable` 级别（head 1573 + 尾部约 2k + 开销已经占了目标的大部分），所以那次冒烟走的全是降级路径，状态块里的索引、读过的文件、diff 正文没有出现。完整路径的真实样例来自探针：`django-15930` 第 70 步，预算 55208，规划 `normal`，提示词 44167 → 3665 token，摘要输入 51808 token（全文，未裁剪），摘要输出 1773 token，$0.0177，状态块带 diff stat 与 diff 正文、3 条落盘输出索引、11 个文件路径，摘要提到了被改的文件（召回率 1.0）。
- **（已被推翻，见 §17.1：模型基本遵守字数，偏差来自"每词 1.5 token"的假设；"压缩后比预期大"也不成立，服务端数字全部低于目标）** **"至多 N 词"不被遵守**：最小模式要求 200 词（约 300 token），摘要实际输出 510 到 806 token；探针里要求 700 词，输出 1773 token（约 1200 词）。字数是软约束，硬上限是 `summary_max_tokens`。后果：规划里按 300 token 估摘要大小偏低约一半；摘要偏长也会让压缩后的提示词比预期大。还没有处理。
- 步数上限：`django-15930` 在 10k 预算下用了 109 步才完成，超过原来的 100，所以改为 150。
- 运行前预估偏低：$0.11（依据是之前两道最简单的题的均值）对 $0.545。

## 15. 文档分层：什么写在 CLAUDE.md，什么写在代码，什么写在这里（2026-10-12，决策记录）

**问题。** CLAUDE.md 写于第 0 天，没有代码可对照。之后每实现一块，就有名字、字段、默认值与它不一致：第 1 天的日志列过一次（6 条，其中直接与 CLAUDE.md 不一致的 3 条：`completion_cost`、缺 `message` span、`no_submit` 没用上），2026-10-12 对账又找出 11 处（`deny_patterns` 实际是 `deny` / `ask`，`cached_input_tokens` 实际叫 `cached_tokens` / `tokens_cached`，回放键的字段列表漏了 `reasoning_effort` 等；其中 7 处是我凭对话记忆起草时写错的）。两次的共同点：**规则很少变，字段清单每次实现都在变，放在同一个文件里就必然过时。** 另外这个文件会被完整读进每一次会话的上下文，写进去的东西不管当前任务用不用得上都占着，稀释真正重要的规则。

**判断标准。** 往 CLAUDE.md 加内容之前问一句：**违反它会破坏实验的可信度或项目规则吗？** 会的留在那里，不会的移走。

| 放哪 | 放什么 | 为什么放这里 |
|---|---|---|
| `CLAUDE.md` | 规则（不用 Agent 框架、命令只在沙箱执行、禁止编造数字、先冻结再对比、控制预算）；必须遵守的不变量（`state.messages` 永不修改、每个 `tool_call` 恰好一条回复、CI 回放不带压缩、`policy.yaml` 与 M0 一起冻结）；指标的名字和定义（报告要统一用） | 很少变；违反了结论就不可信；每次会话都要知道 |
| 代码的模块说明 | **代码现在怎么工作**：span 的字段目录（`tracer.py`）、沙箱的约定和原因（`docker_sandbox.py`）、估计器的误差说明（`tokens.py`）、压缩的规划与状态块（`manager.py`、`restore.py`、`summarize.py`） | 要和代码一起变，在同一个 diff 里，review 时看得到；放在远处的文档里更容易过时 |
| `docs/notes.md`（这里） | **某个日期核实过的外部事实和测量数据**（LiteLLM 的接口行为、误差实测、各 harness 的做法）和**决策的理由** | 它本来就是带日期的核实记录，追加式；不适合放需要原地随字段增删的目录 |
| `plan.md` | 进度、日志、待同步清单 | 一直在变，不进规则文件 |

**这次搬了什么。** 配置表里除决定实验设计的 7 个键以外的全部（改为"以 `config.py` 为准"）；规划五级、滞后基准、状态块内容和降级顺序；各 span 的字段清单；默认数值（`max_tokens`、`timeout_s`、bash 超时，都与 `config.py` 重复）；沙箱的实现原因；LiteLLM 改写 `finish_reason` 这类接口行为（本来就在 §10）。搬走之前逐条核对了目的地有没有这段说明：`manager.py`、`restore.py`、`loop.py`、`replay.py` 已有，`tracer.py`（只列了类型名，还反过来指向 CLAUDE.md §5.7）、`tokens.py`（没有写误差）、`docker_sandbox.py`（原因只在行内注释）原来没有，先补全再删。三处代码改动只改说明文字，AST 比对确认代码逐字不变。

**结果比预期小。** CLAUDE.md 从 26 973 字节降到 24 761 字节，约 **8.2%**；我事先估计的是三分之一，**估错了**：这个文件的大头本来就是规则和不变量（§1 到 §3、§5.1 的重试与终止规则、§6 指标），这些该留着。可搬的主要是我最近加进去的实现细节。所以这次瘦身的意义不在体积，而在把"会过时的那一类"挪走。

**考虑过、没有采用的做法**
- *把这些细节整合进 notes.md 而不是代码说明*：可行，但 notes.md 是追加式的日期日志，字段目录需要原地修改，性质不合；而且离代码远，字段一变不容易想到去改它。保持放在代码里；notes.md 只放核实过的事实、测量数据和理由，代码说明里只留结论并指向这里（例如 `tokens.py` 指向 §11.3）。
- *加一致性测试*（CLAUDE.md 里带下划线、点、斜杠的反引号标识符必须在代码里存在）：能拦住 `cached_input_tokens` 这类改名，但**不加**（用户的决定）。

**这个做法防不住什么。** 在发射处加了字段而忘了改 `tracer.py` 的目录，不会有任何东西报错；放在代码里只是让 review 时更容易注意到，不是保证。实际依靠的是三样：review、`plan.md` 的"待同步清单"（改了设计当场登记一行）、`CLAUDE.md` §8 里那条"往本文件加内容之前先问"的标准。

**以后加新信息怎么选。** 先问上面那一句；不会破坏可信度的，描述"怎么工作"的写进相应模块的说明，描述"某天核实了什么、为什么这样决定"的写进这里。

## 16. 本轮测试遇到的问题（2026-10-12 至 13，dev_5 冒烟与测试编写）

按"问题出在被测对象上"和"问题出在测试方法上"分开；每条写现象、原因、处理和现状。**"未处理"的在 C 节汇总。**

### A. 冒烟暴露的问题（被测对象）

| # | 现象 | 原因 | 处理 | 现状 |
|---|---|---|---|---|
| A1 | 运行前打印的预估是 $0.11，实际 $0.5453，低了约 5 倍 | 预估取"之前同模型已完成任务的均值"，当时只有 2 道最简单的题（$0.0228）；dev_5 里另外 3 题重得多（$0.08 到 $0.27） | 预估会写明依据（"mean of N earlier finished task(s)"），所以读得出它有多不可靠；预算表已按实测更新 | **未处理**：样本太少时仍会给出一个看似确定的数；见 C |
| A2 | `django-15930` 在 10k 预算的压缩冒烟里用了 109 步，超过步数上限 100 | 上限 100 只比 M0 下同题的 79 步多 21%，而压缩会让轨迹变长 | `max_steps` 改为 150，依据写进 `default.yaml` 注释 | 已改 |
| A3 | 摘要不遵守字数要求：要求 200 词，输出 510 到 806 token；要求 700 词，输出 1773 token | ~~字数只是写在提示里的软约束~~ **原因判断错了（§17.1）**：词数在要求的 −11% 到 +25%，是"每词 1.5 token"的假设错了（实测 2.2–3.4） | 按档位实测 token 数，改为可配置（`summary_expected_tokens_*`） | **已处理（2026-10-13）** |
| A4 | 10k 预算下压缩全部走降级路径（`minimal` / `unreachable`），状态块里的索引、读过的文件、diff 正文在真实运行里没出现；32k 配置的真实运行里没触发摘要 | 10k 相对于 head（1573）加尾部（约 2k）太小；`sphinx-7889` 在 32k 下只用了 31 步，没越过触发线 | 用探针在真实的 79 步轨迹上强制压缩一次（第 70 步，44167 → 3665 token，$0.0177） | 部分覆盖：完整路径只有探针这一个样本，没有真实运行 |
| A5 | 压缩把缓存命中率从 97.9% 拉到 84.0% | 压缩改写了前缀 | 预期内的代价，已记录；压缩成本占 5.9% | 如实记录，不是缺陷 |
| A6 | M0 的步数约为 B0 的 1.8 倍（`django-15930`：79 对 29） | M0 有 24 次 `view`（400 行窗口让读文件变碎），B0 全是 `bash`；没有重复调用、格式错误或截断 | 不是 bug，是 A2（`bash_only` 对 `tools`）要检验的因素 | 观察，等消融 |
| A7 | 压缩后重复率 4.2%，不压缩的同题在同一步数处是 0% | 可能是摘要丢了信息，也可能只是偶然 | 只有 4 道题 | 信号，没有结论 |
| A8 | `smoke-32k` 没有判分 | 它是为了看压缩路径，不是为了解决率 | — | 有意的，但文档里要说 |

### B. 测试和操作方法本身的问题（过程）

| # | 现象 | 原因 | 影响 | 处理 |
|---|---|---|---|---|
| B1 | 等待命令 `until ! pgrep -f "run_agent.py …"` 永远不退出，空转了 2 小时；另一个 Monitor 空转到超时 | `pgrep -f` 匹配完整命令行，而等待命令自己的命令行里就含那串字，永远"找得到" | 无实际危害（每 3 秒一次 `pgrep`），但是残留进程；一度让我把 `still running` 当成运行没结束 | 已杀；以后用被等待进程的 PID（`kill -0`）或等日志里的结束标志，不用 `pgrep -f` 匹配带参数的命令行 |
| B2 | 变异检查里"没有任何测试运行" | zsh 不对未加引号的变量做分词，`$T`（多个路径）被当成一个参数 | 第一轮变异结果无效 | 改用数组 `"${T[@]}"` 重跑 |
| B3 | 用 `sed` 或字符串替换改文件，但文件已被 `ruff format` 换行，替换没匹配上也没报错 | 工具默认"没匹配 = 什么都不做" | **没匹配共四次，两次是静默的**：阶段 2 里补测试的两处编辑，和 `aggregate.py` 的一个变异体，都没生效，变异体因此"存活"；另两次（`score.py` 的 M2、`run_b0.py` 的 M5/M6/M8）因为有断言，直接报"没找到" | 之后所有替换都带断言（`count == 1`），没匹配就失败；变异体存活时先查是不是替换没生效 |
| B4 | 仓库根目录多了 `m__x.R.json` | `test_score.py` 的假判分器往"当前目录"写报告，变异检查 M2 故意把运行目录改成了仓库根目录 | 一个未跟踪的垃圾文件 | 已删；正常运行测试不会产生 |
| B5 | 一个新写的测试和之前一个测试里有恒真断言（`or True`） | 写的时候偷懒 | 那几行断言等于没测 | 用 `grep -rn "or True" tests` 找出并改成真实断言 |
| B6 | 假运行器里的 `Crash` 异常不触发 | 用 `BaseException` 模拟 Ctrl-C，但假运行器只对 `Exception` 抛出 | 测试本身写错 | 改成对 `BaseException` 抛出 |
| B7 | 临时目录里我的辅助脚本被清掉 | 不明（会话的 scratchpad 被清理） | 提交前要重建 | 重建；不影响仓库 |
| B8 | 为了让 `harness run`/运行器接受用户未提交的学习笔记，三次用了 `--allow-dirty` | 工作区里有 `docs/learning/python_feature.md` | 三个 smoke 运行的 `run_meta.json` 里 `allow_dirty: true` | 守卫按设计工作，也留了记录；正式运行必须在干净工作区 |

### C. 仍未解决的

1. **`run_b0.py` 没有 `run_agent.py` 的保护**：不拒绝脏工作区，也不检查同一 `run_id` 的配置是否一致（只在 `run_meta.json` 记了 `harness_config_hash`）。M0 和 B0 的对比要靠"同一份配置"，这一条应当补上。
2. **M0-dev 与 B0-dev 的限制不同**：M0-dev 在 `max_steps=100` 的配置下跑，B0-dev 在我改成 150 之后跑。这次没有任何一题触顶（M0 最多 79 步，B0 最多 39 步），所以没有影响结果，但**我在对比时没有说**；正式对比必须用同一份配置（最好同一个 tag）。
3. **预估的可靠性**（A1）：样本少于某个数时，只给上界和"没有足够的依据"，不给看似确定的期望值。
4. ~~**摘要字数不受控**（A3）~~ **已处理（2026-10-13，§17.1）**：字数是受控的，错的是换算假设。
5. **完整状态块在真实运行里没出现过**（A4）：要靠一次预算在 20k 到 40k 之间、足够长的题目的真实运行。
6. **花费统计的范围**：80% 预算守卫只汇总已转换的轨迹，B0 运行进行中的花费不在其中（B0 转换之后才计入）。
7. **`pgrep -f` 这类等待写法**（B1）已经约定不用，没有做成检查。

### D. 教训

- "测试通过"不等于"测的是对的东西"：B3 里有两次变异体"存活"，原因都是编辑没生效，而不是测试不够强。变异检查本身也要验证"改动确实生效了"。
- 估计值要附上依据，并在样本少时承认自己不可靠（A1、B1 的 `still running` 误判都是把一个没验证的判断当成事实）。
- 开发运行的配置要当场固定（A2 之后我改了默认值，同一轮里的两次运行就不再可比）。

## 17. 摘要大小与提示词估计的实测和更正（2026-10-13 至 14）

起因是 §16.A3 那条"摘要不遵守字数"。查下去，A3 的原因判断错了，并且顺带发现了一个影响更大的问题：回传的推理没有被本地计数算进去。本节的结论推翻了 §5 步骤 4、§11.3 和 §14 里的三条旧结论，那几处已在原文旁标注。

### 17.1 摘要的大小：字数是受控的，错的是换算假设

数据：`smoke-compaction` 里 6 次 minimal 档摘要（都来自 `django-15930` 一条轨迹）；在 M0-dev 三条最长的轨迹上各用探针强制做一次 full 档摘要（$0.025，结果在 `results/runs/_probes/`）。摘要模型为 flash，思考关。

| 档位 | 要求词数 | 实际词数 | 服务端输出 token | 本地 cl100k 计数 | 服务端 / cl100k | 每词 token | 像代码的词占比 |
|---|---|---|---|---|---|---|---|
| minimal（n=6） | 200 | 178–250 | 510–806 | 451–716 | 1.10–1.13 | 2.58–3.37 | 34%–39% |
| full（n=3） | 700 | 544–686 | 1332–1544 | 1231–1425 | 1.06–1.11 | 2.22–2.51 | 28%–29% |

- 词数在要求的 −11% 到 +25%，**模型基本遵守**。原代码按"每词 1.5 token"估算（`TOKENS_PER_WORD`），实测每词 2.2–3.4，这才是偏差的来源。
- **决定 token 数的是分词器和文本内容，不是 embedding。** 本地计数用的是 LiteLLM 为 DeepSeek 选的 OpenAI `cl100k_base`（`litellm.utils._select_tokenizer` 核实）；DeepSeek 服务端在摘要文本上只比它多 6%–13%，而且很稳定。每词 token 数的波动几乎全部来自内容：路径、函数名、带下划线或括号的词越多，每词 token 越多。用 `split()` 数词本身也失真，一个文件路径算 1 个词，却是十几个 token。
- 想从被摘要的历史预测摘要密度：输入的"每 token 字符数"（3.3–4.0）和摘要密度没有看得出的相关（n=9），不采用。
- **处理**：每一档直接记实测的 token 数（最大观测值 × 1.25，取整到 50），做成配置键：`summary_expected_tokens_full`（1950）、`summary_expected_tokens_minimal`（1050）；字数也做成配置键（`summary_words_*`，700 / 200）。硬上限仍是摘要调用的 `max_tokens`，预计值不超过它。从文件加载的配置，如果用到摘要，要求摘要上限 ≥ full 档的预计值（`check_summary_cap`）。**换摘要模型或改摘要提示时要重测。**
- **之前对 A6 的风险判断不成立**：我曾说 32k 下 full 档摘要可能超过 2560 的上限而被截断作废，实测最大只有 1544。
- 局限：样本小，minimal 的 6 个不独立（同一条轨迹）；摘要的 token 是 flash 的输出计数，进的是 v4-pro 的提示词，两者分词器是否相同未核实（两边对 cl100k 的比例接近：1.06–1.13 对 1.04–1.21）。

### 17.2 回传的推理计入 `prompt_tokens`

**离线证据**：本地计数去掉推理时，比例（服务端 ÷ 本地）在一次运行里随推理累积上涨。M0-dev 的 3 条轨迹从约 1.06 涨到 1.3–1.8；`smoke-compaction` 的 `django-15930` 涨到 2.0，碰到估计器的上限。计入推理后，各条轨迹的比例稳定在 1.04–1.21，标准差从 0.07–0.23 降到 0.02–0.04。

**真实对照**（2026-10-13，M0 设置：v4-pro、思考开、`reasoning_effort=high`、带工具；同一段历史发 4 种变体，`max_tokens=64`，$0.005）：

| 变体 | `prompt_tokens` |
|---|---|
| A 带推理（2064 字符），后面没有 user | 1708 |
| B 不带推理，后面没有 user | 1216 |
| C 带推理，后面有一条 user | 1710 |
| D 不带推理，后面有一条 user | 1218 |

A−B = C−D = 492：**回传的推理全部计入**，不管后面有没有新的 user 消息。§5 步骤 4 的旧结论（73–92 字符的推理，377 对 377）与此矛盾，原因没有查清，以本节为准。

**为什么之前没发现**：没压缩时的估计有锚（上一次请求的服务端数字已经包含推理），只有新增的一轮是本地估的，误差很小。没有锚的地方才会出问题：压缩的规划、清理时每条消息省下多少、hybrid 判断"清理够不够"、压缩后的大小（也就是滞后基准）。在 `smoke-compaction` 上，压缩后的估计偏差为 −16% 到 +42%。两个方向的原因：只做清理时，比例被抬高，每清一条都多算了省下的量，于是没降够就停了，结果是低估；摘要把旧推理一起替换掉，再用旧比例去乘，结果是高估。

**处理**：删掉 `strip_reasoning`，本地计数保留推理（LiteLLM 本身就计入这个字段：389 对放进 content 的 391）；失败请求的浪费估计（`client.py`）也一样。

### 17.3 估计器修复的验收（离线，不花钱）

服务端 `prompt_tokens` 作为真值。压缩后的数字用**改动前**的代码（commit `12d05c3`）回放出当时的视图，再按新口径估计，因为改动后的代码切点会变，就拿不到对应的服务端数字了。

| 数据 | 旧口径 | 新口径 |
|---|---|---|
| 压缩后（冒烟，4 题中 3 题有压缩，n=27） | −16.3% 到 +42.1% | **−8.0% 到 +4.5%** |
| 有锚：原来那 42 对（dev-flash-1、dev-pro-1） | −8.44% 到 +6.33%，平均绝对误差 1.42% | −2.61% 到 +5.87%，**0.79%** |
| 有锚：M0-dev，184 对 | −9.60% 到 +13.56%，1.33% | −2.94% 到 +12.76%，**0.47%** |

- 旧口径在 42 对上的最小值 −8.44% 和 §11.3 记录的 −8.4% 吻合，说明这次的测法和当时一致。
- M0-dev 里有一个 +12.8% 的点，新旧口径都有，与推理无关，没有追查。
- **测量方法本身出过错**：第一次验收得到 +120% 到 +146% 的"离群点"，原因是我的脚本漏算了工具定义（约 800 token），前几步的比例被抬到 3 以上。真实估计器是计入工具定义的。查出来之后修正了脚本。教训：离群点要先怀疑测量方法。

### 17.4 压缩流程的调整（同一批提交，都在冻结前）

- **规划的预留**：full = 摘要预计值 + 状态块上限（原来是两者的硬上限，128k 下 4096 + 2500）；minimal = min(预计值, 摘要上限) + `summary_restore_min_tokens`（200，原来写死在 `types.py`）。状态块由代码裁剪到不超过上限，所以它的上限是可靠的上界。
- **门槛**：先生成状态块，再判断值不值得摘要，`kept` = 这一档的预计值 + 状态块的**实际** token 数。原来是在生成之前判断，两档都用 full 档的状态块上限，128k 下 minimal 档多算了 2300。被门槛丢弃的状态块不提交它的索引。
- **hybrid 清理后重新判断是否超预算**：原来沿用清理前的判断，所以清理后已经回到预算内的提示词，仍被当作"最后机会"，跳过门槛直接摘要。测试里构造出的一个场景是：中间部分清理后只剩 240 token，摘要却要放回 533，提示词反而变大。原来有 4 个测试依赖这种"亏本摘要"才能触发摘要，已改为让中间部分足够大。
- **跳过的尝试会被记录**：新的 span 类型 `compaction_skipped`，原因包括 infeasible、nothing_to_clear、nothing_to_summarize、no_gain。这些尝试不计入压缩次数，但会推后下一次触发，所以必须看得见。`compaction` span 新增 `ratio`、`ratio_unusual`（预期范围 0.9–1.4）、`summary_words`、`summary_expected_tokens`。
- **报告口径**（`aggregate.py`）：压缩后的大小用下一次 agent 请求的服务端数字。没有下一次请求（溢出、所有尝试都失败）时用估计值，两者分开计数（`tokens_after_source`），均值只取服务端数字。**压缩前的大小永远是估计值**，因为那份提示词从来没被发出去。`monitoring` 一节报告摘要"实际 vs 预计"、跳过的原因和异常比例的次数。

### 17.5 真实冒烟（2026-10-14，开发运行，不进报告，都只跑 `django-15930`，都在干净的提交上）

| 运行 | 配置 | 结果 | 步数 | 花费 | 压缩 | 压缩后估计误差（对服务端） |
|---|---|---|---|---|---|---|
| `smoke-compaction-v2`（`efbded7`） | 10k | 提交，未判分 | 99 | $0.305 | 23 次（4 次带摘要），档位全是 minimal / unreachable | −9.0% 到 +10.2% |
| `smoke-24k`（`563b27a`） | 24k（`summary_max_ratio` 0.1） | 提交，未判分 | 77 | $0.255 | 5 次，全是 normal（1 次带摘要） | −6.0% 到 +3.2% |

- 两次都没有回退、没有摘要失败、没有被跳过的尝试，比例都在 1.05–1.28，没有触发异常标记。
- **完整状态块第一次在真实运行里出现**（24k 第 63 步）：diff 正文、落盘输出索引、读过的文件、todo 全部在内，1074/1200 token，补上了 §16.C5。
- 摘要大小：minimal 4 次，输出 512–670 token（预计 1050），词数 202–269（要求 200，最多超出 34%）；full 1 次，输出 1822 token（预计 1950），737 词（要求 700）。**full 档实测最大值从 1544 刷新为 1822**，离预计值只差 7%。按"最大值 × 1.25"重算应是约 2300；要不要上调，等更多样本。
- 估计的残余偏差有规律：只清理时低估（−0.6% 到 −9%），带摘要时高估（+3% 到 +10%）。比例是整份提示词的平均值，而不同内容（工具输出、推理、散文）相对本地分词器的偏差不同。报告已改用服务端数字；高水位线 0.85 给预算留了 15% 的余量。
- LiteLLM 的"assistant 消息缺少 `reasoning_content`，注入空格占位"警告：以前就有（M0-dev 的日志里 545 次）。来源是少数没有产生推理的回合（`django-15930` 里 79 条 assistant 消息有 5 条），每次重发都会报一次。那几轮本来就没有推理，不丢失信息。

### 17.6 发现：滞后机制的实现和说明不一致（2026-10-14；**已修，见 §18.4**）

`manager._trigger()` 的说明是：上一次压缩**没能降到高水位线以下**（或什么都没做）时，下一次要等提示词再涨一个水位带（`high − low`），上限为预算。实现却对每一次压缩都套用 `min(max(high, last_after + band), budget)`。于是一次已经降到高水位线以下、但没到低水位线的压缩（hybrid 在清理后估计 ≤ 高水位线就接受，这种情况很常见），只要落点高于 `budget − band`，下一次触发线就等于预算本身：提示词一直涨到超出预算，才作为"最后机会"压缩（不检查值不值得；如果放不下就 `context_overflow`）。

- 24k：第 55 步压缩后估计 18503（< 高水位线 20400），下一次触发线 = 24000，第 63 步在 24122 才压缩。
- 10k：23 次压缩里 17 次开始时已超预算；其中 10 次是这个原因，另外 7 次是预算太小，一轮（大的工具输出加推理）就从触发线以下冲过了预算。
- M0（128k）同样会发生：清理后落在 83.2k–108.8k 时，下一次要等到 128k。
- 这两次冒烟都没有溢出，因为最后机会那一次都成功了；但提示词会贴着预算走，摘要一旦失败就溢出，会影响 A6 的 `overflow_rate`。

## 18. 上下文压缩：现在怎么工作、为什么这样设计（2026-10-14，讨论结论）

本节是 2026-10-13 至 14 讨论的结论，只讲机制和取舍；实测数据见 §17。代码以 `context/manager.py` 及 `strategies/` 为准。

### 18.1 一次压缩的顺序

```
每次请求前 build：估计下一次提示词的大小（有锚，见 18.5）
  └─ 超过触发线（18.4）→ 压缩
       1. 规划（只做一次）：定切点和档位（18.2）
          infeasible → 不压缩（超预算就 context_overflow）
       2. 清理：把切点之前较早的工具输出换成占位符，目标是低水位线
       3. 清理后估计 ≤ 高水位线 → 结束；否则摘要：
            先生成状态块 → 判断值不值得（18.3）→ 调用摘要模型 → 替换中间部分
       4. 记下压缩后的大小 → 决定下一次触发线（18.4）
  └─ 压缩后仍超预算 → context_overflow
```

什么都没做的尝试写 `compaction_skipped`（带原因），不计入压缩次数。

### 18.2 规划档位：head 固定，在尾部和摘要之间分配空间

| 量 | 含义 |
|---|---|
| head | system、任务、工具定义；第一次请求后就是服务端的精确值 |
| 最短尾部 | 最新的 `keep_recent_min_turns`（2）轮，必须原样保留 |
| 长版 / 短版预留 | 摘要预计值 + 状态块上限。预计值按字数算（§19）：长版 700 词约 2037，短版 200 词约 807；状态块上限：长版 min(2500, 0.05 × 预算)，短版 200（都可配置） |

判定：最短尾部 + head + 短版 > 预算 → infeasible；+ 长版 ≤ 低水位线 → normal（剩余空间里尾部再往前多收几轮，最多 `tail_cap` = 0.25 × 预算；剩余空间不够时是 tail_shrunk）；否则用短版：+ 短版 ≤ 低水位线 → minimal，否则 unreachable（照样压缩，但降不到目标）。

已知不足（记入 plan 的"后续 TODO"）：摘要只有两档，长版刚好放不下时直接掉到短版，中间的空间就空着（32k 下最多约 2300 token）；`tail_cap` 的 0.25 没有依据。

### 18.3 值不值得摘要（门槛）

`省下的 − 放回的 < min_gain_ratio（0.1）× 预算` → 不摘要。放回的 = 这一档摘要的预计值 + **状态块的实际大小**（状态块只取决于切点，所以先生成它，大小是精确的）。超出预算时豁免门槛（最后一次机会）；hybrid 清理后要用新的估计**重新判断**是否超预算，否则会出现"摘要放回的比省下的还多"。

### 18.4 下一次什么时候压缩（滞后）

目的：两次压缩之间要隔开一段（每次压缩都会让缓存前缀失效），但不能吃掉"高水位线到预算"这段余量（它用来吸收估计误差和单轮的大幅增长）。按上一次压缩后的大小分三种：

| 上次压缩后 | 下一次触发线 | 理由 |
|---|---|---|
| ≤ 低水位线（压缩到位） | 高水位线 | 自然隔开一个水位带 |
| 低水位线 ~ 高水位线（hybrid 里多数是：清理后已降到高水位线以下，所以没有尝试摘要；少数是摘要不值得） | min(上次 + 水位带, 安全线)，安全线 = 高水位线 + `hysteresis_cap` × (预算 − 高水位线) | 隔开一段再压缩，但不越过安全线；到时候清理不够，就会尝试摘要 |
| > 高水位线，或尝试被跳过 | min(上次 + 水位带, 预算) | 压不下去：等提示词再涨一个水位带；不能用安全线作上限，否则触发线可能低于当前提示词，每轮都会触发 |

`hysteresis_cap` 默认 0.5，把余量平分给"间隔"和"安全"，是设计值。

**修复后的真实冒烟**（24k，`django-15930`，提交 `6b9bb5d`，$0.272）：开始时已超预算的压缩从 1 次变为 0 次（最高 22610 / 24000）；压缩从 5 次增加到 7 次（摘要都是 1 次），缓存命中率 92.2% → 89.8%，和预期的代价一致。只有一题（n=1），两次运行里 agent 的路径不同，步数和花费不能直接比。离线按录下的落点推算：新规则下"下一次触发线等于预算"在 24k 和 10k 冒烟里都是 0 次（旧规则分别是 1 次和 10 次）。

**为什么这样改**（被替换的两种做法）：
- 原代码对每次压缩都推后一个水位带、上限为预算。结果是"清理到两条线之间"之后，触发线常常就是预算本身，提示词涨过预算才压缩，而那时是不检查门槛的最后一次机会（24k 冒烟里涨到了 24122/24000，10k 冒烟有 10 次）。
- "两条线之间时直接回到高水位线"会导致蠕动：每隔一两轮就清理一下刚移出尾部的输出，缓存不断失效。

### 18.5 估计：有锚和无锚

- **有锚**（大多数请求）：上一次请求的服务端 `prompt_tokens` + 新增消息的本地计数 × 比例。误差只来自新增的那一部分，很小（平均绝对误差约 0.5%–0.8%）。
- **无锚**（压缩过程中：规划、清理、hybrid 的判断、压缩后的大小）：整份视图的本地计数 × 比例，实测约 ±10%。
- 本地计数用 `cl100k_base`，比例（服务端 ÷ 本地）约 1.04–1.21；**回传的推理服务端会计入，本地也必须计入**（§17.2）。
- 报告里压缩后的大小用下一次请求的服务端数字；压缩前的大小始终是估计值（那份提示词没有发出去过）。

### 18.6 怎么改参数

- 改摘要字数：只要在标定过的范围内（200–700 词），预计值自动跟着变，不用重测；超出范围时加载配置会被拒绝。
- 换摘要模型或改摘要提示：开跑前会被拒绝，必须重新标定（§19）。
- 从文件加载的配置，摘要上限（`summary_max_ratio` × 预算）必须 ≥ full 档的预计值，否则拒绝加载。小预算的配置要调高这个比例（10k 冒烟配置用 0.21）。

## 19. 摘要大小按字数估计（2026-10-14）

**为什么不用"每词 N token"的常数**：要求写得越短，每个词摊到的 token 越多（200 词约 3.1 token/词，700 词约 2.2 token/词）。一是因为有固定的小节标题，二是因为短摘要超出字数更多。所以用带截距的直线：

```
预计 token = (base + per_word × 字数) × margin，再取 min(结果, 摘要上限)
```

**标定**（`summary_probe.py calibrate`，$0.013）：在 M0-dev 的三条轨迹上，分别要求 200 / 450 / 700 词各摘要一次（9 个点），再加上之前的探针（3 个）和真实冒烟里的摘要（12 个），共 24 个点，用最小二乘拟合：

| 字数 | 样本 | 均值 / 最大 | 直线 | 均值偏离直线 |
|---|---|---|---|---|
| 200 | 13 | 583 / 806 | 576 | +1.2% |
| 450 | 3 | 955 / 1098 | 1016 | −6.0% |
| 700 | 8 | 1466 / 1822 | 1455 | +0.8% |

结果：base 225.0，per_word 1.757，margin 1.4（最大的"实际 ÷ 直线"，向上取到 0.05）。预计值：200 词 807，450 词 1422，700 词 2037（原来写死的是 1050 和 1950）。

- **为什么要加真实运行的数据**：只用探针拟合，余量只有 1.2，200 词的预计值约 596，可真实运行（10k 冒烟）里有 806。真实运行在小预算下摘要的内容更密。200 词这一档的真实样本都来自同一道题。
- **线性成立**：三档的均值都在直线 ±6% 以内（只有两档时无法检验，所以补测了 450）。
- **对行为的影响**：短版预留从 1050 降到约 807（一些 unreachable 会变成 minimal），长版从 1950 升到约 2037。

**适用条件，开跑前核对**：摘要模型 `deepseek/deepseek-flash`、摘要提示的哈希 `ff0ad18124c9`（`summarize.PROMPT_SHA`）、字数 200–700。三者记在配置里（`summary_calibration_*`）：
- 字数超出范围，加载配置时拒绝；
- 模型或提示不一致，`harness run` 和 `run_agent.py` 开跑前拒绝。核对只在开跑前做一次：如果在每道题里检查，批量运行会把所有题都记成 `env_error`。

**重新标定**：用 `summary_probe.py calibrate --words … --trajectory … --patch …` 跑探针，把输出的六个值写进配置。最好再加上真实运行的摘要一起拟合。
