# Offline dependency resolver

Python3.10和FastAPI的基础项目，尚未实现依赖解析业务。

项目虚拟环境已准备。运行服务：

```bash
.venv/bin/python -m app
```

默认由系统选择空闲端口，实际地址见Uvicorn启动日志。可用PORT指定端口。

运行测试：

```bash
.venv/bin/python -m pytest
```

恢复依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```
