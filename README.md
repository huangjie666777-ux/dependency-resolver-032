# Offline dependency resolver

离线软件依赖解析 HTTP 服务：根据组件目录、根需求与可选锁定清单，
为每个需要的包选出唯一可共存的版本。Python 3.10 + FastAPI 0.115。

## 运行

```bash
.venv/bin/python -m app            # 默认随机空闲端口
PORT=8321 .venv/bin/python -m app  # 指定端口
```

## 接口

`POST /resolve`，请求体：

```json
{
  "catalog": [
    {"name": "web", "versions": [
      {"version": "2.0.0", "dependencies": [{"name": "db", "range": ">=2.0.0,<3.0.0"}]}
    ]}
  ],
  "requirements": [{"name": "web", "range": ">=1.0.0"}],
  "locks": [{"name": "db", "version": "2.0.0"}]
}
```

- 版本为点分三段非负整数（`1.2.3`），按数值比较（`1.10.0 > 1.9.0`）。
- 范围支持精确版本、`>=`、`>`、`<=`、`<`，逗号连接表示同时满足。
- 规模上限：15 个包、每包 8 个版本。
- `locks` 只约束实际被需要的包，不会额外安装无关包。

### 响应

- `200` `{"status": "resolved", ...}`：返回 `resolved` 版本清单、实际依赖边
  `edges`，以及每个包受到的根/依赖/锁定约束和选中原因 `reason`。
  求解确定性：同一目录与需求，输入数组调序结果与输出顺序不变。
- `409` `{"status": "unsatisfiable", ...}`：返回根需求与锁定项中的最小不可满足
  子集（移除其中任一项即可解）、冲突包与关联依赖链；不返回半套清单。
- `422` `{"status": "invalid", "errors": [...]}`：非法输入（重复包名/版本、
  未知引用、非法范围等），每个错误都指出具体字段路径。

## 示例

```bash
curl -s -X POST http://127.0.0.1:8321/resolve \
  -H 'Content-Type: application/json' -d @examples/backtrack.json
```

- `examples/backtrack.json`：局部最高版本不可行时回退（db 2.1.0 → 2.0.0）。
- `examples/cycle.json`：约束兼容的依赖环（alpha ↔ beta）。
- `examples/lock_conflict.json`：锁定与依赖范围冲突，返回最小不可满足子集。

## 测试

```bash
.venv/bin/python -m pytest
```

## 恢复依赖

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```
