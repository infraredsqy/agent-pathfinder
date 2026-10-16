# pf-issue-discuss 错题本

开工前对照查一遍。规则本身在 SKILL.md 里。

## `issue-discuss-batch-over-decision-quota` · 单批讨论超出 4 个决策上限

**症状**：把十几条 external 一次摆出来，逐条问"改不改？A 还是 B？"

**为什么危险**：每做一个决策要换一次上下文，四五次之后人就没有判断力了。

**纠正**：先过一遍复核把伪 external 摘掉，再按 ≤4 一批；机主想接着谈，等这一批走完再开下一批。

## `issue-discuss-submits-unreviewed-externals` · 未经复核即提交 external 条目

**症状**：池子里标着 external 就照单全端——二十条里十二条其实 suggestion 已经写明怎么修。

**纠正**：进门先跑复核；能写出一句具体修法的就改判 `autofix` 并把那句写进 `reason`，剩下的才摆给机主。

## `issue-discuss-details-instead-of-tradeoffs` · 向机主输出实现细节而非取舍

**症状**：讨论时把函数名 / 变量名 / 行号 / 调用链甩给机主，让他在代码细节里自己找决策点。

**纠正**：对条目写定位，对机主讲取舍——什么毛病、几条路、各自代价；细节等机主追问时再展开。

## `issue-discuss-reason-records-discussed-only` · `--reason` 只记"已讨论"，未落结论本身

**症状**：机主说"统一报错"，reason 里只写"已讨论"。

**纠正**：reason 写机主定的内容本身，后面动手的人只读得到这一句。
