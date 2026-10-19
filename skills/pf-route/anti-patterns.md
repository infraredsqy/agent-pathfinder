# pf-route 错题本

执行前对照检查。判据正文在 [`./SKILL.md`](./SKILL.md) 里，这里只留症状、纠正信号与实例。

## `route-reads-source-code` · 直接读取工程源码

**症状**：用户提及某个功能，Agent 直接 read 了源码文件

**纠正信号**：用户说"你怎么在看代码"

## `route-edits-project-files-directly` · 绕过管线直接修改工程文件

**症状**：讨论过程中 Agent 自己 edit/write 了工程代码或文档正文

**纠正信号**：用户说"我没让你改代码"

## `route-advances-without-reading-graph-or-docs` · 未读图与项目文档即推进讨论

**症状**：用户给个想法，Agent 不读 DAG 也不读项目文档，直接开始讨论和写节点

**纠正信号**：用户说"这个节点已经有了"、"你这个和之前的冲突"

## `route-dumps-raw-data-at-user` · 向用户展示未经提炼的原始数据

**症状**：用户问"图里有什么"，Agent 直接把整份节点 JSON 倒出来

**纠正信号**：用户说"太多了"、"说重点"

## `route-node-name-depends-on-source-context` · 节点命名依赖来源文档的上下文

**症状**：节点名用了"方向5"、"方向1"等来源文档的内部编号或章节引用

**纠正信号**：用户说"这个命名不 make sense"、"名称应该自描述"

## `route-creates-orphan-node` · 新建节点未声明下游依赖，形成孤点

**症状**：新建节点只加了上游依赖（该节点依赖谁），没加下游依赖（谁应依赖该节点）——孤点不在任何推荐路径里，urgency 和 importance 为 0，等于被排除在管线之外

**纠正信号**：列图时这个节点不出现在任何推荐路径里

## `route-deletes-node-or-edge-without-impact-check` · 未经影响面核对即删除节点或依赖边

**症状**：用户说"删掉这个节点"，Agent 直接强制删除；或没查上下游就把边去掉了——断掉的是别人的前置，它静默消失后，下游节点在错误的前提下被跑起来，图上看不出少过什么

**纠正信号**：用户说"这条边你怎么给我断了"、"谁让你删的"

## `route-splits-without-verification-boundary` · 节点拆分不以独立验证边界为依据

**症状**：Agent 提出的节点拆分找不到独立的验证标准。要么多个节点共享同一个验证（拆太细），要么一个节点内部混了两种不同的验证方式（拆太粗）

**纠正信号**：用户说"这两个应该绑在一起做"、"拆这么细干什么"

## `route-spec-has-implementation-detail-or-no-done-check` · spec 写入实现细节，或缺少可执行的完成判定

**症状**：spec 里写进了函数名、文件路径、行数、调用顺序；或者通篇是「做 X 的设计」「调 Y」这类话，找不出一条能跑出通过 / 失败读数的验收口径

**纠正信号**：pf-conduct 备料停手，说「完成判定没法跑」或「spec 与代码前提不成立」

## `route-lowers-parallel-instead-of-adding-edges` · 以调低 parallel 取代在图上声明依赖边

**症状**：发现两格（或三格）要改同一批文件，就去把这条线的 `parallel` 调小或调成 1；理由通常写成「为安全起见」「免得撞车」

**纠正信号**：用户说「撞车就加边，不是降并行」「你别拿 parallel 去补」

## `route-writes-node-before-requirements-settle` · 需求未收敛即落节点与 spec

**症状**：用户还在讲设计、取舍还没定，Agent 就把这一轮讨论压成结论，直接建节点、写 spec，中间没摆过方案

**纠正信号**：用户说「你 jumped too fast」「你先停下来，先跟我讨论」「把你刚加的那几个节点全干掉」

## `route-creates-discussion-or-acceptance-node` · 将讨论与总验收建为图中节点

**症状**：图里出现「与用户讨论 X 的设计」「全部做完之后实机测试／联调／验收」这类节点

**纠正信号**：用户说「这个节点不用建」「这个我自己试就行」
