# agent-pathfinder

[English](README.md) | 中文

`agent-pathfinder`（`pf`）由一条命令与一条执行管线构成：前者维护项目的待办依赖图，后者按节点将工作逐个交付给一个 agent 会话。

想法以节点形式记录在一张有向无环图（DAG）中：节点代表一项待办，边代表依赖关系。一条 **cycle**（执行线）选取当前优先级最高的节点，交由一个新开的会话处理，并记录其返回的结果。会话在 `pf-conduct` 技能下完成准备、执行、独立复审、修复与收尾各阶段，最后提交一行结论，说明是否完成交付。

管线所掌握的全部状态均存放于 `.pf/` 目录。

## 项目状态

本项目仍处于快速迭代期，可能包含不兼容的变更，文件格式亦可能随之调整。

`pf next --spawn` 与 `pf dispatch` 会启动真实的 agent 会话，并产生实际的 token 消耗。

## 安装

环境要求：Python 3.10 及以上、git，以及 Windows 平台上的 PowerShell。在本仓库内执行：

```pwsh
git clone https://github.com/infraredsqy/agent-pathfinder/
cd agent-pathfinder
pwsh -File install.ps1
```

该脚本将 `pf-*` 技能链接至 `~/.agents/skills`，把 `tools/bin` 加入 `PATH`（同时摘掉其它 checkout 的那一条，`pf` 只解析到本仓库）。仓库位置变更后重新执行即可，脚本具备幂等性，收尾自检里包含一次 `pf doctor`。

技能分英文与中文两套，一次只装一套（一个方法名只有一份卡）：缺省装英文，`-Lang zh` 装中文那一套。选择记在 `~/.pf/lang.txt`，之后重跑修复不会把语言换掉。

```pwsh
pf version     # 当前 checkout 的位置
pf doctor      # 检查本仓库与当前项目的状态
```

## 使用

在目标项目的根目录下执行：

```pwsh
pf init                                        # 创建 .pf/，并登记合并驱动
pf dag add "billing page" --tags coding
pf dag add "rate-limit the API" --depends-on <uuid>
pf dag list                                    # 按 urgency × importance 排序
```

随后创建一条执行线，并由其启动首个会话：

```pwsh
pf cycle start billing --title "billing" --tags coding
pf next --spawn
```

`pf next` 是唯一入口：直接执行将推进至下一个动作；`--plan` 给出同样的结果，但不写入任何内容。

## 技能

执行会话由以下技能驱动，均链接至 `~/.agents/skills`：

| 技能 | 职责 |
|---|---|
| `pf-route` | 将需求梳理为节点；维护与查询依赖图 |
| `pf-conduct` | 完成单个节点：准备、执行、独立复审、修复、收尾 |
| `pf-instruct` | 记录一条约束，供后续会话遵循 |
| `pf-issue-scan` | 扫描代码中尚未入库的技术债 |
| `pf-issue-discuss` | 与人工逐批确认 issue 池中的条目 |
| `pf-issue-repair` | 将 issue 池的加权和修复至阈值以下 |
| `pf-test-audit` | 审计测试套件：没验真东西的删掉，拖慢快档的挪出去或拆开 |

## 开发

各工具的套件都在 `tools/` 下跑（在 `tools/` 根上 discover 一个用例都跑不到）：

```pwsh
cd tools
python -m unittest discover -s dag/tests -t .
python -m unittest discover -s issue/tests -t .
python -m unittest discover -s shared/tests -t .
python -m unittest discover -s conduct/tests -t .
```

契约文档位于 `docs/`（中文）。

## 许可证

本项目采用 [MIT](LICENSE) 许可证。