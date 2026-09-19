"""候选医院筛选、排序与推荐依据解释。

筛选维度：机构能力（部位/血管口径/设备）、团队与手术台的实时值守心跳、
路程与道路环境（天气系数、交通事件）、剩余缺血窗口。
排序只表达“在缺血窗口内完成恢复血运的协调可行性”，不是临床诊断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from domain import (
    ARRIVAL_RISK_BUFFER_MIN,
    CAPABILITY_STALE_AFTER_MIN,
    RESOURCE_HEARTBEAT_TIMEOUT_MIN,
    SEVERE_WEATHER_FACTOR,
    ExplainFactor,
    Hospital,
    Injury,
    Recommendation,
    ResourceOption,
    RiskCode,
    RiskFlag,
    RiskLevel,
)
from catalog import NetworkCatalog

# 占用查询：给定团队与手术台，返回占用方案例 id；本案例自己的租约不计。
OccupancyLookup = Callable[[Optional[str], Optional[str]], Optional[str]]


def _age_text(age_sec: Optional[float]) -> str:
    if age_sec is None:
        return "从无心跳"
    return f"{round(age_sec / 60, 1)} 分钟未回报"


def select_free_resources(
    hospital: Hospital,
    injury: Injury,
    now: float,
    occupied: OccupancyLookup,
):
    """返回该机构当前心跳新鲜、能力匹配且未被其他案例锁定的团队与手术台。"""

    free_teams = [
        t
        for t in hospital.teams
        if t.on_duty
        and injury.body_part in t.capable_parts
        and (
            injury.vessel_diameter_mm is None
            or t.min_vessel_mm <= injury.vessel_diameter_mm + 1e-9
        )
        and t.heartbeat_fresh(now)
        and not occupied(t.id, None)
    ]
    free_tables = [
        t
        for t in hospital.tables
        if t.microsurgery_ready
        and t.reported_available
        and t.heartbeat_fresh(now)
        and not occupied(None, t.id)
    ]
    return free_teams, free_tables


@dataclass
class _ResourceChoice:
    team: object
    table: object


class TriageEngine:
    def __init__(self, catalog: NetworkCatalog):
        self.catalog = catalog

    def evaluate_case(
        self,
        *,
        origin_hospital_id: str,
        injury: Injury,
        now: float,
        occupied: OccupancyLookup = lambda *_args: None,
    ) -> list[Recommendation]:
        """对除接诊机构外的全部注册机构逐一评估，返回按安全余量排序的列表。"""

        results = [
            self._evaluate_hospital(h, origin_hospital_id, injury, now, occupied)
            for h in self.catalog.destination_candidates(origin_hospital_id)
        ]
        # 有可行解的在前；可行解按到达后缺血余量降序，再按预计耗时、资料时效。
        results.sort(
            key=lambda r: (
                not r.feasible,
                -(r.ischemia_remaining_on_arrival_min if r.feasible else 0),
                r.eta_minutes if r.eta_minutes >= 0 else 10**9,
                r.capability_age_minutes if r.capability_age_minutes is not None else 10**9,
                r.hospital_name,
            )
        )
        for rank, rec in enumerate(results, start=1):
            rec.rank = rank
        return results

    # ------------------------------------------------------------------

    def _evaluate_hospital(
        self,
        hospital: Hospital,
        origin_hospital_id: str,
        injury: Injury,
        now: float,
        occupied: OccupancyLookup,
    ) -> Recommendation:
        factors: list[ExplainFactor] = []
        risks: list[RiskFlag] = []
        excluded: list[str] = []

        rec = Recommendation(
            hospital_id=hospital.id,
            hospital_name=hospital.name,
            rank=0,
            feasible=True,
            travel_minutes=0.0,
            event_delay_minutes=0.0,
            eta_minutes=-1.0,
            ischemia_remaining_on_arrival_min=0.0,
            capability_age_minutes=None,
            option=None,
            factors=factors,
            risks=risks,
            excluded_reasons=excluded,
        )

        capability_ok = self._check_capability(hospital, injury, now, rec)
        # 资源检查始终执行：即使资料过期，也要把心跳失联等风险同时呈现。
        choice = self._check_resources(hospital, injury, now, occupied, rec)
        route = self._check_route(hospital, origin_hospital_id, injury, now, rec)

        if not capability_ok or choice is None:
            rec.feasible = False
        if route is None:
            rec.feasible = False
        elif rec.feasible and choice is not None:
            rec.option = ResourceOption(
                team_id=choice.team.id,
                team_name=choice.team.name,
                table_id=choice.table.id,
                table_code=choice.table.code,
            )

        # 只要有路线就给出缺血余量与窗口风险；即便因其他维度不可行，
        # 值班员也能看到“如果资源就绪，时间上是否来得及”。
        if route is not None:
            self._check_ischemia(injury, now, rec)

        # 任一 BLOCK 风险即不可作为自动推荐对象。
        if any(r.level == RiskLevel.BLOCK.value for r in risks):
            rec.feasible = False
        return rec

    # -- 维度一：机构能力资料 --------------------------------------------

    def _check_capability(
        self,
        hospital: Hospital,
        injury: Injury,
        now: float,
        rec: Recommendation,
    ) -> bool:
        ok = True

        if not hospital.microsurgery:
            rec.excluded_reasons.append("机构未登记显微外科能力")
            rec.risks.append(
                RiskFlag(
                    RiskCode.CAPABILITY_MISMATCH.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 未登记显微外科能力",
                )
            )
            ok = False

        if injury.body_part not in hospital.supported_parts:
            rec.excluded_reasons.append(f"不支持离断部位 {injury.body_part}")
            rec.risks.append(
                RiskFlag(
                    RiskCode.CAPABILITY_MISMATCH.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 不支持部位 {injury.body_part}",
                )
            )
            ok = False

        if injury.vessel_diameter_mm is not None and hospital.min_vessel_mm is not None:
            if hospital.min_vessel_mm > injury.vessel_diameter_mm + 1e-9:
                rec.excluded_reasons.append(
                    f"机构可吻合最小口径 {hospital.min_vessel_mm}mm "
                    f"粗于目标 {injury.vessel_diameter_mm}mm"
                )
                rec.risks.append(
                    RiskFlag(
                        RiskCode.CAPABILITY_MISMATCH.value,
                        RiskLevel.BLOCK.value,
                        f"可吻合口径不匹配：机构下限 {hospital.min_vessel_mm}mm，"
                        f"目标 {injury.vessel_diameter_mm}mm",
                    )
                )
                ok = False

        missing_equipment = [
            e for e in injury.required_equipment if e not in hospital.equipment
        ]
        if missing_equipment:
            rec.excluded_reasons.append(f"缺少设备: {'、'.join(missing_equipment)}")
            rec.risks.append(
                RiskFlag(
                    RiskCode.CAPABILITY_MISMATCH.value,
                    RiskLevel.BLOCK.value,
                    f"缺少必需设备: {'、'.join(missing_equipment)}",
                )
            )
            ok = False

        rec.factors.append(
            ExplainFactor(
                "能力匹配",
                f"部位 {injury.body_part}；目标血管口径 "
                f"{injury.vessel_diameter_mm if injury.vessel_diameter_mm is not None else '未注明'}mm；"
                f"必需设备 {injury.required_equipment or '无'}",
                value="匹配" if ok else "不匹配",
            )
        )

        age = hospital.capability_age_sec(now)
        age_min = round(age / 60, 1) if age is not None else None
        rec.capability_age_minutes = age_min
        if hospital.capability_stale(now):
            detail = (
                "能力资料从未上报"
                if age is None
                else f"能力资料已 {age_min} 分钟未更新（阈值 {CAPABILITY_STALE_AFTER_MIN} 分钟）"
            )
            rec.risks.append(
                RiskFlag(
                    RiskCode.CAPABILITY_STALE.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name}{detail}，不能据此确认接收能力，需人工核实",
                )
            )
            rec.excluded_reasons.append("能力资料过期")
            ok = False
        rec.factors.append(
            ExplainFactor(
                "资料时效",
                "无法确认上报时间" if age is None else f"最近更新于 {age_min} 分钟前",
                value="过期" if hospital.capability_stale(now) else "有效",
            )
        )
        return ok

    # -- 维度二：团队与手术台实时状态 + 并发占用 ---------------------------

    def _check_resources(
        self,
        hospital: Hospital,
        injury: Injury,
        now: float,
        occupied: OccupancyLookup,
        rec: Recommendation,
    ) -> Optional[_ResourceChoice]:
        capable_teams = [
            t
            for t in hospital.teams
            if t.on_duty
            and injury.body_part in t.capable_parts
            and (
                injury.vessel_diameter_mm is None
                or t.min_vessel_mm <= injury.vessel_diameter_mm + 1e-9
            )
        ]
        fresh_teams = [t for t in capable_teams if t.heartbeat_fresh(now)]
        stale_teams = [t for t in capable_teams if not t.heartbeat_fresh(now)]

        ready_tables = [
            t
            for t in hospital.tables
            if t.microsurgery_ready and t.reported_available
        ]
        fresh_tables = [t for t in ready_tables if t.heartbeat_fresh(now)]
        stale_tables = [t for t in ready_tables if not t.heartbeat_fresh(now)]

        rec.factors.append(
            ExplainFactor(
                "显微外科团队",
                f"可处理该伤情团队 {len(capable_teams)} 支，"
                f"其中 {len(fresh_teams)} 支在 {RESOURCE_HEARTBEAT_TIMEOUT_MIN} 分钟心跳内",
                value="失联" if capable_teams and not fresh_teams else (
                    "无可配团队" if not capable_teams else "在线"
                ),
            )
        )
        rec.factors.append(
            ExplainFactor(
                "手术台",
                f"显微外科台 {len(ready_tables)} 张，"
                f"其中 {len(fresh_tables)} 张状态心跳新鲜",
                value="失联" if ready_tables and not fresh_tables else (
                    "无可用台" if not ready_tables else "在线"
                ),
            )
        )

        if not capable_teams:
            rec.excluded_reasons.append("无值班且口径/部位匹配的团队")
            rec.risks.append(
                RiskFlag(
                    RiskCode.RESOURCE_UNREACHABLE.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 无值班且可处理 {injury.body_part} 的团队",
                )
            )
        elif not fresh_teams:
            rec.excluded_reasons.append("匹配团队全部回报失联")
            ages = ", ".join(
                f"{t.name}({_age_text(t.heartbeat_age_sec(now))})" for t in stale_teams
            )
            rec.risks.append(
                RiskFlag(
                    RiskCode.RESOURCE_UNREACHABLE.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 匹配团队心跳超时（{ages}），无法确认可调用",
                )
            )

        if not ready_tables:
            rec.excluded_reasons.append("无可用显微外科手术台")
            rec.risks.append(
                RiskFlag(
                    RiskCode.RESOURCE_UNREACHABLE.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 无自报可用的显微外科手术台",
                )
            )
        elif not fresh_tables:
            rec.excluded_reasons.append("手术台全部回报失联")
            rec.risks.append(
                RiskFlag(
                    RiskCode.RESOURCE_UNREACHABLE.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 手术台状态心跳超时，无法确认空闲",
                )
            )

        # 在“新鲜”资源中挑一对未被其他案例租约占用的组合。
        busy_teams: list[str] = []
        busy_tables: list[str] = []
        for team in fresh_teams:
            holder = occupied(team.id, None)
            if holder:
                busy_teams.append(f"{team.name}→案例 {holder}")
        for table in fresh_tables:
            holder = occupied(None, table.id)
            if holder:
                busy_tables.append(f"{table.code}→案例 {holder}")

        free_teams, free_tables = select_free_resources(
            hospital, injury, now, occupied
        )
        if busy_teams or busy_tables:
            rec.factors.append(
                ExplainFactor(
                    "并发占用",
                    f"已被其他案例短时锁定：团队 [{'; '.join(busy_teams) or '无'}]，"
                    f"手术台 [{'; '.join(busy_tables) or '无'}]",
                )
            )
        if capable_teams and fresh_teams and not free_teams:
            rec.excluded_reasons.append("匹配团队均被其他案例占用")
            rec.risks.append(
                RiskFlag(
                    RiskCode.RESOURCE_UNREACHABLE.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 可调用团队已被并发急诊占用，租约释放前不可再分配",
                )
            )
        if ready_tables and fresh_tables and not free_tables:
            rec.excluded_reasons.append("可用手术台均被其他案例占用")
            rec.risks.append(
                RiskFlag(
                    RiskCode.RESOURCE_UNREACHABLE.value,
                    RiskLevel.BLOCK.value,
                    f"{hospital.name} 显微外科手术台已被并发急诊占用，租约释放前不可再分配",
                )
            )

        if free_teams and free_tables:
            return _ResourceChoice(team=free_teams[0], table=free_tables[0])
        return None

    # -- 维度三：路程与道路环境 -------------------------------------------

    def _check_route(
        self,
        hospital: Hospital,
        origin_hospital_id: str,
        injury: Injury,
        now: float,
        rec: Recommendation,
    ):
        estimate = self.catalog.estimate_route(origin_hospital_id, hospital.id, now)
        if not estimate.exists:
            rec.risks.append(
                RiskFlag(
                    RiskCode.NO_ROUTE_DATA.value,
                    RiskLevel.BLOCK.value,
                    f"缺少至 {hospital.name} 的路况观测，无法计算到达时间",
                )
            )
            rec.excluded_reasons.append("缺少路线数据")
            return None

        rec.travel_minutes = round(estimate.travel_minutes, 1)
        rec.event_delay_minutes = round(estimate.event_delay_minutes, 1)
        rec.eta_minutes = round(estimate.total_minutes(), 1)

        rec.factors.append(
            ExplainFactor(
                "路程",
                f"{estimate.road}，{estimate.distance_km}km，常规 {estimate.base_minutes} 分钟"
                f"（路况观测于 {round(estimate.route_age_minutes, 1)} 分钟前）",
                value=f"{rec.eta_minutes} 分钟",
            )
        )
        rec.factors.append(
            ExplainFactor(
                "道路环境",
                f"天气 {estimate.weather_condition}，通行系数 {estimate.weather_speed_factor}"
                + (
                    f"，观测于 {round(estimate.weather_age_minutes, 1)} 分钟前"
                    if estimate.weather_age_minutes is not None
                    else ""
                ),
                value=f"{rec.travel_minutes} 分钟",
            )
        )
        if estimate.weather_speed_factor <= SEVERE_WEATHER_FACTOR:
            rec.risks.append(
                RiskFlag(
                    RiskCode.SEVERE_WEATHER.value,
                    RiskLevel.WARN.value,
                    f"前往 {hospital.name} 沿线天气 {estimate.weather_condition}，"
                    f"通行系数仅 {estimate.weather_speed_factor}，实际耗时可能继续上升",
                )
            )
        if estimate.events:
            titles = "、".join(e.title for e in estimate.events)
            rec.factors.append(
                ExplainFactor(
                    "交通事件",
                    f"{titles}，合计附加延误约 {rec.event_delay_minutes} 分钟",
                )
            )
            rec.risks.append(
                RiskFlag(
                    RiskCode.TRAFFIC_INCIDENT.value,
                    RiskLevel.WARN.value,
                    f"沿线交通事件：{titles}（+{rec.event_delay_minutes} 分钟）",
                )
            )
        return estimate

    # -- 维度四：剩余缺血窗口 ---------------------------------------------

    def _check_ischemia(self, injury: Injury, now: float, rec: Recommendation) -> None:
        limit_min = injury.ischemia_limit_sec() / 60
        elapsed_min = injury.elapsed_sec(now) / 60
        remaining_now_min = injury.remaining_sec(now) / 60
        arrival_remaining_min = remaining_now_min - rec.eta_minutes
        rec.ischemia_remaining_on_arrival_min = round(arrival_remaining_min, 1)

        preservation_text = {
            "warm": "常温",
            "cooled": "规范冷存",
            "unknown": "保存条件不明",
        }.get(injury.preservation, injury.preservation)

        rec.factors.append(
            ExplainFactor(
                "缺血窗口",
                f"{preservation_text}时限 {round(limit_min)} 分钟；已流失血 "
                f"{round(elapsed_min)} 分钟；当前余量 {round(remaining_now_min)} 分钟；"
                f"预计到达后余量 {round(arrival_remaining_min)} 分钟",
                value=(
                    "越窗"
                    if arrival_remaining_min < 0
                    else f"余量 {round(arrival_remaining_min)} 分钟"
                ),
            )
        )
        if arrival_remaining_min < 0:
            rec.risks.append(
                RiskFlag(
                    RiskCode.ARRIVAL_WINDOW_EXCEEDED.value,
                    RiskLevel.BLOCK.value,
                    f"预计到达时已超出缺血窗口 {round(-arrival_remaining_min)} 分钟，"
                    "不得自动列入转运计划，立即转人工调度",
                )
            )
            rec.excluded_reasons.append("预计到达越窗")
        elif arrival_remaining_min < ARRIVAL_RISK_BUFFER_MIN:
            rec.risks.append(
                RiskFlag(
                    RiskCode.ARRIVAL_WINDOW_AT_RISK.value,
                    RiskLevel.WARN.value,
                    f"到达后缺血余量仅 {round(arrival_remaining_min)} 分钟"
                    f"（缓冲阈值 {ARRIVAL_RISK_BUFFER_MIN} 分钟），道路稍有恶化即越窗",
                )
            )
