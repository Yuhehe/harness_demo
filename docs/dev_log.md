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
