# pf 工具使用技巧

> 本文件描述 DAG 管理工具的高效使用方法。与 `philosophy.md`（WHY）互补，本文件聚焦 HOW。

## 根原则

**先 `--help`，再动手。** 调 `pf dag` 之前，先看 `--help` 了解所有命令和选项，不凭感觉或记忆操作。命令全集以它为准：`search` 按标题/描述正则搜、`info` 看详情与直接依赖、`context` 看完整上下游、`list` 按 urgency×importance 排序、`open` 拿 spec.md 路径，增删改与连边各有自己的命令。

## 效率规则

### 合并同类查询

`search` 支持正则表达式，相关关键词用 `|` 一次查完：

```
# 坏：5 次调用
search 合并
search 集成
search 迁移
search 整合
search 主项目

# 好：1 次调用
search '合并|集成|迁移|整合|主项目'
```

同理，`info` / `open` / `context` 虽不能合并，但**一次调用获取足够信息**，避免多次往返。

### 优先定向查询，list 是最后手段

`list` 输出全量节点 JSON，DAG 大时信息冗余且消耗上下文。定向查询始终优先：

- 搜节点 → `search <regex>`
- 看详情 → `info <id>`
- 看拓扑 → `context <id>`

只有需要全图排序（如推荐下一步）时才用 `list`，且加 `--limit N` 控制条数。**不要用 `list | grep` 做定向搜索——那是 `search` 的事。**

### pattern 引号

所有 pattern 参数（search 的 regex、add 的 title/description）用双引号包起来，防止 shell 解析特殊字符。
