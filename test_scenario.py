"""值班员同时发起两例急诊的综合编排场景。

验证：能力图/交通事件/案例时钟同时可用；系统解释推荐依据；
同一团队与手术台不会被两例急诊并发占用；第三人转向次优机构。
"""

import threading
import unittest

from domain import ADVICE_NOTICE
from testsupport import make_orchestrator, register_case


class DualEmergencyScenarioTest(unittest.TestCase):
    def setUp(self):
        self.orch, self.clock = make_orchestrator()

    def test_two_emergencies_dispatched_concurrently_without_double_booking(self):
        case_a = register_case(self.orch, pseudonym="P-A", minutes_ago=30)
        case_b = register_case(self.orch, pseudonym="P-B", minutes_ago=45)

        # 案例时钟对两例分别可用。
        clock_a = self.orch.case_clock(case_a.id)
        clock_b = self.orch.case_clock(case_b.id)
        self.assertEqual(clock_a["remaining_min"], 330.0)
        self.assertEqual(clock_b["remaining_min"], 315.0)

        # 并发生成推荐。
        recsets: dict[str, object] = {}

        def recommend(case):
            recsets[case.patient.pseudonym] = self.orch.recommend(case.id)

        threads = [
            threading.Thread(target=recommend, args=(case_a,)),
            threading.Thread(target=recommend, args=(case_b,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(recsets), 2)

        for recset in recsets.values():
            self.assertFalse(recset.escalated)
            top = recset.recommendations[0]
            self.assertEqual(top.hospital_id, "H1")
            self.assertTrue(top.feasible)
            # 推荐必须给出因子级依据，且明确非诊断。
            self.assertTrue(top.factors)
            self.assertEqual(recset.notice, ADVICE_NOTICE)
            ischemia_factor = next(f for f in top.factors if f.name == "缺血窗口")
            self.assertIn("预计到达后余量", ischemia_factor.detail)

        # 两例并发确认 H1：系统自动分配不同团队/手术台。
        plans: list[object] = []
        errors: list[object] = []

        def accept(case):
            try:
                plans.append(self.orch.accept(case.id, "H1", by="市一值班"))
            except Exception as exc:  # noqa: BLE001 - 记录线程内异常
                errors.append(exc)

        threads = [
            threading.Thread(target=accept, args=(case_a,)),
            threading.Thread(target=accept, args=(case_b,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(plans), 2)
        pairs = {(p.team_id, p.table_id) for p in plans}
        self.assertEqual(pairs, {("T1A", "OR1"), ("T1B", "OR2")})
        case_ids = {p.case_id for p in plans}
        self.assertEqual(case_ids, {case_a.id, case_b.id})

        # 租约视图证明同一资源没有两个活跃持有者。
        active = self.orch.active_leases()
        self.assertEqual(len(active), 2)
        self.assertEqual(len({l.team_id for l in active}), 2)
        self.assertEqual(len({l.table_id for l in active}), 2)
        for lease in active:
            self.assertGreater(lease.expires_at, self.clock.now())

    def test_third_emergency_falls_through_to_next_hospital_with_explanation(self):
        case_a = register_case(self.orch, pseudonym="P-A", minutes_ago=30)
        case_b = register_case(self.orch, pseudonym="P-B", minutes_ago=30)
        self.orch.recommend(case_a.id)
        self.orch.recommend(case_b.id)
        self.orch.accept(case_a.id, "H1", by="市一值班")
        self.orch.accept(case_b.id, "H1", by="市一值班")

        case_c = register_case(self.orch, pseudonym="P-C", minutes_ago=20)
        recset = self.orch.recommend(case_c.id)
        h1 = next(r for r in recset.recommendations if r.hospital_id == "H1")
        h2 = next(r for r in recset.recommendations if r.hospital_id == "H2")
        self.assertFalse(h1.feasible)
        self.assertTrue(
            any("并发急诊占用" in r.message for r in h1.risks)
        )
        # 次优机构 H2 自动成为首位，且结冰天气以 WARN 明示。
        self.assertEqual(h2.rank, 1)
        self.assertTrue(h2.feasible)
        self.assertIn("severe_weather", {r.code for r in h2.risks})

        plan = self.orch.accept(case_c.id, "H2", by="省骨值班")
        self.assertEqual(plan.hospital_id, "H2")
        self.assertEqual(plan.version, 1)
        # 三家机构持有的资源两两不相交。
        held = {(l.team_id, l.table_id) for l in self.orch.active_leases()}
        self.assertEqual(
            held, {("T1A", "OR1"), ("T1B", "OR2"), ("T2", "OR3")}
        )

    def test_new_traffic_event_can_push_first_choice_out_of_window(self):
        case = register_case(self.orch, minutes_ago=295)
        recset = self.orch.recommend(case.id)
        h1 = next(r for r in recset.recommendations if r.hospital_id == "H1")
        # H1 到达余量 5 分钟，勉强可行但有 WARN。
        self.assertTrue(h1.feasible)
        self.assertIn("arrival_window_at_risk", {r.code for r in h1.risks})

        # 值班员从交通看板录入新事件，再筛一次。
        from catalog import route_key
        from domain import TrafficEvent

        self.orch.catalog.report_traffic_event(
            TrafficEvent(
                id="E9",
                route_key=route_key("H0", "H1"),
                title="城北快速路隧道内事故全封闭一条道",
                delay_minutes=20.0,
                observed_at=self.clock.now(),
            )
        )
        recset2 = self.orch.recommend(case.id)
        h1b = next(r for r in recset2.recommendations if r.hospital_id == "H1")
        self.assertFalse(h1b.feasible)
        self.assertIn("arrival_window_exceeded", {r.code for r in h1b.risks})
        self.assertTrue(recset2.escalated)


if __name__ == "__main__":
    unittest.main()
