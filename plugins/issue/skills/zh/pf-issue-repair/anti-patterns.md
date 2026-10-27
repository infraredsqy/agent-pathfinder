# pf-issue-repair 错题本

开工前对照查一遍。判据正文在 [`./SKILL.md`](./SKILL.md) 与 `/pf-conduct` 里，这里只留症状与纠正信号。

## `issue-repair-skips-review-for-throughput` · 为提高吞吐跳过复审

**症状**：条目多的时候想"这条太小了不用审"，Fix 完直接 commit——什么算「不用审」你判不出来。

**实例**：一处"只是把 RAII guard 提到共享头文件"的改动漏了 `#include`，编译不过，几个小时后才被复审撞见。

**纠正信号**：这一批只有 Fix 的报告，没有 Review 的那一份。

## `issue-repair-mixes-change-domains-in-one-batch` · 将不同改动域合并为同一批

**症状**：把不同模块、没法一次验证的条目打包给同一个 Fix；或修批内条目时顺手改了批外代码——批外夹带污染批次边界，复审追溯不了。

**纠正信号**：这一批的条目落在两个以上模块，而验证只有一次。

## `issue-repair-batch-composition-unreported` · 汇报未列出批次构成

**症状**：批怎么分的、每批含哪些条目，收尾时一句不提。

**纠正信号**：汇报里有读数与落定表，读的人却拼不出这批改了哪些条目、走的哪一次提交。

## `issue-repair-acknowledge-or-external-as-exit` · 误用 acknowledge 或 external 充当 repair 的出口

**症状**：修不动就 `acknowledge`；把「已经给过解法、解法又成立」的条目改判成 `external`。

**纠正信号**：落定表里出现 `acknowledge`，或一条已给解法的条目被转成 `external`。

## `issue-repair-batch-uncommitted` · 批次通过复审后未提交

**症状**：一整批改完、复审也过了，就接着开下一批，没有 commit。

**纠正信号**：下一批的改动与前一批混在同一份未提交的改动里。

## `issue-repair-widens-scope` · 超出指定范围扩展修复面

**症状**：spec 说"把这一个度量拉回线下"，你顺手把池子里别的域也清了。

**纠正信号**：这一趟 resolve 的条目里，有几条的度量不在叫你来的那份东西里。
