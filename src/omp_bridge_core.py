#!/usr/bin/env python3
"""
omp-a2a-bridge: A2A v1.0 server that fronts `omp --mode rpc` subprocesses.

- Each A2A context_id maps to one long-lived `omp` RPC subprocess.
- Multi-turn within a context: the same subprocess is reused, so omp keeps its
  own conversation history (omp's in-process session).
- Implements:
    GET  /.well-known/agent-card.json  (+ legacy agent.json)
    POST /   JSON-RPC: SendMessage / message/send, tasks/get, tasks/cancel

Design notes:
- `omp --mode rpc` speaks NDJSON over stdio. Each `prompt` request gets an
  immediate `{"type":"response",...}` ACK, then a stream of events, ending in
  `{"type":"prompt_result","id":...,"status":"completed"}`.
- We serialize prompts per-subprocess (omp doesn't support concurrent prompts
  on one stdio channel).
- Assistant text is harvested from `message_end` events (role == "assistant").
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# ---------------- config ----------------

OMP_BIN = os.environ.get("OMP_BIN", "/usr/local/bin/omp")
OMP_CWD = os.environ.get("OMP_A2A_CWD", "/root")          # workspace for omp
OMP_MODEL = os.environ.get("OMP_A2A_MODEL", "")           # optional model flag
HOST = os.environ.get("OMP_A2A_HOST", "127.0.0.1")
PORT = int(os.environ.get("OMP_A2A_PORT", "9911"))
TOKEN = os.environ.get("OMP_A2A_TOKEN", "")               # bearer; empty = no auth
IDLE_TTL_SEC = int(os.environ.get("OMP_A2A_IDLE_TTL", "1800"))  # kill idle workers
PROMPT_TIMEOUT_SEC = int(os.environ.get("OMP_A2A_PROMPT_TIMEOUT", "600"))

AGENT_NAME = os.environ.get("OMP_A2A_AGENT_NAME", "omp-bridge")

# ---------------- logging ----------------

logging.basicConfig(
    level=os.environ.get("OMP_A2A_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("omp-a2a-bridge")


# ---------------- omp rpc worker ----------------

class OmpWorker:
    """One `omp --mode rpc` subprocess, serialized prompt queue."""

    def __init__(self, context_id: str):
        self.context_id = context_id
        self.lock = threading.Lock()       # one prompt at a time
        self.proc: Optional[subprocess.Popen] = None
        self.last_used = time.time()
        self._reader_thread: Optional[threading.Thread] = None
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._closed = False

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        argv = [
            OMP_BIN,
            "--mode", "rpc",
            "--no-ui",
            "--no-extensions",
            "--approval-mode", "yolo",
            "--cwd", OMP_CWD,
        ]
        if OMP_MODEL:
            argv.extend(["--model", OMP_MODEL])
        # NOTE: do NOT pass --no-session; we want omp to keep its session file
        # so the conversation survives even if the bridge restarts and we
        # choose to resume later. Within one bridge process, the live
        # subprocess already preserves history across prompts.
        log.info("starting omp for ctx=%s: %s", self.context_id, " ".join(argv))
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._closed = False
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        # wait for ready
        ready = self._wait_event(lambda e: e.get("type") == "ready", timeout=30)
        if not ready:
            raise RuntimeError("omp did not emit ready within 30s")

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.warning("omp non-JSON line: %s", line[:200])
                continue
            self._events.put(ev)
        self._events.put({"type": "__eof__"})

    def _wait_event(self, pred, timeout: float) -> Optional[dict]:
        deadline = time.time() + timeout
        stashed = []
        try:
            while time.time() < deadline:
                try:
                    ev = self._events.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    break
                if pred(ev):
                    # put stash back
                    for s in stashed:
                        self._events.put(s)
                    return ev
                stashed.append(ev)
        finally:
            for s in stashed:
                self._events.put(s)
        return None

    def prompt(self, text: str, timeout: int = PROMPT_TIMEOUT_SEC) -> str:
        """Send one prompt, block until prompt_result, return final assistant text."""
        with self.lock:
            self.start()
            assert self.proc and self.proc.stdin
            rid = f"a2a-{uuid.uuid4().hex[:12]}"
            frame = {"id": rid, "type": "prompt", "message": text}
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()
            self.last_used = time.time()

            texts: list[str] = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    ev = self._events.get(timeout=max(0.5, deadline - time.time()))
                except queue.Empty:
                    break
                t = ev.get("type")
                if t == "__eof__":
                    raise RuntimeError("omp subprocess exited mid-prompt")
                if t == "message_end":
                    msg = ev.get("message") or {}
                    if msg.get("role") == "assistant":
                        content = msg.get("content") or []
                        for c in content:
                            if c.get("type") == "text":
                                texts.append(c.get("text", ""))
                elif t == "prompt_result" and ev.get("id") == rid:
                    status = ev.get("status", "")
                    if status != "completed":
                        raise RuntimeError(f"omp prompt ended with status={status}")
                    self.last_used = time.time()
                    return "\n".join(texts).strip() or "(omp returned no text)"
            raise TimeoutError(f"omp prompt timed out after {timeout}s")

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception as e:
            log.warning("error stopping omp ctx=%s: %s", self.context_id, e)


class WorkerPool:
    def __init__(self):
        self._workers: dict[str, OmpWorker] = {}
        self._lock = threading.Lock()
        # Reaper thread for idle workers
        threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, context_id: str) -> OmpWorker:
        with self._lock:
            w = self._workers.get(context_id)
            if w is None:
                w = OmpWorker(context_id)
                self._workers[context_id] = w
            return w

    def drop(self, context_id: str) -> bool:
        with self._lock:
            w = self._workers.pop(context_id, None)
        if w:
            w.stop()
            return True
        return False

    def _reaper(self) -> None:
        while True:
            time.sleep(60)
            cutoff = time.time() - IDLE_TTL_SEC
            with self._lock:
                idle = [cid for cid, w in self._workers.items() if w.last_used < cutoff]
                for cid in idle:
                    self._workers.pop(cid, None)
            for cid in idle:
                log.info("reaping idle omp worker ctx=%s", cid)
                # w.stop() outside lock; we lost the ref, look it up first
                # — re-look up safely
                # (We popped above; just spawn a fresh OmpWorker handle to stop it.)
                # Simpler: keep references. Rework:
                pass
            # Re-do with refs:
            # (handled below in a second pass)

    # simpler reaper that keeps refs
    def reaper_loop(self) -> None:  # unused; kept for clarity
        pass


# Replace the broken reaper with a correct one
def _start_reaper(pool: WorkerPool) -> None:
    def loop():
        while True:
            time.sleep(60)
            cutoff = time.time() - IDLE_TTL_SEC
            victims: list[OmpWorker] = []
            with pool._lock:
                for cid in list(pool._workers.keys()):
                    w = pool._workers[cid]
                    if w.last_used < cutoff:
                        victims.append(w)
                        del pool._workers[cid]
            for w in victims:
                log.info("reaping idle omp worker ctx=%s", w.context_id)
                try:
                    w.stop()
                except Exception as e:
                    log.warning("reaper stop err: %s", e)
    threading.Thread(target=loop, daemon=True).start()


POOL = WorkerPool()
_start_reaper(POOL)


# ---------------- task store ----------------

class Task:
    def __init__(self, task_id: str, context_id: str, text: str):
        self.id = task_id
        self.context_id = context_id
        self.text = text
        self.state = "TASK_STATE_SUBMITTED"
        self.reply: Optional[str] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None


TASKS: dict[str, Task] = {}
TASKS_LOCK = threading.Lock()
# Keep at most 1000 tasks, drop oldest finished
def _gc_tasks() -> None:
    with TASKS_LOCK:
        if len(TASKS) <= 1000:
            return
        done = sorted(
            (t for t in TASKS.values() if t.finished_at),
            key=lambda t: t.finished_at or 0,
        )
        for t in done[: len(TASKS) - 1000]:
            TASKS.pop(t.id, None)


def _run_task(task: Task) -> None:
    task.state = "TASK_STATE_WORKING"
    try:
        worker = POOL.get(task.context_id)
        reply = worker.prompt(task.text)
        task.reply = reply
        task.state = "TASK_STATE_COMPLETED"
    except Exception as e:
        log.exception("task %s failed", task.id)
        task.error = str(e)
        task.state = "TASK_STATE_FAILED"
    finally:
        task.finished_at = time.time()
        _gc_tasks()


# ---------------- a2a helpers ----------------

def _extract_user_text(params: dict) -> tuple[str, str]:
    """Return (text, context_id). Raises ValueError on bad shape.

    Supports A2A v1.0 text Parts (member-presence: ``{"text": "...",
    "mediaType": "text/plain"}``) and v0.3 legacy (``{"kind": "text",
    "text": "..."}``).
    """
    msg = (params or {}).get("message") or {}
    ctx = msg.get("contextId") or msg.get("context_id") or ""
    parts = msg.get("parts") or []
    text_bits = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        # v1.0: member-presence discrimination — any part with a string
        # `text` member is a text part.
        txt = p.get("text")
        if isinstance(txt, str):
            text_bits.append(txt)
            continue
        # v0.3 legacy: kind == "text"
        if p.get("kind") == "text" and isinstance(p.get("text"), str):
            text_bits.append(p["text"])
    text = "\n".join(text_bits).strip()
    if not text:
        # fallback: some clients put raw text in `text`
        text = str(msg.get("text") or "").strip()
    if not text:
        raise ValueError("message has no text parts")
    return text, ctx


def _agent_card() -> dict:
    return {
        "protocolVersion": "1.0",
        "name": AGENT_NAME,
        "description": (
            "A2A bridge to `omp` (oh-my-pi coding agent). Each contextId maps to a "
            "long-lived omp RPC subprocess; multi-turn conversation within a context "
            "is preserved by reusing that subprocess. Capabilities: code, build, debug, "
            "general-purpose engineering tasks in " + OMP_CWD
        ),
        "url": f"http://{HOST}:{PORT}/",
        "version": "0.1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "supportedInterfaces": [
            {
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
                "url": f"http://{HOST}:{PORT}/",
            }
        ],
        "skills": [
            {
                "id": "code",
                "name": "code",
                "description": "Read, write, refactor and debug code in the configured workspace.",
                "tags": ["coding", "build", "debug"],
            },
            {
                "id": "build",
                "name": "build",
                "description": "Run builds, tests, linters and fix failures.",
                "tags": ["build", "test", "ci"],
            },
        ],
    }


def _jsonrpc_result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _jsonrpc_error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text_part(text: str) -> dict:
    """A2A v1.0 text Part (member-presence discriminated)."""
    return {"text": text, "mediaType": "text/plain"}


def _task_to_result(task: Task) -> dict:
    status: dict[str, Any] = {"state": task.state}
    if task.state == "TASK_STATE_COMPLETED" and task.reply is not None:
        status["message"] = {
            "kind": "message",
            "messageId": f"msg-{task.id}",
            "role": "ROLE_AGENT",
            "parts": [_text_part(task.reply)],
        }
    if task.state == "TASK_STATE_FAILED" and task.error:
        status["message"] = {
            "kind": "message",
            "messageId": f"msg-{task.id}",
            "role": "ROLE_AGENT",
            "parts": [_text_part(f"omp error: {task.error}")],
        }
    result: dict[str, Any] = {
        "kind": "task",
        "id": task.id,
        "contextId": task.context_id,
        "status": status,
    }
    if task.state == "TASK_STATE_COMPLETED" and task.reply is not None:
        result["artifacts"] = [
            {
                "artifactId": f"art-{task.id}",
                "name": "reply",
                "parts": [_text_part(task.reply)],
            }
        ]
    return result


# ---------------- http handler ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "omp-a2a-bridge/0.1"

    def log_message(self, fmt, *args):  # quieter
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _auth_ok(self) -> bool:
        if not TOKEN:
            return True
        h = self.headers.get("Authorization", "")
        return h == f"Bearer {TOKEN}"

    def do_GET(self) -> None:
        if self.path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            if not self._auth_ok():
                return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return self._send_json(HTTPStatus.OK, _agent_card())
        if self.path == "/healthz":
            return self._send_json(HTTPStatus.OK, {"ok": True, "workers": len(POOL._workers)})
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            return self._send_json(HTTPStatus.UNAUTHORIZED, _jsonrpc_error(None, -32001, "unauthorized"))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            req = json.loads(body)
        except Exception as e:
            return self._send_json(HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32700, f"parse error: {e}"))

        rid = req.get("id")
        method = req.get("method") or ""
        params = req.get("params") or {}

        try:
            if method in ("SendMessage", "message/send"):
                return self._handle_send(rid, params)
            if method in ("tasks/get", "TasksGet"):
                return self._handle_tasks_get(rid, params)
            if method in ("tasks/cancel", "TasksCancel"):
                return self._handle_tasks_cancel(rid, params)
            return self._send_json(HTTPStatus.OK, _jsonrpc_error(rid, -32601, f"method not found: {method}"))
        except ValueError as e:
            return self._send_json(HTTPStatus.OK, _jsonrpc_error(rid, -32602, str(e)))
        except Exception as e:
            log.exception("handler error")
            return self._send_json(HTTPStatus.OK, _jsonrpc_error(rid, -32603, f"internal: {e}"))

    def _handle_send(self, rid: Any, params: dict) -> None:
        text, ctx = _extract_user_text(params)
        if not ctx:
            ctx = uuid.uuid4().hex
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        task = Task(task_id, ctx, text)
        with TASKS_LOCK:
            TASKS[task_id] = task
        # Run synchronously so the caller gets the reply in this RPC response
        # (Hermes' a2a_call expects an immediate Task or Message result).
        _run_task(task)
        return self._send_json(HTTPStatus.OK, _jsonrpc_result(rid, _task_to_result(task)))

    def _handle_tasks_get(self, rid: Any, params: dict) -> None:
        tid = (params or {}).get("id") or (params or {}).get("taskId") or ""
        with TASKS_LOCK:
            t = TASKS.get(tid)
        if not t:
            return self._send_json(HTTPStatus.OK, _jsonrpc_error(rid, -32004, f"task not found: {tid}"))
        return self._send_json(HTTPStatus.OK, _jsonrpc_result(rid, _task_to_result(t)))

    def _handle_tasks_cancel(self, rid: Any, params: dict) -> None:
        # We run synchronously, so by the time cancel arrives it's already done.
        tid = (params or {}).get("id") or ""
        with TASKS_LOCK:
            t = TASKS.get(tid)
        if not t:
            return self._send_json(HTTPStatus.OK, _jsonrpc_error(rid, -32004, f"task not found: {tid}"))
        return self._send_json(HTTPStatus.OK, _jsonrpc_result(rid, _task_to_result(t)))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("omp-a2a-bridge listening on http://%s:%d  (cwd=%s, omp=%s)", HOST, PORT, OMP_CWD, OMP_BIN)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with POOL._lock:
            workers = list(POOL._workers.values())
            POOL._workers.clear()
        for w in workers:
            w.stop()


if __name__ == "__main__":
    main()
