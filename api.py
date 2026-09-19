"""急救编排 HTTP 接口（Python 标准库，无第三方依赖）。

所有时间字段使用 Unix 秒；登记时也可用 amputation_minutes_ago 相对时钟。
推荐、复算等响应统一携带非诊断声明 notice。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from domain import (
    Injury,
    Patient,
    Vitals,
    Preservation,
    TrafficEvent,
    to_jsonable,
)
from orchestrator import DomainError, Orchestrator


def _json_get(payload, key, default=None, required=False):
    value = payload.get(key, default)
    if required and value is None:
        raise DomainError(f"缺少必填字段 {key}", code="bad_request", http_status=400)
    return value


def _parse_case(payload, now):
    minutes_ago = payload.get("amputation_minutes_ago")
    amputated_at = payload.get("amputated_at")
    if amputated_at is None:
        if minutes_ago is None:
            raise DomainError(
                "需提供 amputated_at 或 amputation_minutes_ago",
                code="bad_request",
            )
        amputated_at = now - float(minutes_ago) * 60

    patient_data = payload.get("patient") or {}
    patient = Patient(
        pseudonym=_json_get(patient_data, "pseudonym", required=True),
        real_name=patient_data.get("real_name"),
        id_number=patient_data.get("id_number"),
        contact=patient_data.get("contact"),
        age=patient_data.get("age"),
        sex=patient_data.get("sex"),
    )

    preservation = payload.get("preservation", Preservation.WARM.value)
    if preservation not in {p.value for p in Preservation}:
        raise DomainError(f"未知保存条件 {preservation}", code="bad_request")

    injury = Injury(
        body_part=_json_get(payload, "body_part", required=True),
        amputated_at=float(amputated_at),
        preservation=preservation,
        vessel_diameter_mm=payload.get("vessel_diameter_mm"),
        required_equipment=list(payload.get("required_equipment") or []),
        side=payload.get("side"),
        notes=payload.get("notes"),
    )

    vitals_data = payload.get("vitals") or {}
    vitals = Vitals(
        level=vitals_data.get("level", "stable"),
        sbp_mmhg=vitals_data.get("sbp_mmhg"),
        dbp_mmhg=vitals_data.get("dbp_mmhg"),
        hr_per_min=vitals_data.get("hr_per_min"),
        spo2=vitals_data.get("spo2"),
        gcs=vitals_data.get("gcs"),
        notes=vitals_data.get("notes"),
    )
    return patient, injury, vitals


def create_handler(orchestrator: Orchestrator):
    class Handler(BaseHTTPRequestHandler):
        # ------------------------------------------------------------
        # 基础收发
        # ------------------------------------------------------------

        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                raise DomainError("请求体不是合法 JSON", code="bad_request")

        def log_message(self, *_args):
            return

        # ------------------------------------------------------------
        # 路由
        # ------------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                self._route(method, path, query)
            except DomainError as error:
                self._send(error.http_status, {"error": error.code, "message": str(error)})
            except KeyError as error:
                self._send(404, {"error": "not_found", "message": str(error).strip("'")})

        def _route(self, method, path, query):
            # 健康检查
            if method == "GET" and path == "/health":
                from service import health_payload
                self._send(200, health_payload())
                return

            parts = [p for p in path.split("/") if p]

            if method == "GET" and path == "/cases":
                self._list_cases()
                return
            if method == "POST" and path == "/cases":
                self._register_case()
                return
            if method == "POST" and path == "/admin/capabilities":
                self._update_capability()
                return
            if method == "POST" and path == "/admin/heartbeats":
                self._heartbeat()
                return
            if method == "POST" and path == "/admin/traffic-events":
                self._add_traffic_event()
                return
            if (
                method == "POST"
                and len(parts) == 4
                and parts[:2] == ["admin", "traffic-events"]
                and parts[3] == "clear"
            ):
                self._clear_traffic_event(parts[2])
                return
            if method == "GET" and path == "/leases":
                self._list_leases()
                return
            if method == "GET" and path == "/network":
                self._send(200, orchestrator.network_view())
                return
            if method == "GET" and path == "/routes":
                from_id = (query.get("from") or ["H0"])[0]
                self._send(200, orchestrator.routes_view(from_id))
                return
            if method == "GET" and path == "/escalations":
                self._send(200, {"escalations": orchestrator.open_escalations()})
                return

            if len(parts) >= 2 and parts[0] == "cases":
                case_id = parts[1]
                if method == "GET" and len(parts) == 2:
                    self._get_case(case_id)
                    return
                if method == "GET" and len(parts) == 3 and parts[2] == "clock":
                    self._case_clock(case_id)
                    return
                if method == "POST" and len(parts) == 3 and parts[2] == "recommendations":
                    self._recommend(case_id)
                    return
                if method == "POST" and len(parts) == 3 and parts[2] == "accept":
                    self._accept(case_id)
                    return
                if method == "POST" and len(parts) == 3 and parts[2] == "milestones":
                    self._milestone(case_id)
                    return
                if method == "POST" and len(parts) == 3 and parts[2] == "reroute":
                    self._reroute(case_id)
                    return
                if method == "POST" and len(parts) == 3 and parts[2] == "reassess":
                    self._reassess(case_id)
                    return
                if method == "GET" and len(parts) == 3 and parts[2] == "plans":
                    self._plans(case_id)
                    return
                if method == "GET" and len(parts) == 3 and parts[2] == "identity":
                    self._identity(case_id, query)
                    return
                if method == "POST" and len(parts) == 5 and parts[2] == "escalations" and parts[4] == "ack":
                    self._ack_escalation(case_id, parts[3])
                    return

            self._send(404, {"error": "not_found", "message": f"未知路径 {path}"})

        # ------------------------------------------------------------
        # 案例与时钟
        # ------------------------------------------------------------

        def _register_case(self):
            payload = self._read_json()
            now = orchestrator.clock.now()
            patient, injury, vitals = _parse_case(payload, now)
            case = orchestrator.register_case(
                origin_hospital_id=_json_get(payload, "origin_hospital_id", required=True),
                recorded_by=_json_get(payload, "recorded_by", default="值班员"),
                patient=patient,
                injury=injury,
                vitals=vitals,
            )
            self._send(201, to_jsonable(case))

        def _list_cases(self):
            cases = orchestrator.list_cases()
            self._send(200, {"cases": [to_jsonable(c) for c in cases]})

        def _get_case(self, case_id):
            self._send(200, to_jsonable(orchestrator.get_case(case_id)))

        def _case_clock(self, case_id):
            self._send(200, orchestrator.case_clock(case_id))

        # ------------------------------------------------------------
        # 推荐、接受、里程碑、改道、复算
        # ------------------------------------------------------------

        def _recommend(self, case_id):
            recset = orchestrator.recommend(case_id)
            self._send(200, to_jsonable(recset))

        def _accept(self, case_id):
            payload = self._read_json()
            plan = orchestrator.accept(
                case_id,
                _json_get(payload, "hospital_id", required=True),
                by=_json_get(payload, "by", default="接收医院值班"),
                team_id=payload.get("team_id"),
                table_id=payload.get("table_id"),
            )
            self._send(201, to_jsonable(plan))

        def _milestone(self, case_id):
            payload = self._read_json()
            result = orchestrator.record_milestone(
                case_id,
                client_event_id=_json_get(payload, "client_event_id", required=True),
                code=_json_get(payload, "code", required=True),
                occurred_at=payload.get("occurred_at"),
                location=payload.get("location"),
                note=payload.get("note"),
                carrier_hospital_id=payload.get("carrier_hospital_id"),
                by=payload.get("by"),
            )
            self._send(
                200,
                {"milestone": to_jsonable(result.milestone), "duplicate": result.duplicate},
            )

        def _reroute(self, case_id):
            payload = self._read_json()
            recset = orchestrator.decide_reroute(
                case_id,
                reason=_json_get(payload, "reason", required=True),
                by=_json_get(payload, "by", default="人工调度"),
                client_event_id=payload.get("client_event_id"),
                occurred_at=payload.get("occurred_at"),
            )
            self._send(200, to_jsonable(recset))

        def _reassess(self, case_id):
            self._send(200, orchestrator.reassess_active_plan(case_id))

        def _plans(self, case_id):
            plans = orchestrator.plan_history(case_id)
            self._send(200, {"plans": [to_jsonable(p) for p in plans]})

        def _identity(self, case_id, query):
            hospital_id = (query.get("hospital_id") or [None])[0]
            if not hospital_id:
                raise DomainError("查询需带 hospital_id", code="bad_request")
            self._send(200, orchestrator.identity_view(case_id, hospital_id))

        def _ack_escalation(self, case_id, escalation_id):
            payload = self._read_json()
            esc = orchestrator.acknowledge_escalation(
                case_id,
                escalation_id,
                by=_json_get(payload, "by", default="人工调度"),
                note=payload.get("note", ""),
            )
            self._send(200, to_jsonable(esc))

        def _list_leases(self):
            self._send(200, {"leases": [to_jsonable(l) for l in orchestrator.active_leases()]})

        # ------------------------------------------------------------
        # 网络运行数据录入（值班员/机构上报）
        # ------------------------------------------------------------

        def _update_capability(self):
            payload = self._read_json()
            now = orchestrator.clock.now()
            hospital_id = _json_get(payload, "hospital_id", required=True)
            orchestrator.catalog.update_capability(
                hospital_id,
                microsurgery=payload.get("microsurgery"),
                supported_parts=payload.get("supported_parts"),
                min_vessel_mm=payload.get("min_vessel_mm"),
                equipment=payload.get("equipment"),
                observed_at=payload.get("observed_at", now),
            )
            self._send(200, {"status": "updated", "hospital_id": hospital_id})

        def _heartbeat(self):
            payload = self._read_json()
            now = orchestrator.clock.now()
            kind = _json_get(payload, "kind", required=True)
            target_id = _json_get(payload, "id", required=True)
            if kind == "team":
                orchestrator.catalog.heartbeat_team(
                    target_id, now, on_duty=payload.get("on_duty")
                )
            elif kind == "table":
                orchestrator.catalog.heartbeat_table(
                    target_id, now, available=payload.get("available")
                )
            else:
                raise DomainError("kind 仅支持 team/table", code="bad_request")
            self._send(200, {"status": "ok", "kind": kind, "id": target_id, "at": now})

        def _add_traffic_event(self):
            payload = self._read_json()
            now = orchestrator.clock.now()
            event = TrafficEvent(
                id=_json_get(payload, "id", required=True),
                route_key=_json_get(payload, "route_key", required=True),
                title=_json_get(payload, "title", required=True),
                delay_minutes=float(_json_get(payload, "delay_minutes", required=True)),
                status=payload.get("status", "active"),
                observed_at=payload.get("observed_at", now),
            )
            orchestrator.catalog.report_traffic_event(event)
            self._send(201, to_jsonable(event))

        def _clear_traffic_event(self, event_id):
            orchestrator.catalog.clear_traffic_event(event_id, orchestrator.clock.now())
            self._send(200, {"status": "cleared", "id": event_id})

    return Handler
