"""断肢急救转诊编排的运行入口。

`python3 service.py --check` 执行自检；`python3 service.py --port 8000`
启动装载了种子区域网络的编排服务。
"""

from __future__ import annotations

import argparse
import json
from http.server import ThreadingHTTPServer

from api import create_handler
from bootstrap import build_network
from domain import ADVICE_NOTICE, Clock
from orchestrator import Orchestrator

SERVICE_ID = "limb-referral"
SERVICE_NAME = "断肢急救转诊编排"


def health_payload():
    """返回健康状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_orchestrator(clock: Clock | None = None) -> Orchestrator:
    clock = clock or Clock()
    catalog = build_network(clock.now())
    return Orchestrator(catalog, clock=clock)


def self_check() -> None:
    """启动前自检：身份、网络装配与核心策略常量完整。"""

    assert SERVICE_NAME == health_payload()["name"]
    orchestrator = build_orchestrator()
    assert len(orchestrator.catalog.hospitals()) >= 4
    assert orchestrator.catalog.hospital("H1") is not None
    estimate = orchestrator.catalog.estimate_route("H0", "H1", orchestrator.clock.now())
    assert estimate.exists and estimate.total_minutes() > 0
    assert ADVICE_NOTICE
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        self_check()
        return
    orchestrator = build_orchestrator()
    handler = create_handler(orchestrator)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
