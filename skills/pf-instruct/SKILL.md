---
name: pf-instruct
description: 往项目指令账里记一条约束，让以后干活的 agent 都照办（pf 会话内用）
disable-model-invocation: true
---

# pf-instruct

只在 pf-route / pf-conduct 的会话里有意义——只有它们读指令账；别处敲了就是敲错地方，回一句，不记账。

把调用行里那句话**原话照抄**进账，`--scope` 必须点名，它决定谁能看见这条：

```pwsh
# global：往后每个读账的会话都看见，一直生效
pf instruction add --text "<用户原话>" --scope global

# cycle：这个 cycle 里的会话看见，cycle 结束就没
pf instruction add --text "<用户原话>" --scope cycle --cycle <id>

# node：只管记它的那个节点，节点收尾就没
pf instruction add --text "<用户原话>" --scope node --node <id>
```

- 两条线并排时 `--cycle <id>` / `--node <id>` 要点名，绑不上工具会拒收
- 干活的一棒开工时读一次账：你中途记的，已经在跑的那棒看不见，下一棒起生效
- 「跑多远、何时停」的话（跑够三个就停）→ `--stop-after-nodes 3` 加 `--target boundary`：停机逻辑只读 boundary 栏的条目，漏了它工具不会停
- 记完把条目 id 和 scope 念给用户
