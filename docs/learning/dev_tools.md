# 开发工具笔记（Git、CI 等）

> 本文是学习笔记，不含实验指标。
> 记录项目里用到的通用开发工具：Git（§1）、进程查看 `ps`（§2）、JSONL 文件格式（§3）、LLM 回放（§4）、CI（§5，待补充）等。每节先讲原理，再讲在本项目里怎么用。
> 与 `CLAUDE.md` 的对应规则：§2.4（冻结版本）、§3.1（CI 用 GitHub Actions）、§5.2（确定性序列化）、§5.7（追踪）、§5.8（回放模型）。

---

## 1. Git worktree

### 1.1 是什么

`git worktree` 是 Git 自带的功能（2.5 起就有），让**一个仓库同时检出多个工作目录**，每个目录可以停在不同的分支或提交上。

普通的 `git clone` 是"一个工作目录配一个 `.git`"。用了 worktree 之后变成"一个 `.git` 配多个工作目录"：

```
harness_demo/                 ← 主工作区（master）
├── .git/                     ← 唯一的仓库：对象、分支、tag 都在这里
│   └── worktrees/harness_demo-frozen/   ← 附加工作区自己的 HEAD、index
└── src/ ...

harness_demo-frozen/          ← 附加工作区（v0.1-frozen）
├── .git                      ← 是一个文件，不是目录，内容只有一行：
│                               gitdir: .../harness_demo/.git/worktrees/harness_demo-frozen
└── src/ ...
```

### 1.2 和 Git 的关系

Git 仓库可以拆成两部分看：

| 部分 | 内容 | 在 worktree 之间 |
|---|---|---|
| 仓库本体 | 提交对象、分支、tag、远程、配置 | **共享**，只有一份 |
| 工作状态 | 检出的文件、`HEAD`、暂存区（index） | **每个 worktree 各一份** |

由此可以推出：

- 在任何一个目录里 `git commit`，提交都进同一个仓库，另一个目录马上就能看到（如 `git log master`），不需要 push 或 pull。
- 每个 worktree 有自己的 `HEAD`，所以可以一个在 `master`，另一个停在 tag 上。
- **同一个分支不能同时被两个 worktree 检出**，Git 会拒绝，防止两边互相改动对方的 HEAD。要停在 tag 或某个提交上，就用 `--detach`（分离 HEAD，不在任何分支上）。
- 被 gitignore 的文件不受 Git 管理，也就不会出现在新的 worktree 里：`.env` 要手动复制，`.venv` 要重新 `uv sync`。

### 1.3 常用命令

```bash
git worktree add --detach ../harness_demo-frozen v0.1-frozen   # 新建，停在 tag 上
git worktree add ../feature-x -b feature-x                     # 新建，同时建新分支
git worktree list                                              # 列出所有 worktree
git worktree remove ../harness_demo-frozen                     # 删除（目录和 .git/worktrees 里的记录）
git worktree prune                                             # 目录被手动删掉后，清理残留记录
```

把已有的 worktree 换到新 tag 上，可以直接在里面 `git checkout --detach v0.2-frozen`，也可以 `remove` 后重新 `add`。

### 1.4 解决了什么问题

没有 worktree 时，想同时用两个版本的代码，一般只有两种做法：

1. **在同一个目录里来回 `git checkout` / `git stash`。** 切换时正在跑的程序会读到另一个版本的文件，未提交的改动也要先藏起来。
2. **再 clone 一份。** 这样有两个独立的仓库，提交和 tag 要靠 push/pull 同步，对象还会存两遍。

worktree 让你同时拥有多个独立的工作目录，而仓库只有一份。典型场景：长时间运行的任务（实验、构建）需要固定版本，同时还要继续开发；紧急修 bug 时不想打断手头没提交的改动；并排对比两个版本。

### 1.5 在本项目里的用法

需求（详见 `docs/notes.md` §21）：

- **实验必须跑在冻结的代码上。** CLAUDE.md §2.4 规定所有对比配置都在同一个 tag 上跑；运行器拒绝有未提交改动的工作区，并在 `run_start` 里记录提交号和 `git_dirty`。
- **同时 master 还要继续改**：写文档、改计划，以后还要写安全评测、服务化等新代码。

只有一个目录时这两件事会冲突：实验一跑好几个小时，期间只要改了 `src/` 或 `configs/`，后面的题就可能用到新代码，实验就不可信了。

做法：

- `/home/yuhe/code/labs/harness_demo-frozen` 用 `git worktree add --detach … v0.1-frozen` 建立，永远停在冻结的 tag 上，实验只在这里跑；里面复制了一份 `.env`（权限 600），用 `uv sync --frozen` 装依赖。
- 主目录 `harness_demo/` 在 master 上照常开发和提交。
- 两边共享同一个仓库，master 上打的新 tag 在 frozen 目录里立即可用。修了 harness 的 bug 后：在 master 上改 → 打新 tag → 把 worktree 换到新 tag → 受影响的配置全部重跑，记入 `results/CHANGELOG.md`。
- 结果用 `--results-dir` 写回主目录的 `results/runs/`（`run_agent.py`、`score.py`、`run_b0.py`、`aggregate.py` 都支持），这样 80% 预算守卫统计的是同一份花费，断点续跑也读同一份记录。

注意：程序是在哪个目录启动的，就读哪个目录的代码。如果实验是在主目录启动的，跑完之前主目录同样不能改 `src/` 和 `configs/`。

---

## 2. ps：查看进程

### 2.1 是什么

`ps`（process status）列出**当前进程的一个快照**。数据来自 Linux 的 `/proc/<pid>/` 目录，每个进程在那里有一个目录。它只看一次就退出，不会持续刷新；要持续刷新的效果，用 `top` 或 `htop`。

### 2.2 两套参数风格

`ps` 历史上合并了两个 Unix 流派，所以参数有两种写法，**带不带 `-` 含义不同**：

| 风格 | 例子 | 来源 |
|---|---|---|
| BSD 风格（不带 `-`） | `ps aux` | BSD Unix |
| UNIX/POSIX 风格（带 `-`） | `ps -ef` | System V |

`ps aux` 和 `ps -aux` 不是一回事，后者在新版本里会提示警告。

### 2.3 最常用的两种

**`ps aux`**

- `a`：所有用户的进程（只要有终端）
- `u`：用"面向用户"的格式输出（带 CPU、内存占用）
- `x`：也包括没有终端的进程（后台服务、守护进程）

| 列 | 含义 |
|---|---|
| `USER` | 进程所属用户 |
| `PID` | 进程号 |
| `%CPU` / `%MEM` | CPU、内存占用百分比 |
| `VSZ` / `RSS` | 虚拟内存 / 实际占用的物理内存（KB） |
| `TTY` | 所在终端，`?` 表示没有终端（后台进程） |
| `STAT` | 状态，见 §2.4 |
| `START` / `TIME` | 启动时间 / 累计占用的 CPU 时间 |
| `COMMAND` | 完整命令行 |

**`ps -ef`**

- `-e`：所有进程
- `-f`：完整格式，**多一列 `PPID`（父进程号）**，适合查看进程之间谁启动了谁

### 2.4 STAT 状态码

| 码 | 含义 |
|---|---|
| `R` | 正在运行或可运行 |
| `S` | 可中断睡眠（在等 I/O、网络等，大多数进程是这个状态） |
| `D` | 不可中断睡眠（通常在等磁盘，`kill` 也杀不掉） |
| `Z` | 僵尸进程（已结束，但父进程还没回收） |
| `T` | 已停止（比如按了 Ctrl+Z） |
| 附加：`s` / `l` / `+` / `<` / `N` | 会话首进程 / 多线程 / 前台进程组 / 高优先级 / 低优先级 |

例如 `Ss` 是"睡眠 + 会话首进程"，`Sl` 是"睡眠 + 多线程"。

### 2.5 常见用法

```bash
ps aux | grep run_agent | grep -v grep        # 找某个进程；grep -v grep 去掉 grep 自己那一行
pgrep -af run_agent                           # 更简洁：-f 匹配完整命令行，-a 显示命令行
ps -eo pid,ppid,user,%cpu,%mem,etime,cmd      # -o 自定义列；etime 是进程已运行多久
ps aux --sort=-%mem | head                    # 按内存降序，找最占资源的进程（也可 --sort=-%cpu）
ps -p <PID> -o pid,etime,%cpu,rss,cmd         # 只看某个进程
ps -ef --forest                               # 进程树，看谁启动了谁（或用 pstree -p）
ps -u yuhe                                    # 某个用户的进程
ps -eLf                                       # -L 每个线程显示一行
```

进程树的例子：M0 运行时是 zsh → `uv run` → python3（`run_agent.py`），用 `--forest` 一眼就能看出层级。

### 2.6 在本项目里的用法

- 查实验是否还在跑：`pgrep -af run_agent.py`。
- 沙箱容器里的进程在宿主机的 `ps` 里不按容器视角显示：容器有独立的 PID namespace，容器内外的 PID 不一样。要看容器内的进程用 `docker top <容器>` 或 `docker exec <容器> ps aux`。
- 宿主机上的 `ps` 只能告诉你进程还在不在，看不出实验跑到哪了。进度要看 `results/runs/<run_id>/` 下的 `preds.jsonl` 和日志。

---

## 3. JSONL：一行一条 JSON

### 3.1 是什么

JSONL（JSON Lines）是一种**文件格式**，不是工具，也不是库。它的规则只有两条：

1. 每一行是一个完整、合法的 JSON 值（通常是对象）；
2. 行与行之间用换行符 `\n` 分隔。

### 3.2 和 JSON 的区别

同样存两条记录。

JSON（整个文件是一个 JSON 值，比如一个数组）：

```json
[
  {"name": "a", "n": 1},
  {"name": "b", "n": 2}
]
```

JSONL（每行一个独立的 JSON；文件整体**不是**合法的 JSON）：

```
{"name": "a", "n": 1}
{"name": "b", "n": 2}
```

| | JSON | JSONL |
|---|---|---|
| 文件整体 | 一个合法的 JSON 值 | 不是合法的 JSON，每一行才是 |
| 追加一条记录 | 要读出整个文件、改数组、再整个写回 | 在文件末尾写一行就行 |
| 写到一半进程崩了 | 末尾的 `]` 没写出来，整个文件都解析不了 | 只有最后一行可能残缺，前面的行都还能读 |
| 读取 | 一般要把整个文件读进内存一次解析 | 可以一行一行流式读，文件再大也行 |
| 命令行处理 | 需要 `jq` 之类 | `wc -l` 就是条数，`grep`、`head`、`tail` 直接可用 |
| 适合 | 一份完整的文档：配置、`run_meta.json`、`metrics.json` | 不断增长的记录流：日志、事件、数据集 |

经验法则：一次性写完的"一个东西"用 JSON，一条条往上加的"一串记录"用 JSONL。

### 3.3 和序列化/反序列化的区别

它们不在同一个层次：

- **序列化/反序列化是一个过程**：把内存里的对象（Python 的 dict）转成可存储、可传输的字节或文本，再转回来。在 Python 里就是 `json.dumps`（dict → 字符串）和 `json.loads`（字符串 → dict）。
- **JSON 是这个过程用的一种文本格式。** 同样可以序列化成 pickle、YAML、protobuf、MessagePack 等。
- **JSONL 规定的是多条已经序列化好的 JSON 怎么排进一个文件**：一条一行。它不提供新的序列化方法，每一行仍然用标准的 JSON 序列化。

关系：**序列化（动作）→ 用 JSON 格式得到一条记录 → 多条记录按 JSONL 的约定逐行放进文件。**

项目里的读写函数正好体现了这一层关系（`eval/swebench/run_agent.py` 的 `read_jsonl` / `append_jsonl`）：

```python
def append_jsonl(path, row):
    with path.open("a", encoding="utf-8") as fh:              # "a" 是追加模式：不读旧内容，只往末尾写
        fh.write(json.dumps(row, sort_keys=True) + "\n")      # 序列化一个 dict，再加换行 = JSONL 的一行

def read_jsonl(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]  # 按行切开，逐行反序列化
```

`json.dumps` / `json.loads` 负责序列化和反序列化；`+ "\n"` 和 `splitlines()` 就是 JSONL 格式本身。

### 3.4 在本项目里的用法

| 文件 | 谁写 | 一行是什么 | 用途 |
|---|---|---|---|
| `results/runs/<run_id>/trajectories/<instance_id>.jsonl` | `tracing/tracer.py` 的 `JsonlTracer` | 一个 span（`run_start`、`llm_call`、`tool_call`、`message`、`compaction`、`run_end` 等，字段见 tracer.py 模块说明） | 复现（`run_start` 记录 config_hash、commit 等）；`aggregate.py` 从中算指标；`message` span 可还原整段对话，用于失败分析 |
| `results/runs/<run_id>/preds.jsonl` | `eval/swebench/run_agent.py`（基线由 `run_b0.py` 转换生成） | 一道题的补丁：`instance_id`、`model_name_or_path`、`model_patch` | 官方 `swebench` 判分要读的格式；也是断点续跑的依据：有这一行就算做完 |
| `results/runs/<run_id>/status.jsonl` | `run_agent.py` | 一道题的结束状态：`fail_reason`、`submitted`、`steps`、成本、墙钟时间 | 快速查看整体情况；`--retry-env-errors` 从这里找出要重跑的题 |
| `tests/fixtures/replays/*.jsonl` | `llm/replay.py`（`harness run --record`） | 一次 LLM 请求的哈希 `key` 和完整响应 | 回放测试，见 §4 |

前三类在 `results/runs/` 下，已加入 gitignore；回放文件是测试数据，要提交。

```
run_agent.py ──► trajectories/*.jsonl ──► aggregate.py ──► metrics.json ──► make_tables.py ──► README
           ├──► preds.jsonl ──────────► score.py（swebench 判分）
           └──► status.jsonl（续跑和重试用）
harness run --record ──► replays/*.jsonl ──► e2e 回放测试（以后也给 CI 用）
```

选 JSONL 的原因都和 §3.2 那张表有关：

- **崩溃后还有记录**：`JsonlTracer.emit` 每产生一个 span 就 `write` 一行，紧接着 `flush()`。任务跑到一半进程挂了，前面的 span 都还能读出来（CLAUDE.md §5.7）。
- **断点续跑**：做完一道题就往 `preds.jsonl` 追加一行，不需要改写旧内容；重跑时逐行读出已完成的 `instance_id` 并跳过。
- **对接外部工具**：`swebench` 判分直接读 JSONL 格式的预测文件。
- **结果可复现**：`sort_keys=True` 让同一个 dict 每次序列化出来的文本完全一样（CLAUDE.md §5.2 的"确定性序列化"）。这对回放很重要，因为回放的 key 是对请求文本算的哈希。

常用的查看命令：

```bash
wc -l results/runs/<run_id>/preds.jsonl                          # 做完了几道题
tail -n 1 results/runs/<run_id>/status.jsonl                     # 最近结束的一道题
grep '"type": "run_end"' results/runs/<run_id>/trajectories/*.jsonl   # 每道题的结束 span
```

---

## 4. LLM 回放：录一次，测很多次

### 4.1 是什么

先用真模型跑一遍，把模型的每次回答录下来（**录制**）；之后测试时用录音代替模型（**回放**）。这样整个 Agent 能端到端地测，而且不花钱、不联网、每次结果一样。

### 4.2 解决什么问题

Agent 的核心是"模型回答 → 执行工具 → 再问模型"这个循环。直接用真模型测试有三个毛病：

1. **要花钱**：每跑一次测试都在消耗 API 费用，CI 每次推送都跑就更多。
2. **结果不稳定**：同一个问题，模型每次回答可能不一样，测试会时过时不过。
3. **CI 里没有密钥**：仓库不提交 `.env`，CI 也就调不了真模型。

回放把"模型"这一环换成固定的录音，循环里的其他部分照常运行：上下文管理、工具执行、权限层、沙箱、`git diff`、追踪。

### 4.3 怎么用

录制（只做一次，要花钱）：

```bash
uv run harness run --config tests/fixtures/toy_config.yaml --repo tests/fixtures/toy_repo --task "..." --record tests/fixtures/replays/toy_fix.jsonl
```

回放：`tests/e2e/test_toy_replay.py` 用 `ReplayClient` 代替真客户端，在真的 Docker 沙箱里把整个任务跑一遍，然后断言：

- 修复前，toy_repo 自己的测试**失败**；修复后**通过**。这是最关键的一条。
- 任务已提交，补丁改了 `calc/ops.py`。
- 录下的响应都用上了，没有多出来的调用。

### 4.4 关键设计：请求哈希不含工具输出

回放时要知道每次请求该取哪条录音。查找用的 key 由这些内容哈希而成：模型、`thinking`、`reasoning_effort`、`max_tokens`、工具定义，以及**除工具结果内容外的**全部消息（`llm/replay.py` 模块说明）。

工具输出不进哈希，是因为沙箱的输出换一台机器、换一次运行就会变：耗时（`Ran 1 test in 0.003s`）、临时路径、`ls` 显示的日期、`find` 的输出顺序。要是把它们算进去，换台机器就几乎一定找不到录音。

工具输出不进哈希，但也不是完全不看：

- 把易变部分（时间、路径等）替换成占位符后再比对，不一致只记进 `ReplayClient.mismatches`；
- 设 `HARNESS_STRICT_REPLAY=1` 才把不一致当错误，适合在录制用的那台机器上，重构之后验证工具输出完全没变；
- 真正兜底的是"项目测试从失败变通过"的断言：harness 改坏了，任务就修不好，测试照样会挂。

### 4.5 它会故意失效

只要改了 prompt、工具定义或配置，发出去的请求就会变，找不到录音就**直接报错（`ReplayMiss`），不会猜一个响应**。这样改动不会悄悄混过测试；改完需要重新录制，并在提交信息里写明原因。

### 4.6 限制

- **录制时不能压缩上下文**（`context.strategy: none`）：摘要请求把工具输出拼进一条 user 消息，绕过了"工具输出不进哈希"的规则，换台机器就找不到录音。
- 录制用的配置要自己设好 `llm.model`，因为 CI 没有 `.env` 提供 `$MODEL_MAIN`。
- 回放运行目前不写 `llm_call` span，只有 `run_end` 里的合计（CLAUDE.md §5.8 记为待做）。
- 截至本笔记，仓库里还没有 `.github/workflows/`，回放只在本地端到端测试里用（`pytest`，需要 Docker）。

---

## 5. CI（待补充）

<!-- 第 4 天写 .github/workflows/ci.yml 时补充：GitHub Actions、astral-sh/setup-uv、回放冒烟测试 -->
