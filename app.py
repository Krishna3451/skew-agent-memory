#!/usr/bin/env python3
"""SKEW web app - live concurrency benchmark for agent memory."""
from __future__ import annotations

import asyncio, json, pathlib, queue, threading, uuid

import psycopg
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

from skew.config import CRDB_URL
from skew.backends.impls import ALL
from skew.embed import get_encoder
from skew.harness import build_workload, run_backend, analyse
from skew.recall import recall, explain_recall, read_your_writes

app = FastAPI(title="SKEW")
ENC = get_encoder()
HERE = pathlib.Path(__file__).parent
LAST = {"run_id": None}


def ensure_schema() -> None:
    sql = (HERE / "db" / "schema.sql").read_text()
    with psycopg.connect(CRDB_URL, connect_timeout=30, autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql)


@app.get("/health")
def health():
    return {"ok": True, "encoder": ENC.name}


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "web" / "index.html").read_text()


@app.get("/api/run")
def run(concurrency: int = 24, repeats: int = 14, reset: bool = True):
    """Stream benchmark progress as SSE."""
    q: queue.Queue = queue.Queue()

    def emit(ev: str, data: dict):
        q.put(f"event: {ev}\ndata: {json.dumps(data)}\n\n")

    def work():
        try:
            if reset:
                emit("status", {"msg": "recreating schema"})
                ensure_schema()
            wl = build_workload(ENC, repeats=repeats)
            run_id = str(uuid.uuid4())
            LAST["run_id"] = run_id
            emit("start", {"run_id": run_id, "ops": wl.total_ops,
                           "concurrency": concurrency,
                           "expected_rows": wl.expected_keys,
                           "encoder": ENC.name})
            for cls in ALL:
                emit("backend_start", {"backend": cls.name,
                                       "isolation": cls.isolation or "autocommit (none)"})
                done = {"n": 0}

                def on_event(bname, widx, r):
                    done["n"] += 1
                    if done["n"] % 3 == 0 or done["n"] == wl.total_ops:
                        emit("progress", {"backend": bname, "done": done["n"],
                                          "total": wl.total_ops, "op": r.op,
                                          "retries": r.retries})

                stats = run_backend(cls, CRDB_URL, run_id, wl, concurrency, on_event)
                stats.update(analyse(CRDB_URL, run_id, cls.name, wl))
                emit("backend_done", stats)
                emit("rows", {"backend": cls.name,
                              "rows": _active_rows(run_id, cls.name)})
            emit("ryw", read_your_writes(CRDB_URL, run_id, ENC))
            emit("done", {"run_id": run_id})
        except Exception as e:
            emit("error", {"msg": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    async def gen():
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            yield item

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _active_rows(run_id: str, backend: str):
    with psycopg.connect(CRDB_URL, connect_timeout=30, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            """SELECT subject, predicate, object, observation_count, content
                 FROM memories
                WHERE run_id=%s AND backend=%s AND status='active'
                ORDER BY subject, predicate, object""",
            (run_id, backend))
        return [{"subject": r[0], "predicate": r[1], "object": r[2],
                 "observations": r[3], "content": r[4]} for r in cur.fetchall()]


@app.get("/api/recall")
def api_recall(q: str, backend: str = "serializable"):
    rid = LAST["run_id"]
    if not rid:
        return JSONResponse({"error": "run the benchmark first"}, status_code=400)
    return {"query": q, "backend": backend,
            "hits": recall(CRDB_URL, rid, backend, q, ENC),
            "plan": explain_recall(CRDB_URL, rid, backend, q, ENC)}


if __name__ == "__main__":
    import uvicorn, os
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
