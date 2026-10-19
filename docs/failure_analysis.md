# 失败分析

只做定性分析。按 CLAUDE.md §2.3，这里不写汇总指标；解决率、步数、花费等数字以 `results/runs/<run_id>/metrics.json`、`score.json` 为准。

## verified_25（2026-10-16，`v0.1-frozen`，M0 与 B0）

M0 和 B0 没解决的是**同样 3 道题**：`django__django-11087`、`scikit-learn__scikit-learn-14983`、`sphinx-doc__sphinx-9602`（`results/runs/{M0,B0}/score.json`）。plan 原定看 5 条失败轨迹，但这一批里每个配置只有 3 条失败，都看了。

材料：题目描述、参考补丁与判分测试（数据集，revision 见 `configs/subsets/subsets_meta.json`）；两边提交的补丁（`results/runs/M0/patches/`、`results/runs/B0/mini/preds.json`）；判分日志（`results/runs/{M0,B0}/logs/run_evaluation/.../<id>/report.json`、`test_output.txt`）；完整轨迹（`results/runs/M0/trajectories/`、`results/runs/B0/mini/<id>/<id>.traj.json`）。

**共同点**
- 两边在这 3 题上都是**目标测试（FAIL_TO_PASS）全部失败、原有测试（PASS_TO_PASS）没有一个被改坏**：改了代码，但没改对，没有引入回归。
- 两边都跑了仓库里相关的已有测试并通过；判分用的是题目新增的隐藏测试，agent 运行时看不到。
- M0 一侧没有命令超时、权限拦截、格式错误，也没有发生压缩；长输出触发过"可恢复截断"（完整内容落盘），属正常机制，与失败无关。

**结论：3 题都不是 harness 的问题。** 两个 harness 在同一批题上失败、失败方式几乎相同，指向题目本身或模型，而不是 M0 的组件（上下文、工具、权限层）。

### scikit-learn__scikit-learn-14983：题目描述与隐藏测试冲突（**归类已被更正，见文末**）

- **现象**：`RepeatedKFold`、`RepeatedStratifiedKFold` 的 `repr` 要像其他分割器一样显示参数。
- **题目给出的期望输出**：`RepeatedKFold(n_splits=5, n_repeats=10, random_state=None)`，参数按构造函数顺序。
- **隐藏测试期望**：`RepeatedKFold(n_repeats=6, n_splits=2, random_state=None)`，参数按**字母顺序**。参考补丁复用了 sklearn 已有的 `_build_repr`（会排序），并让它从 `cvargs` 里取 `n_splits`。
- **两边的做法**：都严格按题目写的期望输出实现（M0 在两个子类里各写一个 `__repr__`；B0 在父类里按构造函数签名的顺序拼）。B0 在代码注释里明确写了"不能用 `_build_repr`，因为它按字母排序"，即看到了标准做法却为了符合题目而绕开了它。
- **根因**：题目与测试不一致。按题目做就过不了测试。
- **归类**：题目问题（规格冲突）。不是 harness 的问题；也很难算模型的错。

### django__django-11087：需求理解不完整（遗漏被 `to_field` 引用的字段）

- **现象**：级联删除时，查询关联对象会取所有字段，其中某个文本字段解码出错。要求只取"需要的字段"。
- **参考补丁**："需要的字段"是**被其他模型的外键引用的字段**（`get_candidate_relations_to_delete` 里每个关系的 `foreign_related_fields`）；有 `pre_delete` / `post_delete` 信号接收者或用了 `select_related` 时不省略。测试里 `SecondReferrer` 通过 `to_field='unique_field'` 引用 `Referrer`，所以必须同时取 `id` 和 `unique_field`。
- **M0**：在 `Collector.related_objects` 里，没有信号接收者时 `only('pk')`；同时让 admin 的 `NestedObjects` 取回全部字段（`defer(None)`）。思路和参考补丁很接近，也处理了信号条件，但**漏了被 `to_field` 引用的非主键字段**：测试比对 SQL 时少了 `"delete_referrer"."unique_field"`。
- **B0**：`only(related.field.name)`（只取外键字段），没有处理信号的情况，离正确答案更远。
- **根因**：模型对"需要的字段"的理解不完整；题目正文主要在描述解码报错，"被其他关系引用的字段"需要读懂删除流程才能推出来。
- **归类**：模型问题（需求理解不完整）。不是 harness 的问题。

### sphinx-doc__sphinx-9602：功能正确，但隐藏测试检查实现细节（节点类型）（**归类已被更正，见文末**）

- **现象**：`Literal[True]` 里的 `True` 被当成类生成交叉引用，开 nitpick 时报警告。
- **参考补丁**：`Literal[...]` 里的值先包一层 `nodes.literal`，最后再拆回普通文本节点，从而不生成交叉引用。
- **两边的做法**：都做到了"不生成交叉引用"，但都**各自独立地**把字面值包成 `desc_sig_keyword` / `desc_sig_literal_number` / `desc_sig_literal_string` 这类带格式的节点。
- **判分失败处**：测试用 `assert_node` 检查节点结构，期望 `True` 是普通文本节点；两边都在同一处失败：`The node '[2]' is not 'True': <desc_sig_keyword: <#text: 'True'>>`。
- **根因**：题目只要求"不触发 nitpick 警告"；测试同时规定了节点类型这一实现细节。
- **归类**：题目问题（测试过度约束实现）。不是 harness 的问题。

### 对实验的含义

- 这 3 题在 M0 与 B0 之间不提供区分度；两者解决率的差别（如果在完整 50 题上出现）会来自别的题。
- ~~3 题里有 2 题属于"题目或测试的问题"。~~ **已更正（见文末）**：重跑时这两题都解决了，应归为模型的做法选择。任何情况下都不能剔除题目（子集已冻结，CLAUDE.md §2.4）。

## 更正（2026-10-16）：M0 重跑后，两题的归类改为"模型的做法选择"

同一配置重跑 M0（`M0-r2`，`v0.1.1`，3 个 worker；`results/runs/M0-r2/`）后，scikit-learn-14983 和 sphinx-9602 **都解决了**，只有 django-11087 仍未解决。所以上面对这两题的"题目问题"归类下得太重：

- **scikit-learn-14983**：重跑时 M0 在父类 `_RepeatedSplits` 里直接复用仓库已有的 `_build_repr`（与参考补丁相同），并在子类里补上 `self.n_splits`，参数按字母顺序输出，通过了隐藏测试。题目示例与仓库已有的写法确实冲突，但**遵循仓库惯例就能做对**；第一次运行和 B0 选择了照抄题目示例。
- **sphinx-9602**：重跑时 M0 让 `Literal[...]` 里的值保持普通文本节点，只是不为它们生成交叉引用，正好符合测试检查的结构。**最朴素的做法就能通过**；第一次运行和 B0 额外把值包成了带格式的节点。

**更正后的归类**：这两题是**模型的做法选择**（参照仓库惯例 / 选择最朴素的实现），而且**每次运行会变**；不是题目本身的问题。django-11087 的归类不变（需求理解不完整）。三题仍然都不是 harness 的问题。

**教训**：只凭一次运行的失败给题目下"题目有问题"的结论不可靠；同一配置多跑一次，就可能得到不同结果。

## verified_50 的后 25 题（2026-10-20，M0 在 `v0.1.1`、B0 同时跑；判分见 `results/runs/{M0,B0}/score.json`）

M0 后半没解决 10 题，B0 没解决 8 题。材料同上一节：题目、参考补丁与判分测试（数据集固定 revision）、两边的补丁（`results/runs/M0/preds.jsonl`、`results/runs/B0/preds.jsonl`）、判分报告（`logs/run_evaluation/.../<id>/report.json`）、M0 的完整轨迹。

**先查 harness**：这一批 M0 的未解决题里，没有格式错误、模型输出截断、重试，也没有命令超时；结束方式都是正常提交。harness 的痕迹有两类：权限层的误拦（见下面），以及两次压缩。

**两次压缩（2026-10-20 更正：原文写"没有压缩"，错了）**：django-12663（第 101 步）和 django-15957（第 94 步）在 128k 预算下各发生 1 次压缩，都是在提示词约 109k 时只清理旧的工具输出（`actions: [truncate]`，没有摘要，花费 0），之后又分别走了 30、39 步。django-15957 的 7 次源码修改全部在压缩之前，之后只跑了测试；django-12663 的第一次修改（第 94 步，改在 `Subquery`，即上面说的"错误的层"）也在压缩之前，压缩后的 3 次修改是在同一个思路上继续调整。两题压缩后重复之前调用的比例都是 0。所以压缩没有改变补丁的思路，归属仍是模型；但不能完全排除清理掉旧输出对后续修改有影响。

**权限层误拦与失败的关系**（django-12663、django-15957、sphinx-9461）：三题里 M0 各被 `deny.test_edit_bash` 拦了 1 次，都是想在 `tests/` 下**新建**临时测试文件（如 `tests/queries/test_lazy_repro.py`），属误拦。被拦之后 agent 先用 `ls` 确认文件没写进去，再改成在 `/tmp` 写独立脚本，大约多花 1–2 步；之后仍然多次用项目自己的测试运行器（django-12663 被拦后又跑了 9 次）。B0 在这三题里从没往 `tests/` 写过临时文件，django-12663、django-15957 仍然解决了。所以**误拦增加了少量步数，但不是这三题失败的原因**。误拦本身按第 4 天的误拦率如实统计。

**只有 B0 解决的 3 题**
- **django-12663**（`SimpleLazyObject` 用在嵌套子查询里报错）：参考补丁改 `Query.output_field`，让它用所选列的 `target`；M0 在 `Subquery._resolve_output_field` 里加了一套从 WHERE 子句推断类型的启发式，没修到点上（目标测试 0/1）；B0 换了第三种做法（在 lookup 里把模型实例换成主键），通过了测试。**归属：模型**（定位到了错误的层）。
- **django-15957**（Prefetch 支持切片的 queryset，参考补丁是新功能级的改动）：M0 的实现思路和参考补丁一样（`RowNumber` 窗口函数按分区取前 N 条），但 4 个目标测试错 1 个（`test_reverse_ordering`），还弄坏了 1 个原有测试（`test_attribute_error`）；B0 全部通过。**归属：模型**（实现细节，改动大、步数 133）。
- **scikit-learn-14629**（`cross_val_predict(method='predict_proba')` 配 `MultiOutputClassifier` 报错）：题目正文明确指向 `_validation.py` 里用 `estimator.classes_` 的地方；M0 照着改了调用处，参考补丁和 B0 都改成给 `MultiOutputClassifier` 加上 `classes_` 属性，隐藏测试检查的正是这个属性。**归属：模型**（做法选择：照题目指的位置修，没有修到类本身）。

**只有 M0 解决的 1 题**
- **django-11433**（表单的 `cleaned_data` 允许覆盖字段默认值）：B0 的补丁目标测试没过，还弄坏了 1 个原有测试。**归属（B0）：模型**。

**两边都没解决的 7 题**（两边改的文件大多与参考补丁有重叠，django-11477 例外：两边都改在调用方 `urls/base.py`；sphinx-9461 只部分重叠；都没有 harness 痕迹）
- **django-11477**（`translate_url()` 遇到缺省的命名组）：参考补丁改 `urls/resolvers.py`，两边都改了 `urls/base.py`，3 个目标测试都只过 1 个。**模型**（修在调用方而不是根源）。
- **django-11734**（`exclude()` / `~Q()` 里的 `OuterRef` 用错模型）：参考补丁改了 3 个文件，两边都只改了 `sql/query.py`，目标测试 0/1。**模型**（修得不完整）。
- **django-12273**（子模型把主键设为 None 后保存）：两边都改了正确的文件，目标测试都是 0/2。**模型**。
- **sphinx-7462**（空元组类型注解导致 `IndexError`）：参考补丁同时改 `domains/python.py` 和 `pycode/ast.py`，两边都只改了前者，2 个目标测试都过 1 个。**模型**（修得不完整）。
- **sphinx-9461**（同时用 `@classmethod` 和 `@property` 的方法不被文档化；数据集标为"1-4 hours"）：参考补丁改 3 个文件，两边都改了其中一部分，目标测试都是 0/3。**模型**。
- **sympy-13798**（`latex()` 允许自定义 `mul_symbol`）：两边都支持了自定义符号，但测试还规定了数字之间仍用 `\cdot`（`latex(1.5*3**x, mul_symbol='\\,')` 要得到 `1.5 \cdot 3^{x}`），两边都没做对。**模型**（没有照参考实现的细节处理数字的情况；题目没写这一点，接近规格不完整，但一次运行不下"题目问题"的结论，见上一节的教训）。
- **sympy-21930**（`secondquant` 的 LaTeX 输出缺括号）：测试检查确切的字符串（外层加花括号）。B0 照这个思路加了括号，6 个目标测试过 5 个；M0 改用优先级加括号，输出格式不同，0/6。**模型**（做法选择）。

**结论**：后半的 18 个失败（M0 10、B0 8）**都不是 harness 的问题**，不需要修 harness，不需要换版本重跑。权限层误拦在其中 3 题里多花了少量步数，计入误拦率，但没有决定结果。失败的主要类型：修在调用方或错误的层（django-11477、django-12663、scikit-learn-14629）、多文件修复只做了一部分（django-11734、sphinx-7462、sphinx-9461）、隐藏测试检查确切输出而做法不同（sympy-13798、sympy-21930）。

## 归属标注（机器可读，`eval/report/failure_taxonomy.py` 读这张表）

每行一个"运行 × 未解决的题"，归属只能是 `harness`、`model`、`task` 之一；依据指向上文的分析。没有出现在表里的失败题，脚本记为 `unlabelled`。

| run_id | instance_id | 归属 | 依据 |
|---|---|---|---|
| M0 | django__django-11087 | model | 需求理解不完整：漏了被 `to_field` 引用的字段 |
| M0 | scikit-learn__scikit-learn-14983 | model | 做法选择：照抄题目示例而没有复用 `_build_repr`（更正后的归类） |
| M0 | sphinx-doc__sphinx-9602 | model | 做法选择：把字面值包成带格式的节点（更正后的归类） |
| B0 | django__django-11087 | model | 同 M0，离参考补丁更远 |
| B0 | scikit-learn__scikit-learn-14983 | model | 同 M0（更正后的归类） |
| B0 | sphinx-doc__sphinx-9602 | model | 同 M0（更正后的归类） |
| M0-r2 | django__django-11087 | model | 重跑仍未解决，归类不变 |
| M0 | django__django-11477 | model | 修在 `urls/base.py`，根源在 `resolvers.py` |
| M0 | django__django-11734 | model | 多文件修复只做了一部分 |
| M0 | django__django-12273 | model | 改对了文件，没改对 |
| M0 | django__django-12663 | model | 修在错误的层（`Subquery` 启发式）；误拦 1 次不是原因 |
| M0 | django__django-15957 | model | 实现细节错 1 个目标测试、弄坏 1 个原有测试；误拦 1 次不是原因 |
| M0 | scikit-learn__scikit-learn-14629 | model | 照题目指的调用处修，测试要的是类上的 `classes_` |
| M0 | sphinx-doc__sphinx-7462 | model | 漏了 `pycode/ast.py` |
| M0 | sphinx-doc__sphinx-9461 | model | 多文件修复只做了一部分；误拦 1 次不是原因 |
| M0 | sympy__sympy-13798 | model | 数字之间的乘号没按测试处理 |
| M0 | sympy__sympy-21930 | model | 用优先级加括号，输出格式与测试不同 |
| B0 | django__django-11433 | model | 目标测试没过，弄坏 1 个原有测试 |
| B0 | django__django-11477 | model | 同 M0 |
| B0 | django__django-11734 | model | 同 M0 |
| B0 | django__django-12273 | model | 同 M0 |
| B0 | sphinx-doc__sphinx-7462 | model | 同 M0 |
| B0 | sphinx-doc__sphinx-9461 | model | 同 M0 |
| B0 | sympy__sympy-13798 | model | 同 M0 |
| B0 | sympy__sympy-21930 | model | 加了括号，6 个目标测试过 5 个 |

## 权限层拦截的人工复核（机器可读，供误拦率使用；2026-10-20）

M0（verified_50）的全部拦截。复核方法：读被拦的命令；在该题的官方镜像里（断网、一次性容器）检查被写的路径在基准提交里是否已存在。8 个路径（7 次拦截，其中一次写两个文件）**都不存在**，都是新建的临时测试或测试夹具，没有一次是修改已有的测试或断言，所以都判为**误拦**。原因是 `deny.test_edit_bash` 按"写入 `tests/` 目录"匹配，分不出新建和修改（`docs/notes.md` §25 第 2 条）；`policy.yaml` 与 M0 一起冻结，不改。

| run_id | instance_id | rule_id | 判定 | 被拦的命令（摘要） |
|---|---|---|---|---|
| M0 | django__django-12663 | deny.test_edit_bash | false_block | 新建 `tests/queries/test_lazy_repro.py` |
| M0 | django__django-14999 | deny.test_edit_bash | false_block | 新建 `tests/migrations/test_tmp_rename_db_table.py` |
| M0 | django__django-15957 | deny.test_edit_bash | false_block | 新建 `tests/prefetch_related/test_slice_tmp.py` |
| M0 | sphinx-doc__sphinx-8035 | deny.test_edit_bash | false_block | 新建 `tests/scratch_private_specific_test.py` |
| M0 | sphinx-doc__sphinx-8269 | deny.test_edit_bash | false_block | 新建 `tests/test_tmp_linkcheck_fix.py` |
| M0 | sphinx-doc__sphinx-8621 | deny.test_edit_bash | false_block | 新建 `tests/scratch_kbd_issue.py` |
| M0 | sphinx-doc__sphinx-9461 | deny.test_edit_bash | false_block | 新建 `tests/roots/test-ext-autodoc/target/classmethod_property.py` 和 `tests/test_scratch_issue.py` |
