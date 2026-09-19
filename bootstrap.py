"""演练用种子网络：一家基层接诊机构 + 三家候选医院。

数据中的“当前时间”由编排时钟注入，保证心跳/资料时效在演练时刻可判定。
"""

from __future__ import annotations

from catalog import NetworkCatalog, route_key
from domain import (
    Hospital,
    ORTable,
    RouteInfo,
    Team,
    TrafficEvent,
    Weather,
)

ORIGIN_ID = "H0"


def build_network(now: float) -> NetworkCatalog:
    catalog = NetworkCatalog()
    catalog.register_carrier("EMS1", "市急救中心城东分站")
    catalog.register_carrier("EMS2", "市急救中心南郊分站")

    catalog.register_hospital(
        Hospital(
            id=ORIGIN_ID,
            name="城东社区急救接诊点",
            tier="基层",
            address="城东路 12 号",
            microsurgery=False,
        )
    )

    # H1：市级显微外科中心，两支可配团队、两张显微台，资料新鲜。
    catalog.register_hospital(
        Hospital(
            id="H1",
            name="市第一医院创伤中心",
            tier="三级甲等",
            address="健康路 88 号",
            microsurgery=True,
            supported_parts=["forearm", "hand", "finger", "upper_arm"],
            min_vessel_mm=0.8,
            equipment=["operative_microscope", "vascular_doppler", "loupes"],
            capability_updated_at=now - 5 * 60,
        )
    )
    catalog.add_team(
        "H1",
        Team(
            id="T1A",
            hospital_id="H1",
            name="市一手外一组",
            capable_parts=["forearm", "hand", "finger", "upper_arm"],
            min_vessel_mm=0.8,
            on_duty=True,
            last_heartbeat_at=now - 60,
        ),
    )
    catalog.add_team(
        "H1",
        Team(
            id="T1B",
            hospital_id="H1",
            name="市一手外二组",
            capable_parts=["forearm", "upper_arm"],
            min_vessel_mm=1.5,
            on_duty=True,
            last_heartbeat_at=now - 90,
        ),
    )
    catalog.add_table(
        "H1",
        ORTable(id="OR1", hospital_id="H1", code="1 号复合手术间",
                microsurgery_ready=True, reported_available=True,
                last_heartbeat_at=now - 60),
    )
    catalog.add_table(
        "H1",
        ORTable(id="OR2", hospital_id="H1", code="2 号复合手术间",
                microsurgery_ready=True, reported_available=True,
                last_heartbeat_at=now - 120),
    )

    # H2：省级骨科医院，距离远，沿线结冰；团队与台各一。
    catalog.register_hospital(
        Hospital(
            id="H2",
            name="省骨科医院显微外科",
            tier="三级甲等",
            address="南郊山前路 1 号",
            microsurgery=True,
            supported_parts=["forearm", "hand", "upper_arm", "lower_leg"],
            min_vessel_mm=0.5,
            equipment=["operative_microscope", "vascular_doppler", "loupes", "hypothermia_cart"],
            capability_updated_at=now - 10 * 60,
        )
    )
    catalog.add_team(
        "H2",
        Team(
            id="T2",
            hospital_id="H2",
            name="省骨显微修复组",
            capable_parts=["forearm", "hand", "upper_arm", "lower_leg"],
            min_vessel_mm=0.5,
            on_duty=True,
            last_heartbeat_at=now - 150,
        ),
    )
    catalog.add_table(
        "H2",
        ORTable(id="OR3", hospital_id="H2", code="南楼 7 手术间",
                microsurgery_ready=True, reported_available=True,
                last_heartbeat_at=now - 150),
    )

    # H3：城西医院，物理距离最近，但能力资料过期、资源心跳失联。
    catalog.register_hospital(
        Hospital(
            id="H3",
            name="城西创伤医院",
            tier="三级",
            address="西环大道 5 号",
            microsurgery=True,
            supported_parts=["forearm", "hand"],
            min_vessel_mm=1.0,
            equipment=["operative_microscope"],
            capability_updated_at=now - 95 * 60,
        )
    )
    catalog.add_team(
        "H3",
        Team(
            id="T3",
            hospital_id="H3",
            name="城西手外值班组",
            capable_parts=["forearm", "hand"],
            min_vessel_mm=1.0,
            on_duty=True,
            last_heartbeat_at=now - 12 * 60,
        ),
    )
    catalog.add_table(
        "H3",
        ORTable(id="OR4", hospital_id="H3", code="急诊手术间",
                microsurgery_ready=True, reported_available=True,
                last_heartbeat_at=now - 11 * 60),
    )

    # 路况观测。
    catalog.upsert_route(
        RouteInfo(ORIGIN_ID, "H1", road="城北快速路", distance_km=38.0,
                  base_minutes=45.0, observed_at=now - 8 * 60)
    )
    catalog.upsert_route(
        RouteInfo(ORIGIN_ID, "H2", road="G6 高速转山前路", distance_km=92.0,
                  base_minutes=75.0, observed_at=now - 15 * 60)
    )
    catalog.upsert_route(
        RouteInfo(ORIGIN_ID, "H3", road="西环大道", distance_km=14.0,
                  base_minutes=20.0, observed_at=now - 6 * 60)
    )

    # 道路环境。
    catalog.upsert_weather(
        Weather(route_key(ORIGIN_ID, "H1"), condition="rain",
                speed_factor=0.9, observed_at=now - 12 * 60)
    )
    catalog.upsert_weather(
        Weather(route_key(ORIGIN_ID, "H2"), condition="ice",
                speed_factor=0.72, observed_at=now - 20 * 60)
    )
    catalog.upsert_weather(
        Weather(route_key(ORIGIN_ID, "H3"), condition="clear",
                speed_factor=1.0, observed_at=now - 6 * 60)
    )

    # 交通事件（值班员可在演练中再追加）。
    catalog.report_traffic_event(
        TrafficEvent(
            id="E1",
            route_key=route_key(ORIGIN_ID, "H1"),
            title="城北快速路 K12 追尾占一道",
            delay_minutes=10.0,
            status="active",
            observed_at=now - 18 * 60,
        )
    )
    return catalog
