"""测试共用夹具：确定性时钟 + 种子网络。"""

from bootstrap import build_network
from domain import Injury, MutableClock, Patient, Preservation, Vitals
from orchestrator import Orchestrator

T0 = 1_790_000_000.0  # 固定演练基准时刻（Unix 秒）


def make_orchestrator():
    clock = MutableClock(T0)
    catalog = build_network(T0)
    return Orchestrator(catalog, clock=clock), clock


def make_injury(
    *,
    part="forearm",
    minutes_ago=30.0,
    vessel=2.0,
    preservation=Preservation.WARM.value,
    equipment=None,
    now=None,
):
    amputated_at = (now if now is not None else T0) - minutes_ago * 60
    return Injury(
        body_part=part,
        amputated_at=amputated_at,
        preservation=preservation,
        vessel_diameter_mm=vessel,
        required_equipment=equipment or [],
    )


def make_patient(pseudonym="P-TEST", *, full=True):
    if not full:
        return Patient(pseudonym=pseudonym)
    return Patient(
        pseudonym=pseudonym,
        real_name="测试患者",
        id_number="110101199001011234",
        contact="13800000000",
        age=42,
        sex="M",
    )


def register_case(
    orch,
    *,
    pseudonym="P-TEST",
    part="forearm",
    minutes_ago=30.0,
    vessel=2.0,
    preservation=Preservation.WARM.value,
    equipment=None,
    origin="H0",
    recorded_by="城东接诊点值班员",
):
    case = orch.register_case(
        origin_hospital_id=origin,
        recorded_by=recorded_by,
        patient=make_patient(pseudonym),
        injury=make_injury(
            part=part,
            minutes_ago=minutes_ago,
            vessel=vessel,
            preservation=preservation,
            equipment=equipment,
            now=orch.clock.now(),
        ),
        vitals=Vitals(level="stable", sbp_mmhg=118, hr_per_min=92, spo2=97),
    )
    return case


def feasible(recset, hospital_id):
    return next(r for r in recset.recommendations if r.hospital_id == hospital_id)


def risk_codes(rec):
    return {risk.code for risk in rec.risks}
