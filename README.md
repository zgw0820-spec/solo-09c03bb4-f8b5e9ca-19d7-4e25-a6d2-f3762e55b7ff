# Kubernetes 本地盘上线预检

上线前把「本地 PV 够不够、会不会争用同一块盘」算清楚：导入 PV / Pod 清单 JSON，
网页给出最多同时能启动几个 Pod、每个 Pod 落在哪个节点、每项 PVC 拿到哪块 PV，
以及排不上的 Pod 卡在什么地方。

## 规则

- PV 必须与 PVC 的 `storage_class` 相同，且 `capacity_gi` ≥ 声明容量。
- 每块 PV（每块本地盘）只能分配给一项 PVC。
- 同一个 Pod 的所有卷必须落在同一个候选节点。
- 目标：同时启动的 Pod 数量最多（不是「尽量塞满盘」）。
- 输入上限：16 块 PV、6 个待启动 Pod。

## 运行

```bash
docker compose up --build
```

打开 <http://localhost:8000>，页面会自动载入「争用样例」，直接点「开始试算」即可。
样例里 6 个 Pod 争抢 node-a / node-b 上的三块 fast 盘，结果是 5 个能启动、
`redis-0` 因争用同一块盘被标为未排上。

停止：

```bash
docker compose down
```

## 清单格式

```json
{
  "persistent_volumes": [
    {"name": "pv-a-fast-100", "node": "node-a",
     "storage_class": "local-fast", "capacity_gi": 100}
  ],
  "pods": [
    {"name": "postgres-0",
     "candidate_nodes": ["node-a", "node-b"],
     "claims": [
       {"name": "pgdata", "storage_class": "local-fast", "capacity_gi": 100},
       {"name": "pgwal",  "storage_class": "local-fast", "capacity_gi": 50}
     ]}
  ]
}
```

`capacity_gi` 接受整数（单位 Gi）或 `"100Gi"` 形式。未知字段会被明确报错，
避免拼写错误被静默忽略。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 预检页面 |
| GET | `/healthz` | 健康检查 |
| GET | `/api/sample` | 内置争用样例 |
| GET | `/api/samples` | 样例列表 |
| POST | `/api/plan` | `{"json_text": "..."}`，页面粘贴用 |
| POST | `/api/plan/upload` | multipart 字段 `file`，上限 1 MB |
| POST | `/api/plan/json` | 直接提交清单对象 |

校验失败返回 422，`detail` 是逐条中文错误（带 JSON 路径）。

## 本地测试

```bash
python3 -m unittest discover -s tests -v
```

测试包含：约束单测、输入校验、HTTP 接口，以及在一批随机小规模输入上
与独立暴力枚举（`tests/bruteforce.py`）对拍，校验「最优数量」与「解合法性」。

## 结构

```
app/solver.py            校验 + 求解（纯标准库）
app/main.py              FastAPI 路由
app/static/              页面（原生 HTML/CSS/JS，无构建步骤）
app/samples/             可直接试算的样例
tests/                   单测 + 暴力对拍 + 接口测试
Dockerfile / docker-compose.yml
```

求解思路：逐 Pod 选择「不启动 / 放在某个候选节点」，节点内用二分图增广路
判断多卷能否共用该节点的 PV（不是贪心先到先得，能处理「先占了大盘导致漏解」）；
外层做分支定界，按 Pod 数量取最优。
