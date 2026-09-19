"""患者身份仅在实际参与救治的机构间按阶段开放测试。"""

import unittest

from domain import IdentityStage
from testsupport import make_orchestrator, register_case


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.orch, self.clock = make_orchestrator()
        self.case = register_case(self.orch)
        self.orch.recommend(self.case.id)

    def test_stage_progression_and_field_levels(self):
        view = self.orch.identity_view

        # 接诊机构登记即见完整身份。
        origin = view(self.case.id, "H0")
        self.assertEqual(origin["stage"], IdentityStage.REGISTERED.value)
        self.assertEqual(origin["patient"]["view"], "full")
        self.assertEqual(origin["patient"]["real_name"], "测试患者")

        # 未参与机构看不到任何身份字段（只剩代号）。
        stranger = view(self.case.id, "H1")
        self.assertIsNone(stranger["stage"])
        self.assertEqual(stranger["patient"]["view"], "none")
        self.assertNotIn("real_name", stranger["patient"])
        self.assertNotIn("age", stranger["patient"])

        # 接受后：接收方只见摘要（代号 + 年龄性别），用于术前准备。
        self.orch.accept(self.case.id, "H1", by="市一值班")
        accepted = view(self.case.id, "H1")
        self.assertEqual(accepted["stage"], IdentityStage.ACCEPTED.value)
        self.assertEqual(accepted["patient"]["view"], "summary")
        self.assertIn("age", accepted["patient"])
        self.assertNotIn("real_name", accepted["patient"])
        self.assertNotIn("id_number", accepted["patient"])

        # 出发：实际承担转运的 120 分站见代号与交接摘要。
        self.orch.record_milestone(
            self.case.id, client_event_id="evt-dep", code="departed",
            carrier_hospital_id="EMS1",
        )
        carrier = view(self.case.id, "EMS1")
        self.assertEqual(carrier["stage"], IdentityStage.IN_TRANSIT.value)
        self.assertEqual(carrier["patient"]["view"], "summary")
        self.assertNotIn("real_name", carrier["patient"])

        # 到达但未交接：接收方仍只见摘要。
        self.orch.record_milestone(
            self.case.id, client_event_id="evt-arr", code="arrived_ed",
        )
        self.assertEqual(view(self.case.id, "H1")["patient"]["view"], "summary")

        # 实际交接后才完整开放。
        self.orch.record_milestone(
            self.case.id, client_event_id="evt-hand", code="handover",
        )
        in_care = view(self.case.id, "H1")
        self.assertEqual(in_care["stage"], IdentityStage.IN_CARE.value)
        self.assertEqual(in_care["patient"]["real_name"], "测试患者")
        self.assertEqual(in_care["patient"]["id_number"], "110101199001011234")

    def test_reroute_revokes_old_hospital_access(self):
        view = self.orch.identity_view
        self.orch.accept(self.case.id, "H1", by="市一值班")
        self.orch.record_milestone(
            self.case.id, client_event_id="evt-dep", code="departed",
            carrier_hospital_id="EMS1",
        )
        self.clock.advance_minutes(15)
        now = self.clock.now()
        self.orch.catalog.heartbeat_team("T2", now)
        self.orch.catalog.heartbeat_table("OR3", now)

        self.orch.decide_reroute(
            self.case.id, reason="快速路封闭", by="调度组长",
            client_event_id="evt-rr",
        )
        # 旧接收方尚未把患者送达，授权立即收回。
        self.assertIsNone(view(self.case.id, "H1")["stage"])
        self.assertEqual(view(self.case.id, "H1")["patient"]["view"], "none")

        self.orch.accept(self.case.id, "H2", by="省骨值班")
        new = view(self.case.id, "H2")
        self.assertEqual(new["stage"], IdentityStage.ACCEPTED.value)
        self.assertNotIn("real_name", new["patient"])

        # 接诊机构与转运方的授权不受改道影响。
        self.assertEqual(view(self.case.id, "H0")["patient"]["view"], "full")
        self.assertEqual(view(self.case.id, "EMS1")["patient"]["view"], "summary")

        # 授予与收回均有审计留痕。
        h1_grants = [d for d in self.case.disclosures if d.hospital_id == "H1"]
        self.assertTrue(all(d.revoked_at is not None for d in h1_grants))
        self.assertTrue(
            all("改道" in (d.revoke_reason or "") for d in h1_grants)
        )

    def test_never_participated_hospital_keeps_no_access_throughout(self):
        self.orch.accept(self.case.id, "H1", by="市一值班")
        view = self.orch.identity_view(self.case.id, "H3")
        self.assertEqual(view["patient"]["view"], "none")
        self.assertIsNone(view["stage"])


if __name__ == "__main__":
    unittest.main()
