"""区域救治网络的能力目录与交通看板。

目录只回答“机构自报的能力与资源状态是什么、路况观测如何”，
不持有租约——同一团队/手术台是否被并发占用由编排层叠加租约判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from domain import (
    CAPABILITY_STALE_AFTER_MIN,
    Hospital,
    ORTable,
    RouteInfo,
    Team,
    TrafficEvent,
    Weather,
)


def route_key(from_id: str, to_id: str) -> str:
    return f"{from_id}->{to_id}"


@dataclass
class ActiveEvent:
    id: str
    title: str
    delay_minutes: float
    observed_at: float


@dataclass
class RouteEstimate:
    exists: bool
    road: Optional[str] = None
    distance_km: Optional[float] = None
    base_minutes: Optional[float] = None
    route_observed_at: Optional[float] = None
    route_age_minutes: Optional[float] = None
    weather_condition: str = "clear"
    weather_speed_factor: float = 1.0
    weather_observed_at: Optional[float] = None
    weather_age_minutes: Optional[float] = None
    travel_minutes: Optional[float] = None     # 天气折算后的行驶时间
    events: list[ActiveEvent] = field(default_factory=list)
    event_delay_minutes: float = 0.0

    def total_minutes(self) -> Optional[float]:
        if self.travel_minutes is None:
            return None
        return self.travel_minutes + self.event_delay_minutes


class NetworkCatalog:
    """内存态的机构能力与路况注册表。"""

    def __init__(self):
        self._hospitals: dict[str, Hospital] = {}
        self._routes: dict[str, RouteInfo] = {}
        self._weather: dict[str, Weather] = {}
        self._events: dict[str, TrafficEvent] = {}
        self._carriers: dict[str, str] = {}

    # -- 机构与能力 -------------------------------------------------------

    def register_hospital(self, hospital: Hospital) -> Hospital:
        self._hospitals[hospital.id] = hospital
        return hospital

    def update_capability(
        self,
        hospital_id: str,
        *,
        microsurgery: Optional[bool] = None,
        supported_parts: Optional[list[str]] = None,
        min_vessel_mm: Optional[float] = None,
        equipment: Optional[list[str]] = None,
        observed_at: float,
    ) -> None:
        """机构上报能力资料，刷新资料时间戳。"""

        hospital = self.require_hospital(hospital_id)
        if microsurgery is not None:
            hospital.microsurgery = microsurgery
        if supported_parts is not None:
            hospital.supported_parts = supported_parts
        if min_vessel_mm is not None:
            hospital.min_vessel_mm = min_vessel_mm
        if equipment is not None:
            hospital.equipment = equipment
        hospital.capability_updated_at = observed_at

    def add_team(self, hospital_id: str, team: Team) -> None:
        hospital = self.require_hospital(hospital_id)
        team.hospital_id = hospital_id
        hospital.teams.append(team)

    def add_table(self, hospital_id: str, table: ORTable) -> None:
        hospital = self.require_hospital(hospital_id)
        table.hospital_id = hospital_id
        hospital.tables.append(table)

    def heartbeat_team(self, team_id: str, now: float, on_duty: Optional[bool] = None) -> None:
        team = self.require_team(team_id)
        team.last_heartbeat_at = now
        if on_duty is not None:
            team.on_duty = on_duty

    def heartbeat_table(
        self,
        table_id: str,
        now: float,
        available: Optional[bool] = None,
    ) -> None:
        table = self.require_table(table_id)
        table.last_heartbeat_at = now
        if available is not None:
            table.reported_available = available

    def hospital(self, hospital_id: str) -> Optional[Hospital]:
        return self._hospitals.get(hospital_id)

    def require_hospital(self, hospital_id: str) -> Hospital:
        hospital = self._hospitals.get(hospital_id)
        if hospital is None:
            raise KeyError(f"未知机构: {hospital_id}")
        return hospital

    def require_team(self, team_id: str) -> Team:
        for hospital in self._hospitals.values():
            for team in hospital.teams:
                if team.id == team_id:
                    return team
        raise KeyError(f"未知团队: {team_id}")

    def require_table(self, table_id: str) -> ORTable:
        for hospital in self._hospitals.values():
            for table in hospital.tables:
                if table.id == table_id:
                    return table
        raise KeyError(f"未知手术台: {table_id}")

    def hospitals(self) -> list[Hospital]:
        return list(self._hospitals.values())

    def destination_candidates(self, origin_id: str) -> list[Hospital]:
        return [h for h in self._hospitals.values() if h.id != origin_id]

    # -- 转运力量 ---------------------------------------------------------

    def register_carrier(self, carrier_id: str, name: str) -> None:
        """登记实际承担转运的机构（120 分站等），不参与接收候选筛选。"""

        self._carriers[carrier_id] = name

    def require_carrier(self, carrier_id: str) -> str:
        if carrier_id not in self._carriers:
            raise KeyError(f"未知转运机构: {carrier_id}")
        return self._carriers[carrier_id]

    def carriers(self) -> dict[str, str]:
        return dict(self._carriers)

    # -- 路况与道路环境 ---------------------------------------------------

    def upsert_route(self, route: RouteInfo) -> None:
        self._routes[route_key(route.from_hospital_id, route.to_hospital_id)] = route

    def upsert_weather(self, weather: Weather) -> None:
        self._weather[weather.route_key] = weather

    def report_traffic_event(self, event: TrafficEvent) -> None:
        self._events[event.id] = event

    def clear_traffic_event(self, event_id: str, now: float) -> None:
        event = self._events.get(event_id)
        if event is not None:
            event.status = "cleared"
            event.observed_at = now

    def estimate_route(self, from_id: str, to_id: str, now: float) -> RouteEstimate:
        route = self._routes.get(route_key(from_id, to_id))
        if route is None:
            return RouteEstimate(exists=False)

        weather = self._weather.get(route_key(from_id, to_id))
        factor = weather.speed_factor if weather else 1.0
        travel = route.base_minutes / factor if factor > 0 else float("inf")

        events = [
            ActiveEvent(e.id, e.title, e.delay_minutes, e.observed_at)
            for e in self._events.values()
            if e.route_key == route_key(from_id, to_id)
            and e.status == "active"
        ]
        delay = sum(e.delay_minutes for e in events)

        return RouteEstimate(
            exists=True,
            road=route.road,
            distance_km=route.distance_km,
            base_minutes=route.base_minutes,
            route_observed_at=route.observed_at,
            route_age_minutes=(now - route.observed_at) / 60,
            weather_condition=weather.condition if weather else "clear",
            weather_speed_factor=factor,
            weather_observed_at=weather.observed_at if weather else None,
            weather_age_minutes=((now - weather.observed_at) / 60) if weather else None,
            travel_minutes=travel,
            events=events,
            event_delay_minutes=delay,
        )

    def capability_stale_threshold_min(self) -> int:
        return CAPABILITY_STALE_AFTER_MIN
