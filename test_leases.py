"""短时租约、并发占用与 TTL 自动释放测试。"""

import threading
import unittest

from domain import LeaseState
from orchestrator import DomainError
from testsupport import make_orchestrator, register_case


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.orch, self.clock = make_orchestrator()

    def _accept(self, case, hospital_id="H1", **kwargs):
        self.orch.recommend(case.id)
        return self.orch.accept(case.id, hospital_id, by="接收值班", **kwargs)

    def test_second_emergency_gets_different_pair_not_double_booking(self):
        case_a = register_case(self.orch, pseudonym="P-A")
        plan_a = self._accept(case_a)
        self.assertEqual((plan_a.team_id, plan_a.table_id), ("T1A", "OR1"))

        case_b = register_case(self.orch, pseudonym="P-B")
        recset_b = self.orch.recommend(case_b.id)
        h1_b = next(r for r in recset_b.recommendations if r.hospital_id == "H1")
        # H1 仍可行，但系统自动选了另一支团队和另一张台。
        self.assertTrue(h1_b.feasible)
        self.assertEqual((h1_b.option.team_id, h1_b.option.table_id), ("T1B", "OR2"))
        # 并发占用因子明确列出持有者。
        self.assertTrue(any(f.name == "并发占用" for f in h1_b.factors))
        plan_b = self.orch.accept(case_b.id, "H1", by="接收值班")
        self.assertEqual((plan_b.team_id, plan_b.table_id), ("T1B", "OR2"))

        active = {(l.team_id, l.table_id) for l in self.orch.active_leases()}
        self.assertEqual(active, {("T1A", "OR1"), ("T1B", "OR2")})

    def test_explicit_request_for_occupied_team_or_table_is_rejected(self):
        case_a = register_case(self.orch, pseudonym="P-A")
        self._accept(case_a)
        case_b = register_case(self.orch, pseudonym="P-B")
        self.orch.recommend(case_b.id)

        with self.assertRaises(DomainError) as ctx:
            self.orch.accept(case_b.id, "H1", by="接收值班", team_id="T1A", table_id="OR2")
        self.assertEqual(ctx.exception.code, "team_busy")
        self.assertIn(case_a.id, str(ctx.exception))

        with self.assertRaises(DomainError) as ctx:
            self.orch.accept(case_b.id, "H1", by="接收值班", team_id="T1B", table_id="OR1")
        self.assertEqual(ctx.exception.code, "table_busy")

    def test_third_emergency_cannot_get_h1_when_every_team_held(self):
        self._accept(register_case(self.orch, pseudonym="P-A"))
        self._accept(register_case(self.orch, pseudonym="P-B"))
        case_c = register_case(self.orch, pseudonym="P-C")
        recset = self.orch.recommend(case_c.id)
        h1 = next(r for r in recset.recommendations if r.hospital_id == "H1")
        self.assertFalse(h1.feasible)
        self.assertTrue(any("占用" in reason for reason in h1.excluded_reasons))

    def test_concurrent_accept_threads_only_one_wins_same_resource(self):
        results: list[object] = []

        def attempt(pseudonym):
            try:
                case = register_case(self.orch, pseudonym=pseudonym)
                self.orch.recommend(case.id)
                plan = self.orch.accept(
                    case.id, "H1", by="接收值班", team_id="T1A", table_id="OR1"
                )
                results.append(("ok", pseudonym, plan.id, case.id))
            except DomainError as error:
                results.append(("busy", pseudonym, error.code, None))

        threads = [threading.Thread(target=attempt, args=(name,)) for name in ("P-X", "P-Y")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r[0] == "ok"]
        losers = [r for r in results if r[0] == "busy"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn(losers[0][2], {"team_busy", "table_busy"})
        self.assertEqual(len(self.orch.active_leases()), 1)

    def test_lease_ttl_expires_and_frees_resource(self):
        case = register_case(self.orch)
        plan = self._accept(case)
        lease = self.orch._lease_of(plan.lease_id)
        self.assertEqual(lease.state, LeaseState.ACTIVE.value)

        self.clock.advance_minutes(16)  # 超过 15 分钟租约
        # 机构持续上报心跳，排除“失联”这一独立阻断因素。
        now = self.clock.now()
        self.orch.catalog.heartbeat_team("T1A", now)
        self.orch.catalog.heartbeat_table("OR1", now)

        self.assertEqual(self.orch.active_leases(), [])
        self.assertEqual(lease.state, LeaseState.EXPIRED.value)
        # 资源释放后，新案例可以拿到同一团队与手术台。
        case_b = register_case(self.orch, pseudonym="P-B")
        plan_b = self._accept(case_b)
        self.assertEqual((plan_b.team_id, plan_b.table_id), ("T1A", "OR1"))

    def test_departure_extends_lease_beyond_short_ttl(self):
        case = register_case(self.orch)
        plan = self._accept(case)

        # 未登记的转运机构不能获得身份开放。
        with self.assertRaises(KeyError):
            self.orch.record_milestone(
                case.id, client_event_id="evt-depart-bad", code="departed",
                carrier_hospital_id="H9",
            )

        result = self.orch.record_milestone(
            case.id, client_event_id="evt-depart", code="departed",
            carrier_hospital_id="EMS1",
        )
        self.assertFalse(result.duplicate)

        self.clock.advance_minutes(16)
        lease = self.orch._lease_of(plan.lease_id)
        self.assertTrue(lease.active_at(self.clock.now()))

    def test_reroute_releases_old_lease_for_reuse(self):
        case_a = register_case(self.orch)
        plan_a = self._accept(case_a)
        self.orch.decide_reroute(case_a.id, reason="城北快速路临时封闭", by="调度组长")
        self.assertEqual(
            self.orch._lease_of(plan_a.lease_id).state,
            LeaseState.RELEASED.value,
        )
        # 改道由 A 自己接收 H2（结冰路线仍可通行，仅 WARN）。
        plan_a2 = self.orch.accept(case_a.id, "H2", by="省骨值班")
        self.assertEqual(plan_a2.hospital_id, "H2")

        # 旧资源立即可供新案例使用。
        now = self.clock.now()
        self.orch.catalog.heartbeat_team("T1A", now)
        self.orch.catalog.heartbeat_table("OR1", now)
        case_b = register_case(self.orch, pseudonym="P-B")
        plan_b = self._accept(case_b)
        self.assertEqual((plan_b.team_id, plan_b.table_id), ("T1A", "OR1"))

    def test_lease_expiry_cancels_plan_revokes_access_and_escalates(self):
        case = register_case(self.orch)
        plan = self._accept(case)
        self.clock.advance_minutes(16)
        # 触发一次扫描（新推荐会清理过期租约并联动案例）。
        recset = self.orch.recommend(case.id)
        self.assertTrue(recset.escalated)
        self.assertIn("resource_unreachable", case.escalations[-1].reasons)

        history = self.orch.plan_history(case.id)
        self.assertEqual(history[-1].state, "cancelled")
        self.assertIn("人工调度", history[-1].reroute_reason)
        # 原接收方身份授权被收回。
        view = self.orch.identity_view(case.id, "H1")
        self.assertEqual(view["patient"]["view"], "none")
        self.assertEqual(case.status, "recommended")

    def test_accept_rechecks_window_and_escalates_when_time_ran_out(self):
        # 登记/筛选时余量 10 分钟尚可行；拖到 20 分钟后再确认即越窗。
        case = register_case(self.orch, minutes_ago=290)
        recset = self.orch.recommend(case.id)
        h1 = next(r for r in recset.recommendations if r.hospital_id == "H1")
        self.assertTrue(h1.feasible)
        self.clock.advance_minutes(20)
        # 机构在此期间持续心跳，排除失联因素，单独验证窗口复算。
        now = self.clock.now()
        self.orch.catalog.heartbeat_team("T1A", now)
        self.orch.catalog.heartbeat_table("OR1", now)
        with self.assertRaises(DomainError) as ctx:
            self.orch.accept(case.id, "H1", by="接收值班")
        self.assertEqual(ctx.exception.code, "window_exceeded")
        self.assertTrue(case.escalations)
        self.assertIn("arrival_window_exceeded", case.escalations[-1].reasons)


if __name__ == "__main__":
    unittest.main()
