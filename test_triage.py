"""筛选、排序、解释与风险标记测试。"""

import unittest

from domain import (
    ARRIVAL_RISK_BUFFER_MIN,
    CAPABILITY_STALE_AFTER_MIN,
    Preservation,
    RiskLevel,
)
from testsupport import T0, feasible, make_orchestrator, register_case, risk_codes


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.orch, self.clock = make_orchestrator()

    def recommend(self, case):
        return self.orch.recommend(case.id)

    def test_ranking_prefers_arrival_ischemia_margin(self):
        # 离断 30 分钟、前臂、目标口径 2mm：H1 与 H2 均可行；
        # H1 ETA=45/0.9+10=60 分钟，H2 ETA=75/0.72≈104.2 分钟。
        case = register_case(self.orch, minutes_ago=30, vessel=2.0)
        recset = self.recommend(case)
        self.assertFalse(recset.escalated)
        ranks = [(r.rank, r.hospital_id) for r in recset.recommendations]
        self.assertEqual(ranks[0], (1, "H1"))
        h1 = feasible(recset, "H1")
        h2 = feasible(recset, "H2")
        self.assertGreater(
            h1.ischemia_remaining_on_arrival_min,
            h2.ischemia_remaining_on_arrival_min,
        )

    def test_every_recommendation_carries_factor_level_explanation(self):
        case = register_case(self.orch)
        recset = self.recommend(case)
        h1 = feasible(recset, "H1")
        factor_names = {f.name for f in h1.factors}
        self.assertEqual(
            factor_names,
            {"能力匹配", "资料时效", "显微外科团队", "手术台", "路程", "道路环境", "交通事件", "缺血窗口"},
        )
        self.assertIn("协调排序", recset.notice)
        self.assertIn("不构成诊断", recset.notice)

    def test_stale_capability_and_dead_heartbeat_block_closest_hospital(self):
        # H3 最近（ETA 20 分钟），但资料 95 分钟未更新、心跳 11+ 分钟失联。
        case = register_case(self.orch)
        recset = self.recommend(case)
        h3 = feasible(recset, "H3")
        self.assertFalse(h3.feasible)
        codes = risk_codes(h3)
        self.assertIn("capability_stale", codes)
        self.assertIn("resource_unreachable", codes)
        self.assertTrue(
            all(r.level == RiskLevel.BLOCK.value for r in h3.risks
                if r.code in {"capability_stale", "resource_unreachable"})
        )
        # 阻断原因逐条可读。
        self.assertTrue(any("过期" in reason for reason in h3.excluded_reasons))

    def test_arrival_beyond_window_is_blocked_and_escalates(self):
        # 常温已流失血 310 分钟，H1 ETA 60 分钟 → 到达时越窗 10 分钟。
        case = register_case(self.orch, minutes_ago=310)
        recset = self.recommend(case)
        self.assertTrue(recset.escalated)
        self.assertEqual(case.escalations[-1].status, "open")
        for rec in recset.recommendations:
            self.assertFalse(rec.feasible)
        self.assertIn("arrival_window_exceeded", risk_codes(feasible(recset, "H1")))
        self.assertIn("arrival_window_exceeded", risk_codes(feasible(recset, "H2")))
        # H3 本可在窗口内到达，但资料过期/心跳失联同样阻断，不得自动推荐。
        h3 = feasible(recset, "H3")
        self.assertTrue(risk_codes(h3) & {"capability_stale", "resource_unreachable"})

    def test_tight_margin_warns_but_stays_feasible(self):
        # 离断 290 分钟：H1 到达余量 10 分钟（<15 缓冲但非负）。
        case = register_case(self.orch, minutes_ago=290)
        recset = self.recommend(case)
        h1 = feasible(recset, "H1")
        self.assertTrue(h1.feasible)
        self.assertIn("arrival_window_at_risk", risk_codes(h1))

    def test_cooled_preservation_extends_window(self):
        case_warm = register_case(self.orch, pseudonym="P-W", minutes_ago=330)
        case_cold = register_case(self.orch, pseudonym="P-C", minutes_ago=330,
                                  preservation=Preservation.COOLED.value)
        warm = self.recommend(case_warm)
        cold = self.recommend(case_cold)
        # 常温 360 分钟时限下 H1 越窗；冷存 480 分钟时限下仍有余量。
        self.assertFalse(feasible(warm, "H1").feasible)
        self.assertTrue(feasible(cold, "H1").feasible)

    def test_vessel_caliber_mismatch_excludes_center(self):
        # 目标 0.6mm：H1 机构/团队下限 0.8mm 不匹配；H2 下限 0.5mm 可做。
        case = register_case(self.orch, vessel=0.6)
        recset = self.recommend(case)
        h1 = feasible(recset, "H1")
        h2 = feasible(recset, "H2")
        self.assertFalse(h1.feasible)
        self.assertIn("capability_mismatch", risk_codes(h1))
        self.assertTrue(h2.feasible)

    def test_refreshed_capability_and_heartbeats_restore_h3(self):
        # 值班员核实 H3：补报能力资料、团队与手术台心跳。
        case = register_case(self.orch)
        now = self.clock.now()
        self.orch.catalog.update_capability(
            "H3", observed_at=now,
        )
        self.orch.catalog.heartbeat_team("T3", now)
        self.orch.catalog.heartbeat_table("OR4", now)
        recset = self.recommend(case)
        h3 = feasible(recset, "H3")
        self.assertTrue(h3.feasible)
        # 资料最新、路程最近 → 排名第一。
        self.assertEqual(h3.rank, 1)

    def test_weather_and_traffic_events_are_explained(self):
        case = register_case(self.orch)
        recset = self.recommend(case)
        h2 = feasible(recset, "H2")
        self.assertIn("severe_weather", risk_codes(h2))   # 结冰系数 0.72
        h1 = feasible(recset, "H1")
        self.assertIn("traffic_incident", risk_codes(h1))  # 快速路追尾
        self.assertAlmostEqual(h1.travel_minutes, 50.0, places=1)  # 45/0.9
        self.assertAlmostEqual(h1.eta_minutes, 60.0, places=1)

    def test_capability_threshold_constants_are_policy_not_magic(self):
        self.assertEqual(CAPABILITY_STALE_AFTER_MIN, 30)
        self.assertGreater(ARRIVAL_RISK_BUFFER_MIN, 0)
        self.assertGreater(T0, 0)


if __name__ == "__main__":
    unittest.main()
