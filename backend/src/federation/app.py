"""联合试验中心 HTTP 服务（纯标准库 http.server）。

中心只接触汇总统计量：所有 API 与页面均不返回原始节拍。
路由：
  GET  /health
  POST /api/federation/trials            建试验（固定名单与版本）
  GET  /api/federation/trials            试验列表（待收集/缺站/冲突/已封算/版本）
  GET  /api/federation/trials/{id}       试验进度视图
  POST /api/federation/submissions       站点上报 n/mean/ss（幂等、冲突拒收）
  POST /api/federation/trials/{id}/seal  全齐封算（与接收并发安全）
  GET  /                                 简报页面（无原值）
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import FederationError, FederationService
from .store import Store


def _json_response(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    server_version = "FederationCenter/1.0"

    def log_message(self, fmt, *args):  # 安静日志，避免污染测试输出
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise FederationError("请求体不是合法 JSON")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            _json_response(self, 200, {"status": "healthy"})
        elif path == "/api/federation/trials":
            _json_response(self, 200, self.server.federation.list_views())
        elif path.startswith("/api/federation/trials/"):
            tid = self._trial_id(path, suffix=None)
            if tid is None:
                _json_response(self, 404, {"detail": "路径不存在"})
                return
            try:
                _json_response(self, 200, self.server.federation.trial_view(tid))
            except FederationError as exc:
                _json_response(self, 404, {"detail": str(exc)})
        elif path == "/" or path == "/index.html":
            self._dashboard()
        elif path == "/static/federation.js":
            self._static_js()
        else:
            _json_response(self, 404, {"detail": "路径不存在"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json()
            if path == "/api/federation/trials":
                trial = self.server.federation.create_trial(
                    body.get("name"), body.get("confidence", 0.95),
                    body.get("roster"))
                _json_response(self, 201, trial)
            elif path == "/api/federation/submissions":
                result = self.server.federation.submit(
                    body["trial_id"], body["station_id"],
                    body["sample_version"], body["payload"])
                _json_response(self, 200, result)
            elif path.endswith("/seal") and path.startswith(
                    "/api/federation/trials/"):
                tid = self._trial_id(path, suffix="/seal")
                if tid is None:
                    _json_response(self, 404, {"detail": "路径不存在"})
                    return
                info = self.server.federation.seal(tid)
                _json_response(self, 200, info)
            else:
                _json_response(self, 404, {"detail": "路径不存在"})
        except FederationError as exc:
            _json_response(self, 422, {"detail": str(exc)})
        except (KeyError, TypeError) as exc:
            _json_response(self, 422, {"detail": f"请求缺少字段：{exc}"})

    def _trial_id(self, path, suffix):
        prefix = "/api/federation/trials/"
        rest = path[len(prefix):]
        if suffix:
            if not rest.endswith(suffix):
                return None
            rest = rest[: -len(suffix)]
        try:
            return int(rest)
        except ValueError:
            return None

    def _dashboard(self):
        from .dashboard import render_index
        html = render_index()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static_js(self):
        from .dashboard import FEDERATION_JS
        body = FEDERATION_JS.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(db_path, host="127.0.0.1", port=0, verbose=False):
    store = Store(db_path)
    service = FederationService(store)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.federation = service
    httpd.store = store
    httpd.verbose = verbose
    return httpd


def serve_in_thread(db_path, host="127.0.0.1", port=0):
    """测试用：后台线程启动，返回 (httpd, base_url)。"""
    httpd = build_server(db_path, host, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"
