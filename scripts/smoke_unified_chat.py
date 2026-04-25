"""Phase 9 cutover smoke test for the unified chat surface.

Drives the system over HTTP only — no DB or Redis access required.
Run against staging FIRST, then prod after `UNIFIED_CHAT_ENABLED=true`.

Usage
-----
    set BASE_URL=https://staging.example.com
    set SESSION_COOKIE=session=...
    python scripts/smoke_unified_chat.py

Exits non-zero if any check fails. Designed to be safe to re-run: every
agent name is timestamped.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

import httpx


BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
SESSION_COOKIE = os.environ.get("SESSION_COOKIE", "")
TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "30"))


def _client() -> httpx.Client:
    headers = {"Accept": "application/json"}
    cookies = {}
    if SESSION_COOKIE:
        # Allow either "name=value" or just the session value.
        if "=" in SESSION_COOKIE:
            name, _, value = SESSION_COOKIE.partition("=")
            cookies[name.strip()] = value.strip()
        else:
            cookies["session"] = SESSION_COOKIE
    return httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        cookies=cookies,
        timeout=TIMEOUT,
        follow_redirects=False,
    )


def _check(label: str, ok: bool, detail: Any = "") -> None:
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {label}{(' :: ' + str(detail)) if detail else ''}")
    if not ok:
        raise SystemExit(1)


def smoke() -> None:
    suffix = uuid.uuid4().hex[:6]
    agent_name = f"Smoke {suffix}"

    with _client() as c:
        # 1. Health.
        r = c.get("/health")
        _check("GET /health", r.status_code == 200, r.status_code)

        # 2. Chat: general_chat.
        r = c.post("/chat/message", json={"text": "hello"})
        _check("POST /chat/message general", r.status_code == 200, r.status_code)
        body = r.json()
        _check("chat returns text", isinstance(body.get("text"), str))

        # 3. Chat: create_agent.
        r = c.post(
            "/chat/message",
            json={"text": f"create an agent called {agent_name} that summarises text"},
        )
        _check("POST /chat/message create", r.status_code == 200, r.status_code)
        body = r.json()
        agent_id = (body.get("data") or {}).get("agent_id")
        _check("create returned agent_id", isinstance(agent_id, int), body)

        # 4. Chat: modify_agent.
        r = c.post(
            "/chat/message",
            json={"text": f"add tool core.time_now to {agent_name}"},
        )
        _check("POST /chat/message modify", r.status_code == 200, r.status_code)
        _check("modify text contains updated", "Updated" in (r.json().get("text") or ""))

        # 5. Workflow run (only if engine flag is on; tolerate 404).
        r = c.post(
            "/workflows/run",
            json={
                "steps": [
                    {"id": "now", "type": "tool", "tool": "core.time_now", "args": {}}
                ],
                "inputs": {},
            },
        )
        if r.status_code == 404:
            print("[SKIP] /workflows/run (WORKFLOW_ENGINE_ENABLED off)")
        else:
            _check("POST /workflows/run", r.status_code == 200, r.status_code)
            body = r.json()
            _check("workflow ok", body.get("status") == "ok", body)
            _check("workflow output present", "now" in (body.get("outputs") or {}))

        # 6. Plugin endpoint (only if loader flag is on; tolerate 404/400).
        r = c.get("/admin/plugins")
        if r.status_code == 200:
            body = r.json()
            print(f"[INFO] /admin/plugins enabled={body.get('enabled')} count={len(body.get('items') or [])}")
        else:
            print(f"[INFO] /admin/plugins status={r.status_code} (admin auth required)")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        smoke()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] unexpected error: {exc}", file=sys.stderr)
        sys.exit(2)
