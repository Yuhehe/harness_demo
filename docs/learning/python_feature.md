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

## 函数参数里的 `*`：强制关键字参数（keyword-only）

以 `src/harness/llm/types.py` 中的 `LLMError.__init__` 为例：

```python
def __init__(
    self,
    kind: str,
    message: str,
    *,
    status_code: int | None = None,
    attempts: list[AttemptRecord] | None = None,
) -> None:
```

### 作用

参数列表里单独的 `*` 本身不接收任何值，它是一个分隔符：**`*` 之后的参数只能用关键字形式传入，不能按位置传**。

```python
LLMError("timeout", "超时了")                                   # OK：kind、message 可按位置传
LLMError("server_error", "boom", status_code=503, attempts=[])  # OK：后两个写了参数名
LLMError("server_error", "boom", 503)                           # TypeError：503 不能按位置传给 status_code
```

### 为什么这样写

1. **调用处可读**：`status_code=503` 比一个裸的 `503` 更清楚。
2. **防止传错位置**：可选参数类型不同（`int | None`、`list | None`），强制写名字就不会把顺序搞混。
3. **便于以后扩展**：新增或调整可选参数的顺序，不会破坏已有调用。

### 相关细节

- `*` 之前的参数不受影响，仍然可以按位置或关键字传。
- `*args` 也有同样的分隔效果：它后面的参数同样是 keyword-only。单独的 `*` 只是不收集多余的位置参数。
- 反过来，`/` 之前的参数是 **positional-only**（只能按位置传，Python 3.8+）。三者可以组合：`def f(a, /, b, *, c)`。
- keyword-only 参数可以有默认值，也可以没有。没有默认值时必须显式传入，否则报 `TypeError`。

## `@dataclass(frozen=True)`：不可变的数据类

以 `src/harness/llm/client.py` 中的 `Decision` 为例：

```python
@dataclass(frozen=True)
class Decision:
    kind: str
    retry: bool
    status_code: int | None = None
    retry_after: float | None = None
```

### 作用

`@dataclass` 自动生成 `__init__`、`__repr__`、`__eq__`。`frozen=True` 让实例**创建后不可修改**：

```python
d = Decision(kind="timeout", retry=True)
d.retry = False   # 抛 dataclasses.FrozenInstanceError
```

### 具体效果

1. **禁止赋值和删除**：生成的 `__setattr__` / `__delattr__` 直接抛 `FrozenInstanceError`。
2. **可哈希**：`eq=True`（默认）时，`frozen=True` 会生成基于字段值的 `__hash__`，实例可以放进 `set` 或作为 `dict` 的 key。普通 `@dataclass` 的 `__hash__` 是 `None`，不可哈希。
3. **构造不受影响**：`__init__` 内部用 `object.__setattr__` 赋值。如果在 `__post_init__` 里要改字段，也得用 `object.__setattr__(self, ...)`。

### 为什么 `Decision` 适合用它

`Decision` 是 `classify(exc)` 对一个异常给出的判定（是否重试、错误类型、状态码、`retry_after`），产生后只应被读取。冻结后，重试循环里不会因为误改 `decision.retry` 而出现难查的 bug。

### 注意

冻结只是**浅层**的：字段是 `list` 或 `dict` 时，字段本身不能被重新赋值，但容器里的内容仍然可以修改。`Decision` 的字段都是 `str`、`bool`、`int`、`float` 这类不可变类型，所以没有这个问题。

## asyncio：`await`、阻塞事件循环与 `asyncio.to_thread`

以 `src/harness/llm/client.py` 中的 `_estimate_prompt_tokens` 为例：

```python
async def _estimate_prompt_tokens(self, messages, tools) -> int:
    def count() -> int:
        try:
            return int(litellm.token_counter(model=..., messages=messages, tools=tools))
        except Exception:  # noqa: BLE001 - an estimate must never break a run
            return len(json.dumps(messages, default=str)) // 4

    return await asyncio.to_thread(count)
```

### 1. `await` 的作用

`await x` 做两件事：

1. **取结果**：对可等待对象（协程、`Task`、`Future`）求值，得到返回值；异常也会正常抛出。
2. **让出控制权**：`x` 还没完成时，当前协程被挂起，事件循环去运行别的就绪协程；`x` 完成后再从 `await` 的下一行恢复。

`await` 是协程里**唯一**能把控制权交还给事件循环的位置。

```python
async def a():
    print("a1")
    await asyncio.sleep(1)   # 挂起，让出
    print("a2")

await asyncio.gather(a(), b())   # a、b 各 sleep 1 秒，总共约 1 秒，不是 2 秒
```

- **不等于多线程或并行**：事件循环仍是单线程，同一时刻只有一个协程在跑。并发来自"一个协程等 I/O 时，另一个协程在用 CPU"。
- **不一定会挂起**：被等待的协程如果全是同步计算、没有真正的等待点，`await` 就和普通调用一样，全程不让出。

### 2. 在 `async def` 里调用同步函数的风险：阻塞事件循环

同步函数运行期间，事件循环被占住，同一循环里的其他协程（并发任务、SSE 推送、超时计时）都得等它返回。`litellm.token_counter` 是 CPU 密集的，首次调用还会加载 tokenizer；对大消息列表 `json.dumps` 也有开销。直接调用会让并发任务互相拖慢，延迟统计也会失真。

### 3. 为什么 `count` 不能声明成 `async def`

`async def` 只表示"这里可以被 `await`"，**不会让函数变成非阻塞**。

```python
async def count() -> int:
    return int(litellm.token_counter(...))   # 仍然是同步调用，函数体里没有 await
```

`count` 里只有同步 CPU 计算，没有可以挂起的点，`await count()` 会在事件循环线程里一口气跑完，效果和直接调用一样，还多了协程对象的开销。

而且 `asyncio.to_thread` 要的是**普通函数**：它在线程里直接调用 `count()`。传 `async def` 的函数，线程里只会得到一个未执行的协程对象，`await` 拿到的不是 `int`，计算根本没有跑。

### 4. 真正不阻塞的两种办法

| 办法 | 适用 |
|---|---|
| 函数内部有真正会挂起的 `await`（`asyncio.sleep`、`httpx`、`litellm.acompletion`） | 有异步版本的 I/O |
| `await asyncio.to_thread(sync_func)`，把同步代码放进线程池 | 没有异步版本的同步 I/O 或 CPU 计算（如 `token_counter`、同步 Docker SDK） |

`to_thread` 返回一个 Future，`await` 在它完成前挂起当前协程；线程算完后，事件循环恢复协程，并把 `count()` 的返回值交给 `return`。

### 5. 使用 `to_thread` 的注意事项

- **线程安全**：`count` 在另一个线程里读 `messages`。主线程若同时修改该列表会有数据竞争；CLAUDE.md 规定历史消息只追加不修改，风险较小，但调用点最好传快照。
- **异常**：`count` 内部用 `except Exception` 兜底成粗略估算。但 `except` 里的 `json.dumps` 若再失败会直接向外抛（`default=str` 已覆盖大部分情况）。
- **取消**：线程里的任务无法被取消。外层协程被取消或超时后，`count` 仍会在后台跑完，结果被丢弃。
- **线程池容量**：用默认线程池（`min(32, cpu+4)`）；并发很高且别的阻塞任务占满线程时才会排队。
- **不要滥用**：微秒级的纯内存同步函数直接调用即可，开线程反而更慢。

### 经验规则

在 `async def` 里，耗时超过几毫秒的同步调用（文件和网络 I/O、CPU 密集计算、`time.sleep`、同步的 `docker` SDK 调用）都应放进 `asyncio.to_thread` 或专用 executor。

一句话：`await` = "我要等这个结果，等的时候你（事件循环）先去忙别的，好了再叫我"；`async` 只表示"这里可以挂起"，是否真的挂起取决于函数体里有没有真正会挂起的 `await`。

## pydantic：`extra="forbid"` 与未声明字段

以 `src/harness/config.py` 中的 `_Strict` 为例：

```python
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class GuardConfig(_Strict): ...
class AgentConfig(_Strict): ...
```

### 什么是未声明字段

类里没有定义、但输入数据里出现了的字段。类里带类型注解的属性（如 `enabled: bool`）才算"声明"，输入里不在这个列表中的 key 都是未声明字段。

```yaml
guard:
  enabled: true
  enabld: false     # 拼写错误，类里没有这个字段 → 未声明字段
```

### `extra` 的三种取值

```python
class A(BaseModel):                       # 默认 extra="ignore"
    x: int
A.model_validate({"x": 1, "y": 2})        # 成功，A(x=1)；y 被静默丢掉

class B(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int
B.model_validate({"x": 1, "y": 2})        # ValidationError: y Extra inputs are not permitted

class C(BaseModel):
    model_config = ConfigDict(extra="allow")
    x: int
c = C.model_validate({"x": 1, "y": 2})    # 成功；c.y == 2，model_dump() 里也有 y
```

### 与序列化、反序列化的关系

`extra` 只作用在**反序列化（输入）**这一侧：

| 方向 | pydantic 方法 | `extra` 是否起作用 |
|---|---|---|
| 反序列化：dict / JSON / YAML → 模型 | `model_validate(dict)`、`model_validate_json(str)`、`Model(**kwargs)` | **是**：`ignore` 丢弃，`forbid` 报错，`allow` 保留 |
| 序列化：模型 → dict / JSON | `model_dump()`、`model_dump_json()` | 不影响：输出已声明字段（`allow` 模式下额外字段也会输出） |

读取配置的流程：`yaml.safe_load` 先得到 dict，再交给 `model_validate`，pydantic 拿 dict 的 key 与类声明的字段逐个比对。

### 为什么本项目要用 `forbid`

消融实验靠改配置来"每次只改一个因素"。默认的 `ignore` 会让拼写错误**静默失效**：

- 把 `context.strategy` 误写成 `context.stratgy: summarize`，它被忽略，实验实际跑的还是默认配置。结果数字看起来正常，结论却是错的。
- 开了 `forbid`，这类错误在加载配置时立刻报错，实验跑不起来，问题在花钱之前就暴露。
- 配置项改名或删除后，旧 YAML 里遗留的字段同样会报错，不会悄悄失效。

### 用法与注意

- 用公共基类 `_Strict` 一处设置，各子配置继承它。名字前的下划线表示它是模块内部用的基类。
- **不会向嵌套模型传递**：父模型设了 `forbid`，字段类型里的另一个模型要靠它自己也继承 `_Strict`，这里每个子配置都继承了，没有漏洞。
- 只管**多余**的字段，不管**缺失**的：缺少必填字段报 `missing`；有默认值的字段缺失则静默用默认值，所以才需要 `forbid` 抓住拼错的那个 key。
- 不影响类型校验：`enabled: "abc"` 无论怎样设置都会报错（`"true"`、`"1"` 这类可转换的字符串在非 strict 模式下会被转成 `bool`）。

## pydantic：`@model_validator(mode="after")` 与触发时机

以 `src/harness/config.py` 中的 `LLMConfig._fill_pair` 为例：

```python
class LLMConfig(_Strict):
    thinking: Literal["enabled", "disabled"] = "enabled"
    max_tokens: int | None = None   # None -> paired default for `thinking`
    timeout_s: float | None = None  # None -> paired default for `thinking`

    @model_validator(mode="after")
    def _fill_pair(self) -> LLMConfig:
        d = _THINKING_DEFAULTS[self.thinking]
        if self.max_tokens is None:
            self.max_tokens = d["max_tokens"]
        if self.timeout_s is None:
            self.timeout_s = d["timeout_s"]
        return self
```

### 作用

`@model_validator` 注册一个对**整个模型**做校验或后处理的方法。`mode="after"` 表示它在**所有字段都已解析、类型校验通过之后**运行，此时 `self` 是完整的模型实例。

这里用它根据 `thinking` 补全 `max_tokens` 和 `timeout_s`：

| 配置 | `max_tokens` | `timeout_s` |
|---|---|---|
| `thinking: enabled`，两项都没写 | 65536 | 900 |
| `thinking: disabled`，两项都没写 | 16384 | 300 |
| 显式写了某一项 | 用写的值 | 用写的值 |

对应 CLAUDE.md §5.1：`max_tokens` 与 `timeout_s` 成对设置，超时要大于输出顶满上限所需的时间，失控的生成才会先以 `finish_reason=length` 结束（不重试），而不是先超时（重试 3 次）。

### 为什么不用普通字段默认值

字段默认值是固定的，不能依赖别的字段。这里的默认值要由 `thinking` 决定，所以：

1. 字段声明成 `int | None = None`，`None` 表示"用户没指定"。
2. 校验完成后，在 `after` validator 里读 `self.thinking`，把 `None` 换成配对默认值。

### `before` 与 `after`

| | `mode="before"` | `mode="after"` |
|---|---|---|
| 运行时机 | 字段校验**之前** | 字段校验**之后** |
| 收到的参数 | 原始输入（通常是 dict），写成 `@classmethod` | 完整的模型实例 `self` |
| 适合 | 预处理原始输入、兼容旧格式 | 跨字段校验、依赖其他字段的默认值 |

这里用 `after`，是因为要读取已通过校验的 `self.thinking`，才能安全查 `_THINKING_DEFAULTS`。

### 触发时机：自动，不需要手动调用

它是 pydantic 校验流程的最后一步，通过正常入口创建实例时自动运行：

```python
LLMConfig()                                          # 触发
LLMConfig(thinking="disabled")                       # 触发
LLMConfig.model_validate({"thinking": "enabled"})    # 触发
LLMConfig.model_validate_json('{"thinking": "enabled"}')   # 触发

cfg = LLMConfig(thinking="disabled")
cfg.max_tokens   # 16384，不是 None
```

流程：接收输入 → 字段类型校验、`extra="forbid"` 检查 → 调用 `after` validator → 返回实例。它是 `__init__` 内部校验的一部分，不是 `__init__` 之后另外跑的。

### 不会触发的情况

- **`model_construct()`**：跳过所有校验，`max_tokens` 保持 `None`。配置加载不要用它。
- **创建后改属性**：`cfg.thinking = "disabled"` 默认不重新校验（除非 `validate_assignment=True`），已补全的 `max_tokens` 不会跟着变。配置加载后当只读使用。
- **`model_copy(update={...})`**：同样不校验，`update` 的值直接写入。

### 注意点

- **必须 `return self`**：忘了返回会得到 `None`。
- **赋值 `self.xxx = ...`**：若模型开了 `validate_assignment=True`，赋值会再次触发校验，可能递归。这里没开。
- **类型注解不会收窄**：运行时 `max_tokens` 一定是 `int`，但注解仍是 `int | None`，类型检查器看不出，使用处可能需要断言。
- **抛错**：校验失败时 `raise ValueError(...)`，pydantic 会包装成 `ValidationError`。
- **多个 validator**：同一个类可以有多个，按定义顺序执行。
- **与 `@field_validator` 的区别**：后者针对单个字段，拿不到其他字段的值；跨字段逻辑用 `model_validator`。

### 对可复现性的影响

补全发生在创建时，`model_dump()` 得到的是补全后的具体数字。`run_start` 里记录的配置哈希（CLAUDE.md §5.7）对应补全后的值，所以"没写 `max_tokens`"和"显式写成 65536"得到相同的哈希。

## 列表追加：`append`、`extend`、`+=` 与 `+`（重点：内存使用）

以 `src/harness/tools/registry.py` 中按配置叠加工具为例：

```python
tools: list[Tool] = [BashTool(...), SubmitTool(...)]
if cfg.action_space == "tools":
    tools += [ViewTool(limiter), StrReplaceTool(), SearchTool(limiter)]   # 一次加 3 个
    if cfg.planning == "on":
        tools.append(TodoTool(todo))                                      # 加 1 个
```

### 1. 语义区别：一次加几个

| 写法 | 作用 | 参数 |
|---|---|---|
| `lst.append(x)` | 把 `x` **作为一个元素**加到末尾 | 单个对象 |
| `lst.extend(it)` / `lst += it` | 把 `it` 里的**每个元素**依次加到末尾 | 任意可迭代对象 |
| `lst = lst + other` | **新建**一个列表，内容是两者拼接 | 必须是列表 |

写反的后果：

```python
tools.append([ViewTool(...), SearchTool(...)])   # 末尾多了一个"列表"，不是 2 个工具
tools += TodoTool(todo)                          # TypeError: 'TodoTool' object is not iterable
```

本项目里这里只是按数量选了顺手的写法，功能上没有必然性。要统一风格也可以：`tools += [TodoTool(todo)]`，或全用 `append`。

### 2. 内存使用（重点）

#### 2.1 `append`、`extend`、`+=` 是原地修改，`+` 会新建列表

`+=` 对列表调用的是 `__iadd__`（等价于 `extend`），**在原对象上就地扩展**，不创建新列表。`a = a + b` 则先分配一个新列表，把 `a` 和 `b` 的全部元素拷贝进去，再让名字 `a` 指向它，旧列表等引用计数归零后回收。

实测（Python 3.12）：

```python
a = [1, 2, 3]; b = a; i = id(a)
a += [4]
id(a) == i        # True：还是同一个对象
b                 # [1, 2, 3, 4]：别名 b 也看到了变化

a = [1, 2, 3]; b = a; i = id(a)
a = a + [4]
id(a) == i        # False：新对象
b                 # [1, 2, 3]：别名 b 不受影响
```

两个后果：

- **别名可见性**：多个名字（或对象属性、函数参数）指向同一个列表时，`+=` / `append` 会让所有持有者都看到变化；`a = a + b` 只改变 `a` 这个名字。函数参数里用 `+=` 会**修改调用方传进来的列表**，容易产生副作用。
- **本项目这里安全**：`tools` 是函数内部新建的局部列表，没有别的引用，原地修改没有副作用。

#### 2.2 循环里用 `a = a + [x]` 是 O(n²)

每次 `+` 都要把已有的 n 个元素整体拷贝一遍，且产生一个即将被丢弃的旧列表，内存分配和拷贝都随长度增长。实测往列表里加 20000 个元素（3 次累计）：

| 写法 | 耗时 |
|---|---|
| `l.append(k)` | 约 0.003 s |
| `l = l + [k]` | 约 1.68 s（慢约 500 倍） |

在 agent 循环里累积消息历史、span 记录这类会不断增长的列表时，要用 `append` / `extend`，不要用 `+` 重新赋值。

#### 2.3 `append` 的摊销 O(1)：预留多余空间

列表底层是一块连续的指针数组，容量会**按比例预留多余空间**，扩容时才重新分配并拷贝。实测逐个 `append` 40 个元素，`sys.getsizeof` 只出现 6 个不同的值：

```
[88, 120, 184, 248, 312, 376]   # 字节，说明只扩容了 5 次，不是每次 append 都重新分配
```

所以单次 `append` 摊销下来是 O(1)；代价是列表实际占用的内存通常比元素个数略大。

#### 2.4 临时列表：`+= [x]` 与 `.append(x)` 的细微差别

```python
tools += [TodoTool(todo)]   # 先构造一个只含 1 个元素的临时列表，extend 后丢弃
tools.append(TodoTool(todo))  # 不需要临时列表
```

加单个元素时 `append` 少一次临时对象分配。差别很小，不值得为此改代码，但这是"单个元素用 `append`、多个才用 `+=`"的一个理由。

#### 2.5 浅拷贝：拷贝的是引用，不是对象本身

无论 `+`、`extend` 还是 `list(a)`，拷贝的都是**指针**，列表里的元素对象不会被复制。`tools` 里的 `BashTool`、`ViewTool` 实例只存在一份，列表只是持有它们的引用。因此"列表很大"主要是指针数组的大小（每个元素 8 字节），而不是元素对象的大小。

#### 2.6 `+=` 接受任意可迭代对象，`+` 只接受列表

```python
a = [1, 2, 3]; a += (4, 5)     # OK，a == [1, 2, 3, 4, 5]
[1] + (2,)                     # TypeError: can only concatenate list (not "tuple") to list
```

传生成器给 `extend` / `+=` 时可以边迭代边追加，不需要先在内存里造出完整的中间列表；`+` 做不到这点（必须先有一个现成的列表）。

### 3. 选择建议

| 场景 | 推荐 |
|---|---|
| 加 1 个元素 | `lst.append(x)` |
| 加多个元素或另一个可迭代对象 | `lst.extend(it)` 或 `lst += it`（`+=` 简短，对局部变量没问题） |
| 要保留原列表不变、得到新列表 | `new = lst + other` 或 `[*lst, *other]` |
| 在循环中不断增长的列表 | 永远用 `append` / `extend`，不要 `lst = lst + [...]` |
| 修改函数参数传进来的列表 | 先想清楚是否允许改动调用方的数据；不允许就先拷贝：`lst = list(lst)` |

## `*` 与 `**`：解包与收集

`*` 和 `**` 都是"解包"，区别在于解的对象不同：`*` 展开**可迭代对象**，`**` 展开**字典**。它们和乘法、乘方运算无关。（与"强制关键字参数"的 `*` 不同，见上文"函数参数里的 `*`"一节。）

### 调用时：解包

```python
def f(a, b, c): ...

args = (1, 2, 3)
f(*args)          # 等价于 f(1, 2, 3)，按位置传参

kwargs = {"a": 1, "b": 2, "c": 3}
f(**kwargs)       # 等价于 f(a=1, b=2, c=3)，按关键字传参
```

- `*x`：把 x 里的元素依次变成位置参数。元组、列表、字符串、生成器等任何可迭代对象都可以。
- `**d`：把 d 的键值对变成关键字参数。d 必须是映射，键必须是字符串，并且要和参数名对得上。
- 两者可以混用，顺序是位置参数在前，关键字参数在后：`f(*(1, 2), **{"c": 3})` 等价于 `f(1, 2, c=3)`。

### 本项目中的例子：`state.finish(*stop)`

`limits.check` 在要停止时返回二元组 `(reason, detail)`，不用停就返回 `None`：

```python
stop = limits.check(...)          # (FailReason.step_limit, "50 steps (limit 50)") 或 None
if stop:
    state.finish(*stop)           # 等价于 state.finish(stop[0], stop[1])
```

`AgentState.finish` 的签名是 `finish(self, reason, detail=None)`。不加 `*` 时，整个元组会当成 `reason` 一个参数传入，`detail` 变成 `None`，类型也不对。

`if stop:` 同时判断两件事：`None` 是假值，表示没超限；非空元组是真值，表示超限。一个返回值既表达"要不要停"，也表达"为什么停"。

如果 `check` 返回的是 `{"reason": ..., "detail": ...}`，就要写成 `state.finish(**stop)`。

### 定义函数时：收集（与解包方向相反）

```python
def g(*args, **kwargs):
    print(args)     # 元组：多余的位置参数
    print(kwargs)   # 字典：多余的关键字参数

g(1, 2, x=3)        # args=(1, 2), kwargs={'x': 3}
```

定义处是"打包"，调用处是"解包"，符号相同、方向相反。

### 在字面量里

```python
[*a, *b]            # 合并列表
{**d1, **d2}        # 合并字典，后面的覆盖前面的同名 key
```

### 常见错误

- 对字典用 `*d` 只会展开出它的 key，不会报错，很容易写错却不易发现。
- 对元组用 `**` 会抛 `TypeError`。
- `**d` 里有函数没声明的 key 时，会抛 `TypeError: unexpected keyword argument`。

## `sys.exc_info()`：在 `finally` 里感知异常

`sys.exc_info()` 返回当前正在处理的异常的三元组 `(类型, 异常实例, traceback)`，没有异常时三项都是 `None`。`sys.exc_info()[1]` 取的是异常实例。

### 本项目中的用法（`agent/loop.py`）

```python
try:
    while not state.done:
        ...
finally:
    ...
    crashed = sys.exc_info()[1]
    if crashed is not None:       # harness 自己的 bug：仍然留下完整的 run_end span
        state.finish(FailReason.env_error, f"unexpected {type(crashed).__name__}: {crashed}"[:300])
    ...
    tracer.emit("run_end", ...)
```

`finally` 无论正常结束还是抛异常都会执行，但它本身不会告诉你是不是因为异常才进来的。在 `finally` 里调用 `sys.exc_info()`，如果有异常正在向外传播就能拿到它，正常结束则得到 `None`。

这样不需要再写 `except Exception`：异常照常继续向上抛，不会被吞掉，`finally` 只是顺便把它记录下来。效果是批量跑实验时，一道题因 bug 崩了，也会记成 `env_error`，轨迹里一定有完整的 `run_end`。

### 返回的是"当前正在处理的异常"

只有在某个 `except` 块**内部**，它才指向这个块捕获的异常；这个块结束后，恢复成外层的状态。

```python
def case_a():
    try:
        raise KeyError("outer")          # 主流程的异常，正在向外传播
    finally:
        try:
            raise ValueError("inner")
        except ValueError:
            print(sys.exc_info()[1])     # ValueError('inner')   ← 在内层 except 里，被内层覆盖
        print(sys.exc_info()[1])         # KeyError('outer')     ← 内层结束后恢复

def case_b():
    try:
        pass
    finally:
        try:
            raise ValueError("inner")
        except ValueError:
            pass
        print(sys.exc_info()[1])         # None                  ← 外层本来就没有异常，不会残留
```

以上输出已实测。

### 注意点

- 不要把 `sys.exc_info()` 放进 `finally` 里某个 `except` 块**内部**调用，否则拿到的是那个块的异常，不是真正在传播的异常。放在 `except` 块之外（即使前面有内层 `try/except` 已处理完）是安全的。
- `finally` 里新抛出的异常会取代原异常继续向上传播，原异常保留在新异常的 `__context__` 里。所以 `finally` 里调用的函数（如 `_final_patch`）最好自己处理异常，不要让它抛出来。
- `state.finish` 只在 `fail_reason` 为空时才记录，先调用的原因会保留。因此补丁错误（`patch_error`）先于崩溃异常记录，两者同时出现时，`env_error` 的详情是补丁错误。

## `for ... in` 中的枚举：`enumerate` 与 `enum.Enum`

"枚举"在 Python 里有两个不同的东西：内置函数 `enumerate`（给遍历加下标）和标准库的 `enum.Enum`（枚举类，如本项目的 `FailReason`）。

### `enumerate(iterable, start=0)`

它包装一个可迭代对象，每次产出 `(下标, 元素)` 二元组：

```python
for i, x in enumerate(["a", "b", "c"]):
    print(i, x)      # 0 a / 1 b / 2 c

list(enumerate("ab"))                      # [(0, 'a'), (1, 'b')]
for i, x in enumerate(xs, start=1): ...    # 下标从 1 开始
```

#### 本项目中的例子：嵌套解包（`agent/loop.py`）

```python
for i, (call_id, result) in enumerate(results):
```

`results` 的每个元素本身是 `(call_id, result)` 二元组，所以 `enumerate` 产出的是嵌套结构：

```
(0, (call_id0, result0))
(1, (call_id1, result1))
```

左边的 `i, (call_id, result)` 是嵌套解包：先拆外层得到 `i` 和内层元组，再把内层拆成 `call_id` 和 `result`。括号不能省，写成 `i, call_id, result` 会因为外层只有 2 个元素而抛 `ValueError: not enough values to unpack`。

#### 要点

- 返回的是**惰性迭代器**，不会预先生成完整列表，也不会复制元素。
- 只能遍历一次，遍历完就耗尽了。要多次使用需要 `list(...)`。
- 需要下标时用它代替 `for i in range(len(xs))` 加 `xs[i]`，更简洁，也不容易越界。
- 不依赖序列，生成器、文件对象、`dict.items()` 都能用，例如 `for i, (k, v) in enumerate(d.items())`。
- 遍历过程中不要修改被遍历的列表，下标会错位。

### `enum.Enum`：遍历枚举类

遍历的是**枚举类本身**，按定义顺序产出所有成员：

```python
class FailReason(Enum):
    step_limit = "step_limit"
    cost_limit = "cost_limit"

for m in FailReason:
    print(m.name, m.value)     # step_limit step_limit / cost_limit cost_limit

list(FailReason)               # [<FailReason.step_limit: 'step_limit'>, ...]
```

- 值相同的别名成员不会出现在遍历里，只有第一个（规范成员）会出现。想包含别名要用 `FailReason.__members__`（有序映射，名字到成员）。
- 遍历一个成员实例（如 `for x in FailReason.step_limit`）会抛 `TypeError`，因为成员本身不可迭代，除非它混入了 `str` 等类型。
- 两者可以组合：`for i, m in enumerate(FailReason)`。
