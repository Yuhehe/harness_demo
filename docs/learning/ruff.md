# Ruff：代码检查与格式化

> 学习笔记。依据：本机实测的 ruff 0.16.10、`pyproject.toml`、`CLAUDE.md`。
> 下面第 5 节的例子是在临时文件上重新跑出来的，规则编号和提示信息都是 ruff 的实际输出。

---

## 1. ruff 是什么

Python 的**代码检查（lint）**和**格式化（format）**工具，用 Rust 写的，速度很快。一个工具覆盖了过去要装好几个的功能：flake8（检查）、isort（导入排序）、black（格式化）、pyupgrade（升级旧写法）等。

本项目里它装在 dev 依赖组里（`uv add --dev ruff`），版本锁在 `uv.lock`，所以每个人、每台机器得到的结果一致。

## 2. 在本项目里的作用

| 作用 | 命令 | 做什么 |
|---|---|---|
| **代码检查** | `ruff check` | 不运行代码，静态分析找出问题：没用到的导入和变量、未定义的名字、容易出错的写法、过时的语法。能自动修的会标 `[*]` |
| **格式化** | `ruff format` | 统一代码风格（行宽 88，兼容 black）。风格固定后，每次改动的 diff 里只有真正的修改 |
| **提交前检查** | 上面两条加 `--check`，再加 `pytest` | CLAUDE.md §3.2 和 §7 规定：提交前 `ruff check`、`ruff format --check`、`pytest` 必须全部通过 |

`ruff format --check` 只检查、不改文件，格式不对就以非零状态退出；`ruff format` 才会真的改文件。

## 3. 当前配置

`pyproject.toml` 里只有一项，没有自定义规则：

```toml
[tool.ruff]
extend-exclude = ["*.md"]
```

| 项 | 值 | 来源 |
|---|---|---|
| 版本 | 0.16.10 | `uv.lock` |
| 行宽 | 88（检查和格式化相同） | ruff 默认 |
| 目标 Python 版本 | 3.12 | 从 `requires-python = ">=3.12,<3.13"` 推出 |
| 排除 | 所有 `*.md` | `extend-exclude` |

**为什么排除 Markdown**：ruff 0.16 会格式化 Markdown 里的 Python 代码块。我们的文档（`CLAUDE.md`、`plan.md`、`docs/**`）里有大量**示意用**的代码片段（带 `...` 的伪代码、注释对齐的写法），会让 `ruff format --check .` 一直失败。在临时文件上验证：一个写法不规范的 Python 代码块（字典冒号后缺空格、注释前空格过多），在没有排除的情况下就会被报 `File would be reformatted`。所以让 ruff 只管真正的 Python 源码。

**目前被检查的文件**：`eval/` 和 `tests/` 下的 Python 文件，以及 `pyproject.toml`（`ruff check --show-files` 的输出）。

## 4. 规则集

项目没有写 `select` 或 `ignore`，用的是 ruff 0.16 的**默认规则集**。

- **已确认会触发的**（第 5 节的试验）：F（pyflakes）、UP（pyupgrade）、ISC（隐式字符串拼接）、RUF（ruff 自己的规则）。
- `E731`（把 lambda 赋给变量）**没有启用**，这一点是 ruff 自己告诉我们的（见第 5 节的 RUF100）。
- **没有逐条核对默认集里的所有规则**。`ruff check --show-settings` 能列出完整的启用规则；我只确认了上面几类在本项目里实际触发过。

## 5. 例子：它能抓到什么

下面是一段故意写得有问题的代码（在临时文件里，不在仓库里），其中几处就是本项目开发中真实出现过的：

```python
import os
import sys

by_id = lambda r: r["instance_id"]  # noqa: E731


def ask(todo):
    return input("pull %d images? [y/N] " % len(todo))


CMD = [
    "bash",
    "-lc",
    "cd /testbed && echo a"
    " && echo b",
]


def f():
    x = 1
    return sys.argv
```

`ruff check` 的输出：

| 位置 | 规则 | 提示 | 怎么改 |
|---|---|---|---|
| 第 1 行 | `F401` | `os` 导入了但没用 | 删掉（可自动修） |
| 第 4 行 | `RUF100` | 多余的 `noqa` 指令（`E731` 没启用） | 删掉注释（可自动修）；实际是把那个 lambda 赋值改成直接写在 `sorted(..., key=lambda ...)` 里 |
| 第 8 行 | `UP031` | 用格式化说明符，别用 `%` 格式化 | 改成 f-string：`f"pull {len(todo)} images? [y/N] "` |
| 第 14 行 | `ISC004` | 集合里出现了没加括号的隐式字符串拼接 | 给拼接的几段加上括号（防止漏掉逗号） |
| 第 20 行 | `F841` | 局部变量赋值了但没用到 | 删掉或使用它 |

- 其中 `UP031`、`ISC004` 是写 `eval/swebench/pull_images.py` 时真实遇到的，`RUF100` 是写 `select_subset.py` 时遇到的，都按提示改掉了；`F401`、`F841` 是我补的典型例子，不是本项目里出现过的。
- 输出里 `[*]` 表示能用 `--fix` 自动修；另有 2 个修复被标为"不安全"，需要加 `--unsafe-fixes` 才会应用，不会默认执行。
- `ruff format` 还多次自动调整了换行和缩进，比如超过 88 列的调用被拆成多行。

## 6. 它不做什么

| 不做的事 | 谁来做 |
|---|---|
| **类型检查** | 项目里没有 mypy 或 pyright |
| **运行测试** | pytest |
| **保证"不使用 Agent 框架"**（CLAUDE.md §2.1） | `tests/unit/test_no_agent_frameworks.py` 里的 AST 检查 |
| **检查 Markdown 文档** | 被 `extend-exclude` 排除了 |
| **发现逻辑错误、安全问题** | 需要测试和评审；它只能发现模式上有问题的写法 |

## 7. 常用命令

检查全部文件：

```bash
uv run ruff check .
```

检查并自动应用安全的修复：

```bash
uv run ruff check . --fix
```

只检查格式，不改文件（提交前用）：

```bash
uv run ruff format --check .
```

看格式化会改什么，不动文件：

```bash
uv run ruff format --diff .
```

直接格式化：

```bash
uv run ruff format .
```

## 8. 当前状态（截至 2026-10-05，会过期）

- 没有 pre-commit 钩子，提交前的检查靠手动运行。
- 持续集成相关的内容，等后面建好之后再补充到这里。
