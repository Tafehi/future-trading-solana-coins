"""On-disk cache of raw API answers, so a re-run of the same trace costs 0 calls (PLAN §7).

Layout: `<root>/<chain>/<address>/<query>.json`, holding `{"fetched_at": <unix s>, "answer": <raw JSON>}`. Shared
answers (Hyperliquid `spotMeta`) go under the address `_global`. `refresh=True` ignores what is on disk (it still
writes the new answer). The root must be inside the repo's `audit/` folder (git-ignored): answers hold real addresses.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
AUDIT = REPO / "audit"
DEFAULT_ROOT = AUDIT / "wallet_trace" / "cache"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def inside_audit(path: Path) -> bool:
    """True if `path` is the repo's audit/ folder or below it (output with real addresses must stay git-ignored)."""
    try:
        path.resolve().relative_to(AUDIT.resolve())
    except ValueError:
        return False
    return True


class Cache:
    def __init__(self, root: Path = DEFAULT_ROOT, *, refresh: bool = False) -> None:
        if not inside_audit(root):
            raise ValueError(f"cache must live under {AUDIT} (it holds real addresses); got {root}")
        self.root = root
        self.refresh = refresh
        self.hits = 0

    def _path(self, chain: str, address: str, query: str) -> Path:
        return (
            self.root
            / _SAFE.sub("_", chain)
            / _SAFE.sub("_", address.lower() if address.startswith("0x") else address)
            / (_SAFE.sub("_", query) + ".json")
        )

    def get(self, chain: str, address: str, query: str) -> Any | None:
        if self.refresh:
            return None
        p = self._path(chain, address, query)
        if not p.exists():
            return None
        try:
            answer = json.loads(p.read_text())["answer"]
        except (ValueError, KeyError, OSError):
            return None  # a broken cache file is a miss, never a crash
        self.hits += 1
        return answer

    def put(self, chain: str, address: str, query: str, answer: Any) -> None:
        p = self._path(chain, address, query)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "answer": answer}))
        os.replace(tmp, p)
