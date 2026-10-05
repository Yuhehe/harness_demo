# 基线 B0：mini-SWE-agent

> 核实日期 2026-10-04，版本 mini-swe-agent **2.4.6**（`uv add --dev` 安装，锁在 `uv.lock`）。依据是源码和 `--help` 实测，细节与行号见 `docs/notes.md` §4。
> 本文件里的 `{{…}}` 是还没确定的值（取决于 `configs/default.yaml`，目前尚未创建），不估算。

## 1. 定位

B0 是外部基线：同一个模型、同一个题目子集（`configs/subsets/verified_50.txt`）、同样的步数和成本上限，**只有 harness 本身不同**。判分用同一个官方判分器，保证对比公平。`src/harness/` 不允许 import 它（`tests/unit/test_no_agent_frameworks.py` 检查）。

## 2. 安装与环境

```bash
uv add --dev mini-swe-agent          # 已完成，版本锁在 uv.lock
export MSWEA_GLOBAL_CONFIG_DIR="$PWD/.mswea"   # 必须在任何 mini-* 命令之前设置
set -a; source .env; set +a                    # 导出 OPENAI_API_KEY / OPENAI_API_BASE / MODEL_MAIN 等
```

- mini-swe-agent 在 import 时就会创建全局配置目录并读取其中的 `.env`（`minisweagent/__init__.py:26-36`），默认是 `~/.config/mini-swe-agent`。把它指到仓库内的 `.mswea/`（已加入 `.gitignore`），避免污染家目录，也避免读到别处的密钥。
- 模型走 LiteLLM。`-m` 的值要能被 LiteLLM 识别，并且 LiteLLM 要有它的价格数据（见 `docs/notes.md` §5：建议 `deepseek/` 前缀，尚待一次最小调用确认）。**目前 `.env` 里的 `MODEL_MAIN=deepseek-v4-pro` 没有提供商前缀，直接 `-m "$MODEL_MAIN"` 很可能无法路由**，要先按 §5 处理。缺价格时 `MSWEA_COST_TRACKING=ignore_errors` 能让它不报错，但会让 `cost_limit` 失效，B0 的成本数字就无效，**仅作最后手段**。

## 3. 命令模板（批量，Verified 子集）

```bash
uv run mini-extra swebench \
  --subset SWE-bench/SWE-bench_Verified --split test \
  --filter "^($(paste -sd'|' configs/subsets/verified_50.txt))\$" \
  -w {{WORKERS}} -m "$MODEL_MAIN" \
  -c swebench.yaml \
  -c agent.step_limit={{STEP_LIMIT}} -c agent.cost_limit={{COST_LIMIT}} \
  -c 'environment.run_args=["--rm","--network","none"]' \
  -o results/runs/B0
```

| 参数 | 为什么这样写 |
|---|---|
| `--subset SWE-bench/SWE-bench_Verified` | 别名 `verified` 指向 `princeton-nlp/SWE-Bench_Verified`，与 swebench 判分用的数据集 id 不同，所以显式写全称 |
| `--split test` | 默认是 `dev`，Verified 只有 `test` |
| `--filter "^(id1\|id2\|…)\$"` | 没有读文件的参数；`--filter` 用 `re.match`（前缀匹配），必须加 `^…$` 才是精确匹配。用 `paste -sd'\|'` 直接从子集文件生成，不用另写脚本 |
| 不用 `--shuffle` | 它没有随机种子，不可复现 |
| `-c swebench.yaml` | 传了任何 `-c` 就会**替换**默认配置，所以要显式带上内置的 `swebench.yaml`（默认模型是 anthropic，`-m` 会覆盖它） |
| `agent.step_limit` / `agent.cost_limit` | 默认是 250 步、3.0 美元；要改成与 M0 相同的上限：`{{STEP_LIMIT}}`、`{{COST_LIMIT}}` 取自 `configs/default.yaml` |
| `environment.run_args` | 见 §4；列表按 JSON 解析（`config/__init__.py:39-43`，已离线验证），会**整体替换**默认的 `["--rm"]`，所以 `--rm` 要保留 |
| `-o results/runs/B0` | `results/runs/` 已被 gitignore |

**跑之前先核对数据集 revision**：mini-swe-agent 用 `load_dataset` 加载的是数据集的**最新** revision（`swebench.py:224-226`），不是固定的那个。核对最新 sha 是否仍等于 `configs/subsets/subsets_meta.json` 里的 `revision`：

```bash
PYTHONPATH=eval/swebench uv run python -c "import json; from select_subset import DATASET_ID, resolve_revision; m = json.load(open('configs/subsets/subsets_meta.json')); cur = resolve_revision(DATASET_ID, None); assert cur == m['revision'], (cur, m['revision'])"
```

不一致就说明数据集已更新，要先决定怎么处理，不能直接跑。

重跑会跳过 `preds.json` 里已有的题，实现断点续跑；`--redo-existing` 才会重做。

## 4. 与 M0 对齐的差异清单

唯一应该不同的是 harness 本身，其余尽量对齐。已知的差异（默认值下）：

| 项 | mini-swe-agent 默认 | M0 | 处理 |
|---|---|---|---|
| 网络 | `docker run --rm`，**联网** | `network_mode="none"` | 加 `--network none`（建议，待你确认） |
| 内存 / CPU / pids 上限 | 无 | 有（`configs/default.yaml` 尚未创建） | 在 `run_args` 里加同样的 `--memory`、`--cpus`、`--pids-limit`：`{{M0 container limits}}` |
| 单条命令超时 | 60 秒（`environment.timeout`） | 120 秒（CLAUDE.md §5.4） | 改成同样的值，或在报告里注明差异：`{{decision}}` |
| 步数、成本上限 | 250 步、3.0 美元 | `{{…}}` | 对齐，见上表 |
| 模型 | anthropic 默认 | `{{MODEL_MAIN}}` | `-m` 指定同一个 |
| 提示词、工具 | 只有 bash，自带提示词 | tools + planning 等 | **这是被比较的因素，不对齐** |
| 判分 | — | `swebench eval` | 同一个判分器（见 §6） |

容器是 mini-swe-agent 用宿主机上的 `docker` 命令创建的（`environments/docker.py:75-96`），模型的命令在容器里通过 `docker exec` 执行，不在宿主机上跑。镜像预拉后，`docker run -d` 只需几秒；没预拉时这一步受 `pull_timeout=120` 秒限制，大镜像会超时，所以要先跑 `pull_images.py`。

## 5. 输出

| 文件 | 内容 |
|---|---|
| `preds.json` | `{instance_id: {model_name_or_path, instance_id, model_patch}}` |
| `<instance_id>/<instance_id>.traj.json` | 每题的完整轨迹（成本字段的位置：`{{TBD，冒烟后核实}}`） |
| `exit_statuses_<ts>.yaml` | 每题的退出状态 |
| `minisweagent.log` | 运行日志 |

## 6. 判分（与 M0 共用同一个判分器）

```bash
uv run swebench eval verified -p results/runs/B0/preds.json --run-id B0 -j {{N}} --report-dir results/runs/B0
```

- swebench 5.0.2 的 `-p` 直接接受 mini-swe-agent 的 `preds.json`（字典会被取 values，`swebench/harness/utils.py:65-75`）。
- `verified` 别名解析成 `SWE-bench/SWE-bench_Verified` 的**最新** revision，不支持固定 revision。判分前同样要跑 §3 里的 revision 核对，否则数据集已变，要先处理。
- 缺镜像时 swebench 会自动拉取（`run_evaluation.py:91-99`），不影响结果，只是慢。

## 7. 冒烟测试（尚未执行，需要你确认后再跑，会产生少量 LLM 花费）

```bash
uv run mini-extra swebench --subset SWE-bench/SWE-bench_Verified --split test \
  --filter '^django__django-11066$' -w 1 -m "$MODEL_MAIN" -c swebench.yaml \
  -c agent.cost_limit=0.3 -c 'environment.run_args=["--rm","--network","none"]' \
  -o results/runs/B0_smoke
```

目的：确认命令、`--network none` 下 agent 能正常工作、输出结构和成本记录。`django__django-11066` 的镜像已在本机（dev_5 之一）。结果和花费记入 `plan.md` 日志。

## 附录：`--help` 原文（2.4.6，`COLUMNS=100`）

```text
$ uv run mini-extra --help
This is mini-swe-agent version 2.4.6.
Check the v2 migration guide at https://klieret.short.gy/mini-v2-migration
Loading global config from '<repo>/.mswea/.env'
This is the central entry point for all extra commands from mini-swe-agent.

Available sub-commands:

  config: Manage the global config file
  inspect or i or inspector: Run inspector (browse trajectories)
  swebench: Evaluate on SWE-bench (batch mode)
  swebench-single: Evaluate on SWE-bench (single instance)
  programbench: Run on ProgramBench (batch mode)

$ uv run mini-extra swebench --help
This is mini-swe-agent version 2.4.6.
Check the v2 migration guide at https://klieret.short.gy/mini-v2-migration
Loading global config from '<repo>/.mswea/.env'

 Usage: mini-extra swebench [OPTIONS]

 Run mini-SWE-agent on SWEBench instances.


 More information about the usage: https://mini-swe-agent.com/latest/usage/swebench/


╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Data selection ─────────────────────────────────────────────────────────────────────────────────╮
│ --subset               <str>  SWEBench subset to use or path to a dataset [default: lite]        │
│ --split                <str>  Dataset split [default: dev]                                       │
│ --slice                <str>  Slice specification (e.g., '0:5' for first 5 instances)            │
│ --filter               <str>  Filter instance IDs by regex                                       │
│ --shuffle                     Shuffle instances                                                  │
│ --redo-existing               Redo existing instances                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Basic ──────────────────────────────────────────────────────────────────────────────────────────╮
│ --output   -o      <str>  Output directory                                                       │
│ --workers  -w      <int>  Number of worker threads for parallel processing [default: 1]          │
│ --model    -m      <str>  Model to use                                                           │
│ --config   -c      <str>  Path to config files, filenames, or key-value pairs.                   │
│                           IMPORTANT: If you set this option, the default config file will not be │
│                           used. So you need to explicitly set it e.g., with -c swebench.yaml     │
│                           <other options>                                                        │
│                           Multiple configs will be recursively merged.                           │
│                           Examples:                                                              │
│                           -c model.model_kwargs.temperature=0 You forgot to add the default      │
│                           config file! See above.                                                │
│                           -c swebench.yaml -c model.model_kwargs.temperature=0.5                 │
│                           -c swebench.yaml -c agent.max_iterations=50                            │
│                           [default:                                                              │
│                           <repo>/.venv/lib/python3.12/site-packages/… │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Advanced ───────────────────────────────────────────────────────────────────────────────────────╮
│ --model-class              <str>  Model class to use (e.g., 'anthropic' or                       │
│                                   'minisweagent.models.anthropic.AnthropicModel')                │
│ --environment-class        <str>  Environment type to use. Recommended are docker or singularity │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```
