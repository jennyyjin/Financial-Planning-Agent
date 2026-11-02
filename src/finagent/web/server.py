"""Standard-library HTTP server for the dashboard.

  GET  /             the page (static/index.html)
  GET  /static/...   page assets
  GET  /api/example  {"text": example narrative}
  POST /api/plan     {"text": str, "user_id"?: str} ->
                     {"status": "ready", "view": {...}, "plan": PlanOutput}
                     {"status": "needs_clarification", "message", "questions"}
                     {"status": "unavailable" | "invalid", "message"}   (HTTP 422)
"""

from __future__ import annotations

import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from finagent.output.agents.runtime import ChatModel
from finagent.output.pipeline import generate_plan
from finagent.schema import MemoryMethod
from finagent.web.extract import example_text, extract
from finagent.web.view import build_view

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 1_000_000  # bytes


class PlanHandler(BaseHTTPRequestHandler):
    model: Optional[ChatModel] = None
    memory_method: MemoryMethod = MemoryMethod.full_history
    quiet: bool = False

    def log_message(self, format: str, *args: Any) -> None:
        if not self.quiet:
            super().log_message(format, *args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, data: Any) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/example":
            return self._json(HTTPStatus.OK, {"text": example_text()})
        file = STATIC_DIR / ("index.html" if path == "/" else path.removeprefix("/static/"))
        file = file.resolve()
        if path != "/" and not path.startswith("/static/") or STATIC_DIR not in file.parents or not file.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, file.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/plan":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            text = body["text"]
            if not isinstance(text, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": 'expected JSON {"text": "..."}'})
        user_id = str(body.get("user_id") or "web")

        found = extract(text, user_id=user_id, memory_method=self.memory_method)
        if found.status == "needs_clarification":
            return self._json(HTTPStatus.OK, {"status": found.status, "message": found.message,
                                              "questions": found.questions})
        if found.status != "ready" or found.profile is None:
            return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"status": found.status, "message": found.message})
        try:
            plan = generate_plan(found.profile, model=self.model)
        except Exception as exc:  # report, don't drop the connection
            print(f"error: plan generation failed: {exc!r}", file=sys.stderr)
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error",
                                                                 "message": "Something went wrong building your plan."})
        self._json(HTTPStatus.OK, {"status": "ready", "message": found.message, "view": build_view(plan),
                                   "plan": plan.model_dump(mode="json", exclude={"trace"})})


def make_server(host: str = "127.0.0.1", port: int = 8000, model: Optional[ChatModel] = None,
                memory_method: MemoryMethod = MemoryMethod.full_history, quiet: bool = False) -> ThreadingHTTPServer:
    handler = type("Handler", (PlanHandler,), {"model": model, "memory_method": memory_method, "quiet": quiet})
    return ThreadingHTTPServer((host, port), handler)
