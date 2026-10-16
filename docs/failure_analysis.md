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
