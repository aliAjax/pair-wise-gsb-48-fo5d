"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
BATCH_RE = re.compile(r"^/api/batches/(\d+)$")
BATCH_ACTION_RE = re.compile(r"^/api/batches/(\d+)/(confirm|close)$")
RECON_RE = re.compile(r"^/api/reconciliation/(\d+)/complete$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "securities-settlement/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "securities-settlement", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    records = service.list_records(
                        self._actor(),
                        state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                        batch_id=int(query["batch_id"][0]) if "batch_id" in query else None,
                    )
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if parsed.path == "/api/batches":
                    self._send(200, {"items": service.list_batches(self._actor(), state=query.get("state", [None])[0])})
                    return
                match = BATCH_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_batch(self._actor(), int(match.group(1))))
                    return
                if parsed.path == "/api/entitlements":
                    self._send(200, {"items": service.list_entitlements(self._actor(), instrument=query.get("instrument", [None])[0])})
                    return
                if parsed.path == "/api/reconciliation":
                    self._send(200, {"items": service.list_reconciliation(self._actor(), status=query.get("status", [None])[0])})
                    return
                if parsed.path == "/api/ledger":
                    self._send(200, {"items": service.ledger(
                        self._actor(),
                        batch_id=int(query["batch_id"][0]) if "batch_id" in query else None,
                    )})
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(
                        self._actor(), body.get("reference", ""), body.get("data", {}),
                        batch_ref=body.get("batch_ref"),
                    )
                    self._send(201, record)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                if parsed.path == "/api/batches":
                    if not isinstance(body.get("settlement_day"), int):
                        raise ValidationError("settlement_day必须是整数")
                    batch = service.create_batch(self._actor(), body.get("reference", ""), int(body["settlement_day"]))
                    self._send(201, batch)
                    return
                match = BATCH_ACTION_RE.match(parsed.path)
                if match:
                    batch_id = int(match.group(1))
                    if match.group(2) == "close":
                        self._send(200, service.close_batch(self._actor(), batch_id))
                        return
                    version = body.get("expected_version")
                    if version is not None and not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    self._send(200, service.confirm_batch(self._actor(), batch_id, expected_version=version))
                    return
                if parsed.path == "/api/eod/run":
                    if not isinstance(body.get("settlement_day"), int):
                        raise ValidationError("settlement_day必须是整数")
                    self._send(200, service.run_eod(self._actor(), int(body["settlement_day"])))
                    return
                if parsed.path == "/api/entitlements":
                    entitlement = service.publish_entitlement(self._actor(), body.get("instrument", ""), body.get("terms", {}))
                    self._send(201, entitlement)
                    return
                if parsed.path == "/api/receipts":
                    receipt = service.ingest_receipt(self._actor(), body.get("batch_ref", ""), body.get("data", {}))
                    self._send(201 if not receipt.get("duplicate") else 200, receipt)
                    return
                match = RECON_RE.match(parsed.path)
                if match:
                    self._send(200, service.complete_reconciliation(
                        self._actor(), int(match.group(1)), adjustment_ref=body.get("adjustment_ref"),
                    ))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
