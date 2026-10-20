# 复审 subagent 错题本

拿到复审任务先对照查一遍。判据正文在 [`../subagent-prompts/review.md`](../subagent-prompts/review.md) 里，这里只留症状与纠正信号。

## `conduct-review-fixes-code-itself` · 复审期间自行修改代码

**症状**：发现问题直接动手修，只报告这件事没做到。

**纠正信号**：这一轮里工作区有改动，而你的 prompt 只让你读。

## `conduct-review-only-rechecks-last-issues` · 仅复核上一轮 issue 即报 PASS

**症状**：第二、第三轮只查上一轮的 critical / minor 修好没有，确认完直接报 PASS——修复本身会引入新问题，只做针对性确认等于默认修复没有副作用。

**纠正信号**：这一轮的清单里只有上一轮那几条。

## `conduct-review-adds-features-to-spec` · 在复审报告中增补 spec 未要求的功能

**症状**：「A 是 B 的前置，spec 里没写 A」，于是报告里写「需要增加 A」。

**纠正信号**：issue 列表里出现一条要求 spec 没提过的东西。

## `conduct-review-judges-other-nodes` · 对前置或其他节点作越界判定

**症状**：把前置节点的功能缺失、或者别的节点的活，当成 critical / minor 标出来。

**纠正信号**：一条 issue 的位置落在本节点 spec 的范围之外。

## `conduct-review-accepts-preexisting-failure-claim` · 直接采信既有失败的归因

**症状**：执行方说某个测试失败是既有问题，复审点头放过。

**纠正信号**：报告里只有转述，没有你自己跑出来的那条命令与输出。

## `conduct-review-reports-unrun-verification` · 未执行即报告验证结果

**症状**：报告里写「验证流程应可通过」「测试预计全过」，实际没执行。

**纠正信号**：结论用的是推测语气，命令那一栏空着。

## `conduct-review-count-line-missing-or-mismatched` · 未给计数首行，或计数与列表不一致

**症状**：报告写一堆分析，没有首行的计数，或者数了对不上列表。

**纠正信号**：拿首行两个数去数下面的条目，对不上。

## `conduct-review-copies-known-material` · 将读者已有材料抄录进报告

**症状**：一条 issue 的位置列十个行号，后面每个字段又重列一遍；大段逐字引用 spec 或正稿原文当"依据"；开头先摆一张几份文件的 `sha256` 与行数表。一轮四百多行，其中判断占不到一半。

**纠正信号**：抄录的行数压过判断的行数。

## `conduct-review-clamped-reading-as-evidence` · 以受钳制的读数充当证据

**症状**：自证的脚本里有 `segs = Math.min(slots, …)` 这种把超出部分按回上限的写法，于是"0 处破口"成立；去掉那两处钳制再跑，真实的越界就露出来。

**纠正信号**：自证脚本在旧行为下也报「全过」。（实例：某个节点第四轮修复的自证脚本 `fix4-verify.mjs`。）
