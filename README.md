# DNS SOA 序列号推进审计 API

基于 **Python + FastAPI**，按 [RFC 1982](https://www.rfc-editor.org/rfc/rfc1982) 序列号环形算术，
对多个 DNS 副本当前持有的 32 位无符号 SOA 序列号与拟发布值进行推进审计，并推导最优发布候选。

## 一条命令启动（Docker Compose）

```bash
docker compose up --build -d
```

服务地址（端口 **8000**）：

- 健康检查：<http://localhost:8000/health>
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>

停止服务：

```bash
docker compose down
```

> 无 Docker 时本地运行：`pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000`

## RFC 1982 判定规则

序列号空间为模 2³² 的环。定义前向模距离：

```
distance = (proposed - current) mod 2^32
```

| 距离 | 关系 `relation` | 含义 |
|---|---|---|
| `0` | `equal` | 相等 |
| `1 ≤ distance < 2^31` | `newer` | 严格更新、可比较 |
| `distance = 2^31` | **`ambiguous`** | 正对点，RFC 1982 明确规定**不可比较** |
| `2^31 < distance < 2^32` | `older` | 回退（等价于 proposed 落后 current） |

`relation` 不是 `newer` 的副本都会列入 `blocking_replicas`（阻塞副本）。

## API

服务地址：<http://localhost:8000>（Swagger 文档：<http://localhost:8000/docs>）。
以下示例均可在 `docker compose up --build -d` 一键启动后直接运行。

### `POST /audit` — 审计拟发布值

请求：

```json
{
  "proposed": 2024092101,
  "replicas": [
    {"name": "ns-a", "current": 2024092100},
    {"name": "ns-b", "current": 2024092050},
    {"name": "ns-c", "current": 2024092102}
  ]
}
```

响应（每个副本逐一给出关系、前向模距离、是否阻塞；并汇总阻塞副本名）：

```json
{
  "proposed": 2024092101,
  "results": [
    {"name": "ns-a", "current": 2024092100, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 1, "blocking": false},
    {"name": "ns-b", "current": 2024092050, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 51, "blocking": false},
    {"name": "ns-c", "current": 2024092102, "proposed": 2024092101,
     "relation": "older", "forward_distance": 4294967295, "blocking": true}
  ],
  "blocked": true,
  "blocking_replicas": ["ns-c"]
}
```

正对点示例（差值正好 2³¹，必为 `ambiguous`）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 2147483648,
  "replicas": [{"name": "ns-a", "current": 0}]
}'
# relation: "ambiguous", forward_distance: 2147483648, blocking: true
```

回绕示例（`4294967295 -> 0` 是合法的严格推进）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 0,
  "replicas": [{"name": "ns-a", "current": 4294967295}]
}'
# relation: "newer", forward_distance: 1
```

### `POST /propose` — 推导最优候选

在环形空间中寻找一个对**所有副本**都严格更新且可比较（对每个副本均为 `newer`）的候选值，
最小化 **最大前向模距离** `max_i (proposed - current_i) mod 2^32`；
若多个候选同分，取**数值最小**者。

```bash
curl -s http://localhost:8000/propose -H 'Content-Type: application/json' -d '{
  "replicas": [
    {"name": "ns-a", "current": 100},
    {"name": "ns-b", "current": 150}
  ]
}'
```

```json
{
  "proposed": 151,
  "max_forward_distance": 51,
  "results": [
    {"name": "ns-a", "current": 100, "relation": "newer", "forward_distance": 51},
    {"name": "ns-b", "current": 150, "relation": "newer", "forward_distance": 1}
  ]
}
```

当各副本当前序列号跨越半环、不存在任何单一值能同时严格推进时（例如存在相差正好 2³¹ 的正对点，
或最宽跨度达到 2³¹），返回 **409 Conflict**：

```json
{
  "detail": {
    "error": "no_valid_proposal",
    "message": "不存在能对所有副本都严格更新且可比较的候选值：副本当前序列号跨越半环（含相差 2^31 的正对点）"
  }
}
```

### `POST /transfer-plan` — 规划 IXFR/AXFR 传输计划

接收**目标 SOA 序列号**、各副本当前值与一组**无序 IXFR 增量**
（`from_serial`、`to_serial`）。先做增量集合校验，再为每个副本沿增量链追到目标。

**增量集合校验（422，任一非法即整体拒绝）：**

- 非严格更新的增量：`to_serial` 相对 `from_serial` 不是 RFC 1982 意义上的
  `newer`（即 `equal` / `older` / `ambiguous`，包含前向距离恰好 2³¹ 的正对点）；
- 分叉：同一 `from_serial` 指向不同 `to_serial`；
- 完全相同的重复增量自动去重，不报错。

**链路规划（逐副本，`results` 保持副本输入顺序）：**

| `mode` | 条件 | `ordered_deltas` | `reason` |
|---|---|---|---|
| `up_to_date` | 当前值已等于目标 | `[]` | `null` |
| `ixfr` | 存在唯一完整链（分叉已拒绝，故链唯一） | 按应用顺序排列的增量链 | `null` |
| `axfr` | 遇到缺口 / 环路 / 与目标不可比较，回退全量传输 | `[]` | 见下 |

`reason` 取值：

- `chain_gap`：缺口——链头找不到以它为 `from_serial` 的增量，无法到达目标；
- `cycle_detected`：环路——链重复经过某个序列号（每跳都严格更新也可能绕环一周成环）；
- `target_not_comparable`：目标相对当前值为回退（`older`）或正对点（`ambiguous`），
  沿严格更新的增量链不可能追到目标。

链路匹配只按 `from_serial` 精确衔接下一跳，**遵循 RFC 1982 环形顺序，不按整数大小排序**
（例如 `4294967295 -> 0` 是合法增量，而 `0 -> 4294967295` 是回退，必须拒绝）。

请求（注意增量是乱序给的，还故意混入重复项）：

```bash
curl -s http://localhost:8000/transfer-plan -H 'Content-Type: application/json' -d '{
  "target": 105,
  "replicas": [
    {"name": "ns-a", "current": 100},
    {"name": "ns-b", "current": 105},
    {"name": "ns-c", "current": 50},
    {"name": "ns-d", "current": 200}
  ],
  "deltas": [
    {"from_serial": 103, "to_serial": 105},
    {"from_serial": 100, "to_serial": 101},
    {"from_serial": 101, "to_serial": 103},
    {"from_serial": 100, "to_serial": 101}
  ]
}'
```

```json
{
  "target": 105,
  "results": [
    {
      "name": "ns-a",
      "current": 100,
      "mode": "ixfr",
      "ordered_deltas": [
        {"from_serial": 100, "to_serial": 101},
        {"from_serial": 101, "to_serial": 103},
        {"from_serial": 103, "to_serial": 105}
      ],
      "reason": null
    },
    {
      "name": "ns-b",
      "current": 105,
      "mode": "up_to_date",
      "ordered_deltas": [],
      "reason": null
    },
    {
      "name": "ns-c",
      "current": 50,
      "mode": "axfr",
      "ordered_deltas": [],
      "reason": "chain_gap"
    },
    {
      "name": "ns-d",
      "current": 200,
      "mode": "axfr",
      "ordered_deltas": [],
      "reason": "target_not_comparable"
    }
  ]
}
```

序列号回绕的增量链同样支持（`4294967295 -> 0` 是严格推进）：

```bash
curl -s http://localhost:8000/transfer-plan -H 'Content-Type: application/json' -d '{
  "target": 1,
  "replicas": [{"name": "ns-a", "current": 4294967294}],
  "deltas": [
    {"from_serial": 4294967295, "to_serial": 0},
    {"from_serial": 4294967294, "to_serial": 4294967295},
    {"from_serial": 0, "to_serial": 1}
  ]
}'
# mode: "ixfr"
# ordered_deltas: [4294967294->4294967295, 4294967295->0, 0->1]
```

非法增量集合返回 **422 Unprocessable Entity**：

```json
{"detail": {"error": "delta_not_strictly_newer",
            "from_serial": 0, "to_serial": 4294967295,
            "relation": "older", "forward_distance": 4294967295}}
```

```json
{"detail": {"error": "conflicting_deltas",
            "from_serial": 1, "to_serials": [2, 3]}}
```

### 输入校验（422）

以下情况返回 **422 Unprocessable Entity**：

- 序列号越界：必须为整数且在 `[0, 4294967295]`（即 `[0, 2^32-1]`）；
- 副本名重复（`/audit`、`/propose`、`/transfer-plan` 均检查）；
- 副本名为空 / `replicas` 为空数组；
- `/transfer-plan` 专属：增量非严格更新（`delta_not_strictly_newer`）、
  同一 `from_serial` 分叉（`conflicting_deltas`）。

## 算法说明

`/propose` 中，每个当前值 `c` 的合法候选范围是开弧
`{c+1, …, c+2³¹−1}（mod 2³²）`；所有开弧的交集非空时必为一段连续弧，
最优解必位于弧的最小元素，即某个 `(c + 1) mod 2³²`。
因此只需枚举各副本当前值的“紧邻后继”，检查其对所有副本的前向距离均在 `[1, 2³¹−1]`，
再按 `(最大前向距离, 候选数值)` 取最小即可。

`/transfer-plan` 的算法：增量先按 `from_serial` 建索引（重复去重、分叉拒绝，
并按 RFC 1982 前向距离校验每跳严格更新）；随后从副本当前值出发，以
`from_serial` 精确匹配逐跳推进，并记录已访问序列号——等于目标即 `ixfr`，
无下一跳即 `chain_gap`，重复访问即 `cycle_detected`；起步时目标若不在当前值
前半环内（回退或正对点）则直接判 `target_not_comparable`。整段过程不做整数排序。

## 测试

核心环形算术不依赖 FastAPI，可直接用标准库运行（另含小位宽空间对暴力穷举的对照测试）：

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_serial.py` 覆盖 RFC 1982 环形算术与 `/propose` 推导；
`tests/test_transfer.py` 覆盖增量去重/校验、分叉拒绝与逐副本链路规划，
并在 3/4/6 位小空间上与独立参考实现随机 + 穷举对照。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由与异常
│   ├── schemas.py     # 请求/响应模型与 422 校验
│   ├── serial.py      # RFC 1982 环形算术（纯函数，零第三方依赖）
│   └── transfer.py    # IXFR 增量链规划（纯函数，零第三方依赖）
├── tests/test_serial.py
├── tests/test_transfer.py
├── requirements.txt
├── Dockerfile
├── compose.yaml
└── README.md
```
