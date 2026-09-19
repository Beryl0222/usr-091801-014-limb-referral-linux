"""断肢急救转诊编排服务入口。

在基线健康检查之上提供 REST API：
- 机构能力图 / 路线 / 道路事件维护
- 病例录入、筛查排序、邀约与资源租约、转运计划与改道
- 途中里程碑（幂等补传）、风险升级、身份分阶段视图、审计轨迹

所有业务逻辑在 engine.Orchestrator；本文件只做 HTTP 编解码。
默认内存态运行，--log 指定 JSONL 事件日志后可跨进程重启恢复。
"""

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from engine import DomainError, Orchestrator

SERVICE_ID = "limb-referral"
SERVICE_NAME = "断肢急救转诊编排"

# 默认引擎：无 --log 时为纯内存态，基线契约测试不受文件副作用影响
_DEFAULT_ENGINE = Orchestrator(log_path=os.environ.get("LIMB_LOG_PATH"))


def health_payload():
    """返回健康状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def make_handler(engine: Orchestrator):
    """创建绑定指定引擎的 Handler（多实例/测试用）。"""

    class BoundHandler(Handler):
        pass

    BoundHandler.engine = engine
    return BoundHandler


class Handler(BaseHTTPRequestHandler):
    """HTTP 路由：把 JSON 请求转交给编排引擎。"""

    engine = _DEFAULT_ENGINE

    # ----- 基础 HTTP 工具 -----

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("INVALID_JSON", f"请求体不是合法 JSON：{exc}", 400) from exc
        if not isinstance(data, dict):
            raise DomainError("INVALID_JSON", "请求体必须是 JSON 对象", 400)
        return data

    def _query(self, name: str, default=None):
        values = parse_qs(urlparse(self.path).query).get(name)
        return values[0] if values else default

    def _handle(self, method: str):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            response = self._route(method, path)
        except DomainError as exc:
            self._send_json({"error": {"code": exc.code, "message": exc.message}}, exc.http_status)
            return
        except Exception as exc:  # 防御：任何未预期错误返回 500 而非挂连接
            self._send_json({"error": {"code": "INTERNAL", "message": str(exc)}}, 500)
            return
        if response is _SENT:
            return
        if response is None:
            self._send_json({}, 204)
        else:
            status = 201 if method == "POST" and isinstance(response, dict) and response.get("_created") else 200
            if isinstance(response, dict):
                response.pop("_created", None)
            self._send_json(response, status)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    # ----- 路由表 -----

    def _route(self, method: str, path: str):
        engine = self.engine

        if method == "GET" and path == "/health":
            return health_payload()
        if method == "POST" and path == "/admin/sweep":
            return engine.sweep(self._read_json().get("at"))
        if method == "GET" and path == "/escalations":
            return {"escalations": engine.escalations()}

        if method == "POST" and path == "/hospitals":
            return self._created(engine.register_hospital(self._read_json()))
        m = re.fullmatch(r"/hospitals/([^/]+)/heartbeat", path)
        if method == "POST" and m:
            data = self._read_json()
            return engine.heartbeat(m.group(1), data.get("at"))
        if method == "PUT" and path == "/routes":
            return engine.upsert_route(self._read_json())
        if method == "POST" and path == "/traffic":
            return engine.report_traffic(self._read_json())

        if method == "POST" and path == "/cases":
            return self._created(engine.create_case(self._read_json()))
        if method == "GET" and path == "/cases":
            return {"cases": engine.list_cases()}

        m = re.fullmatch(r"/cases/([^/]+)", path)
        if method == "GET" and m:
            return engine.get_case(m.group(1))

        cid_pattern = r"/cases/(?P<cid>[^/]+)"
        routes = [
            ("POST", rf"{cid_pattern}/vitals", lambda cid: engine.add_vitals(cid, self._read_json())),
            ("POST", rf"{cid_pattern}/screen",
             lambda cid: engine.screen_case(cid, self._read_json().get("at"))),
            ("GET", rf"{cid_pattern}/explanation", lambda cid: engine.explanation(cid)),
            ("POST", rf"{cid_pattern}/offers",
             lambda cid: self._request_offers(engine, cid)),
            ("POST", rf"{cid_pattern}/plan", lambda cid: self._create_plan(engine, cid)),
            ("GET", rf"{cid_pattern}/plan", lambda cid: engine.get_plan(cid)),
            ("POST", rf"{cid_pattern}/reroute", lambda cid: engine.reroute(cid, self._read_json())),
            ("POST", rf"{cid_pattern}/milestones", lambda cid: engine.record_milestone(cid, self._read_json())),
            ("GET", rf"{cid_pattern}/milestones",
             lambda cid: {"milestones": engine.list_milestones(cid)}),
            ("GET", rf"{cid_pattern}/identity",
             lambda cid: engine.identity_view(cid, self._require_query("hospital_id"))),
            ("GET", rf"{cid_pattern}/audit", lambda cid: engine.audit_trail(cid)),
            ("POST", rf"{cid_pattern}/close",
             lambda cid: engine.close_case(cid, self._read_json().get("outcome"))),
        ]
        for verb, pattern, fn in routes:
            match = re.fullmatch(pattern, path)
            if method == verb and match:
                return fn(match.group("cid"))

        m = re.fullmatch(rf"{cid_pattern}/risks/(?P<rid>[^/]+)/ack", path)
        if method == "POST" and m:
            data = self._read_json()
            by = data.get("by") or self._query("by")
            if not by:
                raise DomainError("INVALID_ACK", "确认风险必须提供 by（值班员标识）", 400)
            return engine.acknowledge_risk(match.group("cid"), match.group("rid"), by, data.get("note"))

        m = re.fullmatch(r"/offers/(?P<oid>[^/]+)/respond", path)
        if method == "POST" and m:
            data = self._read_json()
            if "accepted" not in data:
                raise DomainError("INVALID_RESPONSE", "邀约应答必须包含 accepted 布尔值", 400)
            return engine.respond_offer(
                m.group("oid"), bool(data["accepted"]), data.get("at"), data.get("declined_reason"))

        self._send_json({"error": {"code": "NOT_FOUND", "message": f"无此路由：{method} {path}"}}, 404)
        return _SENT

    def _request_offers(self, engine: Orchestrator, case_id: str):
        data = self._read_json()
        return {"offers": engine.request_offers(
            case_id, data.get("hospital_ids"), data.get("at"), bool(data.get("manual", False)))}

    def _create_plan(self, engine: Orchestrator, case_id: str):
        data = self._read_json()
        offer_id = data.get("offer_id")
        if not offer_id:
            raise DomainError("INVALID_PLAN", "创建转运计划必须提供 offer_id", 400)
        return self._created(engine.create_plan(case_id, offer_id, data.get("transport"), data.get("at")))

    def _require_query(self, name: str) -> str:
        value = self._query(name)
        if not value:
            raise DomainError("MISSING_QUERY", f"缺少查询参数 {name}", 400)
        return value

    @staticmethod
    def _created(payload: dict) -> dict:
        payload = dict(payload)
        payload["_created"] = True
        return payload

    def do_PUT(self):
        self._handle("PUT")

    def log_message(self, *_args):
        return


_SENT = object()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--log", help="JSONL 事件日志路径；提供后重启可恢复全部状态")
    args = parser.parse_args()
    if args.check:
        assert SERVICE_NAME == health_payload()["name"]
        # 领域引擎冒烟检查：验证核心策略常量与免责声明存在
        from engine import DISCLAIMER, WARM_ISCHEMIA_MINUTES

        assert WARM_ISCHEMIA_MINUTES == 360 and "诊断" in DISCLAIMER
        print("基础检查通过")
        return
    engine = Orchestrator(log_path=args.log) if args.log else _DEFAULT_ENGINE
    handler = make_handler(engine)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
