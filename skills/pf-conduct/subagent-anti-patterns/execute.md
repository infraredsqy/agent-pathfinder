# Execute / Fix subagent 错题本

执行 spec 前先对照查一遍。判据正文在 [`../subagent-prompts/execute.md`](../subagent-prompts/execute.md) 里，这里只留症状与纠正信号。

## `conduct-execute-modifies-spec` · 执行侧修改 spec.md

**症状**：觉得 spec 里某条不合理，就按自己的理解改了，或者顺手把某个「待确认」按自己的猜测定了。

**纠正信号**：报告里出现 spec 之外的目标，而 spec.md 的改动晚于开工那一刻。

## `conduct-execute-adds-out-of-scope-work` · 自行补入 spec 范围外的内容

**症状**：读代码看到某个地方「还缺一块」，顺手补了——spec 里其实没有这件事。

**纠正信号**：改动清单里有一条说不出对应 spec 的哪一条。

## `conduct-execute-parallel-edits-other-file-group` · 并行执行期间改动他组文件

**症状**：并行执行时改了不属于自己文件组的文件，跟别的 agent 撞车。

**纠正信号**：改动清单里出现没分给你的文件。

## `conduct-execute-does-not-stop-on-blocker` · 遇阻未停止并上报

**症状**：测试反复失败还一直重试；发现 spec 和实际代码对不上还接着改；拿不准的操作闷头试。

**纠正信号**：报告里一路是"再试一次"，没有一份 FAIL。

## `conduct-execute-runs-irreversible-operations` · 执行不可逆操作

**症状**：打着「修复问题」的旗号做破坏性操作——批量删文件/目录、`git reset --hard`、大范围覆盖、删 `.git`。

**纠正信号**：动手前那一刻答不出「这一步怎么退回去」。

## `conduct-execute-continues-past-failing-tests` · 测试失败仍继续推进

**症状**：报告某个测试失败，补一句「不影响后面」。

**纠正信号**：报告里的通过读数与本轮碰过的文件对不上。

## `conduct-execute-assumes-failure-preexisting` · 自行认定失败为既有问题

**症状**：看到失败就下结论「这是之前就有的问题」，跳过去。

**纠正信号**：报告里只有结论，没有哪个测试、什么命令、什么输出。

## `conduct-execute-vague-deviation-report` · 偏差报告未逐条写清

**症状**：执行中和 spec 有出入，报告里一句「有些小调整」。

**纠正信号**：偏差那一项只有形容词，读不出哪一处、实际怎么做的。

## `conduct-execute-pastes-evidence-into-report` · 将证据材料写入报告正文

**症状**：命令原始输出、合成样本、哈希、长引用这些内容整段贴进 `report.md`；一个交付文件对应一千行的说明。

**纠正信号**：报告长到下一棒读不完，而里面大半是读者手上本来就有的材料。

## `conduct-execute-appends-stale-readings` · 过期读数只追加不更正

**症状**：正文的数字后来变了，就在旁边补一句「**历史读数**：……」，旧读数原地留着。报告从几百行涨到两千多行（实例：一个节点正稿 429 行，`report.md` 2017 行）。

**纠正信号**：同一份报告里同一个数出现两次，其中一次标着「历史」。

## `conduct-execute-dispatches-on-conductor-instructions` · 依指挥位说明继续派发 subagent

**症状**：spec 或方法卡里写着「按改动域分批，每批派 Fix / Review，批过即提交」，你就照它派起了 subagent——一层套一层。

**纠正信号**：你这一棒出现了派发动作，而你的 prompt 里没有这一项。

## `conduct-execute-defers-as-delivery` · 以"已知未解决"替代交付

**症状**：spec 里有一条现在做不了，你把它标成"已知未解决"就算交差。

**纠正信号**：报告以一条"已知未解决"收尾，却说不出它缺什么、谁能解、影响哪些验收项。
