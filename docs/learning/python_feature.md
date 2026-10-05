# Python 特性笔记

## `sys.exit(main())` 与 `if __name__ == "__main__"`

```python
if __name__ == "__main__":
    sys.exit(main())
```

### 作用

`sys.exit(main())` 把 `main()` 的返回值变成**进程的退出码（exit code）**。

- `main()` 通常返回整数：`0` 表示成功，非 0 表示失败（例如 1 表示参数错误或数据不足）。
- 只写 `main()` 时，无论函数返回什么，脚本都以 0 退出。shell、CI、`make`、`&&` 串联命令都会认为成功。
- 写成 `sys.exit(main())` 后，`main()` 返回 `1`，进程就以 1 退出。`python xxx.py && 下一步` 会在失败时中断，CI 也会标红。

### 对本项目的意义

CLAUDE.md 要求实验结果可信、可复现。如果 `select_subset.py` 悄悄失败却返回 0，后面的 `pull_images.py`、`run_agent.py` 可能会在不完整的 `verified_50.txt` 上继续跑，浪费预算。

### 相关细节

- `if __name__ == "__main__":` 保证只有直接运行脚本时才执行，被 `import` 时不会触发，所以测试里可以直接 `import` 并调用 `main()`。
- `main()` 返回 `None`（没有显式 return）时，`sys.exit(None)` 等同于退出码 0。因此只要 `main()` 在出错时 `return 1`，成功时返回 0 或 `None`，这个写法就能正常工作。

## argparse 用法

以 `eval/swebench/select_subset.py` 的 `main()` 为例。

### 1. 创建解析器

```python
p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
```

- `ArgumentParser` 是命令行解析的入口。`description` 会显示在 `--help` 的开头。
- `__doc__` 是模块顶部的文档字符串。`.split("\n")[0]` 取它的第一行，作为简短描述，描述只写一份，不会和文档字符串不一致。
- 如果文件没有模块文档字符串，`__doc__` 是 `None`，调用 `.split` 会报错。


### 2. 添加参数

```python
p.add_argument("--seed", type=int, default=SEED)
p.add_argument(
    "--revision",
    help="HF dataset revision; default: resolve the latest and record it",
)
```

- `--seed` 以 `--` 开头，所以是**可选参数**。不带 `--` 的名字（如 `"path"`）是位置参数。
- `type=int` 把命令行字符串转成整数，转换失败时 argparse 自动报错并退出。
- `default=SEED` 是不传该参数时的默认值。固定默认种子有利于实验可复现。
- `help` 是 `--help` 里显示的说明文字。
- 没写 `type` 时，值保持字符串。没写 `default` 时，默认是 `None`，代码可以用 `args.revision is None` 判断用户是否指定。

### 3. 函数签名

```python
def main(argv: list[str] | None = None) -> int:
```

- `argv=None` 时，`p.parse_args(argv)` 会自动读取 `sys.argv[1:]`，也就是真实的命令行。
- 测试时可以直接调用 `main(["--seed", "7"])`，不用改 `sys.argv`，也不用起子进程。
- `-> int` 返回退出码，和 `sys.exit(main())` 配合。

### 4. `action="append"` 与 `metavar`

```python
p.add_argument(
    "--quota",
    action="append",
    metavar="REPO=N",
    help="repeatable; default: built-in quotas",
)
```

默认情况下，同一个参数出现多次，后面的值会覆盖前面的。`action="append"` 让每次出现的值都追加到一个列表里：

```bash
python select_subset.py --quota django=10 --quota sympy=5
```

```python
args.quota  # ["django=10", "sympy=5"]
```

- 一次都没传时，值是 `None`（不是空列表）。可以用 `if args.quota is None: 用内置配额` 判断。
- 每个值仍是字符串，`"django=10"` 要自己拆分，例如 `repo, n = s.split("=")`，再 `int(n)`。
- 如果设置了 `default=[...]` 再配合 `append`，新值会追加到默认列表后面，而不是替换它，这是个常见的坑。
- `metavar` 只影响 `--help` 的显示，不影响解析。不设置时显示 `--quota QUOTA`，设置后显示 `--quota REPO=N`，提示用户值的格式。

### 5. `add_argument` 与 `parse_args` 的关联

`add_argument` 是声明规则，`parse_args` 是按规则解析命令行，结果放进 `Namespace` 对象。

**属性名的规则：** 取第一个长选项名，去掉前面的 `--`，再把 `-` 换成 `_`。

| add_argument 里写的 | 命令行写法 | `args` 里的属性 |
|---|---|---|
| `"--seed"` | `--seed 7` | `args.seed` |
| `"--revision"` | `--revision abc` | `args.revision` |
| `"--quota"` | `--quota a=1` | `args.quota` |
| `"--max-items"` | `--max-items 3` | `args.max_items` |

```python
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, default=42)
p.add_argument("--quota", action="append")

args = p.parse_args(["--seed", "7", "--quota", "django=10", "--quota", "sympy=5"])
# Namespace(seed=7, quota=['django=10', 'sympy=5'])

args = p.parse_args([])
# Namespace(seed=42, quota=None)   ← 没传就取 default，没设 default 就是 None
```

- 每个 `add_argument` 都会在 `Namespace` 里创建一个同名属性，拼写要对应。`args.seeed` 会触发 `AttributeError`。
- 可以用 `dest="xxx"` 手动指定属性名，不用默认规则。
- `parse_args` 同时负责校验：传了没声明的参数、`type` 转换失败、缺少必填项，都会打印用法并以退出码 2 退出。传 `-h` 时打印帮助并以 0 退出。

### 其他常用选项

| 用法 | 作用 |
|---|---|
| `action="store_true"` | 开关型参数，出现则为 `True`，如 `--dry-run` |
| `choices=[...]` | 限制取值范围 |
| `required=True` | 让可选参数变成必填 |
| `nargs="+"` / `"*"` | 接收多个值 |
| `type=Path` | 直接转成 `Path` 对象 |

## `defaultdict`、`sorted(key=...)` 与 `lambda`

以 `select_subset.py` 中按仓库分组的代码为例：

```python
by_repo: dict[str, list[dict]] = defaultdict(list)
for r in sorted(rows, key=lambda r: r["instance_id"]):
    by_repo[r["repo"]].append(r)
```

### `defaultdict(list)`（来自 `collections`）

普通 `dict` 访问不存在的 key 会抛 `KeyError`。`defaultdict` 在 key 不存在时，调用**构造时传入的工厂函数**生成默认值，并把它存进字典：

```python
d = defaultdict(list)
d["a"].append(1)   # "a" 不存在 → 自动创建 [] → 再 append
# d == {"a": [1]}
```

- `list` 不带括号，传的是函数本身（调用 `list()` 得到 `[]`），不是已经创建好的列表。
- 不用它的写法：`if repo not in by_repo: by_repo[repo] = []`，再 `append`。
- 对 `defaultdict` 做**读取**（如 `by_repo["x"]`）也会插入这个 key。只想读取而不产生副作用时用 `.get(key, default)`。

### `sorted(iterable, key=...)`

- 返回一个**新的已排序列表**，不修改原列表（对比 `list.sort()` 是原地排序）。
- `key` 是一个函数，对每个元素调用一次，按它的返回值比较大小。
- 排序是**稳定**的：key 相同的元素保持原有相对顺序。
- 本项目里先排序再分组，是为了消除数据集原始行顺序的影响。后面 `rng.sample(pool, ...)` 的结果取决于 `pool` 的顺序，顺序固定了，同一个 seed 才会得到同样的结果。

### `lambda`

匿名函数，`lambda r: r["instance_id"]` 等价于：

```python
def get_id(r):
    return r["instance_id"]
```

适合 `key=` 这种只用一次的简单函数。冒号前是参数，冒号后是返回值表达式。

### 类型注解 `dict[str, list[dict]]`

- 表示 key 为 `str`、值为 `dict` 列表的字典。
- 只是给人和类型检查器（mypy、IDE）看的提示，运行时不检查。
- Python 3.9+ 可以直接写 `dict[str, ...]`，不需要从 `typing` 导入 `Dict`。

### 效果

```python
by_repo = {
    "django/django": [row1, row2, ...],   # 各组内按 instance_id 排序
    "sympy/sympy": [...],
}
```
