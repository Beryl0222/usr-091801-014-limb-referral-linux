"""断肢急救转诊编排领域引擎。

只用标准库：聚合状态保存在内存，所有状态变更以事件追加到 JSONL 日志，
重放日志即可恢复（断网/重启后原计划与改道理由仍可查）。

关键领域规则：
- 常温缺血窗口默认 6 小时（冷藏保存按配置延长），预计到达越窗即风险并升级人工调度；
- 候选机构必须能力（部位/血管口径/设备）、实时资源（团队/手术台）、
  路程、道路天气与数据新鲜度全部满足才进入排序；
- 资源只在机构“明确接受”后短时租约锁定，重复接受不会重复占用；
- 里程碑按客户端幂等键补传，重复消息不改变状态；
- 改道以版本追加，原路线、决定理由与决定人始终保留；
- 患者身份按 INTAKE / ACCEPTED / IN_TRANSIT / RECEIVING 阶段对参与机构开放。

排序结果仅用于转运协调，不构成诊断建议。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

# ---------- 常量与策略参数 ----------

WARM_ISCHEMIA_MINUTES = 360      # 常温约 6 小时
COLD_ISCHEMIA_MINUTES = 480      # 冷藏保存按 8 小时（可配置的系统策略）
CAPABILITY_STALE_MINUTES = 240   # 能力资料超过 4 小时视为过期
HEARTBEAT_STALE_MINUTES = 10     # 资源回报超过 10 分钟视为失联
OFFER_TTL_MINUTES = 5            # 邀约待应答时限
LEASE_TTL_MINUTES = 30           # 资源租约时长，途中里程碑可续期
COLD_PRESERVATION = {"COLD", "COOLED", "REFRIGERATED"}

DISCLAIMER = "排序与转运计划仅为协调依据，不构成诊断建议；救治决策以接诊医生判断为准。"

RISK_CAPABILITY_STALE = "CAPABILITY_DATA_STALE"        # 能力资料过期
RISK_CONTACT_LOST = "RESOURCE_CONTACT_LOST"            # 资源回报失联
RISK_ETA_WINDOW = "ETA_BEYOND_ISCHEMIA_WINDOW"         # 预计到达越窗
RISK_NO_UNIT = "NO_RECEIVING_UNIT"                     # 无任何可接收单位
RISK_LEASE_LOST = "RESOURCE_LOCK_LOST"                 # 资源租约失联失效
RISK_ACCEPT_BUSY = "RESOURCE_BUSY_AT_ACCEPTANCE"       # 接受时资源已被占用

ESCALATING_SEVERITIES = {"HIGH", "CRITICAL"}

# 天气对车程的影响系数
WEATHER_FACTORS = {
    "CLEAR": 1.0,
    "CLOUDY": 1.0,
    "OVERCAST": 1.05,
    "RAIN": 1.15,
    "HEAVY_RAIN": 1.3,
    "THUNDERSTORM": 1.4,
    "SNOW": 1.35,
    "FOG": 1.25,
}
# 道路事件对车程的影响系数；passable=false 或 BLOCKED 视为阻断
ROAD_FACTORS = {"CLEAR": 1.0, "SLOW": 1.15, "CONGESTED": 1.35, "INCIDENT": 1.5, "BLOCKED": 1.0}

MILESTONE_ORDER = ["DEPARTED", "ENROUTE", "WAYPOINT", "ARRIVED", "REVASCULARIZATION_STARTED"]
STAGE_RANK = {"ACCEPTED": 1, "IN_TRANSIT": 2, "RECEIVING": 3, "INTAKE": 3}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    if value is None:
        raise DomainError("INVALID_TIME", "缺少时间字段", 400)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DomainError("INVALID_TIME", f"无法解析时间：{value}", 400) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class DomainError(Exception):
    """业务规则冲突或输入无效。"""

    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# ---------- 事件批：收集 -> 同步投影 -> 一次落盘 ----------


class Tx:
    """一次用例内产生的全部事件，保证要么全部投影并持久化，要么不动状态。"""

    def __init__(self, engine: "Orchestrator", at: datetime):
        self.engine = engine
        self.at = at
        self.items: list[tuple[str, dict]] = []

    def emit(self, kind: str, payload: dict) -> None:
        self.items.append((kind, payload))

    def raise_risk(self, case_id: str, risk_type: str, severity: str, message: str,
                   at: str | None = None, hospital_ids=None, context=None,
                   replaces: str | None = None) -> str:
        if replaces:
            self.resolve(case_id, replaces)
        risk_id = new_id("risk")
        self.emit("risk.raised", {
            "case_id": case_id,
            "risk": {
                "risk_id": risk_id, "type": risk_type, "severity": severity,
                "message": message, "hospital_ids": hospital_ids or [],
                "context": context or {}, "raised_at": at or iso(self.at),
            },
        })
        return risk_id

    def resolve(self, case_id: str, risk_id: str) -> None:
        self.emit("risk.resolved", {"case_id": case_id, "risk_id": risk_id})


# ---------- 状态初始化与事件重放 ----------


def _new_state() -> dict:
    return {
        "cases": {},
        "hospitals": {},
        "routes": {},          # (origin_key, hospital_id) -> 路线
        "traffic": {},         # traffic_event_id -> 道路事件
        "offers": {},
        "leases": {},
        "plans": {},
        "milestones": {},      # case_id -> [milestone]
        "idem": {},            # case_id:client_event_id -> 已处理结果
        "identity": {},        # case_id -> [grant]
        "last_screen": {},     # case_id -> 最近一次筛查结果
    }


class Orchestrator:
    """编排引擎。所有公开方法都在同一把锁内完成检查与提交。"""

    def __init__(self, log_path: str | None = None, clock=now_utc, config: dict | None = None):
        self._lock = threading.RLock()
        self._state = _new_state()
        self._events: list[dict] = []
        self.log_path = log_path
        self.clock = clock
        cfg = config or {}
        self.warm_minutes = int(cfg.get("warm_ischemia_minutes", WARM_ISCHEMIA_MINUTES))
        self.cold_minutes = int(cfg.get("cold_ischemia_minutes", COLD_ISCHEMIA_MINUTES))
        self.stale_after = timedelta(minutes=int(cfg.get("capability_stale_minutes", CAPABILITY_STALE_MINUTES)))
        self.heartbeat_after = timedelta(minutes=int(cfg.get("heartbeat_stale_minutes", HEARTBEAT_STALE_MINUTES)))
        self.offer_ttl = timedelta(minutes=int(cfg.get("offer_ttl_minutes", OFFER_TTL_MINUTES)))
        self.lease_ttl = timedelta(minutes=int(cfg.get("lease_ttl_minutes", LEASE_TTL_MINUTES)))
        if log_path:
            self._replay()

    def _persist(self, tx: Tx) -> list[dict]:
        """把 Tx 中的事件按顺序投影到内存并追加写入日志。调用方持锁。"""
        events = []
        for kind, payload in tx.items:
            event = {"id": new_id("ev"), "at": iso(tx.at), "kind": kind, "payload": payload}
            self._apply(event)
            self._events.append(event)
            events.append(event)
        if events and self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
        return events

    def _replay(self) -> None:
        try:
            with open(self.log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        event = json.loads(line)
                        self._apply(event)
                        # 重放事件同样进入日志内存视图，保证重启后审计轨迹可查
                        self._events.append(event)
        except FileNotFoundError:
            return

    def event_log(self, case_id: str | None = None) -> list[dict]:
        with self._lock:
            if case_id is None:
                return list(self._events)
            return [
                e for e in self._events
                if e["payload"].get("case_id") == case_id
                or case_id in (e["payload"].get("case_ids") or [])
            ]

    # ===== 事件投影（纯函数式状态迁移，不做 IO） =====

    def _apply(self, event: dict) -> None:  # noqa: C901 - 投影按事件分发
        kind = event["kind"]
        p = event["payload"]
        s = self._state
        at = event["at"]

        if kind == "case.created":
            s["cases"][p["case_id"]] = {
                **p,
                "status": "OPEN",
                "risks": [],
                "escalated": False,
                "plan_id": None,
                "closed_at": None,
            }
        elif kind == "case.vitals_added":
            s["cases"][p["case_id"]]["vitals"].append(p["reading"])
        elif kind == "case.closed":
            case = s["cases"][p["case_id"]]
            case["status"] = "CLOSED"
            case["closed_at"] = at
        elif kind == "hospital.registered":
            s["hospitals"][p["hospital_id"]] = p
        elif kind == "hospital.heartbeat":
            s["hospitals"][p["hospital_id"]]["last_heartbeat_at"] = p["at"]
        elif kind == "route.upserted":
            s["routes"][(p["origin_key"], p["hospital_id"])] = p
        elif kind == "traffic.reported":
            current = s["traffic"].get(p["traffic_event_id"])
            s["traffic"][p["traffic_event_id"]] = (
                {**current, **p, "active": True} if current else {**p, "active": True}
            )
        elif kind == "traffic.expired":
            if p["traffic_event_id"] in s["traffic"]:
                s["traffic"][p["traffic_event_id"]]["active"] = False
        elif kind == "offer.requested":
            s["offers"][p["offer_id"]] = {
                **p, "status": "PENDING", "responded_at": None, "lease_id": None,
            }
        elif kind == "offer.responded":
            offer = s["offers"][p["offer_id"]]
            offer["status"] = p["status"]
            offer["responded_at"] = at
            if p.get("lease_id"):
                offer["lease_id"] = p["lease_id"]
            if p.get("fail_reason"):
                offer["fail_reason"] = p["fail_reason"]
        elif kind == "offer.expired":
            offer = s["offers"][p["offer_id"]]
            offer["status"] = "EXPIRED"
            offer["responded_at"] = at
        elif kind == "offer.superseded":
            offer = s["offers"][p["offer_id"]]
            offer["status"] = "SUPERSEDED"
            offer["responded_at"] = at
        elif kind == "lease.created":
            s["leases"][p["lease_id"]] = {
                **p, "status": "HELD", "released_at": None,
                "release_reason": None, "last_confirmed_at": p["acquired_at"],
            }
        elif kind == "lease.confirmed":
            lease = s["leases"][p["lease_id"]]
            lease["last_confirmed_at"] = p["at"]
            if p.get("new_expires_at"):
                lease["expires_at"] = p["new_expires_at"]
        elif kind == "lease.consumed":
            lease = s["leases"][p["lease_id"]]
            lease["status"] = "CONSUMED"
            lease["last_confirmed_at"] = at
        elif kind == "lease.released":
            lease = s["leases"][p["lease_id"]]
            lease["status"] = "RELEASED"
            lease["released_at"] = at
            lease["release_reason"] = p["reason"]
        elif kind == "lease.expired":
            lease = s["leases"][p["lease_id"]]
            lease["status"] = "EXPIRED"
            lease["released_at"] = at
            lease["release_reason"] = "TTL"
        elif kind == "plan.created":
            s["plans"][p["plan_id"]] = {
                "plan_id": p["plan_id"], "case_id": p["case_id"],
                "hospital_id": p["hospital_id"], "lease_id": p["lease_id"],
                "transport": p.get("transport"), "status": "ACTIVE",
                "versions": [p["version"]], "created_at": at,
            }
            s["cases"][p["case_id"]]["plan_id"] = p["plan_id"]
        elif kind == "plan.rerouted":
            plan = s["plans"][p["plan_id"]]
            plan["versions"].append(p["version"])
            plan["hospital_id"] = p["version"]["hospital_id"]
            plan["lease_id"] = p.get("lease_id", plan["lease_id"])
        elif kind == "plan.status_changed":
            s["plans"][p["plan_id"]]["status"] = p["status"]
        elif kind == "milestone.recorded":
            s["milestones"].setdefault(p["case_id"], []).append(p["milestone"])
        elif kind == "risk.raised":
            s["cases"][p["case_id"]]["risks"].append(
                {**p["risk"], "active": True, "resolved_at": None, "ack_by": None, "ack_at": None}
            )
        elif kind == "risk.resolved":
            for risk in s["cases"][p["case_id"]]["risks"]:
                if risk["risk_id"] == p["risk_id"] and risk["active"]:
                    risk["active"] = False
                    risk["resolved_at"] = at
        elif kind == "risk.acknowledged":
            for risk in s["cases"][p["case_id"]]["risks"]:
                if risk["risk_id"] == p["risk_id"]:
                    risk["ack_by"] = p["by"]
                    risk["ack_at"] = at
                    risk["ack_note"] = p.get("note")
        elif kind == "identity.granted":
            s["identity"].setdefault(p["case_id"], []).append({**p, "active": True, "revoked_at": None})
        elif kind == "identity.revoked":
            for grant in s["identity"].get(p["case_id"], []):
                if grant["hospital_id"] == p["hospital_id"] and grant["active"]:
                    grant["active"] = False
                    grant["revoked_at"] = at
                    grant["revoke_reason"] = p.get("reason")
        elif kind == "screen.recorded":
            s["last_screen"][p["case_id"]] = p["screen"]
        elif kind == "idempotency.recorded":
            s["idem"][f"{p['case_id']}:{p['client_event_id']}"] = {
                "kind": p["kind"], "ref_id": p["ref_id"],
            }
        self._recompute_escalation(p.get("case_id"))

    def _recompute_escalation(self, case_id: str | None) -> None:
        if not case_id or case_id not in self._state["cases"]:
            return
        case = self._state["cases"][case_id]
        case["escalated"] = any(
            r["active"] and r["ack_at"] is None and r["severity"] in ESCALATING_SEVERITIES
            for r in case["risks"]
        )

    # ===== 基础资料：机构 / 路线 / 道路事件 =====

    def register_hospital(self, data: dict) -> dict:
        hid = data.get("hospital_id")
        if not hid:
            raise DomainError("INVALID_HOSPITAL", "缺少 hospital_id", 400)
        profile = {
            "hospital_id": hid,
            "name": data.get("name", hid),
            "capability": self._validated_capability(data.get("capability", {})),
            "teams": [self._validated_team(t) for t in data.get("teams", [])],
            "operating_rooms": [self._validated_room(r) for r in data.get("operating_rooms", [])],
            "status": data.get("status", "NORMAL"),
            "updated_at": data.get("updated_at") or iso(self.clock()),
            "last_heartbeat_at": data.get("last_heartbeat_at"),
        }
        with self._lock:
            self._persist(self._tx_with("hospital.registered", profile))
        return profile

    def _tx_with(self, kind: str, payload: dict, at: datetime | None = None) -> Tx:
        tx = Tx(self, at or self.clock())
        tx.emit(kind, payload)
        return tx

    @staticmethod
    def _validated_capability(cap: dict) -> dict:
        return {
            "parts": list(cap.get("parts", [])),
            "min_vessel_mm": cap.get("min_vessel_mm"),
            "equipment": list(cap.get("equipment", [])),
            "services_247": list(cap.get("services_247", cap.get("services", []))),
        }

    @staticmethod
    def _validated_team(team: dict) -> dict:
        return {
            "team_id": team["team_id"],
            "caliber_min_mm": float(team.get("caliber_min_mm", 99)),
            "parts": list(team.get("parts", [])),
            "status": team.get("status", "ON_CALL"),
        }

    @staticmethod
    def _validated_room(room: dict) -> dict:
        return {
            "room_id": room["room_id"],
            "caliber_min_mm": float(room.get("caliber_min_mm", 99)),
            "parts": list(room.get("parts", [])),
            "status": room.get("status", "AVAILABLE"),
        }

    def heartbeat(self, hospital_id: str, at: str | None = None) -> dict:
        with self._lock:
            self._require_hospital(hospital_id)
            moment = parse_iso(at) if at else self.clock()
            # 先投影心跳，再由 sweep 统一对账失联风险（恢复后即时解除）
            self._persist(self._tx_with(
                "hospital.heartbeat", {"hospital_id": hospital_id, "at": iso(moment)}, moment))
            sweep_tx = Tx(self, moment)
            self._sweep(sweep_tx)
            self._persist(sweep_tx)
        return {"hospital_id": hospital_id, "last_heartbeat_at": iso(moment)}

    def _require_hospital(self, hospital_id: str) -> dict:
        hospital = self._state["hospitals"].get(hospital_id)
        if not hospital:
            raise DomainError("UNKNOWN_HOSPITAL", f"机构不存在：{hospital_id}", 404)
        return hospital

    def upsert_route(self, data: dict) -> dict:
        hospital_id = data.get("destination_hospital_id") or data.get("hospital_id")
        with self._lock:
            self._require_hospital(hospital_id)
            origin_key = self._origin_key(data.get("origin"))
            if not origin_key:
                raise DomainError("INVALID_ORIGIN", "缺少起点 origin（hospital_id 或 lat/lon）", 400)
            travel = float(data["travel_minutes"])
            distance = float(data["distance_km"])
            weather = data.get("weather") or {"condition": "CLEAR"}
            route = {
                "origin_key": origin_key,
                "hospital_id": hospital_id,
                "distance_km": distance,
                "travel_minutes": travel,
                "weather": {
                    "condition": weather.get("condition", "CLEAR"),
                    "observed_at": weather.get("observed_at"),
                },
                "updated_at": iso(self.clock()),
            }
            self._persist(self._tx_with("route.upserted", route))
        return route

    @staticmethod
    def _origin_key(origin: dict | None) -> str | None:
        if not origin:
            return None
        if origin.get("hospital_id"):
            return f"hospital:{origin['hospital_id']}"
        if origin.get("lat") is not None and origin.get("lon") is not None:
            return f"loc:{round(float(origin['lat']), 4)},{round(float(origin['lon']), 4)}"
        return None

    def report_traffic(self, data: dict) -> dict:
        hospital_id = data.get("destination_hospital_id")
        with self._lock:
            self._require_hospital(hospital_id)
            origin_key = self._origin_key(data.get("origin"))
            if not origin_key:
                raise DomainError("INVALID_ORIGIN", "缺少起点 origin", 400)
            event = {
                "traffic_event_id": data.get("traffic_event_id") or new_id("tf"),
                "origin_key": origin_key,
                "hospital_id": hospital_id,
                "severity": data.get("severity", "SLOW"),
                "passable": bool(data.get("passable", True)),
                "summary": data.get("summary", ""),
                "observed_at": data.get("observed_at") or iso(self.clock()),
                "ttl_minutes": int(data.get("ttl_minutes", 120)),
            }
            # 同一 traffic_event_id 的重复上报幂等刷新观测（弱网重传）
            self._persist(self._tx_with("traffic.reported", event))
        return self._state["traffic"][event["traffic_event_id"]]

    # ===== 病例录入 =====

    def create_case(self, data: dict) -> dict:
        case_id = new_id("case")
        case_code = data.get("case_code") or f"L{self.clock().strftime('%Y%m%d')}-{case_id[-6:].upper()}"
        injury = data.get("injury") or {}
        part = injury.get("part")
        if not part:
            raise DomainError("INVALID_INJURY", "缺少离断部位 injury.part", 400)
        caliber = injury.get("vessel_caliber_mm")
        if caliber is None:
            raise DomainError("INVALID_INJURY", "缺少待吻合血管口径 vessel_caliber_mm", 400)
        amputated_at = parse_iso(injury.get("amputated_at"))
        preservation = (injury.get("preservation") or "WARM").upper()
        window = self.cold_minutes if preservation in COLD_PRESERVATION else self.warm_minutes
        deadline = amputated_at + timedelta(minutes=window)
        referring = data.get("referring_hospital_id")
        with self._lock:
            if referring and referring not in self._state["hospitals"]:
                raise DomainError("UNKNOWN_HOSPITAL", f"接诊机构不存在：{referring}", 404)
            now = iso(self.clock())
            case = {
                "case_id": case_id,
                "case_code": case_code,
                "created_at": now,
                "referring_hospital_id": referring,
                "patient": self._validated_patient(data.get("patient") or {}),
                "injury": {
                    "part": part,
                    "amputated_at": iso(amputated_at),
                    "preservation": preservation,
                    "vessel_caliber_mm": float(caliber),
                },
                "ischemia": {
                    "window_minutes": window,
                    "deadline": iso(deadline),
                    "basis": "冷藏保存策略窗口" if preservation in COLD_PRESERVATION else "常温黄金6小时",
                },
                "vitals": list(data.get("vitals", [])),
                "pickup": data.get("pickup") or {},
                "notes": data.get("notes", ""),
            }
            tx = Tx(self, self.clock())
            tx.emit("case.created", case)
            if referring:
                tx.emit("identity.granted", {
                    "grant_id": new_id("grant"), "case_id": case_id,
                    "hospital_id": referring, "stage": "INTAKE",
                    "reason": "接诊机构录入并持有患者资料", "granted_at": now,
                })
            self._persist(tx)
        return self._state["cases"][case_id]

    @staticmethod
    def _validated_patient(patient: dict) -> dict:
        return {
            "name": patient.get("name"),
            "id_number": patient.get("id_number"),
            "contact": patient.get("contact"),
            "pseudonym": patient.get("pseudonym") or new_id("P"),
            "age": patient.get("age"),
            "sex": patient.get("sex"),
        }

    def add_vitals(self, case_id: str, reading: dict) -> dict:
        with self._lock:
            self._require_case(case_id)
            entry = {"at": reading.get("at") or iso(self.clock()),
                     **{k: v for k, v in reading.items() if k != "at"}}
            self._persist(self._tx_with("case.vitals_added", {"case_id": case_id, "reading": entry}))
        return entry

    def _require_case(self, case_id: str) -> dict:
        case = self._state["cases"].get(case_id)
        if not case:
            raise DomainError("UNKNOWN_CASE", f"病例不存在：{case_id}", 404)
        return case

    # ===== 筛查与排序 =====

    def _origin_for_case(self, case: dict) -> str:
        pickup = case.get("pickup") or {}
        key = self._origin_key(pickup)
        if key:
            return key
        if case.get("referring_hospital_id"):
            return f"hospital:{case['referring_hospital_id']}"
        raise DomainError("INVALID_ORIGIN", "病例缺少接载起点，无法计算路程", 400)

    def _route_snapshot(self, origin_key: str, hospital_id: str, at: datetime) -> dict | None:
        route = self._state["routes"].get((origin_key, hospital_id))
        if not route:
            return None
        weather_condition = route["weather"].get("condition", "CLEAR")
        weather_factor = WEATHER_FACTORS.get(weather_condition, 1.0)
        events = []
        road_factor = 1.0
        passable = True
        for ev in self._state["traffic"].values():
            if not ev["active"] or ev["origin_key"] != origin_key or ev["hospital_id"] != hospital_id:
                continue
            if parse_iso(ev["observed_at"]) + timedelta(minutes=ev["ttl_minutes"]) < at:
                continue
            events.append({k: ev[k] for k in ("traffic_event_id", "severity", "summary", "passable")})
            if not ev["passable"] or ev["severity"] == "BLOCKED":
                passable = False
            road_factor = max(road_factor, ROAD_FACTORS.get(ev["severity"], 1.0))
        base = float(route["travel_minutes"])
        adjusted = round(base * weather_factor * road_factor)
        return {
            "origin_key": origin_key,
            "distance_km": route["distance_km"],
            "base_travel_minutes": base,
            "weather": {"condition": weather_condition, "factor": weather_factor,
                        "observed_at": route["weather"].get("observed_at")},
            "road_factor": road_factor,
            "road_events": events,
            "adjusted_travel_minutes": adjusted,
            "passable": passable,
            "computed_at": iso(at),
        }

    def _stale(self, hospital: dict, at: datetime) -> bool:
        return at - parse_iso(hospital["updated_at"]) > self.stale_after

    def _heartbeat_stale(self, hospital: dict, at: datetime) -> bool:
        # 从未回报的机构不判失联，但其必须在接受时产生一次有效回报
        beat = hospital.get("last_heartbeat_at")
        return beat is not None and at - parse_iso(beat) > self.heartbeat_after

    def _available_team(self, hospital: dict, case: dict) -> dict | None:
        caliber = case["injury"]["vessel_caliber_mm"]
        part = case["injury"]["part"]
        candidates = [
            t for t in hospital["teams"]
            if t["status"] == "ON_CALL" and t["caliber_min_mm"] <= caliber and part in t["parts"]
            and not self._resource_busy(hospital["hospital_id"], "team", t["team_id"])
        ]
        return sorted(candidates, key=lambda t: (t["caliber_min_mm"], t["team_id"]))[0] if candidates else None

    def _available_room(self, hospital: dict, case: dict) -> dict | None:
        caliber = case["injury"]["vessel_caliber_mm"]
        part = case["injury"]["part"]
        candidates = [
            r for r in hospital["operating_rooms"]
            if r["status"] == "AVAILABLE" and r["caliber_min_mm"] <= caliber and part in r["parts"]
            and not self._resource_busy(hospital["hospital_id"], "room", r["room_id"])
        ]
        return sorted(candidates, key=lambda r: (r["caliber_min_mm"], r["room_id"]))[0] if candidates else None

    def _resource_busy(self, hospital_id: str, kind: str, resource_id: str) -> bool:
        field = "team_id" if kind == "team" else "room_id"
        return any(
            lease["hospital_id"] == hospital_id and lease[field] == resource_id
            and lease["status"] in ("HELD", "CONSUMED")
            for lease in self._state["leases"].values()
        )

    def _capability_failures(self, hospital: dict, case: dict) -> list[str]:
        reasons: list[str] = []
        cap = hospital["capability"]
        injury = case["injury"]
        if injury["part"] not in cap["parts"]:
            reasons.append(f"不支持离断部位 {injury['part']}")
        if cap["min_vessel_mm"] is None or float(cap["min_vessel_mm"]) > injury["vessel_caliber_mm"]:
            reasons.append(f"无可吻合 {injury['vessel_caliber_mm']}mm 血管口径的能力")
        if not cap["services_247"]:
            reasons.append("无显微外科值守服务登记")
        return reasons

    def screen_case(self, case_id: str, at: str | None = None) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            moment = parse_iso(at) if at else self.clock()
            origin_key = self._origin_for_case(case)
            deadline = parse_iso(case["ischemia"]["deadline"])
            required_caliber = case["injury"]["vessel_caliber_mm"]

            candidates, rejected = [], []
            stale_hospitals, contact_hospitals = [], []
            for hid in sorted(self._state["hospitals"]):
                if hid == case.get("referring_hospital_id"):
                    continue
                hospital = self._state["hospitals"][hid]
                entry: dict = {
                    "hospital_id": hid, "name": hospital["name"],
                    "capability_min_vessel_mm": hospital["capability"]["min_vessel_mm"],
                }
                route = self._route_snapshot(origin_key, hid, moment)
                if route is None:
                    rejected.append({**entry, "reason": "无路线数据"})
                    continue
                entry["route"] = route
                eta = moment + timedelta(minutes=route["adjusted_travel_minutes"])
                remaining = int((deadline - eta).total_seconds() // 60)
                entry["eta"] = iso(eta)
                entry["minutes_remaining_on_arrival"] = remaining

                reasons = self._capability_failures(hospital, case)
                if not route["passable"]:
                    reasons.append("道路阻断，无法通行")
                if hospital["status"] != "NORMAL":
                    reasons.append(f"机构状态 {hospital['status']}，暂停接收")
                if self._stale(hospital, moment):
                    reasons.append("能力资料过期，需先刷新")
                    stale_hospitals.append(hid)
                if self._heartbeat_stale(hospital, moment):
                    reasons.append("资源回报失联")
                    contact_hospitals.append(hid)

                matching_teams = [t for t in hospital["teams"]
                                  if t["status"] == "ON_CALL" and t["caliber_min_mm"] <= required_caliber
                                  and case["injury"]["part"] in t["parts"]]
                free_rooms = [r for r in hospital["operating_rooms"] if r["status"] == "AVAILABLE"]
                matching_rooms = [r for r in free_rooms
                                  if r["caliber_min_mm"] <= required_caliber
                                  and case["injury"]["part"] in r["parts"]]
                if not matching_teams:
                    reasons.append("无当班且口径匹配的显微团队")
                elif not any(not self._resource_busy(hid, "team", t["team_id"]) for t in matching_teams):
                    reasons.append("匹配团队已被其他病例占用")
                if not matching_rooms:
                    reasons.append("无口径匹配的可用手术台")
                elif not any(not self._resource_busy(hid, "room", r["room_id"]) for r in matching_rooms):
                    reasons.append("匹配手术台已被其他病例占用")
                if remaining < 0:
                    reasons.append(f"预计到达超出缺血窗口 {-remaining} 分钟")

                freshness_age = int((moment - parse_iso(hospital["updated_at"])).total_seconds() // 60)
                heartbeat_age = None
                if hospital.get("last_heartbeat_at"):
                    heartbeat_age = int((moment - parse_iso(hospital["last_heartbeat_at"])).total_seconds() // 60)
                entry["data_age_minutes"] = freshness_age
                entry["heartbeat_age_minutes"] = heartbeat_age

                if reasons:
                    rejected.append({**entry, "reason": "；".join(reasons)})
                    continue

                team = self._available_team(hospital, case)
                room = self._available_room(hospital, case)
                score = self._score(route, remaining, hospital)
                entry["score"] = score
                entry["available_team"] = {"team_id": team["team_id"], "caliber_min_mm": team["caliber_min_mm"]}
                entry["available_room"] = {"room_id": room["room_id"], "caliber_min_mm": room["caliber_min_mm"]}
                entry["factors"] = self._explain_factors(route, remaining, team, room, freshness_age)
                candidates.append(entry)

            candidates.sort(key=lambda c: (c["score"], c["route"]["distance_km"], c["hospital_id"]))
            for rank, entry in enumerate(candidates, start=1):
                entry["rank"] = rank

            # 先提交风险变化，再读取最终风险列表，最后落筛查快照
            risk_tx = Tx(self, moment)
            self._sync_capability_stale(risk_tx, case_id, set(stale_hospitals))
            self._sync_contact_lost(risk_tx, case_id, set(contact_hospitals))
            self._sync_no_unit(risk_tx, case_id, bool(candidates))
            self._sweep(risk_tx, only_offers_traffic=True)
            self._persist(risk_tx)

            risk_flags = [r["risk_id"] for r in self._state["cases"][case_id]["risks"] if r["active"]]
            screen = {
                "case_id": case_id,
                "case_code": case["case_code"],
                "screened_at": iso(moment),
                "ischemia_deadline": case["ischemia"]["deadline"],
                "candidates": candidates,
                "rejected": rejected,
                "risk_flags": risk_flags,
                "disclaimer": DISCLAIMER,
            }
            self._persist(self._tx_with("screen.recorded", {"case_id": case_id, "screen": screen}, moment))
            return screen

    @staticmethod
    def _score(route: dict, remaining: int, hospital: dict) -> float:
        score = float(route["adjusted_travel_minutes"])
        # 时间相同时，到达后缺血余量更大者优先
        score -= min(max(remaining, 0), 360) * 0.05
        if hospital["status"] != "NORMAL":
            score += 60
        return round(score, 2)

    @staticmethod
    def _explain_factors(route: dict, remaining: int, team: dict, room: dict, freshness_age: int) -> list[dict]:
        w = route["weather"]
        factors = [
            {"factor": "基础车程", "detail": f"{route['base_travel_minutes']:.0f} 分钟/{route['distance_km']} km"},
            {"factor": "天气", "detail": f"{w['condition']}，系数 {w['factor']}"},
        ]
        if route["road_events"]:
            factors.append({
                "factor": "道路事件",
                "detail": "；".join(f"{e['severity']}:{e['summary'] or e['traffic_event_id']}"
                                    for e in route["road_events"]) + f"，系数 {route['road_factor']}",
            })
        factors += [
            {"factor": "调整后车程", "detail": f"{route['adjusted_travel_minutes']} 分钟"},
            {"factor": "到达后缺血余量", "detail": f"{remaining} 分钟"},
            {"factor": "显微团队", "detail": f"{team['team_id']} 当班，可吻合至 {team['caliber_min_mm']}mm"},
            {"factor": "手术台", "detail": f"{room['room_id']} 可用，可吻合至 {room['caliber_min_mm']}mm"},
            {"factor": "能力资料新鲜度", "detail": f"{freshness_age} 分钟前更新"},
        ]
        return factors

    def _active_risk(self, case_id: str, risk_type: str) -> dict | None:
        return next((r for r in self._state["cases"][case_id]["risks"]
                     if r["active"] and r["type"] == risk_type), None)

    def _sync_capability_stale(self, tx: Tx, case_id: str, stale_set: set[str]) -> None:
        current = self._active_risk(case_id, RISK_CAPABILITY_STALE)
        current_set = set(current["hospital_ids"]) if current else set()
        at = iso(tx.at)
        if stale_set and stale_set != current_set:
            tx.raise_risk(case_id, RISK_CAPABILITY_STALE, "HIGH",
                          f"能力资料过期机构：{'、'.join(sorted(stale_set))}", at,
                          hospital_ids=sorted(stale_set),
                          replaces=current["risk_id"] if current else None)
        elif not stale_set and current:
            tx.resolve(case_id, current["risk_id"])

    def _sync_contact_lost(self, tx: Tx, case_id: str, lost_set: set[str]) -> None:
        """按“筛查可见 + 邀约/租约关联”的并集核对失联风险。"""
        desired = set(lost_set)
        for offer in self._state["offers"].values():
            if offer["case_id"] != case_id or offer["status"] not in ("PENDING", "ACCEPTED"):
                continue
            hospital = self._state["hospitals"].get(offer["hospital_id"])
            if hospital and self._heartbeat_stale(hospital, tx.at):
                desired.add(hospital["hospital_id"])
        for lease in self._state["leases"].values():
            if (lease["case_id"] == case_id and lease["status"] in ("HELD", "CONSUMED")
                    and self._heartbeat_stale(self._state["hospitals"][lease["hospital_id"]], tx.at)):
                desired.add(lease["hospital_id"])
        current = self._active_risk(case_id, RISK_CONTACT_LOST)
        current_set = set(current["hospital_ids"]) if current else set()
        at = iso(tx.at)
        if desired and desired != current_set:
            tx.raise_risk(case_id, RISK_CONTACT_LOST, "CRITICAL",
                          f"资源回报失联机构：{'、'.join(sorted(desired))}", at,
                          hospital_ids=sorted(desired),
                          replaces=current["risk_id"] if current else None)
        elif not desired and current:
            tx.resolve(case_id, current["risk_id"])

    def _sync_no_unit(self, tx: Tx, case_id: str, has_candidates: bool) -> None:
        current = self._active_risk(case_id, RISK_NO_UNIT)
        if not has_candidates and not current:
            tx.raise_risk(case_id, RISK_NO_UNIT, "CRITICAL",
                          "筛查无任何可接收单位，需人工调度立即介入", iso(tx.at))
        elif has_candidates and current:
            tx.resolve(case_id, current["risk_id"])

    # ===== 风险人工确认 / 升级队列 =====

    def acknowledge_risk(self, case_id: str, risk_id: str, by: str, note: str | None = None) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            if not any(r["risk_id"] == risk_id for r in case["risks"]):
                raise DomainError("UNKNOWN_RISK", f"风险不存在：{risk_id}", 404)
            self._persist(self._tx_with(
                "risk.acknowledged", {"case_id": case_id, "risk_id": risk_id, "by": by, "note": note}))
        return {"case_id": case_id, "risk_id": risk_id, "ack_by": by}

    def escalations(self) -> list[dict]:
        with self._lock:
            result = []
            for cid in sorted(self._state["cases"]):
                case = self._state["cases"][cid]
                active = [r for r in case["risks"] if r["active"] and r["ack_at"] is None
                          and r["severity"] in ESCALATING_SEVERITIES]
                if active:
                    result.append({
                        "case_id": cid, "case_code": case["case_code"], "escalated": True,
                        "risks": [{"risk_id": r["risk_id"], "type": r["type"],
                                   "severity": r["severity"], "message": r["message"],
                                   "raised_at": r["raised_at"]} for r in active],
                    })
            return result

    # ===== 邀约 / 接受 / 租约 =====

    def request_offers(self, case_id: str, hospital_ids: list[str] | None = None,
                       at: str | None = None, manual: bool = False) -> list[dict]:
        with self._lock:
            self._require_case(case_id)
            moment = parse_iso(at) if at else self.clock()
            screen = self.screen_case(case_id, iso(moment))
            by_id = {c["hospital_id"]: c for c in screen["candidates"]}
            wanted = list(hospital_ids) if hospital_ids else [c["hospital_id"] for c in screen["candidates"]]
            tx = Tx(self, moment)
            created = []
            for hid in wanted:
                candidate = by_id.get(hid)
                if candidate is None:
                    # 人工调度可向筛查候选之外的机构发邀约；机构仍须明确接受才锁资源
                    if not manual:
                        continue
                    self._require_hospital(hid)
                    candidate = {
                        "hospital_id": hid, "rank": None,
                        "manual_candidate": True,
                        "factors": [{"factor": "人工指定", "detail": "值班员越过筛查结果指定邀约，需机构明确接受"}],
                    }
                offer_id = new_id("offer")
                payload = {
                    "offer_id": offer_id, "case_id": case_id,
                    "hospital_id": hid,
                    "created_at": iso(moment),
                    "expires_at": iso(moment + self.offer_ttl),
                    "candidate_snapshot": candidate,
                }
                tx.emit("offer.requested", payload)
                created.append({**payload, "status": "PENDING", "responded_at": None, "lease_id": None})
            self._persist(tx)
            return created

    def respond_offer(self, offer_id: str, accepted: bool, at: str | None = None,
                      declined_reason: str | None = None) -> dict:
        with self._lock:
            offer = self._state["offers"].get(offer_id)
            if not offer:
                raise DomainError("UNKNOWN_OFFER", f"邀约不存在：{offer_id}", 404)
            moment = parse_iso(at) if at else self.clock()
            if offer["status"] != "PENDING":
                raise DomainError("OFFER_NOT_PENDING",
                                  f"邀约当前状态 {offer['status']}，不可重复应答", 409)
            if moment >= parse_iso(offer["expires_at"]):
                self._persist(self._tx_with("offer.expired", {"offer_id": offer_id}, moment))
                raise DomainError("OFFER_EXPIRED", "邀约已超时，需重新发起", 409)

            case = self._require_case(offer["case_id"])
            hospital = self._require_hospital(offer["hospital_id"])
            tx = Tx(self, moment)
            if not accepted:
                tx.emit("offer.responded", {
                    "offer_id": offer_id, "status": "DECLINED", "fail_reason": declined_reason})
                self._persist(tx)
                return self._state["offers"][offer_id]

            # 机构的明确接受本身就是一次实时资源回报
            tx.emit("hospital.heartbeat", {"hospital_id": hospital["hospital_id"], "at": iso(moment)})
            team = self._available_team(hospital, case)
            room = self._available_room(hospital, case)
            if team is None or room is None:
                missing = "团队与手术台"
                if team and not room:
                    missing = "手术台"
                elif room and not team:
                    missing = "显微团队"
                tx.raise_risk(
                    offer["case_id"], RISK_ACCEPT_BUSY, "HIGH",
                    f"{hospital['name']} 接受时匹配{missing}已被并发占用", iso(moment),
                    context={"offer_id": offer_id})
                tx.emit("offer.responded", {
                    "offer_id": offer_id, "status": "FAILED",
                    "fail_reason": f"匹配{missing}已被占用"})
                self._persist(tx)
                raise DomainError("RESOURCE_BUSY",
                                  f"匹配{missing}已被并发占用，已升级人工调度", 409)

            lease_id = new_id("lease")
            stage = "IN_TRANSIT" if self._departed(case) else "ACCEPTED"
            tx.emit("lease.created", {
                "lease_id": lease_id, "case_id": case["case_id"],
                "hospital_id": hospital["hospital_id"],
                "team_id": team["team_id"], "room_id": room["room_id"],
                "acquired_at": iso(moment), "expires_at": iso(moment + self.lease_ttl),
                "reason": "OFFER_ACCEPTED",
            })
            tx.emit("offer.responded", {"offer_id": offer_id, "status": "ACCEPTED", "lease_id": lease_id})
            tx.emit("identity.granted", {
                "grant_id": new_id("grant"), "case_id": case["case_id"],
                "hospital_id": hospital["hospital_id"], "stage": stage,
                "reason": "机构接受并锁定资源", "granted_at": iso(moment),
            })
            self._persist(tx)
            # 接受即心跳：由 sweep 统一对账所有关联病例的失联风险
            reconcile = Tx(self, moment)
            self._sweep(reconcile)
            self._persist(reconcile)
            return self._state["offers"][offer_id]

    def _departed(self, case: dict) -> bool:
        return any(m["code"] in ("DEPARTED", "ENROUTE", "WAYPOINT")
                   for m in self._state["milestones"].get(case["case_id"], []))

    # ===== 转运计划 =====

    def create_plan(self, case_id: str, offer_id: str, transport: dict | None = None,
                    at: str | None = None) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            offer = self._state["offers"].get(offer_id)
            if not offer or offer["case_id"] != case_id:
                raise DomainError("UNKNOWN_OFFER", "邀约不存在或不属于该病例", 404)
            if offer["status"] != "ACCEPTED" or not offer.get("lease_id"):
                raise DomainError("OFFER_NOT_ACCEPTED",
                                  "候选机构尚未明确接受并锁定资源，不能给出转运计划", 409)
            if case.get("plan_id"):
                raise DomainError("PLAN_EXISTS", "计划已存在；改道请使用 reroute", 409)
            moment = parse_iso(at) if at else self.clock()
            hospital = self._require_hospital(offer["hospital_id"])
            origin_key = self._origin_for_case(case)
            route = self._route_snapshot(origin_key, hospital["hospital_id"], moment)
            if route is None:
                raise DomainError("NO_ROUTE", f"无通往 {hospital['hospital_id']} 的路线数据", 409)
            if not route["passable"]:
                raise DomainError("ROAD_BLOCKED",
                                  "通往已接受机构的路线已阻断，请重新筛查并改向其他机构", 409)
            eta = moment + timedelta(minutes=route["adjusted_travel_minutes"])
            remaining = int((parse_iso(case["ischemia"]["deadline"]) - eta).total_seconds() // 60)
            version = self._plan_version(1, hospital, route, eta, remaining, moment,
                                         offer.get("candidate_snapshot"), reason="初版转运计划",
                                         decided_by=(transport or {}).get("decided_by"))
            plan_id = new_id("plan")
            tx = Tx(self, moment)
            tx.emit("plan.created", {
                "plan_id": plan_id, "case_id": case_id,
                "hospital_id": hospital["hospital_id"], "lease_id": offer["lease_id"],
                "transport": transport, "version": version,
            })
            # 接收机构与计划已确定，“无任何可接收单位”风险不再成立
            no_unit = self._active_risk(case_id, RISK_NO_UNIT)
            if no_unit:
                tx.resolve(case_id, no_unit["risk_id"])
            self._window_risk_items(tx, case, eta, hospital["hospital_id"])
            self._persist(tx)
            return self._state["plans"][plan_id]

    def _plan_version(self, number, hospital, route, eta, remaining, moment,
                      snapshot, reason, decided_by, manual_override=False):
        return {
            "version": number,
            "hospital_id": hospital["hospital_id"],
            "hospital_name": hospital["name"],
            "route": route,
            "eta": iso(eta),
            "minutes_remaining_on_arrival": remaining,
            "reason": reason,
            "decided_by": decided_by,
            "decided_at": iso(moment),
            "manual_override": manual_override,
            "decision_basis": self._basis_text(number, hospital, route, remaining, snapshot, reason),
            "disclaimer": DISCLAIMER,
        }

    @staticmethod
    def _basis_text(number, hospital, route, remaining, snapshot, reason):
        parts = [
            f"v{number}：{reason}。",
            f"目标 {hospital['name']}，调整后车程 {route['adjusted_travel_minutes']} 分钟",
            f"（基础 {route['base_travel_minutes']:.0f} 分钟，天气 {route['weather']['condition']}"
            f" 系数 {route['weather']['factor']}，道路系数 {route['road_factor']}）",
            f"预计到达后缺血余量 {remaining} 分钟。",
        ]
        if snapshot and snapshot.get("factors"):
            parts.append("排序因素：" + "；".join(f["factor"] for f in snapshot["factors"]) + "。")
        parts.append(DISCLAIMER)
        return "".join(parts)

    def reroute(self, case_id: str, data: dict) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            client_key = data.get("client_event_id")
            if client_key:
                dup = self._idem_result(case_id, client_key)
                if dup:
                    return dup
            plan_id = case.get("plan_id")
            if not plan_id:
                raise DomainError("NO_PLAN", "尚无转运计划，不能改道", 409)
            plan = self._state["plans"][plan_id]
            if plan["status"] != "ACTIVE":
                raise DomainError("PLAN_NOT_ACTIVE", f"计划状态 {plan['status']}，不能改道", 409)
            reason = data.get("reason")
            if not reason:
                raise DomainError("INVALID_REROUTE", "改道必须记录原因 reason", 400)
            decided_by = data.get("decided_by")
            if not decided_by:
                raise DomainError("INVALID_REROUTE", "改道必须记录决定人 decided_by", 400)
            moment = parse_iso(data["at"]) if data.get("at") else self.clock()

            target_id = data.get("target_hospital_id") or plan["hospital_id"]
            target = self._require_hospital(target_id)
            origin_key = self._origin_for_case(case)
            route = self._route_snapshot(origin_key, target_id, moment)
            if route is None:
                raise DomainError("NO_ROUTE", f"无通往 {target_id} 的路线数据", 409)
            if not route["passable"] and not data.get("override"):
                raise DomainError("ROAD_BLOCKED",
                                  "目标路线阻断；如仍坚持改道需人工 override 并留理由", 409)

            screen = self._state["last_screen"].get(case_id)
            eligible_ids = {c["hospital_id"] for c in (screen or {}).get("candidates", [])}
            manual = bool(data.get("override"))
            if target_id not in eligible_ids and not manual:
                raise DomainError("TARGET_NOT_ELIGIBLE",
                                  "目标不在当前可接收候选内；人工调度改道需 override=true 并记录理由", 409)

            tx = Tx(self, moment)
            new_lease_id = plan["lease_id"]
            old_hospital = plan["hospital_id"]
            if target_id != old_hospital:
                # 改道到新机构同样要求该机构“明确接受”：凭其已接受邀约对应的租约切换
                offer_id = data.get("offer_id")
                new_offer = self._state["offers"].get(offer_id) if offer_id else None
                if not new_offer or new_offer["case_id"] != case_id:
                    raise DomainError("UNKNOWN_OFFER",
                                      "改道到新机构必须提供该机构已接受邀约的 offer_id", 404)
                if new_offer["hospital_id"] != target_id:
                    raise DomainError("OFFER_TARGET_MISMATCH", "邀约机构与改道目标不一致", 409)
                if new_offer["status"] != "ACCEPTED" or not new_offer.get("lease_id"):
                    raise DomainError("OFFER_NOT_ACCEPTED",
                                      "目标机构尚未明确接受并锁定资源，不能改道", 409)
                target_lease = self._state["leases"].get(new_offer["lease_id"])
                if not target_lease or target_lease["status"] not in ("HELD", "CONSUMED"):
                    raise DomainError("LEASE_INVALID",
                                      "目标机构资源租约已失效，需重新邀约并接受", 409)
                new_lease_id = new_offer["lease_id"]
                old_lease = plan["lease_id"]
                if old_lease and self._state["leases"].get(old_lease, {}).get("status") == "HELD":
                    tx.emit("lease.released", {"lease_id": old_lease, "reason": "DIVERTED"})
                # 原接受邀约随改道作废，不再计入失联对账
                for other in self._state["offers"].values():
                    if (other["case_id"] == case_id and other["hospital_id"] == old_hospital
                            and other["status"] == "ACCEPTED"):
                        tx.emit("offer.superseded", {"offer_id": other["offer_id"]})
                tx.emit("identity.revoked", {
                    "case_id": case_id, "hospital_id": old_hospital, "reason": f"改道：{reason}"})
                tx.emit("identity.granted", {
                    "grant_id": new_id("grant"), "case_id": case_id,
                    "hospital_id": target_id,
                    "stage": "IN_TRANSIT" if self._departed(case) else "ACCEPTED",
                    "reason": f"改道接收：{reason}", "granted_at": iso(moment)})

            eta = moment + timedelta(minutes=route["adjusted_travel_minutes"])
            remaining = int((parse_iso(case["ischemia"]["deadline"]) - eta).total_seconds() // 60)
            number = len(plan["versions"]) + 1
            version = self._plan_version(number, target, route, eta, remaining, moment,
                                         None, reason=reason, decided_by=decided_by,
                                         manual_override=manual)
            tx.emit("plan.rerouted", {
                "plan_id": plan_id, "case_id": case_id,
                "hospital_id": target_id, "lease_id": new_lease_id, "version": version,
            })
            self._window_risk_items(tx, case, eta, target_id)
            if client_key:
                tx.emit("idempotency.recorded", {
                    "case_id": case_id, "client_event_id": client_key,
                    "kind": "REROUTE", "ref_id": plan_id})
            self._persist(tx)
            result = {**self._state["plans"][plan_id]}
            if client_key:
                result["deduplicated"] = False
            return result

    def _window_risk_items(self, tx: Tx, case: dict, eta: datetime, hospital_id: str) -> None:
        deadline = parse_iso(case["ischemia"]["deadline"])
        active = self._active_risk(case["case_id"], RISK_ETA_WINDOW)
        if eta > deadline:
            over = int((eta - deadline).total_seconds() // 60)
            if not active:
                tx.raise_risk(
                    case["case_id"], RISK_ETA_WINDOW, "CRITICAL",
                    f"预计到达 {hospital_id} 超出缺血窗口 {over} 分钟，已升级人工调度", iso(tx.at),
                    context={"eta": iso(eta), "hospital_id": hospital_id})
        elif active:
            tx.resolve(case["case_id"], active["risk_id"])

    def get_plan(self, case_id: str) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            if not case.get("plan_id"):
                raise DomainError("NO_PLAN", "尚无转运计划", 404)
            return self._state["plans"][case["plan_id"]]

    # ===== 里程碑（弱网补传 / 幂等） =====

    def record_milestone(self, case_id: str, data: dict) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            code = (data.get("code") or "").upper()
            if code not in MILESTONE_ORDER:
                raise DomainError("INVALID_MILESTONE", f"未知里程碑 {code}", 400)
            moment = parse_iso(data["at"]) if data.get("at") else self.clock()
            client_key = data.get("client_event_id")

            if client_key:
                dup = self._idem_result(case_id, client_key)
                if dup:
                    return dup
            else:
                # 无客户端键时，同病例同里程碑 60 秒内的重复上报也按重传处理
                for m in self._state["milestones"].get(case_id, []):
                    if m["code"] == code and abs(
                            (parse_iso(m["at"]) - moment).total_seconds()) <= 60:
                        return {**m, "deduplicated": True}

            milestone = {
                "milestone_id": new_id("ms"), "case_id": case_id, "code": code,
                "at": iso(moment), "note": data.get("note", ""),
                "location": data.get("location"),
                "client_event_id": client_key,
            }
            tx = Tx(self, moment)
            tx.emit("milestone.recorded", {"case_id": case_id, "milestone": milestone})

            if code in ("DEPARTED", "ENROUTE", "WAYPOINT") and case.get("plan_id"):
                self._grant_stage_if_lower(tx, case, "IN_TRANSIT")
                plan = self._state["plans"][case["plan_id"]]
                lease = self._state["leases"].get(plan["lease_id"])
                if lease and lease["status"] == "HELD":
                    tx.emit("lease.confirmed", {
                        "lease_id": lease["lease_id"], "at": iso(moment),
                        "new_expires_at": iso(moment + self.lease_ttl)})

            if code == "ARRIVED" and case.get("plan_id"):
                plan = self._state["plans"][case["plan_id"]]
                lease = self._state["leases"].get(plan["lease_id"])
                if lease and lease["status"] == "HELD":
                    tx.emit("lease.consumed", {"lease_id": lease["lease_id"]})
                tx.emit("plan.status_changed", {"plan_id": case["plan_id"], "status": "ARRIVED"})
                self._grant_stage_if_lower(tx, case, "RECEIVING")

            if code == "REVASCULARIZATION_STARTED" and case.get("plan_id"):
                self._grant_stage_if_lower(tx, case, "RECEIVING")

            if client_key:
                tx.emit("idempotency.recorded", {
                    "case_id": case_id, "client_event_id": client_key,
                    "kind": "MILESTONE", "ref_id": milestone["milestone_id"]})

            self._persist(tx)

            remaining_minutes = data.get("remaining_minutes_estimate")
            if remaining_minutes is not None and case.get("plan_id"):
                window_tx = Tx(self, moment)
                eta = moment + timedelta(minutes=float(remaining_minutes))
                self._window_risk_items(window_tx, case, eta,
                                        self._state["plans"][case["plan_id"]]["hospital_id"])
                self._persist(window_tx)

            return {**milestone, "deduplicated": False}

    def _idem_result(self, case_id: str, client_key: str) -> dict | None:
        record = self._state["idem"].get(f"{case_id}:{client_key}")
        if not record:
            return None
        if record["kind"] == "MILESTONE":
            ms = next(m for m in self._state["milestones"][case_id]
                      if m["milestone_id"] == record["ref_id"])
            return {**ms, "deduplicated": True}
        plan = self._state["plans"].get(record["ref_id"])
        return {**plan, "deduplicated": True} if plan else None

    def _grant_stage_if_lower(self, tx: Tx, case: dict, stage: str) -> None:
        plan = self._state["plans"][case["plan_id"]]
        hospital_id = plan["hospital_id"]
        current = [g for g in self._state["identity"].get(case["case_id"], [])
                   if g["hospital_id"] == hospital_id and g["active"]]
        if current and STAGE_RANK.get(current[-1]["stage"], 0) >= STAGE_RANK[stage]:
            return
        tx.emit("identity.granted", {
            "grant_id": new_id("grant"), "case_id": case["case_id"],
            "hospital_id": hospital_id, "stage": stage,
            "reason": f"行程阶段推进：{stage}", "granted_at": iso(tx.at)})

    def list_milestones(self, case_id: str) -> list[dict]:
        with self._lock:
            self._require_case(case_id)
            return list(self._state["milestones"].get(case_id, []))

    # ===== 身份分阶段开放 =====

    def identity_view(self, case_id: str, hospital_id: str) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            self._require_hospital(hospital_id)
            grants = [g for g in self._state["identity"].get(case_id, [])
                      if g["hospital_id"] == hospital_id and g["active"]]
            if not grants:
                return {"visible": False, "case_id": case_id, "hospital_id": hospital_id,
                        "stage": None,
                        "message": "该机构未实际参与本病例救治，身份信息不开放"}
            stage = max(grants, key=lambda g: STAGE_RANK[g["stage"]])["stage"]
            injury = case["injury"]
            view = {
                "visible": True, "case_id": case_id, "hospital_id": hospital_id, "stage": stage,
                "case_code": case["case_code"],
                "patient": {"pseudonym": case["patient"]["pseudonym"]},
                "injury": {"part": injury["part"], "amputated_at": injury["amputated_at"],
                           "preservation": injury["preservation"],
                           "vessel_caliber_mm": injury["vessel_caliber_mm"]},
                "ischemia_deadline": case["ischemia"]["deadline"],
                "referring_hospital_id": case["referring_hospital_id"],
            }
            if stage in ("IN_TRANSIT", "INTAKE", "RECEIVING"):
                view["patient"].update({"age": case["patient"]["age"], "sex": case["patient"]["sex"]})
                view["vitals"] = case["vitals"]
            if stage in ("RECEIVING", "INTAKE"):
                view["patient"].update({
                    "name": case["patient"]["name"],
                    "id_number": case["patient"]["id_number"],
                    "contact": case["patient"]["contact"],
                })
            return view

    # ===== 超时清扫：邀约 / 租约 / 道路事件 / 心跳失联 =====

    def sweep(self, at: str | None = None) -> dict:
        with self._lock:
            moment = parse_iso(at) if at else self.clock()
            tx = Tx(self, moment)
            summary = self._sweep(tx)
            self._persist(tx)
            return summary

    def _sweep(self, tx: Tx, only_offers_traffic: bool = False) -> dict:
        """把到期事件追加到 Tx。only_offers_traffic 供筛查时轻量调用。"""
        moment = tx.at
        expired_offers, expired_leases, expired_traffic, contact_cases = [], [], [], set()

        for oid, offer in list(self._state["offers"].items()):
            if offer["status"] == "PENDING" and moment >= parse_iso(offer["expires_at"]):
                tx.emit("offer.expired", {"offer_id": oid})
                expired_offers.append(oid)

        for lid, lease in list(self._state["leases"].items()):
            if lease["status"] == "HELD" and moment >= parse_iso(lease["last_confirmed_at"]) + self.lease_ttl:
                tx.emit("lease.expired", {"lease_id": lid})
                expired_leases.append(lid)
                case = self._state["cases"].get(lease["case_id"])
                if case and case.get("plan_id"):
                    tx.raise_risk(
                        lease["case_id"], RISK_LEASE_LOST, "CRITICAL",
                        f"{lease['hospital_id']} 资源租约超时失联，锁定已释放，需人工调度",
                        iso(moment), context={"lease_id": lid})

        for tid, ev in list(self._state["traffic"].items()):
            if ev["active"] and parse_iso(ev["observed_at"]) + timedelta(minutes=ev["ttl_minutes"]) < moment:
                tx.emit("traffic.expired", {"traffic_event_id": tid})
                expired_traffic.append(tid)

        if not only_offers_traffic:
            touched: dict[str, set[str]] = {}
            for cid in self._state["cases"]:
                linked: set[str] = set()
                for offer in self._state["offers"].values():
                    if offer["case_id"] == cid and offer["status"] in ("PENDING", "ACCEPTED"):
                        hospital = self._state["hospitals"].get(offer["hospital_id"])
                        if hospital and self._heartbeat_stale(hospital, moment):
                            linked.add(hospital["hospital_id"])
                for lease in self._state["leases"].values():
                    if (lease["case_id"] == cid and lease["status"] in ("HELD", "CONSUMED")
                            and self._heartbeat_stale(self._state["hospitals"][lease["hospital_id"]], moment)):
                        linked.add(lease["hospital_id"])
                if linked:
                    touched[cid] = linked
            for cid, linked in touched.items():
                before = {r["risk_id"] for r in self._state["cases"][cid]["risks"]}
                self._sync_contact_lost(tx, cid, linked)
                after_new = {
                    p["risk"]["risk_id"] for kind, p in tx.items
                    if kind == "risk.raised" and p["case_id"] == cid
                }
                if after_new - before:
                    contact_cases.add(cid)
            # 心跳恢复
            for cid, case in self._state["cases"].items():
                active = self._active_risk(cid, RISK_CONTACT_LOST)
                if not active or cid in touched:
                    continue
                still_lost = {hid for hid in active["hospital_ids"]
                              if self._heartbeat_stale(self._state["hospitals"][hid], moment)}
                if not still_lost:
                    tx.resolve(cid, active["risk_id"])
                elif still_lost != set(active["hospital_ids"]):
                    tx.raise_risk(cid, RISK_CONTACT_LOST, "CRITICAL",
                                  f"资源回报失联机构：{'、'.join(sorted(still_lost))}", iso(moment),
                                  hospital_ids=sorted(still_lost), replaces=active["risk_id"])

        return {"swept_at": iso(moment), "expired_offers": expired_offers,
                "expired_leases": expired_leases, "expired_traffic": expired_traffic,
                "contact_lost_cases": sorted(contact_cases)}

    # ===== 结单 / 查询 =====

    def close_case(self, case_id: str, outcome: str | None = None) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            if case["status"] == "CLOSED":
                raise DomainError("CASE_CLOSED", "病例已关闭", 409)
            tx = Tx(self, self.clock())
            tx.emit("case.closed", {"case_id": case_id, "outcome": outcome})
            for lease in self._state["leases"].values():
                if lease["case_id"] == case_id and lease["status"] == "HELD":
                    tx.emit("lease.released", {"lease_id": lease["lease_id"], "reason": "CASE_CLOSED"})
            for grant in self._state["identity"].get(case_id, []):
                if grant["active"]:
                    tx.emit("identity.revoked", {
                        "case_id": case_id, "hospital_id": grant["hospital_id"], "reason": "病例结单"})
            if case.get("plan_id"):
                tx.emit("plan.status_changed", {"plan_id": case["plan_id"], "status": "CLOSED"})
            self._persist(tx)
            return self._state["cases"][case_id]

    def get_case(self, case_id: str) -> dict:
        with self._lock:
            return self._require_case(case_id)

    def list_cases(self) -> list[dict]:
        with self._lock:
            return [self._state["cases"][cid] for cid in sorted(self._state["cases"])]

    def audit_trail(self, case_id: str) -> dict:
        with self._lock:
            self._require_case(case_id)
            events = self.event_log(case_id)
        return {
            "case_id": case_id,
            "entries": [
                {"event_id": e["id"], "at": e["at"], "kind": e["kind"], "payload": e["payload"]}
                for e in events
            ],
        }

    def explanation(self, case_id: str) -> dict:
        """返回推荐依据：最新筛查、候选排序因素、计划各版本依据与活动风险。"""
        with self._lock:
            case = self._require_case(case_id)
            screen = self._state["last_screen"].get(case_id)
            plan = self._state["plans"].get(case["plan_id"]) if case.get("plan_id") else None
            return {
                "case_id": case_id,
                "case_code": case["case_code"],
                "screen": screen,
                "plan_versions": plan["versions"] if plan else [],
                "active_risks": [
                    {"risk_id": r["risk_id"], "type": r["type"], "severity": r["severity"],
                     "message": r["message"], "raised_at": r["raised_at"], "ack_at": r["ack_at"]}
                    for r in case["risks"] if r["active"]
                ],
                "disclaimer": DISCLAIMER,
            }
