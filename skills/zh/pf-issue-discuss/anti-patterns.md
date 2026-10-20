# pf-issue-discuss 错题本

开工前对照查一遍。判据正文在 [`./SKILL.md`](./SKILL.md) 里，这里只留症状与纠正信号。

## `issue-discuss-batch-over-decision-quota` · 单批讨论超出 4 个决策上限

**症状**：把十几条 external 一次摆出来，逐条问"改不改？A 还是 B？"

**纠正信号**：用户开始回"你看着办"，或这一批摆了五条以上。

## `issue-discuss-submits-unreviewed-externals` · 未经复核即提交 external 条目

**症状**：池子里标着 external 就照单全端——二十条里十二条其实 suggestion 已经写明怎么修。

**纠正信号**：摆到用户面前的第一条，你还没试过替它写一句具体修法。

## `issue-discuss-details-instead-of-tradeoffs` · 向用户输出实现细节而非取舍

**症状**：讨论时把函数名 / 变量名 / 行号 / 调用链甩给用户，让他在代码细节里自己找决策点。

**纠正信号**：用户追问"所以我要定的是什么"。

## `issue-discuss-reason-records-discussed-only` · `--reason` 只记"已讨论"，未落结论本身

**症状**：用户说"统一报错"，reason 里只写"已讨论"。

**纠正信号**：把这条 `--reason` 给后面动手的人看，他读不出用户定了什么。
