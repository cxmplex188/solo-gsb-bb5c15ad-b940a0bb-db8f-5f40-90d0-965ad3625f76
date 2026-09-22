# 计量校准溯源 API（Metrological Calibration Traceability API）

基于 **Python FastAPI + SQLite** 构建。实验室提交仪器、校准证书、父子溯源关系及有效区间；
服务按指定时点从仪器向国家基准重建唯一证书链，校验：

- **证书区间连续性** — 同一仪器历史证书首尾相接，不得断档；
- **签名摘要** — 按规范化证书载荷重算摘要并与登记摘要比对（SHA-224/256/384/512）；
- **测量不确定度** — 扩展不确定度沿溯源方向（下级→上级）只能逐级收缩，即
  下级（子）不确定度不得小于上级（父），且单位必须一致；
- **溯源时点有效性** — 每一级父证书与溯源边在查询时点及子证书校准时刻都必须有效。

当存在多条成链候选（重复送检/多渠道溯源）或区间断裂（无有效证书、死路、父证书失效、环）时，
返回带 `code / severity / node / path_index / detail / 中文说明` 的**可解释冲突列表**。

---

## 一、一键启动（Docker Compose）

```bash
docker compose up -d --build
```

服务地址：

| 用途 | 地址 |
| --- | --- |
| API 根（端点索引） | http://localhost:8000/ |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| OpenAPI Schema | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/health |

- SQLite 数据文件持久化在 Docker 卷 `trace-data`（容器内 `/data/traceability.db`）。
- 首次启动且数据库为空时自动写入演示数据（4 个场景，见第六节）。
  不需要演示数据时，将 `docker-compose.yml` 中的 `SEED_DEMO` 改为 `"0"`。
- 修改代码后重建：`docker compose up -d --build`；清空数据：`docker compose down -v`。

仅用 Docker 运行：

```bash
docker build -t metrological-traceability-api .
docker run -p 8000:8000 -e SEED_DEMO=1 -v trace-data:/data metrological-traceability-api
```

## 二、本地开发运行（无 Docker）

```bash
pip install -r requirements.txt
SEED_DEMO=1 TRACE_DB_PATH=./data/traceability.db \
  uvicorn app.main:app --reload --port 8000
pytest -q        # 运行测试（18 个用例）
```

> 所有时间均按 **UTC** 解释，建议使用 `YYYY-MM-DDTHH:MM:SSZ` 格式。

---

## 三、数据模型

```
instruments（仪器）
    code (PK)、name、manufacturer、model、serial_no、is_national_standard
        │ 1
        │ n
certificates（校准证书）
    id (PK)、instrument_code(FK)、cert_number
    issued_at（校准/签发时刻）、valid_from、valid_until（有效区间，闭区间）
    uncertainty、unit（扩展测量不确定度及单位）
    digest_alg、signature_digest、signed_payload（签名摘要与被签载荷）
        │ 1                    │ 1
        │ n                    │ n
trace_edges（父子溯源边）
    child_cert_id(FK) → parent_cert_id(FK)
    valid_from、valid_until（边本身的有效区间，缺省继承子证书区间）、note
```

- 国家基准：`instruments.is_national_standard = true` 的仪器所持证书是溯源根节点，
  根节点不再向上溯源。
- 同一子证书可挂多条父边，因此同一时点可能枚举出多条候选链。

---

## 四、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/submissions` | 批量提交（upsert）：仪器 + 证书 + 溯源边 |
| POST | `/api/instruments` | 单个仪器 upsert |
| GET | `/api/instruments` · `/api/instruments/{code}` | 仪器列表/详情 |
| POST | `/api/certificates` | 单个证书 upsert（未给摘要时服务端重算） |
| GET | `/api/certificates?instrument_code=` | 证书列表 |
| POST | `/api/edges` | 溯源边 upsert（禁止自环、校验区间） |
| GET | `/api/edges` | 溯源边列表 |
| **GET** | **`/api/trace/{instrument}?at=`** | **链路查询：重建唯一证书链** |
| **GET** | **`/api/reports/{instrument}?at=`** | **校验报告（冲突明细）** |
| **GET** | **`/api/export/{instrument}?at=`** | **JSON 导出（attachment 下载）** |
| **POST** | **`/api/assessments/certificate-deactivation`** | **证书停用影响评估（dry-run，不写库）** |
| POST | `/api/admin/reset` | 清空全部数据（演示/测试用） |

### 1）批量提交

```bash
curl -X POST http://localhost:8000/api/submissions \
  -H 'Content-Type: application/json' -d '{
  "instruments": [
    {"code": "G",  "name": "国家长度基准", "is_national_standard": true},
    {"code": "R",  "name": "一等标准量块"},
    {"code": "W",  "name": "数显卡尺"}
  ],
  "certificates": [
    {"id": "CG", "instrument_code": "G", "cert_number": "NIM-001",
     "issued_at": "2024-01-02T00:00:00Z",
     "valid_from": "2024-01-01T00:00:00Z", "valid_until": "2029-01-01T00:00:00Z",
     "uncertainty": 0.000001, "unit": "mm"},
    {"id": "CR", "instrument_code": "R", "cert_number": "STD-016",
     "issued_at": "2025-06-05T00:00:00Z",
     "valid_from": "2025-06-01T00:00:00Z", "valid_until": "2026-12-31T00:00:00Z",
     "uncertainty": 0.00005, "unit": "mm"},
    {"id": "CW", "instrument_code": "W", "cert_number": "CAL-101",
     "issued_at": "2026-01-10T00:00:00Z",
     "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
     "uncertainty": 0.002, "unit": "mm"}
  ],
  "edges": [
    {"child_cert_id": "CW", "parent_cert_id": "CR", "note": "量块传递"},
    {"child_cert_id": "CR", "parent_cert_id": "CG", "note": "基准送检"}
  ]
}'
```

`signature_digest` 与 `signed_payload` 均可留空：服务端按固定字段集生成
规范化 JSON（键排序、无空白、UTF-8）并计算 SHA-256 摘要，日后逐字节可复核。

### 2）链路查询 / 3）校验报告 / 4）JSON 导出

```bash
curl "http://localhost:8000/api/trace/W?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/W?at=2026-06-15T00:00:00Z"
curl -OJ "http://localhost:8000/api/export/W?at=2026-06-15T00:00:00Z"
```

报告关键字段：

```jsonc
{
  "instrument": "W",
  "at": "2026-06-15T00:00:00Z",
  "resolution": "unique",          // unique=唯一成链 | ambiguous=多候选 | broken=断裂
  "valid": true,                   // 唯一成链且无 ERROR 级问题
  "starts": ["CW"],                // 该时点有效的起点证书
  "active_chain": {                // 叶子→根的有序节点与边
    "nodes": [ {"certificate_id": "CW", ...}, {"...":"CR"}, {"...":"CG（国家基准）"} ],
    "edges": [ {"child_cert_id": "CW", "parent_cert_id": "CR", ...} ],
    "reaches_national_standard": true
  },
  "candidates": [],                // ambiguous 时：>=2 条有效候选链
  "broken_chains": [],             // 未到基准的部分链（死路/断档/环）
  "conflicts": [ /* 可解释冲突，见下表 */ ],
  "error_count": 0, "warning_count": 0,
  "statistics": {"paths_enumerated": 1, "root_reaching_chains": 1, ...}
}
```

### 冲突（conflicts）编码

| code | 级别 | 含义 |
| --- | --- | --- |
| `NO_ACTIVE_CERTIFICATE` | ERROR | 查询时点仪器无有效证书，链无法开始 |
| `MULTIPLE_ACTIVE_CERTIFICATES` | ERROR | 同一时点多张证书有效，起点不唯一 |
| `INTERVAL_GAP` | ERROR | 仪器历史证书区间断档（附前后证书与缺口） |
| `DEAD_END` | ERROR | 非基准证书无有效向上溯源边（附节点） |
| `PARENT_CERT_NOT_ACTIVE` | ERROR | 父证书在查询时点失效/未生效，链断裂 |
| `CYCLE_DETECTED` | ERROR | 溯源边构成环（附重复节点、边 id） |
| `SIGNATURE_DIGEST_MISMATCH` | ERROR | 摘要重算与登记值不符（附两者值） |
| `PARENT_INTERVAL_NOT_COVER_CALIBRATION` | ERROR | 父证书区间未覆盖子证书校准时刻 |
| `UNCERTAINTY_UNIT_MISMATCH` | ERROR | 父子不确定度单位不一致，无法比较 |
| `UNCERTAINTY_REGRESSION` | ERROR | 下级不确定度小于上级（沿链收缩） |
| `MULTIPLE_VALID_CHAINS` | ERROR | 多条候选链均到基准且通过校验，链不唯一 |
| `ALL_CHAINS_FAILED_CHECKS` | ERROR | 有到基准的链但全部未通过校验 |
| `CHAIN_BROKEN` | ERROR | 全部分支在到基准前终止 |
| `UNCERTAINTY_NOT_DECAY` | WARN | 父子不确定度相同，未体现逐级放大 |
| `EDGE_NOT_COVER_CALIBRATION` | WARN | 溯源边区间未覆盖子证书校准时刻 |
| `ROOT_HAS_PARENT` | WARN | 国家基准证书仍存在向上溯源边 |
| `CANDIDATE_CHAIN_FAILED_CHECKS` / `ALTERNATIVE_BRANCH_PRESENT` | WARN | 唯一链之外的其他分支提示 |

每个冲突都带 `node`（证书 id）、`path_index`（所属候选链下标，节点级为 null）
与 `detail`（结构化证据），便于定位到具体证书和证据数值。

### 5）证书停用影响评估（dry-run 模拟）

证书因吊销/过期/发现问题需要停用时，先评估影响范围再操作。提交**查询时点**与
**待停用证书 id 列表**，服务端在内存中模拟移除这些证书及其全部关联溯源边
（`child` 或 `parent` 命中即移除），**不写入数据库**（响应恒带 `dry_run: true`），
重新评估每台受影响仪器的唯一链 / 多候选 / 断裂状态，并给出：

- `before` / `after`：停用前后的完整评估快照（与 `/api/reports` 同构）；
- `transition`：状态迁移（如 `unique_to_broken`、`ambiguous_to_unique`、`unchanged`）；
- `severed_chains`：停用前存在、因途经被停用证书而被切断的链（含被切断的候选链）；
- `alternative_chain` / `alternative_chains`：停用后仍成立的唯一替代链 / 全部候选链摘要；
- `simulation_notes`：可解释说明（`severity=INFO`，非真实数据冲突）。

默认自动覆盖所有受影响仪器（被停用证书所属仪器 + 停用前任一溯源链经过这些证书的
仪器）；也可用 `instrument_codes` 仅筛选指定仪器评估。

```bash
curl -X POST http://localhost:8000/api/assessments/certificate-deactivation \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "certificate_ids": ["CR"],
  "instrument_codes": ["W"]
}'
```

```jsonc
{
  "at": "2026-06-15T00:00:00Z",
  "dry_run": true,                       // 恒为 true：模拟不写库
  "disabled_certificate_ids": ["CR"],
  "instruments_filter": ["W"],           // null 表示自动覆盖全部受影响仪器
  "affected_owner_instruments": ["R"],   // 被停用证书直接所属仪器
  "removed_edge_count": 2,               // 一并模拟移除的溯源边数
  "affected_instrument_count": 1,
  "evaluated_instrument_count": 1,
  "transition_counts": {"unique_to_broken": 1},
  "results": [
    {
      "instrument": "W",
      "impacted": true,
      "transition": "unique_to_broken",
      "before": { "resolution": "unique", "active_chain": { /* ... */ } },
      "after":  { "resolution": "broken", "conflicts": [ /* ... */ ] },
      "alternative_chain": null,         // 停用后无唯一有效替代链
      "alternative_chains": [],
      "severed_chains": [
        { "role": "active",              // active | candidate | broken
          "certificate_ids": ["CW", "CR", "CG"],
          "reaches_national_standard": true,
          "disabled_certificates_on_path": ["CR"],
          "chain": { "nodes": [ /* ... */ ], "edges": [ /* ... */ ] } }
      ],
      "simulation_notes": [
        {"code": "SIMULATED_CHAIN_SEVERED", "severity": "INFO", "message": "..."},
        {"code": "SIMULATED_NO_ALTERNATIVE_CHAIN", "severity": "INFO", "message": "..."}
      ]
    }
  ],
  "statistics": {"certificates_before": 3, "certificates_after": 2,
                 "edges_before": 2, "edges_after": 0}
}
```

> 多候选场景停用一条参考通道后，若另一通道仍成立，`transition` 为
> `ambiguous_to_unique`，替代链出现在 `alternative_chain`。
> 待停用证书 id 不存在或筛选仪器不存在时返回 **422**（中文原因）；
> 重复/带空白的证书 id 会自动去重。

模拟说明（`simulation_notes`，均为 INFO 级）：

| code | 含义 |
| --- | --- |
| `SIMULATED_START_CERTIFICATE_REMOVED` | 仪器在查询时点的有效起点证书被停用 |
| `SIMULATED_CHAIN_SEVERED` | 停用前的链/候选链途经被停用证书，已被切断（附链与命中证书） |
| `SIMULATED_ALTERNATIVE_CHAIN_AVAILABLE` | 停用后仍存在唯一有效替代链（附替代链节点） |
| `SIMULATED_NO_ALTERNATIVE_CHAIN` | 停用后断裂，不存在唯一有效替代链 |
| `SIMULATED_AMBIGUOUS_AFTER_DEACTIVATION` | 停用后仍为多候选，无唯一替代链 |
| `SIMULATED_NO_EFFECT` | 停用证书不在该仪器该时点的溯源路径上，状态不变 |

---

## 六、内置演示场景

演示查询时点统一为 **2026-06-15T00:00:00Z**：

| 仪器 | 结果 | 说明 |
| --- | --- | --- |
| `INST-W1` 数显卡尺 | `unique` / valid | CW1-1 → CR1-1 → CG-1（国家长度基准），全部检查通过 |
| `INST-P1` 精密压力表 | `ambiguous` | 经活塞压力计 A 或数字校准器 B 均能到国家压力基准，2 条有效候选链 |
| `INST-T1` 热电偶 | `broken` | 历史证书 2025 年夏季断档（`INTERVAL_GAP`），且参考标准未再溯源（`DEAD_END`） |
| `INST-B1` 三坐标 | `broken` | 链结构完整，但证书登记摘要被篡改（`SIGNATURE_DIGEST_MISMATCH`） |

```bash
curl "http://localhost:8000/api/reports/INST-W1?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/INST-P1?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/INST-T1?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/INST-B1?at=2026-06-15T00:00:00Z"
```

停用评估示例（dry-run）：停用压力多候选中的活塞压力计通道证书，观察
`INST-P1` 由 `ambiguous` 变为经数字校准器 B 的 `unique` 替代链；停用一等量块
证书 `CR1-1` 则会使 `INST-W1` 由唯一链变为断裂：

```bash
curl -X POST http://localhost:8000/api/assessments/certificate-deactivation \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "certificate_ids": ["CPRA-1"]
}'

curl -X POST http://localhost:8000/api/assessments/certificate-deactivation \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "certificate_ids": ["CR1-1"],
  "instrument_codes": ["INST-W1"]
}'
```

## 七、溯源算法说明

1. 取目标仪器在 `at` 生效（`valid_from <= at <= valid_until`）的证书作为起点；
2. 自起点沿在 `at` 有效的边做 DFS 枚举（同父子证书的并行边合并；检测环；500 条上限保护）；
3. 终止条件：到达国家基准仪器证书（成链）、无有效边（死路）、父证书失效（断档）、成环；
4. 对每条链执行节点级（时点有效性、签名摘要）与链级（父区间覆盖校准时刻、
   不确定度单调不收缩、单位一致、边覆盖校准时刻）检查；对链上全部仪器检查历史区间连续性；
5. 决策：成链且零 ERROR 的路径恰有 1 条 → `unique`；≥2 条 → `ambiguous`；
   0 条 → `broken`。所有 ERROR/WARN 汇总为可解释冲突列表。
6. 证书停用影响评估：在内存 Catalog 中移除证书及其关联边（不写库），对每台受影响
   仪器分别在停用前/后的 Catalog 上重跑步骤 1~5，比较状态与链指纹，输出迁移、
   被切断链、替代链与 INFO 级模拟说明。

## 八、项目结构

```
app/
  main.py            FastAPI 入口、路由注册、启动建库/播种
  database.py        SQLite 连接、表结构
  schemas.py         Pydantic 请求/响应模型
  crud.py            upsert 与写入期校验
  engine.py          溯源链重建（DFS）、校验规则、停用影响模拟（内存 Catalog）
  seed.py            四场景演示数据
  routers/           instruments / certificates / edges /
                     submissions / trace(report + 停用影响评估) / export / admin
tests/test_trace.py        10 个接口级测试
tests/test_deactivation.py 8 个停用影响评估测试
Dockerfile · docker-compose.yml · requirements.txt
```
