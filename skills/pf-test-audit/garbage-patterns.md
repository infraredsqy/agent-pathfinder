# Garbage Test Patterns

> 此文件供 subagent 扫描测试时用，指挥者不用读。

## 6 种 Garbage Test 模式

| # | 模式 | 语言无关描述 | 典型例子（Catch2 / Jest / pytest） |
|---|------|-------------|-------------------------------|
| P1 | `hardcoded-count` | 断言集合/方法/条目的具体数量值 | `REQUIRE(result.size() == 36)`, `expect(methods.length).toBe(10)` |
| P2 | `trivial-init` | 验证语言/框架保证的默认初始化状态 | `REQUIRE(field == false)`, `assert(field is None)`, `expect(obj.count).toBe(0)` |
| P3 | `field-roundtrip` | 赋值后立即读回来验证 | `x = val; REQUIRE(x == val)`, `obj.name = "foo"; assert obj.name == "foo"` |
| P4 | `enum-bound` | 枚举/常量绑定到具体整数值 | `REQUIRE(static_cast<int>(Enum::A) == 0)`, `assert(Color.RED == 1)` |
| P5 | `container-op` | 测试容器基本增删查操作 | `map.erase(k); REQUIRE(map.size() == 0)`, `arr.append(x); assert len(arr) == 1` |
| P6 | `trivial-error` | 仅测试错误入参返回固定错误值 | `REQUIRE(func(bad) == -1)`, `assert(parse("").is_err())` |

## 判定原则

- 如果整个 TEST_CASE（或同类测试容器）的所有断言都属于以上模式 → **整段删除**（judgment: `entire`）
- 如果部分断言属于以上模式、部分验证业务逻辑 → **只删垃圾断言行**，保留有效部分（judgment: `partial`）
- 如果文件内所有测试都被删光 → **从构建系统（CMakeLists.txt / Cargo.toml / package.json 等）中移除该文件**

## 删除的覆盖门槛

命中垃圾模式只是一半。**删之前还要确认它验的那份业务在别处有覆盖**。

- 两条都成立 → 删
- 命中垃圾模式、但它是这份业务的**唯一**覆盖 → 留下，把这件事写进报告（这条写得难看，然而它是唯一的哨兵）
- 拿不准有没有别处覆盖 → 报告里点出这条业务，由指挥者核；**宁可留着**

要真去搜，别凭印象：同文件的邻居用例、同类模块的测试、端到端用例里的间接覆盖。

## 安全例外

以下情况**不是**垃圾测试，不要误删：
- 验证某字段被显式初始化为非零/非空值（业务有要求且与默认值不同）
- 验证边界条件行为（空集合查询不崩溃、负数索引被 clamp）
- 验证序列化 roundtrip（写入后再读回来检查字段是否一致）
- 验证状态机转换的合法性

## 返回格式

```json
{
  "file": "tests/app/test_example.cpp",
  "findings": [
    {
      "test_name": "Enum values are correct",
      "type": "enum-bound",
      "line_start": 15,
      "line_end": 22,
      "judgment": "entire"
    }
  ]
}
```

- `judgment`：`entire` = 整段测试全是垃圾断言；`partial` = 混合了有用断言，只删垃圾行
