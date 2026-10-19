# pf-test-audit 错题本

开工前对照查一遍。判据正文在 [`./SKILL.md`](./SKILL.md)、[`./garbage-patterns.md`](./garbage-patterns.md) 与 [`./compression-criteria.md`](./compression-criteria.md) 里，这里只留症状与纠正信号。

## `test-audit-conductor-reads-code` · 指挥者读代码

**症状**：指挥者用 Read 翻测试源文件。

**纠正信号**：subagent 报的内容，指挥者早看过原文。

## `test-audit-ships-without-verifying` · 不验证就收工

**症状**：改完不编译、不跑剩余测试。

**纠正信号**：收尾报告里没有编译与测试的读数。

## `test-audit-deletes-a-whole-mixed-case` · 混合用例整段全删

**症状**：一个 TEST_CASE 里只有部分断言是垃圾，整个却被删。

**纠正信号**：删除粒度是整段容器，而段里还留着验业务的断言。

## `test-audit-empties-a-file-without-the-build` · 清空文件后不更新构建系统

**症状**：删光内容的测试文件留在原地，构建系统还在编译它。

**纠正信号**：文件空了，CMakeLists.txt / Cargo.toml / package.json 里还列着它。

## `test-audit-splits-for-the-count` · 为凑数拆 subagent

**症状**：文件不多也按模块机械拆出四五个 scan subagent。

**纠正信号**：一个 subagent 本来背得下整份清单，却被拆成好几个。

## `test-audit-trades-coverage-for-time` · 拿删除换时间

**症状**：为了压快档墙钟，删掉慢但验真业务的测试。

**纠正信号**：待删条目只在压缩判据上命中，质量那一格空着。

## `test-audit-moves-one-lane-only` · 只动快档、不管另一档

**症状**：把测试移出快档，却没同步慢档命令 / 标记 / 收集清单；或者移完没真跑一遍。

**纠正信号**：被移走的用例两档都不在，或报告里没有移档后的实测读数。

## `test-audit-scanner-decides` · 扫描结果自带「建议删除」

**症状**：scan subagent 在返回里写"建议删除""可以删掉"。

**纠正信号**：结构化返回里带着动作裁定，两条判据的命中事实倒没了。

## `test-audit-asks-the-human` · 把裁定推回用户

**症状**：报告里写"请你勾选要删哪些""等你确认再动手"。

**纠正信号**：一次跑完的报告里出现待确认的问句。