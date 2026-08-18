import os
from pathlib import Path

def _load_env() -> None:
    for d in (Path(__file__).resolve().parents[2], Path.cwd()):
        f = d / ".env"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

CRDB_URL = os.environ.get("CRDB_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
EMBED_DIM = 1024
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
