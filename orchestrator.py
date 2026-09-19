"""急救编排用例层：登记 → 推荐 → 显式接受与租约 → 转运 → 交接。

并发安全：所有改变状态的方法都在同一把锁内完成“检查 + 占用”，
因此两例急诊并发接受同一团队/手术台时，只有一例成功。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from catalog import NetworkCatalog, RouteEstimate
from domain import (
    ADVICE_NOTICE,
    RESOURCE_LEASE_TTL_MIN,
    SEVERE_WEATHER_FACTOR,
    Clock,
    Disclosure,
    Escalation,
    EscalationStatus,
    Case,
    CaseStatus,
    IdentityStage,
    Injury,
    Lease,
    LeaseState,
    Milestone,
    MilestoneCode,
    Patient,
    PlanState,
    RecommendationSet,
    RouteSnapshot,
    TransportPlan,
    Vitals,
    identity_view_level,
    min_to_sec,
    patient_view,
)
from triage import TriageEngine, select_free_resources


class DomainError(Exception):
    def __init__(self, message: str, *, code: str = "domain_error", http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass
class MilestoneResult:
    milestone: Milestone
    duplicate: bool


class Orchestrator:
    def __init__(self, catalog: NetworkCatalog, clock: Optional[Clock] = None):
        self.catalog = catalog
        self.clock = clock or Clock()
        self.triage = TriageEngine(catalog)
        self._lock = threading.RLock()
        self._cases: dict[str, Case] = {}
        self._leases: list[Lease] = []
        self._seq = 0

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    # ------------------------------------------------------------------
    # 案例登记
    # ------------------------------------------------------------------

    def register_case(
        self,
        *,
        origin_hospital_id: str,
        recorded_by: str,
        patient: Patient,
        injury: Injury,
        vitals: Vitals,
    ) -> Case:
        now = self.clock.now()
        self.catalog.require_hospital(origin_hospital_id)
        with self._lock:
            case = Case(
                id=self._new_id("case"),
                created_at=now,
                origin_hospital_id=origin_hospital_id,
                recorded_by=recorded_by,
                patient=patient,
                injury=injury,
                vitals=vitals,
            )
            self._cases[case.id] = case
            self._grant(
                case,
                origin_hospital_id,
                IdentityStage.REGISTERED,
                now,
                "接诊机构登记伤情，需完整身份完成首诊记录",
            )
            return case

    def get_case(self, case_id: str) -> Case:
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise DomainError(f"案例 {case_id} 不存在", code="not_found", http_status=404)
            return case

    def case_clock(self, case_id: str) -> dict:
        """案例时钟：离断时刻、缺血时限、已流失血与当前余量。"""

        case = self.get_case(case_id)
        now = self.clock.now()
        injury = case.injury
        return {
            "case_id": case.id,
            "now": now,
            "amputated_at": injury.amputated_at,
            "preservation": injury.preservation,
            "ischemia_limit_min": round(injury.ischemia_limit_sec() / 60),
            "elapsed_min": round(injury.elapsed_sec(now) / 60, 1),
            "remaining_min": round(injury.remaining_sec(now) / 60, 1),
            "deadline_at": injury.amputated_at + injury.ischemia_limit_sec(),
            "status": case.status,
        }

    # ------------------------------------------------------------------
    # 推荐与升级
    # ------------------------------------------------------------------

    def recommend(self, case_id: str) -> RecommendationSet:
        now = self.clock.now()
        with self._lock:
            # 先处理到期租约的联动，再判定案例当前状态。
            self._sweep_expired(now)
            case = self.get_case(case_id)
            if case.status not in (
                CaseStatus.OPEN.value,
                CaseStatus.RECOMMENDED.value,
            ):
                raise DomainError(
                    f"案例当前状态 {case.status} 不允许重新筛选",
                    code="invalid_state",
                    http_status=409,
                )
            recset = self._build_recommendation(case, now)
            case.status = CaseStatus.RECOMMENDED.value
            return recset

    def _build_recommendation(self, case: Case, now: float) -> RecommendationSet:
        self._sweep_expired(now)
        recommendations = self.triage.evaluate_case(
            origin_hospital_id=case.origin_hospital_id,
            injury=case.injury,
            now=now,
            occupied=self._occupied,
        )
        recset = RecommendationSet(
            id=self._new_id("recset"),
            case_id=case.id,
            generated_at=now,
            recommendations=recommendations,
            escalated=False,
        )
        case.recommendations.append(recset)

        feasible = [r for r in recommendations if r.feasible]
        blocking = sorted(
            {risk.code for r in recommendations for risk in r.risks if risk.level == "block"}
        )
        if not feasible:
            recset.escalated = True
            self._open_escalation(
                case,
                blocking,
                "全部候选机构均不可自动接收（能力资料、资源心跳或缺血窗口不满足），"
                "排序仅作参考，立即由人工调度介入",
                now,
            )
        return recset

    def _open_escalation(self, case: Case, reasons: list[str], message: str, now: float) -> Escalation:
        # 同案例同原因的未关闭升级不重复开单。
        for esc in reversed(case.escalations):
            if esc.status == EscalationStatus.OPEN.value and esc.reasons == reasons:
                return esc
        esc = Escalation(
            id=self._new_id("esc"),
            case_id=case.id,
            reasons=reasons,
            message=message,
            status=EscalationStatus.OPEN.value,
            created_at=now,
        )
        case.escalations.append(esc)
        return esc

    def acknowledge_escalation(self, case_id: str, escalation_id: str, by: str, note: str = "") -> Escalation:
        now = self.clock.now()
        with self._lock:
            case = self.get_case(case_id)
            esc = next((e for e in case.escalations if e.id == escalation_id), None)
            if esc is None:
                raise DomainError("升级单不存在", code="not_found", http_status=404)
            if esc.status == EscalationStatus.OPEN.value:
                esc.status = EscalationStatus.ACKNOWLEDGED.value
                esc.acknowledged_at = now
                esc.acknowledged_by = by
                esc.resolution_note = note
            return esc

    # ------------------------------------------------------------------
    # 显式接受 + 原子租约 + 转运计划
    # ------------------------------------------------------------------

    def accept(
        self,
        case_id: str,
        hospital_id: str,
        *,
        by: str,
        team_id: Optional[str] = None,
        table_id: Optional[str] = None,
    ) -> TransportPlan:
        """候选医院明确接受。重新核验资源与窗口，通过后原子锁定并给出计划。"""

        now = self.clock.now()
        with self._lock:
            self._sweep_expired(now)
            case = self.get_case(case_id)
            rerouting = case.status == CaseStatus.REROUTING.value
            if case.status not in (
                CaseStatus.RECOMMENDED.value,
                CaseStatus.REROUTING.value,
            ):
                raise DomainError(
                    "须先完成筛选且案例未交接，候选医院才能接受",
                    code="invalid_state",
                    http_status=409,
                )

            recset = case.latest_recommendation()
            rec = next((r for r in recset.recommendations if r.hospital_id == hospital_id), None)
            if rec is None:
                raise DomainError("该机构不在最新候选名单中，请重新筛选", code="not_candidate", http_status=409)
            if not rec.feasible:
                raise DomainError(
                    f"{rec.hospital_name} 存在阻断性风险，不能确认接收："
                    + "；".join(r.message for r in rec.risks if r.level == "block"),
                    code="candidate_blocked",
                    http_status=409,
                )

            hospital = self.catalog.require_hospital(hospital_id)
            explicit_resource = team_id is not None or table_id is not None
            team = self.catalog.require_team(team_id or rec.option.team_id)
            table = self.catalog.require_table(table_id or rec.option.table_id)
            try:
                self._validate_resource_for_accept(case, hospital, team, table, now)
            except DomainError as error:
                # 未显式点名资源时：推荐生成后资源被并发抢走，
                # 在锁内自动改选另一对仍空闲的资源，而不是让确认失败。
                if explicit_resource or error.code not in ("team_busy", "table_busy"):
                    raise
                free_teams, free_tables = select_free_resources(
                    hospital, case.injury, now, self._occupied
                )
                if not free_teams or not free_tables:
                    raise
                team, table = free_teams[0], free_tables[0]
                self._validate_resource_for_accept(case, hospital, team, table, now)

            estimate = self.catalog.estimate_route(case.origin_hospital_id, hospital_id, now)
            if not estimate.exists:
                raise DomainError("路线数据缺失，无法生成转运计划", code="no_route", http_status=409)
            eta_minutes = estimate.total_minutes()
            arrival_remaining_min = case.injury.remaining_sec(now) / 60 - eta_minutes
            if arrival_remaining_min < 0:
                self._open_escalation(
                    case,
                    ["arrival_window_exceeded"],
                    f"接受时复算预计到达已越窗 {round(-arrival_remaining_min)} 分钟，转人工调度",
                    now,
                )
                raise DomainError(
                    "接受时复算预计到达已超出缺血窗口，必须改由人工调度",
                    code="window_exceeded",
                    http_status=409,
                )

            old_plan = case.plans[-1] if rerouting and case.plans else case.active_plan()
            old_lease = None
            if rerouting:
                if old_plan is None:
                    raise DomainError("改道状态缺少原计划", code="invalid_state", http_status=409)
                old_lease = self._lease_of(old_plan.lease_id)
            elif old_plan is not None:
                raise DomainError("案例已有生效计划，如需变更请先走改道流程", code="invalid_state", http_status=409)

            lease = Lease(
                id=self._new_id("lease"),
                case_id=case.id,
                hospital_id=hospital_id,
                team_id=team.id,
                table_id=table.id,
                state=LeaseState.ACTIVE.value,
                granted_at=now,
                expires_at=now + min_to_sec(RESOURCE_LEASE_TTL_MIN),
            )
            self._leases.append(lease)

            snapshot = self._snapshot(estimate)
            version = max((p.version for p in case.plans), default=0) + 1
            plan = TransportPlan(
                id=self._new_id("plan"),
                case_id=case.id,
                version=version,
                hospital_id=hospital_id,
                hospital_name=hospital.name,
                team_id=team.id,
                table_id=table.id,
                lease_id=lease.id,
                route=snapshot,
                eta_minutes=round(eta_minutes, 1),
                planned_at=now,
                planned_arrival_at=now + eta_minutes * 60,
                ischemia_deadline=case.injury.amputated_at + case.injury.ischemia_limit_sec(),
                ischemia_remaining_on_arrival_min=round(arrival_remaining_min, 1),
                state=PlanState.ACTIVE.value,
                basis_recommendation_id=recset.id,
                decided_by=by,
                predecessor_plan_id=(case.plans[-1].id if case.plans else None),
            )
            case.plans.append(plan)

            if rerouting:
                old_plan.superseded_by = plan.id
                if old_plan.state == PlanState.ACTIVE.value:
                    old_plan.state = PlanState.SUPERSEDED.value
                    if old_lease is not None and old_lease.state == LeaseState.ACTIVE.value:
                        self._release_lease(old_lease, now, "改道：调度决定转往其他机构")
                    self._revoke(case, old_plan.hospital_id, now, "改道：不再送往该机构")
                # 旧计划若已被租约过期联动取消，则保持 cancelled，
                # 释放、收回授权与升级均已由过期联动完成，不重复处置。

            self._grant(
                case,
                hospital_id,
                IdentityStage.ACCEPTED,
                now,
                f"{hospital.name} 已明确接受并锁定团队/手术台，开放伤情摘要用于术前准备",
            )

            departed = any(m.code == MilestoneCode.DEPARTED.value for m in case.milestones)
            if departed:
                # 在途改道产生的新租约同样要覆盖到预计到达之后，
                # 不能因 15 分钟短 TTL 在途中被自动释放。
                lease.expires_at = max(
                    lease.expires_at, plan.planned_arrival_at + min_to_sec(15)
                )
            case.status = CaseStatus.IN_TRANSIT.value if departed else CaseStatus.ACCEPTED.value
            return plan

    def _validate_resource_for_accept(self, case, hospital, team, table, now) -> None:
        if team.hospital_id != hospital.id or table.hospital_id != hospital.id:
            raise DomainError("团队或手术台不属于接受机构", code="resource_mismatch", http_status=409)
        if not team.on_duty:
            raise DomainError(f"团队 {team.name} 未在值班", code="team_off_duty", http_status=409)
        if case.injury.body_part not in team.capable_parts:
            raise DomainError(f"团队 {team.name} 不能处理该部位", code="team_incapable", http_status=409)
        if (
            case.injury.vessel_diameter_mm is not None
            and team.min_vessel_mm > case.injury.vessel_diameter_mm + 1e-9
        ):
            raise DomainError(
                f"团队 {team.name} 可吻合口径下限 {team.min_vessel_mm}mm，不满足目标口径",
                code="team_caliber",
                http_status=409,
            )
        if not team.heartbeat_fresh(now):
            raise DomainError(
                f"团队 {team.name} 心跳失联，接受前必须重新确认值守状态",
                code="team_unreachable",
                http_status=409,
            )
        if not table.microsurgery_ready or not table.reported_available:
            raise DomainError(f"手术台 {table.code} 非可用显微外科台", code="table_unavailable", http_status=409)
        if not table.heartbeat_fresh(now):
            raise DomainError(
                f"手术台 {table.code} 状态心跳失联，接受前必须重新确认空闲",
                code="table_unreachable",
                http_status=409,
            )
        team_holder = self._occupied(team.id, None)
        table_holder = self._occupied(None, table.id)
        if team_holder and team_holder != case.id:
            raise DomainError(
                f"团队 {team.name} 已被并发案例 {team_holder} 锁定",
                code="team_busy",
                http_status=409,
            )
        if table_holder and table_holder != case.id:
            raise DomainError(
                f"手术台 {table.code} 已被并发案例 {table_holder} 锁定",
                code="table_busy",
                http_status=409,
            )

    # ------------------------------------------------------------------
    # 转运里程碑（弱网补传、幂等）
    # ------------------------------------------------------------------

    def record_milestone(
        self,
        case_id: str,
        *,
        client_event_id: str,
        code: str,
        occurred_at: Optional[float] = None,
        location: Optional[str] = None,
        note: Optional[str] = None,
        carrier_hospital_id: Optional[str] = None,
        by: Optional[str] = None,
    ) -> MilestoneResult:
        now = self.clock.now()
        if occurred_at is None:
            occurred_at = now
        with self._lock:
            case = self.get_case(case_id)
            try:
                milestone_code = MilestoneCode(code)
            except ValueError:
                raise DomainError(f"未知里程碑类型 {code}", code="unknown_milestone", http_status=400)

            # 幂等：同一客户端事件 ID 重复到达不产生任何状态变化。
            existing = next(
                (m for m in case.milestones if m.client_event_id == client_event_id),
                None,
            )
            if existing is not None:
                return MilestoneResult(milestone=existing, duplicate=True)

            milestone = Milestone(
                client_event_id=client_event_id,
                code=milestone_code.value,
                occurred_at=occurred_at,
                recorded_at=now,
                location=location,
                note=note,
                replay=occurred_at < now - 1.0,
            )
            # 先完成全部状态校验与变更，再落档里程碑，避免半应用状态。
            self._apply_milestone(case, milestone_code, now, carrier_hospital_id)
            case.milestones.append(milestone)
            return MilestoneResult(milestone=milestone, duplicate=False)

    def _apply_milestone(self, case: Case, code: MilestoneCode, now: float, carrier_id: Optional[str]) -> None:
        plan = case.active_plan()

        if code == MilestoneCode.DEPARTED:
            if plan is None:
                raise DomainError("没有生效计划，不能记录出发", code="no_plan", http_status=409)
            case.status = CaseStatus.IN_TRANSIT.value
            # 出发后租约延长到预计到达之后，避免长途转运中 15 分钟 TTL 提前释放。
            lease = self._lease_of(plan.lease_id)
            if lease is not None and lease.active_at(now):
                lease.expires_at = max(
                    lease.expires_at, plan.planned_arrival_at + min_to_sec(15)
                )
            if carrier_id:
                self.catalog.require_carrier(carrier_id)
                self._grant(
                    case,
                    carrier_id,
                    IdentityStage.IN_TRANSIT,
                    now,
                    "转运机构实际承担途中护送，开放代号与交接信息",
                )
            return

        if code == MilestoneCode.WAYPOINT:
            return  # 途中节点仅留痕，不改变状态

        if code == MilestoneCode.REROUTE_DECIDED:
            # 改道决定应通过 decide_reroute 落账，直接补记里程碑不重复处理。
            return

        if code == MilestoneCode.ARRIVED_ED:
            if plan is None:
                raise DomainError("没有生效计划，不能记录到达", code="no_plan", http_status=409)
            if case.status != CaseStatus.IN_TRANSIT.value:
                raise DomainError("仅转运途中可记录到达急诊", code="invalid_state", http_status=409)
            return  # 已到达但尚未交接，接收方仍只见摘要

        if code == MilestoneCode.HANDOVER:
            if plan is None:
                raise DomainError("没有生效计划，不能记录交接", code="no_plan", http_status=409)
            if case.status not in (CaseStatus.IN_TRANSIT.value, CaseStatus.ACCEPTED.value):
                raise DomainError("当前状态不能完成交接", code="invalid_state", http_status=409)
            self._grant(
                case,
                plan.hospital_id,
                IdentityStage.IN_CARE,
                now,
                "患者已实际送达并交接，接收机构开始参与救治，开放完整身份",
            )
            lease = self._lease_of(plan.lease_id)
            if lease is not None and lease.state == LeaseState.ACTIVE.value:
                self._release_lease(lease, now, "患者已交接，资源进入救治使用")
            plan.state = PlanState.COMPLETED.value
            case.status = CaseStatus.DELIVERED.value
            return

        if code == MilestoneCode.BLOOD_FLOW_RESTORED:
            if case.status not in (CaseStatus.DELIVERED.value, CaseStatus.CLOSED.value):
                raise DomainError("须先完成交接才能记录恢复血运", code="invalid_state", http_status=409)
            case.status = CaseStatus.CLOSED.value

    # ------------------------------------------------------------------
    # 改道：原计划与决定理由完整保留
    # ------------------------------------------------------------------

    def decide_reroute(
        self,
        case_id: str,
        *,
        reason: str,
        by: str,
        client_event_id: Optional[str] = None,
        occurred_at: Optional[float] = None,
    ) -> RecommendationSet:
        now = self.clock.now()
        if occurred_at is None:
            occurred_at = now
        with self._lock:
            self._sweep_expired(now)
            case = self.get_case(case_id)
            event_id = client_event_id or f"reroute-{case.id}-{len(case.plans)}"

            # 断网重连后重复提交同一改道决定：直接沿用既有结论，
            # 不重复释放租约、不重复开单，也不因案例已在改道中而报错。
            existing = next(
                (m for m in case.milestones if m.client_event_id == event_id), None
            )
            if existing is not None:
                return case.latest_recommendation()

            if case.status not in (
                CaseStatus.ACCEPTED.value,
                CaseStatus.IN_TRANSIT.value,
                CaseStatus.REROUTING.value,
            ):
                raise DomainError(
                    "仅已确认计划且尚未交接的案例可以改道",
                    code="invalid_state",
                    http_status=409,
                )

            last_plan = case.plans[-1] if case.plans else None
            # 租约过期联动已把计划取消并完成释放/收回/升级时，
            # 这里只补记人工改道决定并重筛，不重复处置旧计划。
            expiry_cancelled = (
                case.status == CaseStatus.REROUTING.value
                and last_plan is not None
                and last_plan.state == PlanState.CANCELLED.value
            )

            case.status = CaseStatus.REROUTING.value
            case.milestones.append(
                Milestone(
                    client_event_id=event_id,
                    code=MilestoneCode.REROUTE_DECIDED.value,
                    occurred_at=occurred_at,
                    recorded_at=now,
                    note=reason,
                    replay=occurred_at < now - 1.0,
                )
            )
            if expiry_cancelled:
                last_plan.reroute_reason = (
                    f"{last_plan.reroute_reason}；人工改道理由：{reason}"
                )
                last_plan.decided_by = by
                last_plan.decided_at = occurred_at
                return self._build_recommendation(case, now)

            old_plan = case.active_plan()
            if old_plan is None:
                raise DomainError("改道时缺少生效计划", code="no_plan", http_status=409)
            old_plan.state = PlanState.SUPERSEDED.value
            old_plan.reroute_reason = reason
            old_plan.decided_by = by
            old_plan.decided_at = occurred_at
            lease = self._lease_of(old_plan.lease_id)
            if lease is not None and lease.state == LeaseState.ACTIVE.value:
                self._release_lease(lease, now, f"改道：{reason}")
            self._revoke(case, old_plan.hospital_id, now, f"改道：{reason}")
            return self._build_recommendation(case, now)

    def reassess_active_plan(self, case_id: str) -> dict:
        """途中复算当前计划的道路与窗口；越窗即醒目标记并升级人工调度。"""

        now = self.clock.now()
        with self._lock:
            case = self.get_case(case_id)
            plan = case.active_plan()
            if plan is None:
                raise DomainError("没有生效计划可复算", code="no_plan", http_status=409)
            estimate = self.catalog.estimate_route(
                case.origin_hospital_id, plan.hospital_id, now
            )
            risks = []
            eta_minutes = estimate.total_minutes() if estimate.exists else None
            if not estimate.exists:
                risks.append({"code": "no_route_data", "level": "block", "message": "路况观测中断"})
            else:
                if estimate.weather_speed_factor <= SEVERE_WEATHER_FACTOR:
                    risks.append({
                        "code": "severe_weather",
                        "level": "warn",
                        "message": f"天气恶化，通行系数 {estimate.weather_speed_factor}",
                    })
                if estimate.events:
                    risks.append({
                        "code": "traffic_incident",
                        "level": "warn",
                        "message": "新增交通事件："
                        + "、".join(e.title for e in estimate.events),
                    })
                arrival_remaining = case.injury.remaining_sec(now) / 60 - eta_minutes
                if arrival_remaining < 0:
                    risks.append({
                        "code": "arrival_window_exceeded",
                        "level": "block",
                        "message": f"复算预计到达越窗 {round(-arrival_remaining)} 分钟",
                    })
                    self._open_escalation(
                        case,
                        ["arrival_window_exceeded"],
                        f"途中复算预计到达越窗 {round(-arrival_remaining)} 分钟，"
                        "需立即人工决定改道",
                        now,
                    )
            return {
                "case_id": case.id,
                "plan_id": plan.id,
                "version": plan.version,
                "reassessed_at": now,
                "eta_minutes": round(eta_minutes, 1) if eta_minutes is not None else None,
                "risks": risks,
                "notice": ADVICE_NOTICE,
            }

    # ------------------------------------------------------------------
    # 身份分阶段开放
    # ------------------------------------------------------------------

    def identity_view(self, case_id: str, hospital_id: str) -> dict:
        """机构查询自己在该案例下可见的患者身份层级。"""

        now = self.clock.now()
        with self._lock:
            case = self.get_case(case_id)
            stage = self._active_stage(case, hospital_id, now)
            level = identity_view_level(stage) if stage else "none"
            return {
                "case_id": case.id,
                "hospital_id": hospital_id,
                "stage": stage.value if stage else None,
                "patient": patient_view(case.patient, level),
            }

    def _active_stage(self, case: Case, hospital_id: str, now) -> Optional[IdentityStage]:
        active = [d for d in case.disclosures if d.hospital_id == hospital_id and d.active_at(now)]
        if not active:
            return None
        # 以其中最高权限阶段为准。
        order = [
            IdentityStage.REGISTERED,
            IdentityStage.ACCEPTED,
            IdentityStage.IN_TRANSIT,
            IdentityStage.IN_CARE,
        ]
        granted = {IdentityStage(d.stage) for d in active}
        return next((s for s in reversed(order) if s in granted), None)

    def _grant(
        self, case: Case, hospital_id: str, stage: IdentityStage, now: float, reason: str
    ) -> Disclosure:
        for d in case.disclosures:
            if (
                d.hospital_id == hospital_id
                and d.stage == stage.value
                and d.active_at(now)
            ):
                return d
        disclosure = Disclosure(
            id=self._new_id("disc"),
            case_id=case.id,
            hospital_id=hospital_id,
            stage=stage.value,
            granted_at=now,
            reason=reason,
        )
        case.disclosures.append(disclosure)
        return disclosure

    def _revoke(self, case: Case, hospital_id: str, now: float, reason: str) -> None:
        for d in case.disclosures:
            if d.hospital_id == hospital_id and d.active_at(now):
                d.revoked_at = now
                d.revoke_reason = reason

    # ------------------------------------------------------------------
    # 租约与占用
    # ------------------------------------------------------------------

    def _lease_of(self, lease_id: str) -> Optional[Lease]:
        return next((l for l in self._leases if l.id == lease_id), None)

    def _sweep_expired(self, now: float) -> None:
        for lease in self._leases:
            if lease.state != LeaseState.ACTIVE.value or now < lease.expires_at:
                continue
            lease.state = LeaseState.EXPIRED.value
            lease.released_at = now
            lease.release_reason = "租约 TTL 到期未确认，自动释放"
            self._handle_lease_expiry(lease, now)

    def _handle_lease_expiry(self, lease: Lease, now: float) -> None:
        """租约失联到期：计划不可继续执行，醒目标记并升级人工调度。"""

        case = self._cases.get(lease.case_id)
        if case is None:
            return
        plan = next((p for p in case.plans if p.lease_id == lease.id), None)
        if plan is not None and plan.state == PlanState.ACTIVE.value:
            plan.state = PlanState.CANCELLED.value
            plan.reroute_reason = "资源租约到期自动释放，原计划取消，等待人工调度"
            plan.decided_at = now
            plan.decided_by = "system"
            self._revoke(case, plan.hospital_id, now, "租约到期：资源未确认，原计划取消")
            self._open_escalation(
                case,
                ["resource_unreachable"],
                f"{plan.hospital_name} 的团队/手术台租约到期未确认，资源已自动释放，"
                "原转运计划取消，须立即由人工调度重新指派",
                now,
            )
            if case.status == CaseStatus.ACCEPTED.value:
                case.status = CaseStatus.RECOMMENDED.value
            elif case.status == CaseStatus.IN_TRANSIT.value:
                case.status = CaseStatus.REROUTING.value

    def _occupied(self, team_id: Optional[str], table_id: Optional[str]) -> Optional[str]:
        """返回当前有效租约的持有案例 id（调用前需先 sweep，并持锁）。"""

        now = self.clock.now()
        for lease in self._leases:
            if not lease.active_at(now):
                continue
            if team_id is not None and lease.team_id == team_id:
                return lease.case_id
            if table_id is not None and lease.table_id == table_id:
                return lease.case_id
        return None

    def active_leases(self) -> list[Lease]:
        now = self.clock.now()
        with self._lock:
            self._sweep_expired(now)
            return [l for l in self._leases if l.active_at(now)]

    def _release_lease(self, lease: Lease, now: float, reason: str) -> None:
        lease.state = LeaseState.RELEASED.value
        lease.released_at = now
        lease.release_reason = reason

    def _snapshot(self, estimate: RouteEstimate) -> RouteSnapshot:
        return RouteSnapshot(
            road=estimate.road,
            distance_km=estimate.distance_km,
            base_minutes=estimate.base_minutes,
            weather_condition=estimate.weather_condition,
            weather_speed_factor=estimate.weather_speed_factor,
            travel_minutes=round(estimate.travel_minutes, 1),
            event_delay_minutes=round(estimate.event_delay_minutes, 1),
            event_titles=[e.title for e in estimate.events],
            route_observed_at=estimate.route_observed_at,
            weather_observed_at=estimate.weather_observed_at,
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_cases(self) -> list[Case]:
        with self._lock:
            return list(self._cases.values())

    def open_escalations(self) -> list[dict]:
        """值班台视角：全部未结案的人工调度升级单，按时间倒序。"""

        now = self.clock.now()
        with self._lock:
            self._sweep_expired(now)
            items = []
            for case in self._cases.values():
                for esc in case.escalations:
                    if esc.status != EscalationStatus.RESOLVED.value:
                        items.append({
                            "escalation_id": esc.id,
                            "case_id": case.id,
                            "pseudonym": case.patient.pseudonym,
                            "reasons": esc.reasons,
                            "message": esc.message,
                            "status": esc.status,
                            "created_at": esc.created_at,
                            "age_minutes": round((now - esc.created_at) / 60, 1),
                        })
            items.sort(key=lambda item: item["created_at"], reverse=True)
            return items

    def network_view(self) -> dict:
        """能力图：机构能力资料、团队/手术台心跳与当前占用。"""

        now = self.clock.now()
        with self._lock:
            self._sweep_expired(now)
            hospitals = []
            for hospital in self.catalog.hospitals():
                team_views = []
                for team in hospital.teams:
                    holder = self._occupied(team.id, None)
                    team_views.append({
                        "id": team.id,
                        "name": team.name,
                        "capable_parts": team.capable_parts,
                        "min_vessel_mm": team.min_vessel_mm,
                        "on_duty": team.on_duty,
                        "heartbeat_age_minutes": (
                            round(team.heartbeat_age_sec(now) / 60, 1)
                            if team.heartbeat_age_sec(now) is not None else None
                        ),
                        "heartbeat_fresh": team.heartbeat_fresh(now),
                        "held_by_case": holder,
                    })
                table_views = []
                for table in hospital.tables:
                    holder = self._occupied(None, table.id)
                    table_views.append({
                        "id": table.id,
                        "code": table.code,
                        "microsurgery_ready": table.microsurgery_ready,
                        "reported_available": table.reported_available,
                        "heartbeat_age_minutes": (
                            round(table.heartbeat_age_sec(now) / 60, 1)
                            if table.heartbeat_age_sec(now) is not None else None
                        ),
                        "heartbeat_fresh": table.heartbeat_fresh(now),
                        "held_by_case": holder,
                    })
                hospitals.append({
                    "id": hospital.id,
                    "name": hospital.name,
                    "tier": hospital.tier,
                    "microsurgery": hospital.microsurgery,
                    "supported_parts": hospital.supported_parts,
                    "min_vessel_mm": hospital.min_vessel_mm,
                    "equipment": hospital.equipment,
                    "capability_age_minutes": (
                        round(hospital.capability_age_sec(now) / 60, 1)
                        if hospital.capability_age_sec(now) is not None else None
                    ),
                    "capability_stale": hospital.capability_stale(now),
                    "teams": team_views,
                    "tables": table_views,
                })
            carriers = [
                {"id": carrier_id, "name": name}
                for carrier_id, name in self.catalog.carriers().items()
            ]
            return {"as_of": now, "hospitals": hospitals, "carriers": carriers}

    def routes_view(self, from_id: str) -> dict:
        """交通看板：从接诊机构出发到各候选机构的路况与道路环境。"""

        now = self.clock.now()
        with self._lock:
            self.catalog.require_hospital(from_id)
            estimates = []
            for hospital in self.catalog.destination_candidates(from_id):
                estimate = self.catalog.estimate_route(from_id, hospital.id, now)
                if not estimate.exists:
                    estimates.append({
                        "hospital_id": hospital.id,
                        "hospital_name": hospital.name,
                        "route_available": False,
                    })
                    continue
                estimates.append({
                    "hospital_id": hospital.id,
                    "hospital_name": hospital.name,
                    "route_available": True,
                    "road": estimate.road,
                    "distance_km": estimate.distance_km,
                    "base_minutes": estimate.base_minutes,
                    "weather": estimate.weather_condition,
                    "weather_speed_factor": estimate.weather_speed_factor,
                    "travel_minutes": round(estimate.travel_minutes, 1),
                    "events": [
                        {"id": e.id, "title": e.title, "delay_minutes": e.delay_minutes}
                        for e in estimate.events
                    ],
                    "event_delay_minutes": round(estimate.event_delay_minutes, 1),
                    "eta_minutes": round(estimate.total_minutes(), 1),
                })
            return {"as_of": now, "from_hospital_id": from_id, "routes": estimates}

    def plan_history(self, case_id: str) -> list[TransportPlan]:
        """改道审计：返回全部版本计划（含已作废版本），决定理由随计划留存。"""

        case = self.get_case(case_id)
        return list(case.plans)
