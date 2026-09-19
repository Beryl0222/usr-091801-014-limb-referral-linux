"""弱网补传、幂等里程碑与改道留痕测试。"""

import unittest

from domain import CaseStatus, PlanState
from orchestrator import DomainError
from testsupport import make_orchestrator, register_case


class MilestoneRerouteTest(unittest.TestCase):
    def setUp(self):
        self.orch, self.clock = make_orchestrator()
        self.case = register_case(self.orch)
        self.orch.recommend(self.case.id)
        self.plan = self.orch.accept(self.case.id, "H1", by="市一值班")

    def _depart(self, event_id="evt-1"):
        return self.orch.record_milestone(
            self.case.id, client_event_id=event_id, code="departed",
            carrier_hospital_id="EMS1",
        )

    def test_duplicate_client_event_does_not_change_state(self):
        first = self._depart("evt-depart")
        self.assertFalse(first.duplicate)
        self.assertEqual(self.case.status, CaseStatus.IN_TRANSIT.value)
        milestones_before = len(self.case.milestones)

        # 弱网重试：相同事件 ID、重复送达，只返回原记录。
        retry = self._depart("evt-depart")
        self.assertTrue(retry.duplicate)
        self.assertIs(retry.milestone, first.milestone)
        self.assertEqual(len(self.case.milestones), milestones_before)

        # 途中节点重复补传同样幂等。
        w1 = self.orch.record_milestone(
            self.case.id, client_event_id="evt-wp1", code="waypoint",
            location="城北快速路 K8",
        )
        w1_retry = self.orch.record_milestone(
            self.case.id, client_event_id="evt-wp1", code="waypoint",
            location="被重试覆盖的位置",
        )
        self.assertTrue(w1_retry.duplicate)
        self.assertEqual(w1_retry.milestone.location, "城北快速路 K8")
        self.assertFalse(w1.milestone.replay)

    def test_late_replay_is_marked_and_kept_in_order(self):
        self._depart()
        self.clock.advance_minutes(10)
        # 断网 10 分钟后补传出发当时的节点（occurred_at 为过去时刻）。
        past = self.clock.now() - 10 * 60
        result = self.orch.record_milestone(
            self.case.id, client_event_id="evt-wp-old", code="waypoint",
            occurred_at=past, location="K10",
        )
        self.assertTrue(result.milestone.replay)
        self.assertEqual(result.milestone.recorded_at, self.clock.now())
        self.assertEqual(result.milestone.occurred_at, past)

    def test_reroute_keeps_original_plan_and_decision_reason(self):
        self._depart()
        self.clock.advance_minutes(20)
        # 沿途机构持续回报值守心跳。
        now = self.clock.now()
        self.orch.catalog.heartbeat_team("T2", now)
        self.orch.catalog.heartbeat_table("OR3", now)
        reason = "城北快速路 K12 多车追尾，预计拥堵加剧，改走省骨科"
        recset = self.orch.decide_reroute(
            self.case.id, reason=reason, by="调度组长",
            client_event_id="evt-reroute-1",
        )
        # 改道后立即生成新候选，旧计划仍可查且带理由。
        history = self.orch.plan_history(self.case.id)
        self.assertEqual(len(history), 1)  # 新医院尚未接受，只有旧版本
        old = history[0]
        self.assertEqual(old.id, self.plan.id)
        self.assertEqual(old.state, PlanState.SUPERSEDED.value)
        self.assertEqual(old.reroute_reason, reason)
        self.assertEqual(old.decided_by, "调度组长")
        self.assertIsNotNone(old.decided_at)

        h2 = next(r for r in recset.recommendations if r.hospital_id == "H2")
        self.assertTrue(h2.feasible)
        new_plan = self.orch.accept(self.case.id, "H2", by="省骨值班")
        self.assertEqual(new_plan.version, 2)
        self.assertEqual(new_plan.predecessor_plan_id, old.id)

        # 在途改道的新租约覆盖到预计到达之后，不会在途中被短 TTL 释放。
        self.clock.advance_minutes(16)
        new_lease = self.orch._lease_of(new_plan.lease_id)
        self.assertTrue(new_lease.active_at(self.clock.now()))

        history = self.orch.plan_history(self.case.id)
        self.assertEqual([p.version for p in history], [1, 2])
        self.assertEqual([p.state for p in history], ["superseded", "active"])
        # 旧版本的路线快照不被后续天气变化覆盖。
        self.assertEqual(history[0].route.road, "城北快速路")
        self.assertEqual(history[1].route.road, "G6 高速转山前路")

    def test_reroute_decision_is_idempotent_after_reconnect(self):
        self._depart()
        self.clock.advance_minutes(5)
        kwargs = dict(reason="临时交通管制", by="调度组长",
                      client_event_id="evt-reroute-net")
        first = self.orch.decide_reroute(self.case.id, **kwargs)
        leases_after_first = len(self.orch._leases)
        # 断网恢复，客户端拿着同一决定再次提交：不重复释放、不重复开单。
        second = self.orch.decide_reroute(self.case.id, **kwargs)
        self.assertIs(second, first)
        self.assertEqual(len(self.orch._leases), leases_after_first)
        reroute_milestones = [
            m for m in self.case.milestones if m.code == "reroute_decided"
        ]
        self.assertEqual(len(reroute_milestones), 1)

    def test_reassess_flags_exceeded_window_and_escalates(self):
        self._depart()
        # 离断已 30 分钟出发；再推进 275 分钟后已流失血 305 分钟，
        # H1 ETA 60 分钟（含事件延误 10）→ 到达余量 -5 分钟。
        self.clock.advance_minutes(275)
        report = self.orch.reassess_active_plan(self.case.id)
        codes = {r["code"] for r in report["risks"]}
        self.assertIn("arrival_window_exceeded", codes)
        self.assertTrue(
            any(e.status == "open" for e in self.case.escalations)
        )

    def test_human_redispatch_after_lease_expiry_keeps_cancelled_plan_in_history(self):
        self.clock.advance_minutes(16)  # H1 租约过期、计划取消并升级
        # 人工调度响应升级：先核实 H2 值守与能力资料，再重新筛选。
        now = self.clock.now()
        self.orch.catalog.heartbeat_team("T2", now)
        self.orch.catalog.heartbeat_table("OR3", now)
        self.orch.catalog.update_capability("H2", observed_at=now)
        recset = self.orch.recommend(self.case.id)
        h2 = next(r for r in recset.recommendations if r.hospital_id == "H2")
        self.assertTrue(h2.feasible)

        new_plan = self.orch.accept(self.case.id, "H2", by="省骨值班")
        self.assertEqual(new_plan.version, 2)
        self.assertEqual(new_plan.predecessor_plan_id, self.plan.id)
        history = self.orch.plan_history(self.case.id)
        self.assertEqual([p.state for p in history], ["cancelled", "active"])
        self.assertIn("人工调度", history[0].reroute_reason)

    def test_milestone_without_plan_is_rejected_atomically(self):
        # 新案例未出计划即报到达：拒绝且不留里程碑。
        case2 = register_case(self.orch, pseudonym="P-2")
        with self.assertRaises(DomainError):
            self.orch.record_milestone(
                case2.id, client_event_id="evt-bad", code="arrived_ed",
            )
        self.assertEqual(case2.milestones, [])


if __name__ == "__main__":
    unittest.main()
