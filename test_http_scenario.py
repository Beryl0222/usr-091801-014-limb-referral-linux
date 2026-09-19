"""值班员视角的 HTTP 端到端场景。

两例急诊经真实 HTTP 接口并发生成推荐与确认；弱网重复补传不改状态；
断网恢复后提交改道，旧计划经接口仍可查；身份按阶段开放。
"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from api import create_handler
from domain import MutableClock
from orchestrator import Orchestrator
from bootstrap import build_network
from testsupport import T0


def _request(base, method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as error:
        return error.code, json.loads(error.read().decode())


class HttpScenarioTest(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(T0)
        catalog = build_network(T0)
        self.orch = Orchestrator(catalog, clock=self.clock)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.orch))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _register(self, pseudonym, minutes_ago):
        return _request(self.base, "POST", "/cases", {
            "origin_hospital_id": "H0",
            "recorded_by": "值班员甲",
            "amputation_minutes_ago": minutes_ago,
            "body_part": "forearm",
            "vessel_diameter_mm": 2.0,
            "patient": {"pseudonym": pseudonym, "real_name": "张三",
                        "id_number": "110101199001011234", "age": 42, "sex": "M"},
            "vitals": {"level": "stable", "spo2": 97},
        })

    def test_full_dual_emergency_workflow_over_http(self):
        status, case_a = self._register("P-A", 30)
        self.assertEqual(status, 201)
        status, case_b = self._register("P-B", 45)
        self.assertEqual(status, 201)

        # 案例时钟。
        _, clock_a = _request(self.base, "GET", f"/cases/{case_a['id']}/clock")
        _, clock_b = _request(self.base, "GET", f"/cases/{case_b['id']}/clock")
        self.assertEqual(clock_a["remaining_min"], 330.0)
        self.assertEqual(clock_b["remaining_min"], 315.0)

        # 两例推荐。
        _, rec_a = _request(self.base, "POST", f"/cases/{case_a['id']}/recommendations")
        _, rec_b = _request(self.base, "POST", f"/cases/{case_b['id']}/recommendations")
        for recset in (rec_a, rec_b):
            self.assertEqual(recset["recommendations"][0]["hospital_id"], "H1")
            self.assertIn("不构成诊断", recset["notice"])
            # 推荐解释含四大维度因子。
            names = {f["name"] for f in recset["recommendations"][0]["factors"]}
            self.assertIn("缺血窗口", names)
            self.assertIn("道路环境", names)

        # 并发确认 H1：HTTP 层同样不得双占。
        plans = {}
        statuses = {}

        def accept(case_key, case_id):
            code, body = _request(self.base, "POST", f"/cases/{case_id}/accept",
                                  {"hospital_id": "H1", "by": "市一值班"})
            statuses[case_key] = code
            plans[case_key] = body

        threads = [
            threading.Thread(target=accept, args=("A", case_a["id"])),
            threading.Thread(target=accept, args=("B", case_b["id"])),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(list(statuses.values()), [201, 201])
        pairs = {
            (plans["A"]["team_id"], plans["A"]["table_id"]),
            (plans["B"]["team_id"], plans["B"]["table_id"]),
        }
        self.assertEqual(pairs, {("T1A", "OR1"), ("T1B", "OR2")})

        _, leases = _request(self.base, "GET", "/leases")
        self.assertEqual(len(leases["leases"]), 2)

        # 未参与机构无身份；接收方只见摘要。
        _, identity_h3 = _request(
            self.base, "GET", f"/cases/{case_a['id']}/identity?hospital_id=H3"
        )
        self.assertEqual(identity_h3["patient"]["view"], "none")
        _, identity_h1 = _request(
            self.base, "GET", f"/cases/{case_a['id']}/identity?hospital_id=H1"
        )
        self.assertEqual(identity_h1["patient"]["view"], "summary")
        self.assertNotIn("real_name", identity_h1["patient"])

        # 弱网：同一里程碑补传两次。
        milestone_payload = {
            "client_event_id": "evt-http-1", "code": "departed",
            "carrier_hospital_id": "EMS1",
        }
        _, m1 = _request(self.base, "POST", f"/cases/{case_a['id']}/milestones",
                         milestone_payload)
        _, m2 = _request(self.base, "POST", f"/cases/{case_a['id']}/milestones",
                         milestone_payload)
        self.assertFalse(m1["duplicate"])
        self.assertTrue(m2["duplicate"])
        self.assertEqual(m1["milestone"]["client_event_id"],
                         m2["milestone"]["client_event_id"])

        # 时间推进，机构保持心跳；断网恢复后提交改道。
        self.clock.advance_minutes(25)
        now = self.clock.now()
        _request(self.base, "POST", "/admin/heartbeats",
                 {"kind": "team", "id": "T2", "on_duty": True})
        _request(self.base, "POST", "/admin/heartbeats",
                 {"kind": "table", "id": "OR3", "available": True})
        # 人工调度核实 H2 能力资料仍有效，刷新资料时间戳。
        code, _ = _request(self.base, "POST", "/admin/capabilities",
                           {"hospital_id": "H2"})
        self.assertEqual(code, 200)
        _request(self.base, "POST", "/admin/traffic-events", {
            "id": "E8", "route_key": "H0->H1",
            "title": "隧道事故加剧拥堵", "delay_minutes": 25,
        })
        reroute_payload = {
            "reason": "隧道事故加剧拥堵，改往省骨科",
            "by": "调度组长", "client_event_id": "evt-http-rr",
        }
        _, rr1 = _request(self.base, "POST", f"/cases/{case_a['id']}/reroute",
                          reroute_payload)
        # 客户端没收到响应，重连后原样重发：不产生第二版结论。
        _, rr2 = _request(self.base, "POST", f"/cases/{case_a['id']}/reroute",
                          reroute_payload)
        self.assertEqual(rr1["id"], rr2["id"])

        _, body = _request(self.base, "POST", f"/cases/{case_a['id']}/accept",
                           {"hospital_id": "H2", "by": "省骨值班"})
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["predecessor_plan_id"], plans["A"]["id"])

        # 原计划仍可查，理由保留；路线快照不可变。
        _, history = _request(self.base, "GET", f"/cases/{case_a['id']}/plans")
        versions = [(p["version"], p["state"]) for p in history["plans"]]
        self.assertEqual(versions, [(1, "superseded"), (2, "active")])
        self.assertEqual(history["plans"][0]["reroute_reason"],
                         "隧道事故加剧拥堵，改往省骨科")
        self.assertEqual(history["plans"][0]["route"]["road"], "城北快速路")

        # 旧接收方授权已收回。
        _, identity_after = _request(
            self.base, "GET", f"/cases/{case_a['id']}/identity?hospital_id=H1"
        )
        self.assertEqual(identity_after["patient"]["view"], "none")

    def test_operator_views_network_routes_and_escalations(self):
        # 能力图：三家候选 + 接诊点，含心跳时效与占用字段。
        _, network = _request(self.base, "GET", "/network")
        ids = {h["id"] for h in network["hospitals"]}
        self.assertEqual(ids, {"H0", "H1", "H2", "H3"})
        h3 = next(h for h in network["hospitals"] if h["id"] == "H3")
        self.assertTrue(h3["capability_stale"])
        self.assertEqual(
            {(t["id"], t["heartbeat_fresh"]) for t in h3["teams"]},
            {("T3", False)},
        )
        self.assertIn({"id": "EMS1", "name": "市急救中心城东分站"}, network["carriers"])

        # 交通看板：ETA 含天气折算与事件延误。
        _, routes = _request(self.base, "GET", "/routes?from=H0")
        h1_route = next(r for r in routes["routes"] if r["hospital_id"] == "H1")
        self.assertEqual(h1_route["travel_minutes"], 50.0)
        self.assertEqual(h1_route["eta_minutes"], 60.0)
        self.assertEqual(h1_route["events"][0]["id"], "E1")

        # 一例越窗案例产生未结升级单。
        self._register("P-D", 310)
        # 找到该案例 id 后触发推荐。
        _, listing = _request(self.base, "GET", "/cases")
        case_d = next(c for c in listing["cases"] if c["patient"]["pseudonym"] == "P-D")
        code, recset = _request(self.base, "POST", f"/cases/{case_d['id']}/recommendations")
        self.assertEqual(code, 200)
        self.assertTrue(recset["escalated"])
        _, escalations = _request(self.base, "GET", "/escalations")
        self.assertTrue(escalations["escalations"])
        top = escalations["escalations"][0]
        self.assertEqual(top["status"], "open")
        self.assertIn("arrival_window_exceeded", top["reasons"])
        self.assertEqual(top["case_id"], case_d["id"])

    def test_all_blocked_returns_escalation_ready_for_human_dispatch(self):
        status, case_c = self._register("P-C", 310)
        self.assertEqual(status, 201)
        _, recset = _request(self.base, "POST", f"/cases/{case_c['id']}/recommendations")
        self.assertTrue(recset["escalated"])
        _, case_view = _request(self.base, "GET", f"/cases/{case_c['id']}")
        self.assertTrue(case_view["escalations"])
        self.assertEqual(case_view["escalations"][-1]["status"], "open")


if __name__ == "__main__":
    unittest.main()
