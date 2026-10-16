# pf-issue-scan 错题本

开工前对照查一遍。规则本身在 SKILL.md 与 [`../shared/pf/pool.md`](../shared/pf/pool.md) 里。

## `issue-scan-full-corpus-in-one-agent` · 以单个 subagent 覆盖全量扫描

**症状**：一个 subagent 扫所有模块，回来一堆重复代码 + 职责越界 + 结构问题混在一起。

**纠正**：按模块 × 维度拆，一个 subagent 只盯一格；模块少时自己裁量要不要合并维度。

## `issue-scan-explores-without-reading-docs` · 未读项目文档即开始探查

**症状**：不读 CLAUDE.md / docs 就派 scan。

**纠正**：模块划分、架构约定、已知问题都在文档里，它们决定怎么拆面、怎么写 prompt。

## `issue-scan-finding-not-locatable` · 入库发现缺少可定位信息

**症状**："game_ui.js 里有重复代码"——没有函数名，也没有语义。

**纠正**：定位信息按函数名 / 类名 → 语义描述 → 文件路径排序写；行号可省。

## `issue-scan-rescans-without-pool-dedup` · 重复扫描未与池子去重

**症状**：跑一次全量扫描就往池子里加一遍，池子里早有同一条。

**纠正**：第一步读池子；新发现入库前先搜同一条，写库时去重合并。

## `issue-scan-absence-read-as-resolved` · 以缺席推定条目已消失

**症状**：批量 resolve 不在存活名单里的条目；或 scan 没找到某条的代码就判它已修（实际只是代码挪了位置）；或扫描刚入库就跑核对，新条目因"不在名单"被误清。

**为什么危险**："没确认 = 死了"只在闭环假设下成立——名单得覆盖全部在位条目，且扫描开始后池子没有新增。add 之后跑核对必然错配：新条目入库晚于扫描启动，扫描自然没见过它们；名单上这次缺席记的是入库时点，与代码现状无关。

**纠正**：照 SKILL「合并入库」的存活核对走——`mark` 只累加 check_count 并报 candidates、永不改状态；结掉一条只走逐条 `resolve <id> --reason`。动手前按三层定位核实。

## `issue-scan-refactor-as-small-item` · 将结构性重构记为可随手修复的小额条目

**症状**：发现某模块结构极差，写成一条 `autofix` 的小条目丢进池子。

**纠正**：小规模重构（提取两三个函数、合并一个工具模块）可以当普通条目；更大的按 SKILL「边界」那条，单独立一条 `decision=external` 并建议转 `/engineering`。

## `issue-scan-decides-for-the-owner` · 扫描阶段代机主判定 decision

**症状**：拿不准的条目一律标成"要机主定"，回来一看二十条 external。

**纠正**：只报事实——读到了什么、缺哪一课。判 `autofix` 还是 `external` 不归你（你不写 `decision`），那是 `/pf-issue-discuss` 的活；把读不懂的东西说成"要拍板"，是把你的功课推给机主。
