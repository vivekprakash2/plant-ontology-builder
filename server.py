#!/usr/bin/env python3
"""Local chat server for the Plant Ontology agent.

Zero-dependency (stdlib `http.server` only) so it runs with no installs.

Routes:
    GET  /                 chat UI (frontend/index.html)
    GET  /app.js, /style.css   static frontend assets
    GET  /api/suggestions  the demo questions from TEAM_HANDBOOK.md Sec 7
    GET  /api/graph        the whole plant ontology (nodes/edges) for the explorer panel
    POST /api/chat         {"question": "..."} -> text/event-stream of live plan/tool-call/
                           tool-result events, ending with one "final" event carrying the same
                           answer+evidence+panel JSON shape as before (see docs/CHAT_RAG.md §3).

Run:
    python3 server.py
Then open http://127.0.0.1:8000/

SECURITY_NOTE: binds to 127.0.0.1 only (not 0.0.0.0) -- local demo use.
All responses are static/derived-from-local-data JSON; no secrets are ever
read or echoed. Request bodies are size-capped and JSON-validated before use.
"""
from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ontology_builder.agent import stream_answer
from ontology_builder.pipeline import load_or_build

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"

# Load a local, gitignored .env file (KEY=value lines) if present, so
# credentials (e.g. DATABRICKS_TOKEN) never need to be pasted into chat or
# committed to source control. Real environment variables always win.
# SECURITY_NOTE: values are read straight into os.environ and never logged.
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))

MAX_BODY_BYTES = 4_096
MAX_QUESTION_LEN = 500

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/logo.svg": ("logo.svg", "image/svg+xml"),
}

SUGGESTED_QUESTIONS = [
    "Why is Crude Charge Pump P-101 vibrating?",
    "Why is fired heater H-101's fuel consumption rising?",
    "Why is column C-101's differential pressure high?",
    "Is the Recycle Gas Compressor K-101 experiencing the same problem as P-101?",
    "Why are there so many alarms on tank TK-201?",
    "Show me everything known about P-101 across all systems.",
    "What maintenance happened on the crude charge pump in the last week, and did anything change in operations around the same time?",
]

# Loaded once at startup -- Stage 0/1/2/3 pipeline result is static per run.
# Prefers the on-disk cache (output/unified_entities.json + output/graph.json)
# written by a previous run_pipeline.py/run_graph.py/server.py run, skipping
# re-ingesting Data/*.csv and re-running entity resolution entirely. Set
# FORCE_REBUILD=1 to always regenerate from source data (e.g. after editing
# Data/*.csv or the resolution logic) instead of trusting a stale cache.
_force_rebuild = os.environ.get("FORCE_REBUILD", "").strip() not in ("", "0", "false", "False")
print("Loading unified entities + knowledge graph"
      f" ({'forced rebuild' if _force_rebuild else 'cache if available'})...")
_ENTITIES, _KG = load_or_build(force_rebuild=_force_rebuild)
print(f"Ready: {len(_ENTITIES)} assets, {len(_KG.nodes)} graph nodes.")

# Warm up the language-model provider (if any) on the MAIN thread before the
# server starts handling requests. mlx-lm's Metal device initialization is
# not safe to trigger lazily from a background thread -- doing it here once
# avoids an NSException crash on the first chat request.
from ontology_builder.llm_provider import NullTextGenerationProvider, get_text_generation_provider  # noqa: E402

print("Warming up text-generation provider...")
_provider = get_text_generation_provider()
if isinstance(_provider, NullTextGenerationProvider):
    print("No language model available -- answers will use the deterministic rule-based text only.")
else:
    print(f"Language model ready: {getattr(_provider, 'model', type(_provider).__name__)}")


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "OntologyChat/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Minimal hardening headers for a local demo server.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API name)
        if self.path == "/api/suggestions":
            self._send_json(HTTPStatus.OK, {"questions": SUGGESTED_QUESTIONS})
            return

        if self.path == "/api/graph":
            # Whole plant ontology (all nodes/edges) for the persistent
            # explorer panel -- small enough (~160 nodes) to send in one
            # shot, no pagination needed. Read-only, no secrets.
            self._send_json(HTTPStatus.OK, _KG.to_node_link_json())
            return

        entry = _STATIC_FILES.get(self.path)
        if entry is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        filename, content_type = entry
        file_path = FRONTEND_DIR / filename
        try:
            data = file_path.read_bytes()
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid or oversized request body"})
            return

        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be valid JSON"})
            return

        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or not question.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'question' (non-empty string) is required"})
            return
        question = question.strip()[:MAX_QUESTION_LEN]

        # Server-Sent Events: one "data: <json>\n\n" line per live event from
        # stream_answer() (plan updates, tool-call start, tool-call result),
        # ending with one "final" event carrying the same response shape
        # /api/chat always returned before this was streamed. Headers are
        # sent up front (status can't change once the stream starts) --
        # stream_answer() itself never raises (it has its own deterministic
        # fallback), so there's no risk of needing to send an error status
        # after the stream has already begun.
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            for event in stream_answer(question, _ENTITIES, _KG):
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away / closed the tab mid-stream -- nothing to do

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    host, port = "127.0.0.1", 8000
    # Single-threaded on purpose: the local language model (mlx-lm, if used) is warmed up on this main
    # thread, and handling requests on other threads risks a Metal
    # initialization crash. Fine for a local single-user demo server.
    httpd = HTTPServer((host, port), ChatHandler)
    print(f"Serving on http://{host}:{port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
