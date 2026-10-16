# pf-issue-repair 错题本

开工前对照查一遍。规则本身在 SKILL.md 里；循环与角色卡在 `/pf-conduct`。

## `issue-repair-skips-review-for-throughput` · 为提高吞吐跳过复审

**症状**：条目多的时候想"这条太小了不用审"，Fix 完直接 commit。

**为什么危险**：你判不出什么算"不用审"。一个节点里曾有一处"只是把 RAII guard 提到共享头文件"的改动漏了 `#include`，编译不过，几个小时后才被复审撞见。

**纠正**：永远不跳复审。量大的做法是多分批，每批一轮 Fix → Review，不自行降级。

## `issue-repair-conductor-edits-code` · 指挥者自行修改工程代码

**症状**：指挥者觉得"这条一眼就能改"，顺手 edit 了。

**纠正**：改代码的只有你派的 Fix。你自己不读实现代码、不改工程代码——要了解代码就派 subagent，把路径和要问的问题写进 prompt。

## `issue-repair-mixes-change-domains-in-one-batch` · 将不同改动域合并为同一批

**症状**：把不同模块、没法一次验证的条目打包给同一个 Fix；或修批内条目时顺手改了批外代码。

**为什么危险**：跨域改动无法集中验证；批外夹带污染批次边界，复审时追溯不了。

**纠正**：批 = 一个改动域；确需跨模块的改动族整批显式声明。批外发现记进池子，留给后面的批次或扫描。

## `issue-repair-batch-composition-unreported` · 汇报未列出批次构成

**症状**：批怎么分的、每批含哪些条目，收尾时一句不提。

**纠正**：分批由你自主（同域成批，不必逐批问人），但**构成要写进汇报**：批、域、条目、commit 四列对齐。

## `issue-repair-acknowledge-or-external-as-exit` · 误用 acknowledge 或 external 充当 repair 的出口

**症状**：修不动就 `acknowledge`——押后是池子人机面（`pf-issue-discuss`）的动作，repair 这一侧不做它；同族的另一种错法是把「已经给过解法、解法又成立」的条目改判成 `external`。

**纠正**：repair 的出口只有三扇——修掉（`resolve`）、查下来这条动不了或取舍在机主（`external`）、查下来不是债（`reject`）。这一趟排不下的条目留着不动（权重照算），在汇报的「已知未解决项」里点名；解法成立的不许转 `external`。

## `issue-repair-batch-uncommitted` · 批次通过复审后未提交

**症状**：一整批改完、复审也过了，就接着开下一批，没有 commit。

**纠正**：批过即提交是 checkpoint，也是下一批的干净边界。提交带上池子改动（`.pf/issues/`）；这一批只动了池子就单独发一发（`chore: record issue pool changes`）。

## `issue-repair-widens-scope` · 超出指定范围扩展修复面

**症状**：spec 说"把这一个度量拉回线下"，你顺手把池子里别的域也清了。

**纠正**：范围由叫你来的那份东西定，判据过了就停手。别的度量越线、别的活更急，都不是这一格的范围——谁越线谁自己进来一趟。
