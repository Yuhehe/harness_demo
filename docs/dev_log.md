# 开发日志：阶段性实验结论

> **这是 CLAUDE.md §2.3 的例外**（2026-10-16 用户决定）：本文件里的数字是从 `results/` **手抄**的阶段性快照，用于记录开发过程中的判断，**不是最终结果**。README、简历条目和 `results/summary/` 里的数字一律由 `eval/report/make_tables.py` 生成，不得引用本文件。每个数字都注明出处（文件和字段）；如与 `results/` 不一致，以 `results/` 为准。

## 2026-10-16：M0 对 B0，verified_25

**运行条件**：`v0.1-frozen`（提交 `ff11f3b`），配置哈希 `6ea20eaecc69`（M0 与 B0 相同：150 步 / $2 / 1800 秒），模型 deepseek-v4-pro（思考开，`reasoning_effort=high`），25 题（`configs/subsets/verified_25.txt`，从 verified_50 按仓库分层抽取）。M0 在主目录（当时正位于 tag 的提交、工作区干净）启动，B0 在冻结 worktree 中启动（`docs/notes.md` §21）。

**出处**：`results/runs/M0/metrics.json`（含 `paired`，base = B0）、`results/runs/B0/metrics.json`、两者的 `score.json`。指标由 `eval/report/aggregate.py`（提交 `3753ae4`）和 `score.py --reuse-report` 计算。判定规则见 `docs/notes.md` §22。

### 主比较（逐题配对，§22.1）

| 指标 | 结果 | 出处字段 | 判定 |
|---|---|---|---|
| 解决率 | 两边都是 22/25，没有一方单独解决的题；符号检验 p = 1.0；Wilson 95% 区间都是 0.70–0.96 | `resolve_rate`、`paired.resolved` | **无法区分** |
| 成本 | M0/B0 每题比值中位数 1.34，95% 区间 [1.19, 1.53]；20/25 题 M0 更贵 | `paired.cost_usd` | 区间不含 1：**M0 更贵** |
| 步数 | 中位数比值 1.42，区间 [1.23, 1.51]；21/25 题 M0 更多 | `paired.steps` | **M0 步数更多** |
| 耗时 | 中位数比值 1.50，区间 [1.32, 1.67]；22/25 题 M0 更慢 | `paired.wall_seconds` | 方向上 M0 更慢，**但比值偏大**（见注意事项 1） |
| 每解决一题的成本 | M0 $0.198，B0 $0.143 | `quality.cost_per_resolved` | 描述 |

### 质量指标（§22.7）

| 指标 | M0 | B0 | 出处字段 |
|---|---|---|---|
| 回归（PASS_TO_PASS 失败） | 0/25 | 0/25 | `quality.tests.regressions` |
| 目标测试部分通过 | 0 | 0 | `quality.tests.partial_fix` |
| 补丁改了测试文件 | 0 | 0 | `quality.patch.touch_test_files` |
| 补丁带新建文件 | 0 | 0 | `quality.patch.add_new_files` |
| 平均改动文件数 / 改动行数中位数 | 1.08 / 8 | 1.04 / 5 | `quality.patch.files_mean`、`lines_changed_median` |
| 平均每题工具调用次数 | 51.0 | 39.8 | `quality.tool_calls.mean` |
| 工具调用分布 | bash 821、view 235、search 104、str_replace 46、todo 44、submit 25 | bash 994 | `quality.tool_calls.by_tool` |

M0 的耗时构成（`quality.time_split`）：模型调用 78%，工具执行 21%，其余 0.5%（不含容器启动和销毁）。

### 分层（只描述，§22.7 #2；`quality.by_repo`、`quality.by_difficulty`）

| 分组 | 题数 | M0：解决 / 平均成本 / 平均步数 | B0：解决 / 平均成本 / 平均步数 |
|---|---|---|---|
| django | 9 | 8 / $0.120 / 39.2 | 8 / $0.103 / 32.2 |
| scikit-learn | 5 | 4 / $0.168 / 46.4 | 4 / $0.133 / 33.6 |
| sphinx | 4 | 3 / $0.300 / 66.2 | 3 / $0.171 / 44.2 |
| sympy | 7 | 7 / $0.177 / 51.9 | 7 / $0.126 / 39.6 |
| 难度 "<15 min fix" | 11 | 10 / $0.140 / 39.5 | 10 / $0.119 / 36.1 |
| 难度 "15 min - 1 hour" | 14 | 12 / $0.202 / 55.6 | 12 / $0.132 / 36.8 |

### 结论（阶段性）

1. **解决率上 M0 和 B0 无法区分**：同样解决 22 题，失败的是同样 3 题，3 题都不是 harness 的问题（`docs/failure_analysis.md`）。
2. **M0 更贵、步数更多**：配对区间都不包含 1，逐题方向也很一致。
3. **多出来的调用主要来自 `view`**（235 次），与"用 `view` 分段读文件、调用更碎"的观察一致；这正是 A2（只用 bash）要检验的。
4. **两边的补丁都干净**：没有回归、没有改测试文件、没有留下临时文件。
5. **harness 自身开销可以忽略**：M0 的耗时绝大部分是模型调用和工具执行。
6. 差距在 sphinx 和较难的题上最大；B0 在两档难度上步数相近，M0 在较难的题上步数明显更多。每组只有 4 到 14 题，只是现象。

### 注意事项

1. **耗时口径不一致**：B0 的耗时是从第一次模型响应到最后一次，少算了第一次调用的等待；M0 从任务开始算。所以 1.50 的比值偏高。统一口径要等补 trace 字段（留到下一版，`docs/notes.md` §22）。
2. **还没有随机波动的基准**（§22.7 #1）：同一配置重跑的差异还没测，"M0 更贵、步数更多"是否大于自身波动，还不能严格判断。
3. **样本小**：25 题，解决率的区间很宽；分层结果每组只有 4 到 14 题。
4. 128k 下 M0 一次压缩都没触发，所以这次比较里**上下文策略没有起作用**，不能从中得出关于压缩的任何结论。

## 2026-10-16（续）：同一配置重跑 M0，作为随机波动的基准

**运行条件**：`M0-r2`，`v0.1.1`（提交 `77f0d9e`；harness、配置、依赖与 `v0.1-frozen` 相同），配置哈希 `6ea20eaecc69`，**3 个 worker**，同一批 verified_25。在冻结 worktree 中运行。

**出处**：`results/runs/M0-r2/metrics.json`（含 `paired`，base = M0）、`score.json`；M0 对 B0 的数字出自 `results/runs/M0/metrics.json` 的 `paired`。

| 指标 | M0 对 B0（两个 harness 的差别） | M0-r2 对 M0（同一配置的随机波动） | 出处字段 |
|---|---|---|---|
| 只有一方解决的题 | 0 对 0，p = 1.0 | 0 对 2，p = 0.5 | `paired.resolved` |
| 成本比值中位数 [95% 区间] | 1.34 [1.19, 1.53]，20/25 更高 | 0.96 [0.77, 1.13]，12/25 更高 | `paired.cost_usd` |
| 步数比值中位数 [95% 区间] | 1.42 [1.23, 1.51]，21/25 更高 | 1.00 [0.83, 1.08]，11/25 更高 | `paired.steps` |
| 耗时比值中位数 [95% 区间] | 1.50 [1.32, 1.67]，22/25 更高 | 0.80 [0.78, 0.94]，7/25 更高 | `paired.wall_seconds` |

M0-r2 自身：解决 24/25（只有 django-11087 未解决），合计花费 $4.17，每题平均 $0.167、中位数 $0.122，平均 47.5 步，缓存命中率 98.1%，没有回归、没有压缩（`resolve_rate`、`cost_per_task`、`steps_mean`、`cache_hit_rate`、`quality.tests`、`compaction`）。耗时构成：模型调用 83%、工具执行 16%（`quality.time_split`）。

**结论（更新）**

1. **"M0 比 B0 更贵、步数更多"经受住了随机波动的检验**：同一配置重跑的成本、步数区间都包含 1，而 M0 对 B0 的区间整体在其之外。
2. **解决率的差别在这个规模上看不出来**：同一配置两次运行就差了 2 题（22 对 24）。此前"M0 与 B0 解决率相同"只能说明无法区分。
3. **延迟在不同时间的运行之间不可比**：3 个 worker 的重跑反而比串行的首跑快约 20%（模型调用占大部分耗时，最可能是模型服务负载不同）。M0 对 B0 的耗时比值同样受此影响，只作描述。
4. **更正**：此前把 scikit-learn-14983、sphinx-9602 归为"题目的问题"，重跑时 M0 两题都解决了；改归为"模型的做法选择，每次运行会变"（`docs/failure_analysis.md` 文末）。上一条记录的结论 1 里"3 题都不是 harness 的问题"仍成立。

## 2026-10-16（续）：并行（3 个 worker）与串行的运行表现对比

**比较对象**：M0（串行，10-15 17:20–19:48）与 M0-r2（3 个 worker，10-16 13:13–13:58），同一配置哈希 `6ea20eaecc69`、同一批 verified_25；运行条件见上两条记录。两次运行的时段不同，所以这里比较的是"并行 + 时段"的合并效果，二者无法完全分开。

**出处与算法**：都从 `results/runs/{M0,M0-r2}/trajectories/*.jsonl` 和 `status.jsonl` 现算（一次性分析脚本，未进仓库）。
- `llm_call` 的 `outcome`、`retries`、`retry_reason`、`latency_s`、`finish_reason`、`completion_tokens`。输出速度 = `completion_tokens / latency_s`，只统计输出 ≥ 200 token 的调用。
- `tool_call` 的 `tool`、`duration_s`、`exit_code`（124 表示超时）。
- `run_end` 的 `format_errors`、`truncated_outputs`、`failed_attempts`。
- `status.jsonl` 的 `wall_seconds`。
- 分位数用线性插值。"同时在跑的任务数"按每题轨迹首尾 span 的 `ts` 划定区间，在每次调用的中点计数。

| 项目 | M0（串行） | M0-r2（3 个 worker） | 出处字段 |
|---|---|---|---|
| 运行跨度 / 各题耗时合计 | 2.48 h / 2.48 h | 0.74 h / 2.09 h | span `ts`；`status.wall_seconds` |
| LLM 调用（全部 `outcome=ok`） | 1213 | 1187 | `llm_call.outcome` |
| 重试、失败尝试 | 0 / 0 | 0 / 0 | `llm_call.retries`、`run_end.failed_attempts` |
| `length` 截断、格式错误 | 0 / 0 | 0 / 0 | `run_end.truncated_outputs`、`format_errors` |
| 单次调用延迟 p50 / p95 / 最大 | 3.2 / 18.1 / 137.2 s | 2.9 / 15.9 / 88.1 s | `llm_call.latency_s` |
| 输出速度 p50 | 65.9 tok/s | 75.1 tok/s | 同上，按上述算法计算 |
| bash 次数 / 耗时 p50 / p95 | 821 / 0.50 / 3.54 s | 805 / 0.41 / 2.74 s | `tool_call.duration_s` |
| bash 超时（退出码 124） | 1 | 3 | `tool_call.exit_code` |

补充数字（同一出处）：
- **逐题配对**：M0-r2 相对 M0，每题输出速度中位数的比值为 1.23，25 题里 20 题更快；每题 bash 总耗时的比值中位数为 0.77，6 题变慢。
- **M0-r2 内部，按同时在跑的任务数分组**：1、2、3 个任务时，输出速度 p50 分别是 66.2、76.6、75.3 tok/s，调用数分别是 16、17、550。
- **M0 内部，按小时分组**：17、18、19 点的输出速度 p50 分别是 56.2、64.3、70.2 tok/s。

**结论（阶段性）**

1. **并行没有带来可观察到的负面影响**：没有 429 和重试，也没有新增截断或格式错误。整体运行跨度缩短到串行的约 30%。
2. **模型调用更快主要是时段造成的，不是并行造成的**：同一次运行内，同时跑 3 个任务不比跑 1–2 个慢；串行那次内部，速度随时段变化了约 25%。这再次说明，不同时段的运行之间延迟不可比（上一条结论 3）。
3. **4 次 bash 超时都是命令本身造成的，与资源争用无关**：
   - sympy-19040 有 3 次：复现题目里的卡死 bug（`factor(..., extension=[I])`），两次运行都出现了；
   - scikit-learn-14983 有 1 次：`grep -R ... /` 扫了整个文件系统。
4. **离超时最近的是 scikit-learn-25232 的测试命令**：串行时 8 次耗时 80–99 s，并行时 2 次耗时 100 s 和 109 s，上限是 120 s（`bash_timeout_s`）。
   - 这两次运行时，同时在跑的其他任务在这段时间里执行工具合计只有 1–6 s，基本都在等模型回复。
   - 资源也够：3 个容器 × 2 CPU（`sandbox.cpus`）没有超过主机的 10 核。
   - 所以这次不是 CPU 争用造成的。但这道题本来就离上限很近，提高 worker 数之前应该先复查它。
5. **bash 在并行时反而略快**：一种可能是串行那次是镜像拉取后第一次使用，页缓存还是冷的。这只是推测，现有数据无法区分。

**注意事项**：
- 容器启动和销毁的时间目前没有记录：`status.wall_seconds` 与轨迹跨度只差约 0.5 s，两者都不含容器启停。并行时 Docker 的这部分开销看不到，需要等下一版补 trace 字段（`docs/notes.md` §22）。
- 只有 1 对运行、25 题，上面都是描述性的观察，没有做统计检验。

## 2026-10-16：A2（只用 bash）对 M0-r2，verified_25

**运行条件**：冻结 worktree，`v0.1.1`（提交 `77f0d9e`），配置 `configs/ablations/bash_only.yaml`（哈希 `8a196050cbdc`，与主配置只差 `action_space: tools -> bash_only`），3 个 worker，25 题（`verified_25.txt`）。配对基准 M0-r2（同一提交、同样 3 个 worker，但在不同时段运行）。

**出处**：`results/runs/A2/metrics.json`（`aggregate.py --run-id A2 --paired M0-r2 --difficulty`，含 `paired`，base = M0-r2）、`results/runs/A2/score.json`、`results/runs/A2/failure_taxonomy.json`、`results/runs/M0-r2/metrics.json`。随机波动基准（M0-r2 对 M0）和 A2 对 B0 两组配对没有写进任何文件，是用 `aggregate.paired` 对两个运行的轨迹现算的（同一函数、同一种子）。判定规则见 `docs/notes.md` §22.2、§22.9。

**事先登记的问题**（§22.2）：专用工具是否让步数和成本变多；主指标 `steps`、`cost_per_task`。数据背景：M0 比 B0 贵、步数多，多出的调用主要是 `view`（§23.2 第 9 条）。

| 指标 | A2 | M0-r2 | 出处字段 |
|---|---|---|---|
| 解决 | 23/25（Wilson 0.75–0.98） | 24/25 | `resolve_rate` |
| 每题成本 均值 / P50 | $0.211 / $0.141 | $0.167 / — | `cost_per_task` |
| 平均步数 | 54.4 | 47.5 | `steps_mean` |
| 平均输入 / 输出 token | 1 846 536 / 21 489 | — | `tokens_in_mean`、`tokens_out_mean` |
| 缓存命中率 | 98.1% | — | `cache_hit_rate` |
| 工具调用 | bash 1338、submit 24 | bash 805、view 235、search 86、todo 51、str_replace 40、submit 25 | `quality.tool_calls.by_tool` |
| 结束方式 | 提交 24（其中 2 题未解决），`step_limit` 1 | — | `fail_taxonomy` |
| 每解决一题的成本 | $0.230 | — | `quality.cost_per_resolved` |

**逐题配对**（中位数比值，bootstrap 95% 区间）：

| 比较 | 成本 | 步数 | 解决情况分歧 |
|---|---|---|---|
| A2 / M0-r2（`paired`） | 1.29 [1.05, 1.41]，18/25 题 A2 更贵 | 1.11 [1.04, 1.29]，18/25 题 A2 更多 | 只 M0-r2 解决 2，只 A2 解决 1，p = 1.0 |
| 随机波动基准：M0-r2 / M0（现算） | 0.96 [0.77, 1.13] | 1.00 [0.83, 1.08] | 只 M0-r2 解决 2，p = 0.5 |
| A2 / B0（现算，探索性，不是事先登记的比较） | 1.51 [1.23, 1.84] | 1.42 [1.33, 1.67] | 只 A2 解决 1，p = 1.0 |

**结论（阶段性）**

1. **去掉专用工具没有让成本和步数变少**，方向反而是变多：A2 对 M0-r2 的成本和步数区间都不含 1，且偏向 A2 更多。按 §22.9 的规则（差别区间要**整体**落在随机波动区间之外），成本区间 [1.05, 1.41] 与波动区间 [0.77, 1.13] 有重叠，步数也一样（[1.04, 1.29] 对 [0.83, 1.08]），所以**不能说超出了随机波动**；能说的是"没有变便宜"。
2. **"M0 比 B0 贵是因为 `view` 调用多"这个假设不成立**（§23.2 第 9 条）：A2 去掉了 `view`，和 B0 一样只用 bash，但仍比 B0 贵、步数更多（探索性比较，1.51 / 1.42）。M0 与 B0 的差距来自工具集以外的部分（system prompt、规划提示、输出截断方式等），需要另行分析，目前没有数据能区分。
3. **解决率无法区分**：23 对 24，分歧题 3 道，p = 1.0。
4. A2 有 2 题在 128k 下触发了压缩（django-11087、sphinx-9602；M0 和 M0-r2 一次都没有），A2 的平均输入 token 也更多。一种可能是 bash 输出（`cat`、`grep`）比 `view` 的分段读取更长，但这只是推测，没有核对。
5. django-11087 在 A2 里用满了 150 步（`step_limit`），但提交的补丁通过了判分：这是第一次有运行解决这道题。
6. 质量：没有回归、没有改测试文件、没有新建文件（`quality.tests`、`quality.patch`）；没有格式错误、截断或失败重试；没有卡死嫌疑（`failure_taxonomy.json` 的 `loop_signal`）。

**注意事项**：25 题、各 1 次运行；耗时只描述（A2 与 M0-r2 不在同一时段）；未解决的 2 题还没人工看，`failure_taxonomy.json` 里归属是 `unlabelled`。

## 2026-10-16：A6（预算 32k，hybrid）对 M0-r2，verified_25

**运行条件**：冻结 worktree，`v0.1.1`（提交 `77f0d9e`），配置 `configs/ablations/budget_32k.yaml`（哈希 `9765e9636470`，与主配置只差 `context.budget_tokens: 128000 -> 32000`），3 个 worker，25 题，在 A2 之后紧接着跑。配对基准 M0-r2。

**出处**：`results/runs/A6/metrics.json`（`aggregate.py --run-id A6 --paired M0-r2 --difficulty`）、`results/runs/A6/score.json`、`results/runs/A6/failure_taxonomy.json`、`results/runs/M0-r2/metrics.json`；"M0-r2 提示词超过 32k 的题数"由 M0-r2 轨迹里 `llm_call.prompt_tokens` 的每题最大值现算；压缩的种类（只清理 / 带摘要）由 A6 轨迹的 `compaction` span 现算。

**事先登记的问题**（§22.2）：上下文紧张时压缩能否防止溢出、代价多大；主指标 `overflow_rate`。**这个问题本次答不完**：要和同样 32k、不压缩的 A6×A1（`budget_32k_none`）比，那个配置还没跑。下面只做描述。

| 指标 | A6 | M0-r2 | 出处字段 |
|---|---|---|---|
| 解决 | 22/25（Wilson 0.70–0.96） | 24/25 | `resolve_rate` |
| `overflow_rate` | 0 | 0 | `overflow_rate` |
| 每题成本 均值 / P50 | $0.250 / $0.178 | $0.167 / — | `cost_per_task` |
| 平均步数 | 59.8 | 47.5 | `steps_mean` |
| 缓存命中率 | 92.4% | 98.1% | `cache_hit_rate` |
| 结束方式 | 提交 24（其中 2 题未解决），`step_limit` 1 | — | `fail_taxonomy` |
| 每解决一题的成本 | $0.284 | — | `quality.cost_per_resolved` |

**压缩**（`compaction`、`monitoring`）：
- 17/25 题发生了压缩，共 86 次，全部是 `normal` 档；其中 17 次带 LLM 摘要，69 次只清理旧输出（现算）。没有跳过的尝试、没有比例异常。
- 压缩成本占这些题总成本的 4.2%（`compaction_cost_share`）。
- 第一次压缩之后平均还走了 51.6 步（`steps_after_compaction_mean`）。
- 压缩后重复之前调用的比例 0.25%（`post_compaction_repeat_rate_mean`），摘要提到改过的文件的比例 1.0（`summary_file_recall_mean`）。两者都还没有和不压缩的运行在同一步数处对比（要 A6×A1）。
- 压缩后的提示词平均 19 010 token（服务端数字，86 次全有），压缩 span 里的估计平均偏差 −1.3%（`tokens_after`）。
- 17 次摘要都没有超过预计大小，最大是预计的 0.96（`monitoring.summary_size`）。

**逐题配对**（A6 / M0-r2，`paired`）：成本 1.33 [0.99, 1.60]，17/25 题 A6 更贵；步数 1.13 [1.00, 1.36]；解决情况只 M0-r2 解决 2 题、只 A6 解决 0 题，p = 0.5。

**结论（阶段性，描述性）**

1. **32k 预算下没有溢出**：25 题都没有以 `context_overflow` 结束。作为参照（不是反事实），M0-r2 的 25 题里有 14 题提示词超过过 32 000 token，这 14 题在 A6 里都发生了压缩。但"不压缩就会溢出多少"要等 A6×A1 实测。
2. **代价**：成本和步数的区间下端正好碰到 1（[0.99, 1.60]、[1.00, 1.36]），不能说一定更贵、更长。缓存命中率从 98.1% 降到 92.4%，符合"压缩改写了前缀"的预期。压缩本身只占成本的 4.2%，多出来的成本主要来自更多的步数和更低的缓存命中。
3. **解决率无法区分**：22 对 24，p = 0.5。A6 没解决的 sympy-15875 在 M0、M0-r2 里都解决了；在人工看轨迹之前，不能归因于压缩。
4. **压缩机制在真实压力下工作正常**：没有回退、没有摘要失败、没有超出预计的摘要、估计误差很小；1 次格式错误（django-11299，最终解决），没有截断和失败重试。
5. django-11087 又一次用满 150 步（`step_limit`），这次没有解决。

**注意事项**：25 题、各 1 次运行；A6 与 M0-r2 不在同一时段，耗时只描述；A6 的 3 道未解决题还没人工看（归属 `unlabelled`）。

## 2026-10-16：A6×A1（预算 32k，不压缩）对 A6，verified_25

**运行条件**：冻结 worktree，`v0.1.1`（提交 `77f0d9e`），配置 `configs/ablations/budget_32k_none.yaml`（哈希 `cf505bbbc109`，与主配置差 `context.budget_tokens: 128000 -> 32000` 和 `context.strategy: hybrid -> none`，即 A6 再关掉压缩），3 个 worker，25 题，在 A6 之后紧接着跑。

**出处**：`results/runs/A6xA1/metrics.json`（`aggregate.py --run-id A6xA1 --paired M0-r2 --difficulty`）、`results/runs/A6xA1/score.json`、`results/runs/A6xA1/failure_taxonomy.json`、`results/runs/A6xA1/status.jsonl`；A6 对 A6×A1 的两组比较没有写进文件，是用 `aggregate.paired`（逐题配对）和 `aggregate.compare`（只看 A6 里发生过压缩的题）对两个运行的轨迹现算的。

**事先登记的问题**（§22.2）：上下文紧张时压缩能否防止溢出、代价多大；主指标 `overflow_rate`，辅助指标 `resolve_rate`、`steps_after_compaction`、`post_compaction_repeat_rate` 等。

| 指标 | A6（压缩） | A6×A1（不压缩） | 出处字段 |
|---|---|---|---|
| **`overflow_rate`** | **0**（0/25） | **0.6**（15/25） | `overflow_rate`、`fail_taxonomy` |
| 解决 | 22/25（Wilson 0.70–0.96） | 15/25（Wilson 0.41–0.77） | `resolve_rate` |
| 每题成本 均值 / P50 | $0.250 / $0.178 | $0.080 / $0.087 | `cost_per_task` |
| 平均步数 | 59.8 | 29.0 | `steps_mean` |
| 缓存命中率 | 92.4% | 95.8% | `cache_hit_rate` |
| 运行合计花费 | $6.24 | $2.00 | 运行器日志 `_logs/{A6,A6xA1}.log` 末行 |

**逐题配对（A6 / A6×A1，现算）**：解决情况 两者都解决 15，只 A6 解决 7，只 A6×A1 解决 0，**符号检验 p = 0.016**；成本比值中位数 2.03 [1.24, 2.86]，步数 1.59 [1.14, 2.12]（A6×A1 溢出就提前结束，所以更便宜、更短）。

**溢出的细节**（`status.jsonl`、`score.json`、`failure_taxonomy.json`，现算）：
- A6×A1 溢出的 15 题，在 A6 里**全部**发生过压缩；A6 压缩过、但 A6×A1 没溢出的有 2 题（django-12304、sphinx-8269）。溢出时步数的中位数是 34。
- 15 题溢出中，5 题溢出前留下的补丁已经通过了判分（django-11292、django-15525、scikit-learn-12682、sympy-13757、sympy-19495）；另 10 题没解决，其中 9 题补丁为空（`score.json` 的 `empty_patch`），1 题（sympy-15875）补丁没改对。
- A6×A1 的 10 道未解决题全部以 `context_overflow` 结束（`failure_taxonomy.json` 的 `by_end`）。

**压缩之后**（`aggregate.compare`，A6 中压缩过的 17 题，同题对 A6×A1）：A6 平均多走 46.5 步；第一次压缩后重复之前调用的比例 0.11%，A6×A1 在同一步数处是 0。A6×A1 的这些题大多在压缩点附近就溢出结束了，"同一步数处"能比的调用很少，这个对比基本没有信息量。解决情况：两者都解决 7、只 A6 解决 7、都没解决 3。

**结论（阶段性）**

1. **主问题：32k 下压缩能防止溢出**。溢出率从 60%（15/25）降到 0（0/25），同一批题、同一提交、同一模型，只差压缩开关。
2. **并且换来了解决率**：22/25 对 15/25；7 道分歧题全部偏向 A6，符号检验 p = 0.016，按 §22.1 的规则可以说 A6 解决得更多（这是 A6 这组事先指定的辅助指标，主指标是溢出率）。
3. **代价**：A6 每题成本是 A6×A1 的约 2 倍、步数约 1.6 倍，但这是"继续干活"的成本，不能和"溢出提前结束"直接比；更有意义的是 A6 对 128k 的 M0-r2（上一节：成本 1.33 [0.99, 1.60]，解决率无法区分）。压缩本身占 A6 成本的 4.2%。
4. **结论的适用范围**：32k 是人为制造的上下文压力。128k 下 M0 和 M0-r2 都没触发过压缩（A2 有 2 题），所以这个结论说明的是"预算紧张时压缩有用"，不是"主配置靠压缩取胜"。
5. 质量：A6×A1 有 2 次格式错误，没有截断和失败重试，没有卡死嫌疑。

**注意事项**：25 题、各 1 次运行；溢出的判定用的是运行器发送请求前的本地估计（`fail_detail` 形如 "prompt would be ~32348 tokens (budget 32000)"），估计误差约 ±8%（`docs/notes.md` §17），贴着预算的题可能有个别误判；A6×A1 的 10 道未解决题都是溢出，未做人工分析。
