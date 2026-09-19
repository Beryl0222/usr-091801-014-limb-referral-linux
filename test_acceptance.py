"""验收测试：对应值班员的真实举证场景。

覆盖：
- 能力图/交通/案例时钟同时驱动两例急诊，推荐依据可解释；
- 并发接受时同一团队或手术台不会被重复占用，失败方醒目标记并升级人工调度；
- 能力资料过期、资源失联、预计到达越窗的风险与升级/解除；
- 只有机构明确接受并锁定资源后才能给出转运计划；
- 弱网里程碑按幂等键补传，重复消息不改变状态；
- 改道保留原计划、原因与决定人，重连后重放不产生新版本；
- 患者身份按阶段仅对实际参与机构开放，改道/结单即收回；
- 事件日志重放后全部状态（含原计划与改道）可查。
"""

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engine import (
    DISCLAIMER,
    DomainError,
    Orchestrator,
)
from service import make_handler

T0 = "2026-09-19T08:00:00Z"


def _hospital(hid, name, teams=1, rooms=1, parts=None, caliber=0.8, updated_at=T0):
    parts = parts or ["HAND", "FOREARM"]
    return {
        "hospital_id": hid,
        "name": name,
        "capability": {"parts": parts, "min_vessel_mm": caliber,
                       "services_247": ["MICROSURGERY_247"]},
        "teams": [
            {"team_id": f"{hid}_T{i + 1}", "caliber_min_mm": caliber, "parts": parts}
            for i in range(teams)
        ],
        "operating_rooms": [
            {"room_id": f"{hid}_R{i + 1}", "caliber_min_mm": caliber, "parts": parts}
            for i in range(rooms)
        ],
        "updated_at": updated_at,
    }


def _route(hid, minutes, km=None, origin="H0", weather="CLEAR"):
    return {
        "origin": {"hospital_id": origin},
        "destination_hospital_id": hid,
        "travel_minutes": minutes,
        "distance_km": km if km is not None else minutes * 0.8,
        "weather": {"condition": weather, "observed_at": T0},
    }


def _case(amputated_at="2026-09-19T07:00:00Z", part="HAND", caliber=1.0,
          preservation="WARM", referring=None, pickup_hospital="H0", patient=None):
    return {
        "referring_hospital_id": referring,
        "pickup": {"hospital_id": pickup_hospital},
        "patient": patient or {
            "name": "张三", "id_number": "110101199001011234",
            "contact": "13900000000", "age": 34, "sex": "M",
        },
        "injury": {"part": part, "amputated_at": amputated_at,
                   "preservation": preservation, "vessel_caliber_mm": caliber},
    }


class NetworkFixture:
    """按表快速搭建一张区域救治网络。"""

    def __init__(self, engine=None):
        self.e = engine or Orchestrator()

    def add(self, spec, minutes, heartbeat=T0, weather="CLEAR", origin="H0"):
        self.e.register_hospital(spec)
        if heartbeat:
            self.e.heartbeat(spec["hospital_id"], heartbeat)
        self.e.upsert_route(_route(spec["hospital_id"], minutes, weather=weather, origin=origin))
        return self

    def case(self, **kwargs):
        return self.e.create_case(_case(**kwargs))


class ScreeningTest(unittest.TestCase):
    def setUp(self):
        self.fx = NetworkFixture()
        self.fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40)
        self.fx.add(_hospital("H_B", "县二院"), 55)
        self.fx.add(_hospital("H_C", "远郊中心"), 120)

    def test_ranks_by_adjusted_travel_within_ischemia_window(self):
        case = self.fx.case()
        screen = self.fx.e.screen_case(case["case_id"], T0)
        self.assertEqual([c["hospital_id"] for c in screen["candidates"]], ["H_A", "H_B", "H_C"])
        self.assertEqual(screen["ischemia_deadline"], "2026-09-19T13:00:00Z")
        first = screen["candidates"][0]
        self.assertEqual(first["minutes_remaining_on_arrival"], 260)
        factor_names = [f["factor"] for f in first["factors"]]
        self.assertEqual(factor_names[0], "基础车程")
        self.assertIn("到达后缺血余量", factor_names)
        self.assertIn("不构成诊断建议", screen["disclaimer"])

    def test_weather_and_road_events_change_eta_and_ranking(self):
        # H_A 路线雷暴 + 拥堵，调整后车程 40*1.4*1.35 ≈ 76 分钟，被 H_B 反超
        self.fx.e.upsert_route(_route("H_A", 40, weather="THUNDERSTORM"))
        self.fx.e.report_traffic({
            "origin": {"hospital_id": "H0"}, "destination_hospital_id": "H_A",
            "severity": "CONGESTED", "summary": "早高峰拥堵", "observed_at": T0})
        case = self.fx.case()
        screen = self.fx.e.screen_case(case["case_id"], T0)
        a = next(c for c in screen["candidates"] if c["hospital_id"] == "H_A")
        self.assertEqual(a["route"]["adjusted_travel_minutes"], 76)
        self.assertTrue(any(f["factor"] == "道路事件" for f in a["factors"]))
        self.assertEqual([c["hospital_id"] for c in screen["candidates"]][0], "H_B")
        # 推荐依据必须可解释：被天气与道路拖慢的 H_A 因素里保留了完整计算链
        explanation = self.fx.e.explanation(case["case_id"])
        a_explained = next(c for c in explanation["screen"]["candidates"]
                           if c["hospital_id"] == "H_A")
        factors = [f["factor"] for f in a_explained["factors"]]
        self.assertIn("天气", factors)
        self.assertIn("道路事件", factors)
        self.assertIn("到达后缺血余量", factors)
        self.assertIn("不构成诊断建议", explanation["disclaimer"])

    def test_blocked_road_rejects_candidate(self):
        self.fx.e.report_traffic({
            "origin": {"hospital_id": "H0"}, "destination_hospital_id": "H_A",
            "severity": "BLOCKED", "passable": False, "summary": "事故封路", "observed_at": T0})
        case = self.fx.case()
        screen = self.fx.e.screen_case(case["case_id"], T0)
        self.assertNotIn("H_A", [c["hospital_id"] for c in screen["candidates"]])
        rejected = next(r for r in screen["rejected"] if r["hospital_id"] == "H_A")
        self.assertIn("道路阻断", rejected["reason"])

    def test_capability_part_and_caliber_mismatch_rejected(self):
        fx = NetworkFixture()
        fx.add(_hospital("H_D", "专科手外", parts=["FINGER"], caliber=1.5), 30)
        case = fx.case(part="HAND", caliber=0.8)
        screen = fx.e.screen_case(case["case_id"], T0)
        rejected = next(r for r in screen["rejected"] if r["hospital_id"] == "H_D")
        self.assertIn("离断部位", rejected["reason"])
        self.assertIn("血管口径", rejected["reason"])
        self.assertEqual(screen["candidates"], [])

    def test_stale_capability_data_raises_high_risk_and_escalates(self):
        fx = NetworkFixture()
        fx.add(_hospital("H_S", "资料过期医院", updated_at="2026-09-19T03:00:00Z"), 40)
        case = fx.case()
        screen = fx.e.screen_case(case["case_id"], T0)
        self.assertNotIn("H_S", [c["hospital_id"] for c in screen["candidates"]])
        escalations = fx.e.escalations()
        # 唯一候选资料过期 -> 同时触发“资料过期”和“无可接收单位”两类风险
        types = {r["type"] for item in escalations for r in item["risks"]}
        self.assertIn("CAPABILITY_DATA_STALE", types)
        self.assertIn("NO_RECEIVING_UNIT", types)
        stale = next(r for item in escalations for r in item["risks"]
                     if r["type"] == "CAPABILITY_DATA_STALE")
        self.assertEqual(stale["severity"], "HIGH")
        self.assertIn("H_S", stale["message"])
        # 值班员逐项确认后升级队列清空，但风险记录仍在
        for item in fx.e.escalations():
            for r in item["risks"]:
                fx.e.acknowledge_risk(case["case_id"], r["risk_id"], "dispatcher-7", "已电话处理")
        self.assertEqual(fx.e.escalations(), [])
        active = fx.e.get_case(case["case_id"])["risks"]
        self.assertTrue(any(r["risk_id"] == stale["risk_id"] and r["active"] for r in active))

    def test_all_units_beyond_window_escalates_no_unit(self):
        fx = NetworkFixture()
        fx.add(_hospital("H_F", "超远医院"), 200)
        # 离断已 4 小时，单程 200 分钟，必越 6 小时窗口
        case = fx.case(amputated_at="2026-09-19T04:00:00Z")
        screen = fx.e.screen_case(case["case_id"], T0)
        self.assertEqual(screen["candidates"], [])
        rejected = screen["rejected"][0]
        self.assertIn("超出缺血窗口", rejected["reason"])
        risk = fx.e.escalations()[0]["risks"][0]
        self.assertEqual(risk["type"], "NO_RECEIVING_UNIT")
        self.assertEqual(risk["severity"], "CRITICAL")

    def test_cold_preservation_extends_window(self):
        fx = NetworkFixture()
        fx.add(_hospital("H_F", "超远医院"), 200)
        # 同样 4 小时 + 200 分钟车程：冷藏窗口 8 小时可到达
        case = fx.case(amputated_at="2026-09-19T04:00:00Z", preservation="COLD")
        self.assertEqual(case["ischemia"]["window_minutes"], 480)
        screen = fx.e.screen_case(case["case_id"], T0)
        self.assertEqual([c["hospital_id"] for c in screen["candidates"]], ["H_F"])
        self.assertGreater(screen["candidates"][0]["minutes_remaining_on_arrival"], 0)


class OfferLeasePlanTest(unittest.TestCase):
    def setUp(self):
        self.fx = NetworkFixture()
        self.fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40)
        self.fx.add(_hospital("H_B", "县二院"), 55)
        self.case = self.fx.case()
        self.cid = self.case["case_id"]

    def _offers(self, at=T0):
        return self.fx.e.request_offers(self.cid, at=at)

    def test_plan_requires_explicit_acceptance_and_lease(self):
        offers = self._offers()
        offer = offers[0]
        with self.assertRaises(DomainError) as cm:
            self.fx.e.create_plan(self.cid, offer["offer_id"], at=T0)
        self.assertEqual(cm.exception.code, "OFFER_NOT_ACCEPTED")
        accepted = self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:02:00Z")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertIsNotNone(accepted["lease_id"])
        plan = self.fx.e.create_plan(
            self.cid, offer["offer_id"],
            {"mode": "AMBULANCE", "decided_by": "dispatcher-1"}, "2026-09-19T08:02:00Z")
        self.assertEqual(plan["hospital_id"], "H_A")
        self.assertEqual(plan["status"], "ACTIVE")
        self.assertEqual(plan["versions"][0]["version"], 1)
        self.assertIn("不构成诊断建议", plan["versions"][0]["decision_basis"])

    def test_duplicate_acceptance_does_not_double_lock(self):
        offers = self._offers()
        offer = next(o for o in offers if o["hospital_id"] == "H_A")
        self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:02:00Z")
        with self.assertRaises(DomainError) as cm:
            self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:03:00Z")
        self.assertEqual(cm.exception.code, "OFFER_NOT_PENDING")
        held = [l for l in self.fx.e._state["leases"].values() if l["status"] == "HELD"]
        self.assertEqual(len(held), 1)

    def test_offer_expires_after_ttl(self):
        offers = self._offers()
        offer = offers[0]
        with self.assertRaises(DomainError) as cm:
            self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:06:00Z")
        self.assertEqual(cm.exception.code, "OFFER_EXPIRED")
        self.assertEqual(self.fx.e._state["offers"][offer["offer_id"]]["status"], "EXPIRED")

    def test_lease_lost_after_ttl_escalates_critical(self):
        offers = self._offers()
        offer = next(o for o in offers if o["hospital_id"] == "H_A")
        self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:02:00Z")
        self.fx.e.create_plan(self.cid, offer["offer_id"], {"decided_by": "d1"},
                              "2026-09-19T08:02:00Z")
        summary = self.fx.e.sweep("2026-09-19T08:35:00Z")
        self.assertEqual(len(summary["expired_leases"]), 1)
        risk = self.fx.e.escalations()[0]["risks"][0]
        self.assertEqual(risk["type"], "RESOURCE_LOCK_LOST")
        self.assertEqual(risk["severity"], "CRITICAL")

    def test_enroute_milestone_renews_lease(self):
        offers = self._offers()
        offer = next(o for o in offers if o["hospital_id"] == "H_A")
        self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:02:00Z")
        self.fx.e.create_plan(self.cid, offer["offer_id"], {"decided_by": "d1"},
                              "2026-09-19T08:02:00Z")
        # 08:25 途中补传里程碑，租约续到 08:55
        self.fx.e.record_milestone(self.cid, {
            "code": "WAYPOINT", "at": "2026-09-19T08:25:00Z", "client_event_id": "w1"})
        summary = self.fx.e.sweep("2026-09-19T08:35:00Z")
        self.assertEqual(summary["expired_leases"], [])
        held = [l for l in self.fx.e._state["leases"].values() if l["status"] == "HELD"]
        self.assertEqual(len(held), 1)

    def test_contact_lost_escalates_and_heartbeat_resolves(self):
        offers = self.fx.e.request_offers(self.cid, hospital_ids=["H_A"],
                                          at="2026-09-19T08:08:00Z")
        offer = offers[0]
        # 邀约未决期间 H_A 静默超过失联阈值（08:08 发起，08:11 已 11 分钟无回报）
        summary = self.fx.e.sweep("2026-09-19T08:11:00Z")
        self.assertIn(self.cid, summary["contact_lost_cases"])
        risk = self.fx.e.escalations()[0]["risks"][0]
        self.assertEqual(risk["type"], "RESOURCE_CONTACT_LOST")
        self.assertIn("H_A", risk["message"])
        # 机构恢复联系并应答：接受本身即实时回报，失联风险自动解除
        self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:12:00Z")
        self.assertEqual(self.fx.e._state["offers"][offer["offer_id"]]["status"], "ACCEPTED")
        self.assertEqual(self.fx.e.escalations(), [])

    def test_plan_blocked_when_route_closed_between_acceptance_and_plan(self):
        offers = self._offers()
        offer = next(o for o in offers if o["hospital_id"] == "H_A")
        self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:02:00Z")
        self.fx.e.report_traffic({
            "origin": {"hospital_id": "H0"}, "destination_hospital_id": "H_A",
            "severity": "BLOCKED", "passable": False, "summary": "隧道坍塌",
            "observed_at": "2026-09-19T08:03:00Z"})
        with self.assertRaises(DomainError) as cm:
            self.fx.e.create_plan(self.cid, offer["offer_id"], {"decided_by": "d1"},
                                  "2026-09-19T08:03:00Z")
        self.assertEqual(cm.exception.code, "ROAD_BLOCKED")

    def test_plan_already_beyond_window_raises_critical_risk(self):
        fx = NetworkFixture()
        fx.add(_hospital("H_F", "超远医院"), 200)
        # 04:30 离断，窗口 10:30 截止；08:00 出发 +200 分钟 = 11:20 到达，越窗 50 分钟
        case = fx.case(amputated_at="2026-09-19T04:30:00Z")["case_id"]
        screen = fx.e.screen_case(case, T0)
        self.assertEqual(screen["candidates"], [])
        # 人工调度仍可坚持指定 H_F（机构接受），计划必须醒目标记越窗并升级
        offers = fx.e.request_offers(case, hospital_ids=["H_F"], at=T0, manual=True)
        fx.e.respond_offer(offers[0]["offer_id"], True, "2026-09-19T08:01:00Z")
        fx.e.create_plan(case, offers[0]["offer_id"], {"decided_by": "d1"},
                         "2026-09-19T08:01:00Z")
        risk = fx.e.escalations()[0]["risks"][0]
        self.assertEqual(risk["type"], "ETA_BEYOND_ISCHEMIA_WINDOW")
        self.assertTrue(risk["message"].endswith("，已升级人工调度") or "超出缺血窗口" in risk["message"])

    def test_eta_breach_during_transport_raises_critical_risk(self):
        offers = self._offers()
        offer = next(o for o in offers if o["hospital_id"] == "H_A")
        self.fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:02:00Z")
        self.fx.e.create_plan(self.cid, offer["offer_id"], {"decided_by": "d1"},
                              "2026-09-19T08:02:00Z")
        # 12:30 仍在途中，剩余车程 40 分钟 -> 13:10 到达，超过 13:00 窗口
        self.fx.e.record_milestone(self.cid, {
            "code": "WAYPOINT", "at": "2026-09-19T12:30:00Z",
            "remaining_minutes_estimate": 40, "client_event_id": "late"})
        risk = self.fx.e.escalations()[0]["risks"][0]
        self.assertEqual(risk["type"], "ETA_BEYOND_ISCHEMIA_WINDOW")
        self.assertIn("10 分钟", risk["message"])


class ConcurrentDualEmergencyTest(unittest.TestCase):
    """值班员同时发起两例急诊：证明同一团队/手术台不会被并发占用。"""

    def _build(self):
        fx = NetworkFixture()
        # H_A：两组团队两张台；H_B：只有一组团队一张台（争抢焦点）；H_C：远
        fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40, weather="RAIN")
        fx.add(_hospital("H_B", "县二院", teams=1, rooms=1), 55)
        fx.add(_hospital("H_C", "远郊中心", teams=2, rooms=2), 120)
        fx.e.report_traffic({
            "origin": {"hospital_id": "H0"}, "destination_hospital_id": "H_A",
            "severity": "INCIDENT", "summary": "隧道事故缓行", "observed_at": T0})
        return fx

    def test_concurrent_acceptance_never_double_books_team_or_room(self):
        fx = self._build()
        c1 = fx.case(part="HAND", caliber=1.0)["case_id"]
        c2 = fx.case(part="FOREARM", caliber=1.2)["case_id"]

        results = {}
        barrier = threading.Barrier(2)

        def dispatch(cid, tag):
            screen = fx.e.screen_case(cid, T0)
            # 两例都先向容量最小的 H_B 发起邀约
            offers = fx.e.request_offers(cid, hospital_ids=["H_B"], at=T0)
            offer = offers[0]
            # 等双方邀约就位后再同时接受，压测“接受瞬间”的资源竞态
            barrier.wait()
            try:
                fx.e.respond_offer(offer["offer_id"], True, "2026-09-19T08:01:00Z")
                results[cid] = ("ACCEPTED", offer["offer_id"])
            except DomainError as exc:
                results[cid] = (exc.code, offer["offer_id"])
            return screen

        with ThreadPoolExecutor(max_workers=2) as pool:
            s1, s2 = list(pool.map(lambda args: dispatch(*args), [(c1, "c1"), (c2, "c2")]))

        tags = [results[c1][0], results[c2][0]]
        self.assertEqual(sorted(tags), ["ACCEPTED", "RESOURCE_BUSY"])

        # 失败方病例必须醒目标记风险并进入人工调度升级队列
        loser = c1 if results[c1][0] == "RESOURCE_BUSY" else c2
        winner = c2 if loser == c1 else c1
        esc = {item["case_id"]: item for item in fx.e.escalations()}
        self.assertIn(loser, esc)
        self.assertEqual(esc[loser]["risks"][0]["type"], "RESOURCE_BUSY_AT_ACCEPTANCE")
        self.assertEqual(esc[loser]["risks"][0]["severity"], "HIGH")
        self.assertNotIn(winner, esc)

        # 失败方改由 H_A 接受成功：两例各持不同团队和手术台
        offers_a = fx.e.request_offers(loser, hospital_ids=["H_A"], at=T0)
        fx.e.respond_offer(offers_a[0]["offer_id"], True, "2026-09-19T08:02:00Z")
        fx.e.create_plan(winner, results[winner][1], {"decided_by": "d-w"},
                         "2026-09-19T08:02:30Z")
        fx.e.create_plan(loser, offers_a[0]["offer_id"], {"decided_by": "d-l"},
                         "2026-09-19T08:03:00Z")

        active = [l for l in fx.e._state["leases"].values()
                  if l["status"] in ("HELD", "CONSUMED")]
        self.assertEqual(len(active), 2)
        teams = [(l["hospital_id"], l["team_id"]) for l in active]
        rooms = [(l["hospital_id"], l["room_id"]) for l in active]
        self.assertEqual(len(teams), len(set(teams)))
        self.assertEqual(len(rooms), len(set(rooms)))

        # 系统能对两例分别解释推荐依据：受天气与道路影响的 H_A 保留完整计算链
        for screen in (s1, s2):
            a = next(c for c in screen["candidates"] if c["hospital_id"] == "H_A")
            detail = " ".join(f["detail"] for f in a["factors"])
            self.assertIn("RAIN", detail)
            self.assertTrue(any(f["factor"] == "道路事件" for f in a["factors"]))
            # H_A 因此被排在容量更小但更快的 H_B 之后
            ordered = [c["hospital_id"] for c in screen["candidates"]]
            self.assertLess(ordered.index("H_B"), ordered.index("H_A"))

    def test_http_concurrent_dispatchers_no_double_booking(self):
        """两值班员经真实 HTTP 并发操作，服务端串行化资源占用。"""
        fx = self._build()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fx.e))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def call(method, path, payload=None):
            req = Request(base + path, method=method,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=5) as resp:
                    return resp.status, json.load(resp)
            except HTTPError as exc:
                return exc.code, json.load(exc)

        try:
            _, c1 = call("POST", "/cases", _case())
            _, c2 = call("POST", "/cases", _case())
            barrier = threading.Barrier(2)

            def accept_top(cid):
                _, offers = call("POST", f"/cases/{cid}/offers",
                                 {"hospital_ids": ["H_B"], "at": T0})
                barrier.wait()
                return call("POST", f"/offers/{offers['offers'][0]['offer_id']}/respond",
                            {"accepted": True, "at": "2026-09-19T08:01:00Z"})

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(accept_top, [c1["case_id"], c2["case_id"]]))
            statuses = [(code, body.get("status") or body.get("error", {}).get("code"))
                        for code, body in outcomes]
            self.assertEqual(sorted(s for _, s in statuses), ["ACCEPTED", "RESOURCE_BUSY"])
            active = [l for l in fx.e._state["leases"].values() if l["status"] == "HELD"]
            self.assertEqual(len(active), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class MilestoneIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.fx = NetworkFixture()
        self.fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40)
        self.cid = self.fx.case()["case_id"]
        offers = self.fx.e.request_offers(self.cid, at=T0)
        self.offer = next(o for o in offers if o["hospital_id"] == "H_A")
        self.fx.e.respond_offer(self.offer["offer_id"], True, "2026-09-19T08:02:00Z")
        self.fx.e.create_plan(self.cid, self.offer["offer_id"], {"decided_by": "d1"},
                              "2026-09-19T08:02:00Z")

    def test_weak_network_retry_with_same_client_event_id_does_not_change_state(self):
        payload = {"code": "DEPARTED", "at": "2026-09-19T08:10:00Z",
                   "client_event_id": "truck-1-depart", "note": "出发"}
        first = self.fx.e.record_milestone(self.cid, payload)
        second = self.fx.e.record_milestone(self.cid, payload)
        third = self.fx.e.record_milestone(self.cid, payload)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertTrue(third["deduplicated"])
        milestones = self.fx.e.list_milestones(self.cid)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["milestone_id"], second["milestone_id"])

    def test_repeated_waypoint_without_client_key_deduped_within_grace_window(self):
        payload = {"code": "ENROUTE", "at": "2026-09-19T08:11:00Z"}
        first = self.fx.e.record_milestone(self.cid, payload)
        second = self.fx.e.record_milestone(self.cid, payload)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(self.fx.e.list_milestones(self.cid)), 1)
        self.assertEqual(first["milestone_id"], second["milestone_id"])

    def test_invalid_milestone_rejected(self):
        with self.assertRaises(DomainError) as cm:
            self.fx.e.record_milestone(self.cid, {"code": "TURNED_BACK", "at": T0})
        self.assertEqual(cm.exception.code, "INVALID_MILESTONE")

    def test_arrival_consumes_lease(self):
        self.fx.e.record_milestone(self.cid, {
            "code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "d"})
        self.fx.e.record_milestone(self.cid, {
            "code": "ARRIVED", "at": "2026-09-19T08:55:00Z", "client_event_id": "a"})
        lease_id = self.fx.e._state["offers"][self.offer["offer_id"]]["lease_id"]
        lease = self.fx.e._state["leases"][lease_id]
        self.assertEqual(lease["status"], "CONSUMED")
        plan = self.fx.e.get_plan(self.cid)
        self.assertEqual(plan["status"], "ARRIVED")


class RerouteTest(unittest.TestCase):
    def _network_with_plan(self):
        fx = NetworkFixture()
        fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40)
        fx.add(_hospital("H_B", "县二院", teams=2, rooms=2), 55)
        cid = fx.case()["case_id"]
        offers = fx.e.request_offers(cid, at=T0)
        oa = next(o for o in offers if o["hospital_id"] == "H_A")
        fx.e.respond_offer(oa["offer_id"], True, "2026-09-19T08:02:00Z")
        fx.e.create_plan(cid, oa["offer_id"], {"decided_by": "dispatcher-1"},
                         "2026-09-19T08:02:00Z")
        fx.e.record_milestone(cid, {
            "code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "dep"})
        return fx, cid

    def test_reroute_keeps_original_plan_and_records_reason_and_decider(self):
        fx, cid = self._network_with_plan()
        fx.e.report_traffic({
            "origin": {"hospital_id": "H0"}, "destination_hospital_id": "H_A",
            "severity": "BLOCKED", "passable": False, "summary": "国道事故",
            "observed_at": "2026-09-19T08:20:00Z"})
        fx.e.heartbeat("H_B", "2026-09-19T08:20:00Z")
        offers = fx.e.request_offers(cid, hospital_ids=["H_B"], at="2026-09-19T08:20:00Z")
        fx.e.respond_offer(offers[0]["offer_id"], True, "2026-09-19T08:21:00Z")

        plan = fx.e.reroute(cid, {
            "target_hospital_id": "H_B", "offer_id": offers[0]["offer_id"],
            "reason": "国道事故封路，改走县二院", "decided_by": "dispatcher-1",
            "at": "2026-09-19T08:21:00Z", "client_event_id": "divert-1"})
        self.assertEqual(len(plan["versions"]), 2)
        v1, v2 = plan["versions"]
        self.assertEqual(v1["hospital_id"], "H_A")
        self.assertEqual(v1["reason"], "初版转运计划")
        self.assertEqual(v2["hospital_id"], "H_B")
        self.assertIn("国道事故", v2["reason"])
        self.assertEqual(v2["decided_by"], "dispatcher-1")
        self.assertFalse(v2["manual_override"])
        # 旧租约释放、旧邀约作废，新机构持约
        statuses = {(l["hospital_id"], l["status"]) for l in fx.e._state["leases"].values()}
        self.assertEqual(statuses, {("H_A", "RELEASED"), ("H_B", "HELD")})
        self.assertTrue(any(
            o["hospital_id"] == "H_A" and o["status"] == "SUPERSEDED"
            for o in fx.e._state["offers"].values()))

    def test_reroute_requires_reason_and_decider(self):
        fx, cid = self._network_with_plan()
        with self.assertRaises(DomainError) as cm:
            fx.e.reroute(cid, {"at": T0, "decided_by": "d1"})
        self.assertEqual(cm.exception.code, "INVALID_REROUTE")
        with self.assertRaises(DomainError) as cm:
            fx.e.reroute(cid, {"at": T0, "reason": "封路"})
        self.assertEqual(cm.exception.code, "INVALID_REROUTE")

    def test_reroute_to_new_hospital_requires_its_acceptance(self):
        fx, cid = self._network_with_plan()
        fx.e.heartbeat("H_B", "2026-09-19T08:20:00Z")
        with self.assertRaises(DomainError) as cm:
            fx.e.reroute(cid, {"target_hospital_id": "H_B", "reason": "封路",
                               "decided_by": "d1", "at": "2026-09-19T08:20:00Z"})
        self.assertEqual(cm.exception.code, "UNKNOWN_OFFER")

    def test_same_hospital_replan_appends_version_without_new_lease(self):
        fx, cid = self._network_with_plan()
        plan_before = fx.e.get_plan(cid)
        plan = fx.e.reroute(cid, {
            "reason": "高速临时管制，绕行市区多 15 分钟", "decided_by": "dispatcher-1",
            "at": "2026-09-19T08:25:00Z"})
        self.assertEqual(len(plan["versions"]), 2)
        self.assertEqual(plan["versions"][1]["hospital_id"], "H_A")
        self.assertEqual(plan["lease_id"], plan_before["lease_id"])

    def test_manual_override_reroute_to_noneligible_unit_is_flagged(self):
        fx = NetworkFixture()
        fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40)
        # H_B 能力资料过期（5 小时前），筛查不合格
        fx.add(_hospital("H_B", "县二院", teams=2, rooms=2,
                         updated_at="2026-09-19T03:00:00Z"), 55)
        cid = fx.case()["case_id"]
        offers = fx.e.request_offers(cid, at=T0)
        oa = next(o for o in offers if o["hospital_id"] == "H_A")
        fx.e.respond_offer(oa["offer_id"], True, "2026-09-19T08:02:00Z")
        fx.e.create_plan(cid, oa["offer_id"], {"decided_by": "d1"}, "2026-09-19T08:02:00Z")
        fx.e.record_milestone(cid, {
            "code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "dep"})

        # 人工调度坚持改向资料过期的 H_B：先人工发邀约并获机构接受
        manual_offers = fx.e.request_offers(cid, hospital_ids=["H_B"],
                                            at="2026-09-19T08:19:00Z", manual=True)
        fx.e.respond_offer(manual_offers[0]["offer_id"], True, "2026-09-19T08:20:00Z")
        # 无 override 时拒绝
        with self.assertRaises(DomainError) as cm:
            fx.e.reroute(cid, {"target_hospital_id": "H_B", "offer_id": manual_offers[0]["offer_id"],
                               "reason": "调度中心电话确认可接", "decided_by": "supervisor-9",
                               "at": "2026-09-19T08:20:00Z"})
        self.assertEqual(cm.exception.code, "TARGET_NOT_ELIGIBLE")
        # override 留痕后放行，版本明确标记人工越权
        plan = fx.e.reroute(cid, {"target_hospital_id": "H_B", "offer_id": manual_offers[0]["offer_id"],
                                  "reason": "调度中心电话确认可接", "decided_by": "supervisor-9",
                                  "at": "2026-09-19T08:20:00Z", "override": True})
        self.assertEqual(plan["versions"][-1]["hospital_id"], "H_B")
        self.assertTrue(plan["versions"][-1]["manual_override"])
        self.assertEqual(plan["versions"][-1]["decided_by"], "supervisor-9")

    def test_reroute_replay_after_reconnect_does_not_add_version(self):
        fx, cid = self._network_with_plan()
        fx.e.heartbeat("H_B", "2026-09-19T08:20:00Z")
        offers = fx.e.request_offers(cid, hospital_ids=["H_B"], at="2026-09-19T08:20:00Z")
        fx.e.respond_offer(offers[0]["offer_id"], True, "2026-09-19T08:21:00Z")
        payload = {"target_hospital_id": "H_B", "offer_id": offers[0]["offer_id"],
                   "reason": "国道事故封路", "decided_by": "dispatcher-1",
                   "at": "2026-09-19T08:21:00Z", "client_event_id": "divert-1"}
        fx.e.reroute(cid, payload)
        # 弱网重连后救护车端重放同一改道消息
        replay = fx.e.reroute(cid, payload)
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(len(replay["versions"]), 2)


class IdentityStagingTest(unittest.TestCase):
    def test_identity_unfolds_by_stage_and_retracted_on_divert_and_close(self):
        fx = NetworkFixture()
        # H0 是基层接诊机构，H_A/H_B 是接收机构，H_X 未参与
        fx.e.register_hospital(_hospital("H0", "基层卫生院", teams=1, rooms=1))
        fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40)
        fx.add(_hospital("H_B", "县二院", teams=2, rooms=2), 55)
        fx.add(_hospital("H_X", "无关医院", teams=2, rooms=2), 90)
        case = fx.case(referring="H0")
        cid = case["case_id"]

        # 接诊机构在 INTAKE 阶段可见完整身份
        view0 = fx.e.identity_view(cid, "H0")
        self.assertEqual(view0["stage"], "INTAKE")
        self.assertEqual(view0["patient"]["name"], "张三")
        self.assertEqual(view0["patient"]["id_number"], "110101199001011234")

        # 未参与机构不可见
        self.assertFalse(fx.e.identity_view(cid, "H_X")["visible"])

        offers = fx.e.request_offers(cid, at=T0)
        oa = next(o for o in offers if o["hospital_id"] == "H_A")
        fx.e.respond_offer(oa["offer_id"], True, "2026-09-19T08:02:00Z")
        fx.e.create_plan(cid, oa["offer_id"], {"decided_by": "d1"}, "2026-09-19T08:02:00Z")

        # 已接受未出发：仅代号与伤情，不见姓名证件
        accepted = fx.e.identity_view(cid, "H_A")
        self.assertEqual(accepted["stage"], "ACCEPTED")
        self.assertIn("pseudonym", accepted["patient"])
        self.assertNotIn("name", accepted["patient"])
        self.assertNotIn("id_number", accepted["patient"])
        self.assertNotIn("vitals", accepted)

        # 在途：增加年龄性别与生命体征，仍不见姓名证件
        fx.e.add_vitals(cid, {"at": "2026-09-19T08:10:00Z", "bp": "118/76", "hr": 92})
        fx.e.record_milestone(cid, {
            "code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "dep"})
        transit = fx.e.identity_view(cid, "H_A")
        self.assertEqual(transit["stage"], "IN_TRANSIT")
        self.assertIn("age", transit["patient"])
        self.assertNotIn("name", transit["patient"])
        self.assertEqual(len(transit["vitals"]), 1)

        # 到院接收：开放完整身份
        fx.e.record_milestone(cid, {
            "code": "ARRIVED", "at": "2026-09-19T08:50:00Z", "client_event_id": "arr"})
        receiving = fx.e.identity_view(cid, "H_A")
        self.assertEqual(receiving["stage"], "RECEIVING")
        self.assertEqual(receiving["patient"]["name"], "张三")
        self.assertEqual(receiving["patient"]["id_number"], "110101199001011234")
        self.assertEqual(len(receiving["vitals"]), 1)

        # 改道到 H_B：H_A 权限立即收回，H_B 按在途阶段开放
        fx.e.heartbeat("H_B", "2026-09-19T08:20:00Z")
        offers_b = fx.e.request_offers(cid, hospital_ids=["H_B"], at="2026-09-19T08:20:00Z")
        fx.e.respond_offer(offers_b[0]["offer_id"], True, "2026-09-19T08:21:00Z")
        # 将时钟拨到在途改道（ARRIVED 已过的场景仅用于验证阶段视图；改道在另一条时间线）
        # 这里直接以“到院前”的新病例验证收回逻辑
        case2 = fx.case(referring="H0")
        cid2 = case2["case_id"]
        offers2 = fx.e.request_offers(cid2, at=T0)
        a2 = next(o for o in offers2 if o["hospital_id"] == "H_A")
        b2 = next(o for o in offers2 if o["hospital_id"] == "H_B")
        fx.e.respond_offer(a2["offer_id"], True, "2026-09-19T08:02:00Z")
        fx.e.create_plan(cid2, a2["offer_id"], {"decided_by": "d1"}, "2026-09-19T08:02:00Z")
        fx.e.record_milestone(cid2, {
            "code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "dep2"})
        fx.e.heartbeat("H_B", "2026-09-19T08:20:00Z")
        ob2 = fx.e.request_offers(cid2, hospital_ids=["H_B"], at="2026-09-19T08:20:00Z")[0]
        fx.e.respond_offer(ob2["offer_id"], True, "2026-09-19T08:21:00Z")
        fx.e.reroute(cid2, {"target_hospital_id": "H_B", "offer_id": ob2["offer_id"],
                            "reason": "封路", "decided_by": "d1",
                            "at": "2026-09-19T08:21:00Z", "client_event_id": "div2"})
        self.assertFalse(fx.e.identity_view(cid2, "H_A")["visible"])
        self.assertEqual(fx.e.identity_view(cid2, "H_B")["stage"], "IN_TRANSIT")

        # 结单后所有机构权限收回
        fx.e.close_case(cid2, outcome="血运重建成功")
        self.assertFalse(fx.e.identity_view(cid2, "H_A")["visible"])
        self.assertFalse(fx.e.identity_view(cid2, "H_B")["visible"])
        self.assertFalse(fx.e.identity_view(cid2, "H0")["visible"])


class EventReplayTest(unittest.TestCase):
    def test_restart_replays_plan_reroute_milestones_and_idempotency(self):
        log_fd, log_path = tempfile.mkstemp(prefix="limb-events-", suffix=".jsonl")
        os.close(log_fd)
        try:
            fx = NetworkFixture(Orchestrator(log_path=log_path))
            fx.add(_hospital("H_A", "市一手外", teams=2, rooms=2), 40)
            fx.add(_hospital("H_B", "县二院", teams=2, rooms=2), 55)
            cid = fx.case()["case_id"]
            offers = fx.e.request_offers(cid, at=T0)
            ob = next(o for o in offers if o["hospital_id"] == "H_B")
            fx.e.respond_offer(ob["offer_id"], True, "2026-09-19T08:02:00Z")
            fx.e.create_plan(cid, ob["offer_id"], {"decided_by": "d1"}, "2026-09-19T08:02:00Z")
            fx.e.record_milestone(cid, {
                "code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "dep"})
            fx.e.heartbeat("H_A", "2026-09-19T08:20:00Z")
            oa2 = fx.e.request_offers(cid, hospital_ids=["H_A"], at="2026-09-19T08:20:00Z")[0]
            fx.e.respond_offer(oa2["offer_id"], True, "2026-09-19T08:21:00Z")
            divert = {"target_hospital_id": "H_A", "offer_id": oa2["offer_id"],
                      "reason": "道路塌陷改道", "decided_by": "d1",
                      "at": "2026-09-19T08:21:00Z", "client_event_id": "div"}
            fx.e.reroute(cid, divert)

            # 模拟断网重启：新进程从日志重放
            restored = Orchestrator(log_path=log_path)
            self.assertEqual(len(restored.list_cases()), 1)
            plan = restored.get_plan(cid)
            self.assertEqual([v["hospital_id"] for v in plan["versions"]], ["H_B", "H_A"])
            self.assertEqual(plan["versions"][0]["reason"], "初版转运计划")
            self.assertEqual(plan["versions"][1]["reason"], "道路塌陷改道")
            self.assertEqual(plan["versions"][1]["decided_by"], "d1")
            milestones = restored.list_milestones(cid)
            self.assertEqual([m["code"] for m in milestones], ["DEPARTED"])

            # 重放后重传旧消息仍然幂等：不产生新版本/新里程碑
            replay_plan = restored.reroute(cid, divert)
            self.assertTrue(replay_plan["deduplicated"])
            self.assertEqual(len(restored.get_plan(cid)["versions"]), 2)
            replay_ms = restored.record_milestone(cid, {
                "code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "dep"})
            self.assertTrue(replay_ms["deduplicated"])
            self.assertEqual(len(restored.list_milestones(cid)), 1)

            # 身份授权也完整恢复
            self.assertEqual(restored.identity_view(cid, "H_A")["stage"], "IN_TRANSIT")
            self.assertFalse(restored.identity_view(cid, "H_B")["visible"])

            # 审计轨迹完整可查
            trail = restored.audit_trail(cid)
            kinds = [e["kind"] for e in trail["entries"]]
            self.assertIn("plan.created", kinds)
            self.assertIn("plan.rerouted", kinds)
            self.assertEqual(kinds.count("plan.rerouted"), 1)
        finally:
            os.unlink(log_path)


class HttpApiTest(unittest.TestCase):
    """经真实 HTTP 验证状态码、JSON 错误结构与端到端 API 链路。"""

    @classmethod
    def setUpClass(cls):
        cls.fx = NetworkFixture()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.fx.e))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, payload=None):
        req = Request(self.base + path, method=method,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_full_journey_over_http_with_status_codes_and_errors(self):
        # 机构、心跳、路线
        status, hospital = self.call("POST", "/hospitals", _hospital("H_A", "市一手外", teams=2, rooms=2))
        self.assertEqual(status, 201)
        self.assertEqual(hospital["hospital_id"], "H_A")
        self.assertEqual(self.call("POST", "/hospitals/H_A/heartbeat", {"at": T0})[0], 200)
        self.assertEqual(self.call("PUT", "/routes", _route("H_A", 40))[0], 200)

        # 非法病例 -> 400 + 结构化错误码
        status, err = self.call("POST", "/cases", {"injury": {"amputated_at": T0}})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "INVALID_INJURY")

        status, case = self.call("POST", "/cases", _case())
        self.assertEqual(status, 201)
        cid = case["case_id"]
        self.assertEqual(case["ischemia"]["deadline"], "2026-09-19T13:00:00Z")

        status, screen = self.call("POST", f"/cases/{cid}/screen", {"at": T0})
        self.assertEqual(status, 200)
        self.assertEqual([c["hospital_id"] for c in screen["candidates"]], ["H_A"])

        status, offers = self.call("POST", f"/cases/{cid}/offers", {"at": T0})
        self.assertEqual(status, 200)
        offer_id = offers["offers"][0]["offer_id"]

        # 未接受先建计划 -> 409
        status, err = self.call("POST", f"/cases/{cid}/plan", {"offer_id": offer_id, "at": T0})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "OFFER_NOT_ACCEPTED")

        status, accepted = self.call(
            "POST", f"/offers/{offer_id}/respond",
            {"accepted": True, "at": "2026-09-19T08:02:00Z"})
        self.assertEqual(status, 200)
        self.assertEqual(accepted["status"], "ACCEPTED")

        status, plan = self.call("POST", f"/cases/{cid}/plan", {
            "offer_id": offer_id, "at": "2026-09-19T08:02:00Z",
            "transport": {"mode": "AMBULANCE", "decided_by": "d1"}})
        self.assertEqual(status, 201)
        self.assertEqual(plan["hospital_id"], "H_A")

        # 幂等里程碑
        ms_payload = {"code": "DEPARTED", "at": "2026-09-19T08:10:00Z", "client_event_id": "m1"}
        self.call("POST", f"/cases/{cid}/milestones", ms_payload)
        status, replay = self.call("POST", f"/cases/{cid}/milestones", ms_payload)
        self.assertEqual(status, 200)
        self.assertTrue(replay["deduplicated"])

        # 身份视图：未参与机构不可见；接收机构按阶段开放
        self.call("POST", "/hospitals", _hospital("H_X", "无关医院", teams=2, rooms=2))
        self.call("POST", "/hospitals/H_X/heartbeat", {"at": T0})
        self.call("PUT", "/routes", _route("H_X", 90))
        status, denied = self.call("GET", f"/cases/{cid}/identity?hospital_id=H_X")
        self.assertEqual(status, 200)
        self.assertFalse(denied["visible"])
        status, view = self.call("GET", f"/cases/{cid}/identity?hospital_id=H_A")
        self.assertEqual(view["stage"], "IN_TRANSIT")
        self.assertNotIn("name", view["patient"])

        # 解释依据与升级队列
        status, explanation = self.call("GET", f"/cases/{cid}/explanation")
        self.assertEqual(status, 200)
        self.assertIn("不构成诊断建议", explanation["disclaimer"])
        self.assertEqual(len(explanation["plan_versions"]), 1)
        status, escalations = self.call("GET", "/escalations")
        self.assertEqual(escalations["escalations"], [])

        # 审计轨迹
        status, trail = self.call("GET", f"/cases/{cid}/audit")
        self.assertEqual(status, 200)
        kinds = [e["kind"] for e in trail["entries"]]
        self.assertIn("plan.created", kinds)
        self.assertIn("milestone.recorded", kinds)

        # 未知路由与未知病例
        status, err = self.call("GET", f"/cases/{cid}/identity")
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "MISSING_QUERY")
        status, err = self.call("GET", "/cases/case_unknown")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "UNKNOWN_CASE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
