# 断肢急救转诊编排

连接基层接诊机构、显微外科团队、手术台与转运力量的急救编排后端。值班员记录伤情后，
系统按**实时能力、路程、道路天气与剩余缺血窗口**筛选可接收单位；候选机构**明确接受
并短时锁定资源**后才输出转运计划。能力资料过期、资源回报失联、预计到达越窗都会醒目
标记风险并升级人工调度。所有排序与计划均附免责声明——**仅为协调依据，不构成诊断建议**。

## 领域规则

### 缺血窗口

- 常温（WARM）离断：黄金 **6 小时（360 分钟）**，自离断时刻起算；
- 冷藏保存（COLD/COOLED/REFRIGERATED）：按策略 **8 小时（480 分钟）**；
- 预计到达时刻晚于截止时刻即触发 `ETA_BEYOND_ISCHEMIA_WINDOW`（CRITICAL）并升级人工调度；
- 途中里程碑可携带 `remaining_minutes_estimate`，系统按最新剩余车程动态复核窗口。

### 候选筛选（全部满足才进入排序）

1. 机构能力覆盖离断部位与待吻合血管口径，且登记 24 小时显微外科服务；
2. 能力资料在 **240 分钟**新鲜度内（过期 → `CAPABILITY_DATA_STALE` HIGH 风险，拒绝候选）；
3. 最近一次资源回报在 **10 分钟**内（超时 → `RESOURCE_CONTACT_LOST` CRITICAL，拒绝候选）；
4. 有当班、口径与部位匹配且未被占用的显微团队和手术台；
5. 存在路线数据，且道路未阻断（天气/道路事件按系数调整车程）；
6. 调整后的预计到达仍在缺血窗口内。

排序按调整后车程升序，时间相同时到达后缺血余量大者优先；每个候选都带可解释的
`factors`（基础车程、天气系数、道路事件、调整后车程、缺血余量、团队、手术台、资料新鲜度）。

### 邀约、接受与资源租约

- 值班员对候选发起邀约，机构须在 **5 分钟** TTL 内应答，超时自动 EXPIRED；
- **只有机构明确接受（accepted=true）才创建租约**，锁定具体团队与手术台，租约 30 分钟；
- 途中 `DEPARTED/ENROUTE/WAYPOINT` 里程碑自动为租约续期；`ARRIVED` 后租约转为 CONSUMED；
- 租约超时未续 → `RESOURCE_LOCK_LOST` CRITICAL；
- 并发接受时引擎在同一把锁内完成“检查-锁定”，同一团队/手术台绝不会被两例重复占用，
  失败方收到 409 `RESOURCE_BUSY` 且病例出现 `RESOURCE_BUSY_AT_ACCEPTANCE`（HIGH）并升级。

### 风险与人工调度

| 风险类型 | 级别 | 触发 |
|---|---|---|
| `CAPABILITY_DATA_STALE` | HIGH | 能力资料超过新鲜度阈值 |
| `RESOURCE_CONTACT_LOST` | CRITICAL | 资源回报超过失联阈值（邀约/租约关联机构） |
| `ETA_BEYOND_ISCHEMIA_WINDOW` | CRITICAL | 计划或在途 ETA 越窗 |
| `NO_RECEIVING_UNIT` | CRITICAL | 筛查无任何候选（确定接收机构后自动解除） |
| `RESOURCE_LOCK_LOST` | CRITICAL | 租约超时失联 |
| `RESOURCE_BUSY_AT_ACCEPTANCE` | HIGH | 接受瞬间匹配资源被并发占用 |

`GET /escalations` 返回所有未被值班员确认的 HIGH/CRITICAL 风险；`POST .../risks/{id}/ack`
确认后退出升级队列，风险记录仍保留在病例与审计轨迹中。心跳恢复、候选重新出现等情况
会自动解除对应风险。

### 弱网补传与幂等

- 里程碑与改道消息携带 `client_event_id`；同一病例相同键的重复消息返回
  `deduplicated: true`，**不新增状态、不新增版本**；
- 无客户端键时，同病例同里程碑 60 秒内的重复上报也按重传处理；
- 道路事件按 `traffic_event_id` 幂等刷新。

### 改道（版本化，不覆盖原计划）

- 每次改道追加一个计划版本，原目标医院、原路线、决定原因（reason）与决定人
  （decided_by）永久保留；
- 改道到**新机构必须提供该机构已接受邀约的 offer_id**——不允许系统未经机构接受就锁资源；
- 原机构租约释放、邀约置 SUPERSEDED、身份授权收回；新机构按其接受的租约接管；
- 目标不在筛查候选内时须 `override=true` 人工越权，版本中 `manual_override` 留痕；
- 道路阻断且未 override 时拒绝改道。

### 患者身份分阶段开放

| 阶段 | 谁可见 | 可见字段 |
|---|---|---|
| INTAKE | 基层接诊机构 | 完整身份、伤情、生命体征 |
| ACCEPTED | 已接受的接收机构 | 代号、伤情、缺血截止时刻 |
| IN_TRANSIT | 在途接收机构 | 加：年龄、性别、生命体征 |
| RECEIVING | 到院接收机构 | 加：姓名、证件号、联系方式 |

未实际参与救治的机构一律 `visible:false`；改道后原机构权限立即收回，结单后全部收回。

### 事件溯源

所有状态变更追加写入 JSONL 事件日志（`--log`）。重启后重放日志恢复全部状态，
包括历史计划版本、改道理由、里程碑、风险、身份授权与幂等键——断网重连后重放旧消息
仍是幂等的，原计划始终可查（`GET /cases/{id}/audit`）。

## 运行

```bash
python3 service.py --check                 # 基础检查
python3 service.py --port 8000             # 内存态启动（/health）
python3 service.py --port 8000 --log data/events.jsonl   # 持久化、可重启恢复
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/hospitals` | 登记/更新机构能力图（团队、手术台、资料更新时刻） |
| POST | `/hospitals/{id}/heartbeat` | 资源回报（接受邀约也视为一次心跳） |
| PUT | `/routes` | 录入/更新起点→机构的基础车程与天气 |
| POST | `/traffic` | 道路事件（幂等，按 traffic_event_id） |
| POST | `/cases` | 录入病例（部位、离断时间、保存、口径、生命体征、接载点） |
| GET | `/cases` / `/cases/{id}` | 病例查询 |
| POST | `/cases/{id}/vitals` | 追加生命体征 |
| POST | `/cases/{id}/screen` | 实时筛查排序（可传 at 指定时钟） |
| POST | `/cases/{id}/offers` | 对候选发起邀约（`hospital_ids`、`manual`） |
| POST | `/offers/{id}/respond` | 机构应答 `{accepted}`；接受即锁资源 |
| POST | `/cases/{id}/plan` | 凭**已接受** offer 创建转运计划 |
| POST | `/cases/{id}/reroute` | 改道（reason/decided_by 必填；新机构须 offer_id） |
| GET | `/cases/{id}/plan` | 当前计划（含全部版本与每版决定依据） |
| POST | `/cases/{id}/milestones` | 途中里程碑（client_event_id 幂等补传） |
| GET | `/cases/{id}/milestones` | 里程碑列表 |
| GET | `/cases/{id}/identity?hospital_id=` | 分阶段身份视图 |
| GET | `/cases/{id}/explanation` | 推荐依据：筛查因素、计划版本、活动风险 |
| POST | `/cases/{id}/risks/{rid}/ack` | 值班员确认风险 |
| GET | `/escalations` | 待人工处理的升级队列 |
| POST | `/admin/sweep` | 推进时钟清扫（邀约/租约/道路事件/失联对账） |
| GET | `/cases/{id}/audit` | 事件审计轨迹 |
| POST | `/cases/{id}/close` | 结单（释放租约、收回身份授权） |

所有写接口支持在请求体传 `at`（ISO-8601）驱动案例时钟，便于演练与复盘。
错误统一返回 `{"error":{"code","message"}}`，HTTP 400/404/409 区分输入错误、不存在与规则冲突。

## 测试

```bash
npm test          # 基线契约 + 31 项验收测试
```

验收测试（`test_acceptance.py`）直接对应值班员的举证场景：

- **两例并发急诊**（进程内 + 真实 HTTP 两组）：屏障对齐同时接受，证明同一团队/手术台
  不会重复占用，失败方醒目标记 `RESOURCE_BUSY_AT_ACCEPTANCE` 并进入升级队列；
- **能力图 × 交通事件 × 案例时钟**：雷暴系数、道路拥堵改变 ETA 与排序，阻断道路拒绝候选；
- 资料过期、心跳失联、越窗（出发时与在途更新两种）、租约失联的风险升级与解除；
- 弱网三次重发同一里程碑只产生一条记录；断网重启后重放改道/里程碑仍幂等；
- 改道保留 v1 原路线与 v2 原因/决定人，旧机构授权收回、租约释放；
- 身份按 INTAKE/ACCEPTED/IN_TRANSIT/RECEIVING 四阶段开放，未参与者不可见，结单全收回；
- 人工 override 改向非候选机构时版本明确留痕。

## 文件

- `engine.py` — 领域引擎：事件溯源、筛查排序、租约、风险、幂等、身份分阶段（仅标准库）；
- `service.py` — HTTP 入口与 JSON 编解码，保持基线 `/health`、`--check` 契约；
- `service_contract.py` — 基线健康检查契约测试；
- `test_acceptance.py` — 验收场景测试。
