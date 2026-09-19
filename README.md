# 断肢急救转诊编排

连接基层接诊机构、显微外科团队、手术台与转运力量的急救编排后端。
系统只做**资源协调排序**，不替代医生诊断；每条推荐均附带非诊断声明。

## 救治流程

```
登记伤情（部位/离断时间/保存条件/生命体征）
   → 实时筛选（能力 · 团队与手术台心跳 · 路程与道路环境 · 缺血窗口）
   → 因子级推荐依据 + 醒目风险标记
   → 候选医院明确接受 → 原子锁定团队+手术台（15 分钟短时租约）
   → 转运计划（路线快照不可变）
   → 弱网里程碑补传（幂等）
   → 改道（旧计划、路线快照与决定理由永久保留）
   → 交接（身份完整开放）/ 越窗·失联（升级人工调度）
```

## 关键规则（`domain.py` 顶部可调策略参数）

| 参数 | 值 | 含义 |
| --- | --- | --- |
| `WARM_ISCHEMIA_LIMIT_MIN` | 360 | 常温保存恢复血运时限约 6 小时 |
| `COLD_ISCHEMIA_LIMIT_MIN` | 480 | 规范冷存放宽时限 |
| `CAPABILITY_STALE_AFTER_MIN` | 30 | 能力资料超期即阻断，需人工核实 |
| `RESOURCE_HEARTBEAT_TIMEOUT_MIN` | 5 | 团队/手术台心跳失联阈值 |
| `RESOURCE_LEASE_TTL_MIN` | 15 | 接受后的短时资源租约；出发后自动延至预计到达后 15 分钟 |
| `ARRIVAL_RISK_BUFFER_MIN` | 15 | 到达余量低于该值仅 WARN，越窗为 BLOCK |

风险分级：`block` 不能自动列入计划（资料过期、心跳失联、能力不匹配、无路线、预计越窗、资源被并发占用）；
`warn` 醒目标注但仍可人工判断（余量不足、恶劣天气、交通事件）。
**没有任何可行候选、接受时复算越窗、租约失联到期、途中复算越窗**都会生成人工调度升级单。

## 身份分阶段开放

接诊机构登记即见完整身份；候选医院接受后只见伤情摘要（代号/年龄/性别）；
实际承担转运的 120 分站在出发时见代号与交接信息；**只有实际交接**才向接收方开放完整身份。
改道立即收回原接收方授权。授予与收回全部留痕（`GET /cases/{id}/identity`）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 登记案例（支持 `amputation_minutes_ago`） |
| GET | `/cases` / `/cases/{id}` | 案例查询 |
| GET | `/cases/{id}/clock` | 案例时钟：时限、已流失血、当前余量 |
| POST | `/cases/{id}/recommendations` | 筛选排序，返回因子级依据与风险 |
| POST | `/cases/{id}/accept` | 候选医院显式接受，原子锁定资源并生成计划 |
| POST | `/cases/{id}/milestones` | 里程碑（`client_event_id` 幂等，弱网补传标记 `replay`） |
| POST | `/cases/{id}/reroute` | 改道；重复提交同一决定幂等，旧计划保留 |
| POST | `/cases/{id}/reassess` | 途中按最新路况复算窗口，越窗即升级 |
| GET | `/cases/{id}/plans` | 全部计划版本（含 superseded/cancelled 与理由） |
| GET | `/cases/{id}/identity?hospital_id=X` | 该机构当前可见的身份层级 |
| POST | `/cases/{id}/escalations/{eid}/ack` | 人工调度签收升级单 |
| GET | `/network` | 能力图：资料时效、心跳、当前占用 |
| GET | `/routes?from=H0` | 交通看板：天气折算、事件延误、ETA |
| GET | `/escalations` | 未结人工调度升级单 |
| GET | `/leases` | 当前有效资源租约 |
| POST | `/admin/capabilities` `/admin/heartbeats` `/admin/traffic-events` | 机构上报资料/心跳/交通事件 |

## 运行与验证

```bash
python3 service.py --check     # 装配自检
python3 service.py --port 8000 # 启动服务（自带种子区域网络）
npm test                       # 运行全部 35 个测试
```

种子网络：城东接诊点 H0；市第一医院 H1（双团队双手术台，快速路雨天+追尾）；
省骨科 H2（口径能力最强但路远、沿线结冰）；城西 H3（最近但资料过期、心跳失联）；
120 城东/南郊分站 EMS1/EMS2。

## 模块结构

- `domain.py`：领域模型、策略参数、身份投影
- `catalog.py`：能力目录与交通看板（天气系数、事件延误）
- `triage.py`：四维筛选排序、风险标记、因子级解释
- `orchestrator.py`：案例/租约/计划/里程碑/改道/升级/身份（单锁保证并发不双占）
- `api.py`、`bootstrap.py`、`service.py`：HTTP 接口、种子数据、运行入口
