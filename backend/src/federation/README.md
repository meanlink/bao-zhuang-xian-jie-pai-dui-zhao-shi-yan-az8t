# 联合试验（联邦汇总）子系统

两地车间**不外传原始节拍**，仍可按原有 Welch 口径联合比较工艺。原始片段只存在
各站点本地库；站点之间、站点与中心之间只交换三个充分统计量：

- 件数 `n`
- 均值 `mean`
- 平方离差和 `ss`（Σ(x−mean)²）

中心把各站同组的 `(n, mean, ss)` 用平行合并公式拼成合并样本，再执行与
`src/services/business.py` 完全相同的 Welch 检验（改善量 A−B、
Welch–Satterthwaite 自由度、双侧 t、t 分布区间、Cohen d）。
t 分布由 `tdist.py` 用标准库（不完全贝塔函数）实现，不依赖 scipy。

## 隐私边界

- 站点本地库 `fragments` 表保存原始片段，**永不进入上报报文**。
- 中心库只有试验名册、各站样本版本与 A/B 两组的 `n/mean/ss`；
  任何表都没有 `seconds` 列，无法还原单件节拍。
- 中心简报页面与 API 响应只出现汇总件数/均值/标准差，不出现原值。

## 业务规则与对应实现

| 需求 | 实现 |
|---|---|
| 固定站点名单与各站样本版本 | `Store.create_trial` 落库后不可变 |
| 只交换件数、均值、平方离差和 | `statistics.local_summary`（Welford） |
| 按合并样本做原有比较 | `welch.analyze_payloads`，口径同普通比较 |
| 站点离线可补交 | `station.StationStore` 发件箱 outbox，失败留 `pending` |
| 重试不重复合计 | 规范化 `content_hash`，同站同版同值判 `duplicate` |
| 同站同版异值拒收 | `Store.accept_submission` 记冲突并保留原汇总 |
| 旧试验迟到数据不混入新试验 | 按 `trial_id` 隔离；封算后迟到一律拒收留痕 |
| 全部站点到齐才可封算 | `Store.seal_if_complete` 缺站返回未到齐 |
| 接收与封算并发结果一致 | 单库 `BEGIN IMMEDIATE` 串行化，封算事务内复核并快照 |
| 中断后恢复原进度 | SQLite/WAL 持久化，重开即恢复；重复封算返回同一快照 |
| 页面展示待收集/缺站/冲突/已封算及版本 | `dashboard.py` 简报页 |
| 普通比较保留 | 未改动原有 Dataset/Job/Report 流程，联邦为独立子系统 |

## 运行（无第三方依赖，纯标准库）

```bash
# 1) 启动中心（简报页 http://127.0.0.1:19303/）
python -m src.federation.cli center --db /tmp/fed/center.db --port 19303

# 2) 生成两站本地原始片段与演示试验（原值只落站点库）
python -m src.federation.cli demo --workdir /tmp/fed --url http://127.0.0.1:19303

# 3) 站点联网后打开发件箱补交（离线时自动排队，重试幂等）
python -m src.federation.cli station-flush --db /tmp/fed/station-YC-NORTH.db --url http://127.0.0.1:19303
python -m src.federation.cli station-flush --db /tmp/fed/station-YC-SOUTH.db --url http://127.0.0.1:19303

# 4) 全齐后封算
python -m src.federation.cli seal --url http://127.0.0.1:19303 --trial 1
```

## 两种部署方式

- **独立 stdlib 服务**：`cli center`（`federation/app.py`），无需 FastAPI/MySQL/Redis。
- **内嵌主应用**：`federation/fastapi_router.py` 挂到 `/federation`，复用现有鉴权；
  docker-compose 的 `federation-center` 服务与 nginx 的 `/federation/` 代理即为此路径。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/federation/trials` | 建试验，固定名册与版本 |
| GET | `/api/federation/trials` | 试验列表与进度 |
| GET | `/api/federation/trials/{id}` | 待收集/缺站/冲突/版本/封算结果 |
| POST | `/api/federation/submissions` | 上报 `n/mean/ss`（幂等、冲突/版本/封算拒收） |
| POST | `/api/federation/trials/{id}/seal` | 全齐封算并固化结果与所用汇总 |

## 测试

```bash
python -m unittest src.federation.tests.test_statistics \
                   src.federation.tests.test_service \
                   src.federation.tests.test_end_to_end
```

覆盖：合并与集中样本等价、t 分布教材临界值（有 scipy 时另做交叉验证）、
幂等重试、同站同版异值拒收、版本/非名册/封算后拒收、缺站拒封、
接收与封算并发一致性、离线补交、跨试验隔离、中断恢复、端到端无原值泄露。
