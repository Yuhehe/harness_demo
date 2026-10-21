**Setup**

- Model: `deepseek/deepseek-v4-pro` (thinking enabled, effort high); summarizer: `deepseek/deepseek-flash` (called only when a context strategy writes a summary)
- Tasks: `verified_50`, 50 tasks from SWE-bench Verified across 4 repos (django 18, scikit-learn 9, sphinx-doc 9, sympy 14); seed 42, dataset revision `78f471bf655a`. The ablations use `verified_25`, half of these drawn by repo
- Limits per task: 150 steps, $2.000, 1800 s; context budget 128000 tokens
- Main run: commits `77f0d9e` (v0.1.1); `ff11f3b` (v0.1-frozen); config hash 6ea20eaecc69
- Harness size: 4468 non-blank lines of Python in `src/harness`
- API cost of the runs in these tables (M0, B0, M0-r2, A2, A5, A6, A6xA1): $40.230 at standard prices
