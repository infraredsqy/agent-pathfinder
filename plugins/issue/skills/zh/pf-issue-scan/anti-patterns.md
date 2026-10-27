# pf-issue-scan 错题本

开工前对照查一遍。判据正文在 [`./SKILL.md`](./SKILL.md)、[`./scan.md`](./scan.md) 与 [`../pf-issue-repair/pool.md`](../pf-issue-repair/pool.md) 里，这里只留症状与纠正信号。

## `issue-scan-full-corpus-in-one-agent` · 以单个 subagent 覆盖全量扫描

**症状**：一个 subagent 扫所有模块，回来一堆重复代码 + 职责越界 + 结构问题混在一起。

**纠正信号**：派出去的面数与模块数一比，一个 agent 背了好几份。

## `issue-scan-explores-without-reading-docs` · 未读项目文档即开始探查

**症状**：不读 CLAUDE.md / docs 就派 scan。

**纠正信号**：拆面与写 prompt 时答不出这个项目的模块怎么分。

## `issue-scan-finding-not-locatable` · 入库发现缺少可定位信息

**症状**："game_ui.js 里有重复代码"——没有函数名，也没有语义。

**纠正信号**：拿这条发现回去找，落不到哪个函数、哪段语义上。

## `issue-scan-rescans-without-pool-dedup` · 重复扫描未与池子去重

**症状**：跑一次全量扫描就往池子里加一遍，池子里早有同一条。

**纠正信号**：新增的条目里有几条与池中旧条目同址同症。

## `issue-scan-absence-read-as-resolved` · 以缺席推定条目已消失

**症状**：批量 resolve 不在存活名单里的条目；或 scan 没找到某条的代码就判它已修（实际只是代码挪了位置）；或扫描刚入库就跑核对，新条目因"不在名单"被误清。

**纠正信号**：一次核对下来，被结掉的条目里有一条没经过逐条核实。

## `issue-scan-refactor-as-small-item` · 将结构性重构记为可随手修复的小额条目

**症状**：发现某模块结构极差，写成一条 `autofix` 的小条目丢进池子。

**纠正信号**：这条债的文件面铺开在三个以上模块，而条目的体积写得像顺手一改。

## `issue-scan-decides-for-the-user` · 扫描阶段代用户判定 decision

**症状**：拿不准的条目一律标成"要用户定"，回来一看二十条 external。

**纠正信号**：条目的 `description` / `suggestion` 里出现"要拍板"，却说不出缺的是哪一课。
