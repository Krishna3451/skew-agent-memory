#!/usr/bin/env python3
"""SKEW - run the agent-memory concurrency benchmark."""
from __future__ import annotations

import argparse, json, pathlib, sys, uuid

import psycopg

from skew.config import CRDB_URL
from skew.backends.impls import ALL
from skew.embed import get_encoder
from skew.harness import build_workload, run_backend, analyse


def ensure_schema(dsn: str) -> None:
    sql = (pathlib.Path(__file__).parent / "db" / "schema.sql").read_text()
    with psycopg.connect(dsn, connect_timeout=30, autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--reset", action="store_true", help="recreate tables")
    ap.add_argument("--json", type=str, default="")
    a = ap.parse_args()

    if not CRDB_URL:
        print("CRDB_URL not set (put it in .env)", file=sys.stderr)
        return 2

    if a.reset:
        print("recreating schema ...")
        ensure_schema(CRDB_URL)

    enc = get_encoder()
    wl = build_workload(enc, repeats=a.repeats)
    run_id = str(uuid.uuid4())

    print(f"\nSKEW  run {run_id}")
    print(f"encoder={enc.name}  ops={wl.total_ops}  concurrency={a.concurrency}"
          f"  distinct facts={wl.expected_keys}\n")

    rows = []
    for cls in ALL:
        stats = run_backend(cls, CRDB_URL, run_id, wl, a.concurrency)
        stats.update(analyse(CRDB_URL, run_id, cls.name, wl))
        rows.append(stats)
        print(f"  ran {stats['backend']:<15} "
              f"anomalies={stats['total_anomalies']:<4} retries={stats['retries']}")

    hdr = (f"\n{'backend':<16}{'isolation':<22}{'dupes':>7}{'contra':>8}"
           f"{'lost':>7}{'rows':>7}{'ANOM':>7}{'retry':>7}{'p95ms':>8}")
    print(hdr); print("-" * len(hdr.strip()) )
    for r in rows:
        print(f"{r['backend']:<16}{r['isolation']:<22}"
              f"{r['duplicates']:>7}{r['live_contradictions']:>8}"
              f"{r['lost_reinforcements']:>7}"
              f"{r['active_rows']}/{r['expected_active_rows']:<4}"
              f"{r['total_anomalies']:>7}{r['retries']:>7}{r['p95_ms']:>8}")
    print(f"\nexpected active rows = {wl.expected_keys} "
          f"(one per distinct subject+predicate)\n")

    payload = {"run_id": run_id, "encoder": enc.name, "ops": wl.total_ops,
               "concurrency": a.concurrency, "results": rows}
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(payload, indent=2))
        print(f"wrote {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
