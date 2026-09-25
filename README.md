# Offline dependency resolver

离线软件依赖解析 HTTP 服务（Python 3.10 + FastAPI 0.115）。研发安装前核对内部组件版本能否共存。

## 运行

```bash
.venv/bin/python -m app          # PORT 环境变量指定端口，默认由系统分配
PORT=8123 .venv/bin/python -m app
```

## 接口

`POST /resolve`，请求体：

```json
{
  "catalog": [
    {"name": "web", "versions": [
      {"version": "3.0.0", "dependencies": [{"name": "db", "range": ">=2.0.0"}]}
    ]}
  ],
  "requirements": [{"name": "web", "range": ">=3.0.0"}],
  "locks": [{"name": "db", "version": "2.0.0"}]
}
```

- `catalog`：组件目录，最多 15 个包、每包最多 8 个版本。版本为点分三段非负整数，按数值比较。
- `range`：支持精确版本（`1.2.3` 或 `==1.2.3`）、`>=`、`>`、`<=`、`<`，逗号连接表示同时满足。
- `locks`：可选，限定对应包版本；只约束实际需要的包，不会额外安装无关包。

### 响应

- 成功：`{"status": "resolved", "resolved": [...], "edges": [...]}`。`resolved` 含每个包被选版本、受到的根/依赖/锁定约束及选中原因；`edges` 为实际依赖边。同一目录与需求即使输入数组调序，方案与输出顺序一致（按包名排序、版本从高到低确定性搜索）。
- 无解：`{"status": "unsatisfiable", "minimal_unsatisfiable_subset": [...], "conflicts": [...]}`。子集为根需求与锁定项中的最小不可满足集（移除任一项即可解），`conflicts` 给出冲突包、可用版本及每条约束的来源与依赖链。不返回半套安装清单。
- 非法输入：HTTP 422，`{"status": "invalid_input", "field": "...", "message": "..."}`，拒绝重复包名/版本、未知引用、非法范围与超限目录，并指出具体字段。

## 求解规则

从根需求出发为每个需要的包选择唯一版本，递归满足所选版本的全部依赖；共享依赖同时满足所有来源约束；允许约束兼容的依赖环；局部最高版本不可行时回退到较低版本，保证不遗漏可行解；不安装与根无关的包。

## 示例

```bash
curl -s -X POST http://127.0.0.1:8123/resolve -H 'Content-Type: application/json' \
  -d @examples/success.json | .venv/bin/python -m json.tool
```

- `examples/success.json`：带锁定的成功解析。
- `examples/backtrack.json`：局部最高版本不可行，回退到 `lib 2.0.0`。
- `examples/cycle.json`：约束兼容的依赖环 `a <-> b`。
- `examples/lock-conflict.json`：锁定与根需求冲突，返回最小不可满足子集。

## 测试

```bash
.venv/bin/python -m pytest
```

## 恢复依赖

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

