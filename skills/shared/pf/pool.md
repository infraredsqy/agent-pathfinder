# 池子（issue 库）

> `pf-issue-scan` / `pf-issue-repair` / `pf-issue-discuss` 三家共用这一份。命令细节以 `--help` 为准，这里只写口径。

池子住在项目的 `.pf/issues/`，是统一的 to-do list。`source` 只说这条是怎么来的：`scan`（扫描入池）、`manual`（机主随手记的）、`conduct`（干活会话撞见的）。

## 只走工具

```pwsh
pf issue <command> --help    # 动手前先看它
pf issue status             # 现状
pf issue top --limit 10     # 按优先级
pf issue search '<正则>'    # 定位；同类词用 | 一次搜完
pf issue weight -v          # 加权和是怎么加出来的
```

- 与池子的**一切**读写只走这个工具。**禁止直接 read `.pf/issues/issues.json`**：巨型 JSON，烧上下文，还绕过工具自己的统计口径。

## 口径

- 状态：`open` / `acknowledged` / `resolving` 三档算 actionable，`resolved` / `rejected` 不算。
- `decision`：`autofix`（能自己修）／`external`（取舍还开在机主手里）／`unknown`（还没判，入库时的默认）。**已经拍过板、解法写在条目里的条目一律 `autofix`**——落在别的节点或别的线上也算。
- **加权和** ＝ P3/P2/P1/P0 = 1/2/4/8，只算 actionable 且 `decision` 不为 `external` 的条目。`unknown` 与 `autofix` 一样压着这条线；判成 `external` 就掉出计数。
- 严重度（写每条时照它判）：**P0** 重复且带 bug 风险，或踩在业务关键路径上；**P1** 职责越界阻碍迭代（UI 里写业务逻辑、逻辑层碰 DOM、跨层依赖）；**P2** 结构改善提升效率（可提取的重复模式、散落的配置、加一个功能要改 3+ 文件）；**P3** 整洁（命名、过时注释、死代码、格式）。
- **大改动单独立条**：≥3 文件、且要改模块间依赖的重构信号，单独立一条，建议转 `/engineering`。池子里只留那条债与它的判据，重构方案不在这里展开。
- **入库前先搜同一条**：新发现进池子之前，先拿同类词搜一遍池子；写库时去重合并——同一条债两份记录，读的人要自己认亲。
