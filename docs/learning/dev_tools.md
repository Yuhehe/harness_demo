# 开发工具笔记（Git、CI 等）

> 本文是学习笔记，不含实验指标。
> 记录项目里用到的通用开发工具：Git（§1）、进程查看 `ps`（§2）、CI（§3，待补充）等。每节先讲原理，再讲在本项目里怎么用。
> 与 `CLAUDE.md` 的对应规则：§2.4（冻结版本）、§3.1（CI 用 GitHub Actions）。

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

## 3. CI（待补充）

<!-- 第 4 天写 .github/workflows/ci.yml 时补充：GitHub Actions、astral-sh/setup-uv、回放冒烟测试 -->
