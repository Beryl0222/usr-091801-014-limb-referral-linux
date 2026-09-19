"""断肢急救转诊编排的领域模型与全网统一策略参数。

时间在领域内部统一使用 Unix 秒（float）；分钟仅用于输入/展示边界换算。
所有排序结论均为资源协调依据，不构成诊断建议（见 ADVICE_NOTICE）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, is_dataclass
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# 全网统一救治策略参数（分钟）。这些是协调阈值，不是临床判断。
# ---------------------------------------------------------------------------

# 常温保存下争取恢复血运的公认缺血时限约 6 小时；规范冷存可适当放宽。
WARM_ISCHEMIA_LIMIT_MIN = 360
COLD_ISCHEMIA_LIMIT_MIN = 480
# 能力资料超过该时效即视为过期，不得据此确认接收能力。
CAPABILITY_STALE_AFTER_MIN = 30
# 团队/手术台状态心跳超过该时长即视为回报失联。
RESOURCE_HEARTBEAT_TIMEOUT_MIN = 5
# 候选医院接受后锁定团队与手术台的短时租约时长，逾期自动释放。
RESOURCE_LEASE_TTL_MIN = 15
# 预计到达后缺血余量低于该缓冲时，即使未越窗也标记高风险。
ARRIVAL_RISK_BUFFER_MIN = 15
# 天气导致道路通行能力下降到该系数及以下时，单独提示道路环境风险。
SEVERE_WEATHER_FACTOR = 0.75

ADVICE_NOTICE = (
    "本系统依据机构能力、资源心跳、路程与道路环境给出协调排序与依据，"
    "不构成诊断或治疗建议；最终救治决策由临床医生作出。"
)

MINUTES = 60


def min_to_sec(minutes: float) -> float:
    return minutes * MINUTES


class Preservation(str, Enum):
    """离断体保存条件。"""

    WARM = "warm"            # 常温、未冷存
    COOLED = "cooled"        # 规范 4℃ 左右冷存（干燥冷藏，未浸泡）
    UNKNOWN = "unknown"      # 现场无法确认


class VitalsLevel(str, Enum):
    STABLE = "stable"
    UNSTABLE = "unstable"
    CRITICAL = "critical"


class CaseStatus(str, Enum):
    OPEN = "open"                     # 已登记，等待筛选
    RECOMMENDED = "recommended"       # 已给出候选排序
    ACCEPTED = "accepted"             # 候选医院已接受并锁定资源
    IN_TRANSIT = "in_transit"         # 转运途中
    DELIVERED = "delivered"           # 已交接
    CLOSED = "closed"                 # 救治结束
    REROUTING = "rerouting"           # 正在改道（旧计划保留可查）


class LeaseState(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"     # 主动释放（改道、取消）
    EXPIRED = "expired"       # TTL 到期自动释放


class PlanState(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"  # 改道后被新版本取代，原文保留
    COMPLETED = "completed"
    CANCELLED = "cancelled"    # 租约失联/过期且未出发


class RiskLevel(str, Enum):
    WARN = "warn"
    BLOCK = "block"


class RiskCode(str, Enum):
    CAPABILITY_STALE = "capability_stale"                 # 能力资料过期
    RESOURCE_UNREACHABLE = "resource_unreachable"         # 资源回报失联
    ARRIVAL_WINDOW_EXCEEDED = "arrival_window_exceeded"   # 预计到达越窗
    ARRIVAL_WINDOW_AT_RISK = "arrival_window_at_risk"     # 余量不足
    SEVERE_WEATHER = "severe_weather"                     # 恶劣天气
    TRAFFIC_INCIDENT = "traffic_incident"                 # 交通事件
    CAPABILITY_MISMATCH = "capability_mismatch"           # 能力不匹配
    NO_ROUTE_DATA = "no_route_data"                       # 缺少路况观测


class EscalationStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class IdentityStage(str, Enum):
    """患者身份对单个机构的开放阶段，只能逐级授予，改道后可收回。"""

    REGISTERED = "registered"  # 接诊机构：完整资料
    ACCEPTED = "accepted"      # 接收医院：伤情摘要+代号，用于术前准备
    IN_TRANSIT = "in_transit"  # 实际承担转运的机构：代号与交接信息
    IN_CARE = "in_care"        # 实际交接、参与救治：完整身份


# 各阶段可见的身份字段层级。
IDENTITY_VIEW_NONE = "none"
IDENTITY_VIEW_SUMMARY = "summary"
IDENTITY_VIEW_FULL = "full"

_STAGE_VIEW = {
    IdentityStage.REGISTERED: IDENTITY_VIEW_FULL,
    IdentityStage.ACCEPTED: IDENTITY_VIEW_SUMMARY,
    IdentityStage.IN_TRANSIT: IDENTITY_VIEW_SUMMARY,
    IdentityStage.IN_CARE: IDENTITY_VIEW_FULL,
}


class MilestoneCode(str, Enum):
    DEPARTED = "departed"               # 出发
    WAYPOINT = "waypoint"               # 途中节点（弱网补传）
    REROUTE_DECIDED = "reroute_decided"
    ARRIVED_ED = "arrived_ed"           # 到达急诊
    HANDOVER = "handover"               # 完成交接，身份向接收方完整开放
    BLOOD_FLOW_RESTORED = "blood_flow_restored"


# 交接类里程碑：到达后接收机构才真正参与救治。
HANDOVER_MILESTONES = {MilestoneCode.ARRIVED_ED, MilestoneCode.HANDOVER}


class Clock:
    """可替换的时间源，便于演练确定性时钟。"""

    def now(self) -> float:
        return time.time()


class MutableClock(Clock):
    def __init__(self, now: float):
        self._now = float(now)

    def now(self) -> float:
        return self._now

    def set(self, now: float) -> None:
        self._now = float(now)

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def advance_minutes(self, minutes: float) -> None:
        self.advance(min_to_sec(minutes))


# ---------------------------------------------------------------------------
# 登记侧模型
# ---------------------------------------------------------------------------


@dataclass
class Patient:
    """患者身份与就诊信息。真实字段只按身份阶段开放。"""

    pseudonym: str                      # 网络内代号，非身份信息阶段也可见
    real_name: Optional[str] = None
    id_number: Optional[str] = None
    contact: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None


@dataclass
class Injury:
    body_part: str                      # 离断部位代码，如 forearm / hand / finger
    amputated_at: float                 # 离断发生时间
    preservation: str = Preservation.WARM.value
    vessel_diameter_mm: Optional[float] = None  # 需吻合的目标血管口径
    required_equipment: list[str] = field(default_factory=list)
    side: Optional[str] = None
    notes: Optional[str] = None

    def ischemia_limit_sec(self) -> float:
        if self.preservation == Preservation.COOLED.value:
            return min_to_sec(COLD_ISCHEMIA_LIMIT_MIN)
        return min_to_sec(WARM_ISCHEMIA_LIMIT_MIN)

    def elapsed_sec(self, now: float) -> float:
        return max(0.0, now - self.amputated_at)

    def remaining_sec(self, now: float) -> float:
        return self.ischemia_limit_sec() - self.elapsed_sec(now)


@dataclass
class Vitals:
    level: str = VitalsLevel.STABLE.value
    sbp_mmhg: Optional[int] = None
    dbp_mmhg: Optional[int] = None
    hr_per_min: Optional[int] = None
    spo2: Optional[int] = None
    gcs: Optional[int] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# 能力侧模型
# ---------------------------------------------------------------------------


@dataclass
class Team:
    id: str
    hospital_id: str
    name: str
    capable_parts: list[str]
    min_vessel_mm: float                # 可完成吻合的最小血管口径
    on_duty: bool = True
    last_heartbeat_at: Optional[float] = None

    def heartbeat_age_sec(self, now: float) -> Optional[float]:
        if self.last_heartbeat_at is None:
            return None
        return max(0.0, now - self.last_heartbeat_at)

    def heartbeat_fresh(self, now: float) -> bool:
        age = self.heartbeat_age_sec(now)
        return age is not None and age <= min_to_sec(RESOURCE_HEARTBEAT_TIMEOUT_MIN)


@dataclass
class ORTable:
    id: str
    hospital_id: str
    code: str
    microsurgery_ready: bool = True
    reported_available: bool = True     # 机构最近一次自报可用
    last_heartbeat_at: Optional[float] = None

    def heartbeat_age_sec(self, now: float) -> Optional[float]:
        if self.last_heartbeat_at is None:
            return None
        return max(0.0, now - self.last_heartbeat_at)

    def heartbeat_fresh(self, now: float) -> bool:
        age = self.heartbeat_age_sec(now)
        return age is not None and age <= min_to_sec(RESOURCE_HEARTBEAT_TIMEOUT_MIN)


@dataclass
class Hospital:
    id: str
    name: str
    tier: str                           # 机构等级，如 三级甲等
    address: str = ""
    microsurgery: bool = False
    supported_parts: list[str] = field(default_factory=list)
    min_vessel_mm: Optional[float] = None
    equipment: list[str] = field(default_factory=list)
    capability_updated_at: Optional[float] = None
    teams: list[Team] = field(default_factory=list)
    tables: list[ORTable] = field(default_factory=list)

    def capability_age_sec(self, now: float) -> Optional[float]:
        if self.capability_updated_at is None:
            return None
        return max(0.0, now - self.capability_updated_at)

    def capability_stale(self, now: float) -> bool:
        age = self.capability_age_sec(now)
        return age is None or age > min_to_sec(CAPABILITY_STALE_AFTER_MIN)


@dataclass
class RouteInfo:
    """两机构间的常规路况观测。"""

    from_hospital_id: str
    to_hospital_id: str
    road: str
    distance_km: float
    base_minutes: float                 # 常规通行时间
    observed_at: float


@dataclass
class Weather:
    route_key: str                      # "from->to"
    condition: str                      # clear / rain / snow / ice / fog
    speed_factor: float                 # 1.0 无影响，越小越慢
    observed_at: float


@dataclass
class TrafficEvent:
    id: str
    route_key: str
    title: str
    delay_minutes: float
    observed_at: float
    status: str = "active"              # active / cleared


# ---------------------------------------------------------------------------
# 筛选与解释模型
# ---------------------------------------------------------------------------


@dataclass
class RiskFlag:
    code: str
    level: str
    message: str


@dataclass
class ExplainFactor:
    """一条可向值班员展示的推荐依据。"""

    name: str
    detail: str
    value: Optional[str] = None


@dataclass
class ResourceOption:
    team_id: str
    team_name: str
    table_id: str
    table_code: str


@dataclass
class Recommendation:
    hospital_id: str
    hospital_name: str
    rank: int
    feasible: bool
    travel_minutes: float               # 经天气折算后的行驶时间
    event_delay_minutes: float          # 交通事件合计延误
    eta_minutes: float                  # 自筛选时刻起的预计到达总耗时
    ischemia_remaining_on_arrival_min: float
    capability_age_minutes: Optional[float]
    option: Optional[ResourceOption]
    factors: list[ExplainFactor] = field(default_factory=list)
    risks: list[RiskFlag] = field(default_factory=list)
    excluded_reasons: list[str] = field(default_factory=list)


@dataclass
class RecommendationSet:
    id: str
    case_id: str
    generated_at: float
    recommendations: list[Recommendation]
    escalated: bool
    notice: str = ADVICE_NOTICE


# ---------------------------------------------------------------------------
# 运行时模型
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    """候选医院接受后对团队+手术台的短时独占锁定。"""

    id: str
    case_id: str
    hospital_id: str
    team_id: str
    table_id: str
    state: str
    granted_at: float
    expires_at: float
    released_at: Optional[float] = None
    release_reason: Optional[str] = None

    def active_at(self, now: float) -> bool:
        return self.state == LeaseState.ACTIVE.value and now < self.expires_at


@dataclass
class RouteSnapshot:
    """生成计划时对路况结论的留档，事后不随天气/事件变化而改变。"""

    road: str
    distance_km: float
    base_minutes: float
    weather_condition: str
    weather_speed_factor: float
    travel_minutes: float
    event_delay_minutes: float
    event_titles: list[str]
    route_observed_at: float
    weather_observed_at: float


@dataclass
class TransportPlan:
    id: str
    case_id: str
    version: int
    hospital_id: str
    hospital_name: str
    team_id: str
    table_id: str
    lease_id: str
    route: RouteSnapshot
    eta_minutes: float
    planned_at: float
    planned_arrival_at: float
    ischemia_deadline: float
    ischemia_remaining_on_arrival_min: float
    state: str
    basis_recommendation_id: str
    decided_by: Optional[str] = None
    reroute_reason: Optional[str] = None
    predecessor_plan_id: Optional[str] = None
    superseded_by: Optional[str] = None
    decided_at: Optional[float] = None


@dataclass
class Milestone:
    client_event_id: str                # 弱网客户端生成的幂等键
    code: str
    occurred_at: float                  # 事件实际发生时间（允许早于记录时间）
    recorded_at: float                  # 服务端收到时间
    location: Optional[str] = None
    note: Optional[str] = None
    replay: bool = False                # 是否为断网恢复后的补传


@dataclass
class Escalation:
    id: str
    case_id: str
    reasons: list[str]
    message: str
    status: str
    created_at: float
    acknowledged_at: Optional[float] = None
    acknowledged_by: Optional[str] = None
    resolution_note: Optional[str] = None


@dataclass
class Disclosure:
    """身份开放审计记录：授予与收回都留痕。"""

    id: str
    case_id: str
    hospital_id: str
    stage: str
    granted_at: float
    reason: str
    revoked_at: Optional[float] = None
    revoke_reason: Optional[str] = None

    def active_at(self, now: float) -> bool:
        return self.revoked_at is None or self.revoked_at > now


@dataclass
class Case:
    id: str
    created_at: float
    origin_hospital_id: str
    recorded_by: str
    patient: Patient
    injury: Injury
    vitals: Vitals
    status: str = CaseStatus.OPEN.value
    recommendations: list[RecommendationSet] = field(default_factory=list)
    plans: list[TransportPlan] = field(default_factory=list)
    milestones: list[Milestone] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    disclosures: list[Disclosure] = field(default_factory=list)

    def latest_recommendation(self) -> Optional[RecommendationSet]:
        return self.recommendations[-1] if self.recommendations else None

    def active_plan(self) -> Optional[TransportPlan]:
        for plan in reversed(self.plans):
            if plan.state == PlanState.ACTIVE.value:
                return plan
        return None

    def plan(self, plan_id: str) -> Optional[TransportPlan]:
        return next((p for p in self.plans if p.id == plan_id), None)


def identity_view_level(stage: str) -> str:
    return _STAGE_VIEW[IdentityStage(stage)]


def patient_view(patient: Patient, level: str) -> dict:
    """按授权层级投影患者资料：未授权不含任何身份字段。"""

    base = {"view": level, "pseudonym": patient.pseudonym}
    if level == IDENTITY_VIEW_NONE:
        return base
    if level == IDENTITY_VIEW_SUMMARY:
        base.update(age=patient.age, sex=patient.sex)
        return base
    base.update(
        real_name=patient.real_name,
        id_number=patient.id_number,
        contact=patient.contact,
        age=patient.age,
        sex=patient.sex,
    )
    return base


def to_jsonable(value):
    """dataclass/枚举/列表 → 可 JSON 序列化的普通结构。"""

    if is_dataclass(value):
        return {k: to_jsonable(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    return value
