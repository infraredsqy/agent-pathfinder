# conduct 工具开发纪律

> 本文件（AGENTS.md / CLAUDE.md）不得超过 100 行。

## 布局

- 三个工具同一条规则：`<工具>/` 顶上是入口脚本，旁边的子目录是它背后的机器，`tests/` 是测试。
  conduct 根上因此是四个入口（`state-manage.py` / `conduct_pipeline.py` / `handoff.py` / `signal.py`）
  与四个机器目录：`state/`（写侧：cycle 簿、常备指令、设置、init）、`pipeline/`（推进与节点收口）、
  `spawn/`（开会话的机器链）、`ledger_merge/`（合并驱动的正文）。
- `ledger-merge.py` 必须留在 conduct 根：`pf` 的 `merge-ledger` 转发按这个相对路径找它。项目里的登记走
  `pf register`，登记的是 `pf merge-ledger %O %A %B %P`——与 checkout 位置无关，搬完不用重登记；
  `pf` 不在 PATH 上时先落绝对形式，补好 PATH 重跑一次 `pf register` 换回来。
- 入口互相引用走 HERE 相对路径，搬文件必须同簇搬并同步这些引用；改完跑 `python -m unittest discover -s conduct/tests -t .`（cwd 在 `tools/`）。

## 测试的写法

- 每个用例一个临时项目，按 CPU 核数并行跑 —— 这些用例的时间几乎全花在等子进程上。
- **能进程内调用就进程内调用**：`conduct_pipeline.py` 用 importlib 按路径加载后直接 `apply()` / `record()`，
  只有「被测对象就是 CLI」的用例才真调 `state-manage.py`；DAG 与 `state.json` 的骨架直接用写文件造。
- 判红的口径落在单条用例上（≤5 秒，见根 `AGENTS.md` 的时间纪律）；全量套件的实际耗时照常报出来。

## 派发通路是实弹

- 这套工具的 `--spawn` / `dispatch` 会经 `conduct_spawn.py` → spawner 脚本（DSH 上
  `dsh-spawn.ps1`，ZCode 上 `zcode-spawn.ps1`）开出**真实的会话**并烧 token；ZCode 那一份还会往
  用户装机的 `~/.zcode/v2/tasks-index.sqlite` 写行。测试、调试、跑样例一律不经过它。
- 验证推进逻辑，到「返回 action + prompt 文本落盘」为止。要断言派发语义，用 fake spawner
  （先例：`tests/cases_cli.py` 的 `TestOneSpawner`）。
- 验证机制改动一律在**一次性沙箱项目**里做：把图的记录拷进 temp 下的 git 仓库（或直接造一份假图），
  配 fake spawner 驱动；真实项目上只许只读查询（`cycle list` / `cycle current` / `--plan`），不许开 cycle、
  不许 `dispatch` / `--spawn`。
- 新增测试不许把 `--spawn` 拼进命令串；临时项目用 `tempfile` 沙箱，跑完清掉。ZCode 侧的沙箱用
  `ZCODE_DATA_BASE_DIR` 指向临时目录（先例：`tests/cases_zcode.py`）。
