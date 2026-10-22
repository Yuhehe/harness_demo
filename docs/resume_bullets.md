# Résumé bullets

Every number below comes from a file in `results/summary/`, which `eval/report/make_tables.py` generates from `results/runs/` (CLAUDE.md §2.3). None is taken from `docs/dev_log.md`. Rounding is the only change: the numbers are rounded for display, and the sources give the exact values. Claims without a number point to the document that supports them.

## English

```text
harness-lab — Framework-free Coding Agent Harness | Python, LiteLLM, Docker, FastAPI, MCP, OpenTelemetry, SWE-bench | 2026 | github.com/Yuhehe/harness_demo
- Built a coding-agent harness from scratch (~5.5k lines of Python, no agent framework; LiteLLM only for transport) with Docker-sandboxed execution and config-switchable components; on a 50-task SWE-bench Verified subset it resolved 74% (95% CI 60–84%) vs. 78% for mini-SWE-agent on the same model, a difference that is not statistically distinguishable (sign test p = 0.63), at $0.21 vs. $0.16 per task.
- Engineered the context layer for long tasks: under a 32K context budget, rule-based output clearing plus LLM summarization cut context-overflow failures from 60% to 0% and resolved 7 more of 25 tasks (p = 0.016); an append-only, KV-cache-friendly prompt layout kept a 98.2% prompt-cache hit rate.
- Ran the evaluation like an experiment: decision rules written before the runs, task-paired bootstrap intervals, a rerun as a noise calibration, and pooled repeated runs for power; used them to decompose the cost gap to the baseline (the prompt explains part, the alignable harness mechanics nothing measurable) and retracted a single-run finding that did not hold up when pooled.
- Built defense in depth around a network-isolated sandbox: a tiered, regex-based permission guard with a 0.25% false-block rate across 2,793 tool calls (every block reviewed by hand; all came from one rule, since fixed to protect only git-tracked tests and checked offline against the recorded calls), interactive approval for risky calls, and MCP tool servers that run inside the sandbox and need approval unless allow-listed.
- Shipped it as a FastAPI + SSE service with Docker Compose and an optional OpenTelemetry trace export; replay-based end-to-end tests run in GitHub Actions CI without an API key.
```

## 中文

```text
harness-lab —— 从零实现的 Coding Agent 运行框架 | Python, LiteLLM, Docker, FastAPI, MCP, OpenTelemetry, SWE-bench | 2026 | github.com/Yuhehe/harness_demo
- 不依赖任何 Agent 框架、从零实现 Coding Agent 的运行框架（约 5.5k 行 Python，LiteLLM 只做请求传输），Docker 沙箱执行，各组件可由配置开关；在 SWE-bench Verified 50 题子集上解决率 74%（95% CI 60–84%），同模型的 mini-SWE-agent 为 78%，差异在统计上无法区分（符号检验 p = 0.63），单题成本 $0.21 对 $0.16。
- 设计长任务的上下文层：在 32K 上下文预算下，规则清理旧输出 + LLM 摘要把上下文溢出失败从 60% 降到 0%，25 题中多解决 7 题（p = 0.016）；只追加、对 KV cache 友好的提示词布局使提示缓存命中率保持在 98.2%。
- 按实验的标准做评测：开跑前写定判定规则、逐题配对的 bootstrap 区间、用同配置重跑校准噪声、合并重复运行提高功效；据此拆解与基线的成本差距（提示词解释一部分，可对齐的框架机制测不出作用），并收回了一个合并重复运行后不成立的单次结论。
- 围绕断网沙箱做纵深防御：分级的正则权限层，在 2,793 次工具调用上误拦率 0.25%（每次拦截人工复核；全部来自同一条规则，之后改为只保护 git 跟踪的测试文件，并用记录的全部调用离线核对）；高风险调用由用户交互确认；MCP 工具 server 运行在沙箱内，未列入白名单的工具须经确认。
- 以 FastAPI + SSE 服务和 Docker Compose 交付，可选导出 OpenTelemetry trace；基于回放的端到端测试在 GitHub Actions CI 中运行，无需 API key。
```

## Where each number comes from

| Bullet | Number | File | Field |
|---|---|---|---|
| 1 | ~5.5k lines | `results/summary/setup.json` | `harness_lines` (5509) |
| 1 | 50 tasks | `results/summary/tldr.json` | `tasks` |
| 1 | 74% (95% CI 60–84%) | `results/summary/tldr.json` | `main.resolve_rate.rate` (0.74), `.wilson95` (0.604, 0.841) |
| 1 | 78% | `results/summary/tldr.json` | `baseline.resolve_rate.rate` (0.78) |
| 1 | p = 0.63 | `results/summary/tldr.json` | `resolved_pairs.sign_test_p` (0.625) |
| 1 | $0.21 vs. $0.16 | `results/summary/tldr.json` | `main.cost_per_task.mean` (0.206), `baseline.cost_per_task.mean` (0.161) |
| 2 | 60% → 0% | `results/summary/tldr.json` | `pressure.overflow_without` (0.6), `pressure.overflow_with` (0.0) |
| 2 | 7 more of 25 tasks, p = 0.016 | `results/summary/tldr.json` | `pressure.resolved.only_other` (7), `.only_base` (0), `.sign_test_p` (0.015625), `pressure.tasks` (25) |
| 2 | 98.2% | `results/summary/tldr.json` | `main.cache_hit_rate` (0.982) |
| 3 | no number | `docs/notes.md` §22.9 (decision rule, calibration), §29 (decomposition, pooled runs, the retracted tools finding) | qualitative |
| 4 | 0.25% | `results/summary/security.json` | `false_block_rate` (0.0025) |
| 4 | 2,793 tool calls | `results/summary/security.json` | `decisions` |
| 4 | every block reviewed | `results/summary/security.json` | `denies` (7), `false_blocks` (7), `unreviewed` (0) |
| 4 | fixed, checked offline (no number) | `docs/notes.md` §30; `results/CHANGELOG.md` v0.4-exp | qualitative |
| 4 | approval, MCP in the sandbox (no number) | `docs/notes.md` §32; `src/harness/tools/mcp.py`; tests `tests/unit/test_approval.py`, `test_mcp.py`, `test_mcp_docker.py` | qualitative |
| 5 | OpenTelemetry export, CI (no number) | `docs/notes.md` §31; GitHub Actions run 39629927498 | qualitative |

## Wording limits (do not drop them)

- **Bullet 1 is not a win.** The two resolve rates cannot be told apart, and harness-lab costs more per task. Keep "not statistically distinguishable", and do not write "outperforms" or "matches".
- **Bullet 2 is about context pressure.** The overflow result comes from an artificial 32K budget on 25 tasks. At the 128K budget of the main run, compaction almost never happens.
- **Bullet 3 has no numbers on purpose.** The decomposition's numbers live in `docs/dev_log.md`, which the résumé may not cite. Say "explains part" and "nothing measurable"; do not write "found the cause" or "closed the gap": the rest of the gap was not identified. The retracted finding is the single-run "edit tools are cheaper than bash" (×1.29 in the ablation table); do not cite it as a result.
- **Bullet 4: the 0.25% is the frozen policy's (v1).** The fixed policy (v2) was checked call by call against recorded runs, not rerun, so it has no measured false-block rate: write "since fixed" and "checked offline", never "0% false blocks". No injection claim: the prompt-injection suite was not run. MCP and interactive approval are tested features, not used in the experiments: no claim that they improve results.
- **Bullet 5: say "OpenTelemetry", not "Langfuse",** until a trace has been checked in Langfuse (the export was verified against a local Jaeger only, `docs/notes.md` §31). **CI passed on GitHub** (run 39629927498 at `0698f91`); if a later push breaks it, drop "CI" until it passes again.

## 面试时可以展开的点

- **为什么不用 Agent 框架**：循环、重试分类、上下文压缩、权限层都要能单独开关、做消融，并且每一步都要可追踪、可回放；框架会把这些藏起来。
- **结果持平而成本更高，怎么解释**：解决率分不出（p = 0.63），成本差距用逐题配对比值拆解：只用 bash 时的步数差距，提示词能解释一部分，可对齐的机制合起来测不出作用，剩下的在 25 题上和重跑噪声分不开。这个结论是如实报告"没找到"，不是"已解决"。
- **怎么保证数字可信**：README 和简历的数字全部由脚本从运行数据生成；判定规则先写后跑；用同配置重跑做校准；发现某个结论在合并重复运行后不成立，就收回（简历第 3 条）。
- **安全怎么设计**：真正的边界是断网沙箱，权限层是纵深防御；误拦全部来自一条规则（分不清新建和修改测试文件），改成只保护 git 跟踪的文件，用记录的调用离线核对；MCP server 也跑在沙箱里，server 给的工具描述和 schema 按不可信输入处理（可能是提示注入的入口）。
