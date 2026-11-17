"""Breadth-first walk over the transfer graph (PLAN §7).

From the start wallets, follow the top-N counterparties by USD in each direction, down to `depth` steps. Never visit:
system addresses (the money left the chain), vaults (protocol pools, not people), labelled wallets (exchanges, bot
apps; `labels.toml` in audit/), and wallets already seen. When the call budget is used up the walk stops and lists the
wallets it did not reach, so a short budget is visible, never silent.
"""

import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.data.http import DataError
from app.tools.wallet_trace.hl_source import OutOfCalls
from app.tools.wallet_trace.model import Profile

Fetch = Callable[[str], Awaitable[Profile]]


def load_labels(path: Path) -> dict[str, str]:
    """`labels.toml`: a `[labels]` table of address = "name". Missing file = no labels."""
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    return {
        str(k).lower() if str(k).startswith("0x") else str(k): str(v) for k, v in (data.get("labels") or {}).items()
    }


@dataclass
class Node:
    address: str
    depth: int
    parent: str = ""
    via_usd: float = 0.0
    direction: str = ""  # "in" = parent received from it, "out" = parent sent to it


@dataclass
class WalkResult:
    profiles: dict[str, Profile] = field(default_factory=dict)
    nodes: dict[str, Node] = field(default_factory=dict)
    stopped: dict[str, str] = field(default_factory=dict)  # address -> why it was not followed
    unvisited: list[str] = field(default_factory=list)  # reached but not fetched (budget)
    errors: dict[str, str] = field(default_factory=dict)


def _next_hops(p: Profile, top: int) -> list[tuple[str, float, str]]:
    ins = sorted(p.in_usd.items(), key=lambda kv: -kv[1])[:top]
    outs = sorted(p.out_usd.items(), key=lambda kv: -kv[1])[:top]
    return [(a, usd, "in") for a, usd in ins] + [(a, usd, "out") for a, usd in outs]


async def walk(
    starts: list[str],
    fetch: Fetch,
    *,
    depth: int,
    top: int,
    is_system: Callable[[str], bool],
    labels: dict[str, str] | None = None,
) -> WalkResult:
    labels = labels or {}
    res = WalkResult()
    queue: list[Node] = [Node(a, 0) for a in dict.fromkeys(starts)]
    vaults: set[str] = set()
    out_of_calls = False
    while queue:
        node = queue.pop(0)
        a = node.address
        if a in res.nodes:
            continue
        res.nodes[a] = node
        if out_of_calls:
            res.unvisited.append(a)
            continue
        try:
            p = await fetch(a)
        except OutOfCalls:
            out_of_calls = True
            res.unvisited.append(a)
            continue
        except DataError as exc:
            res.errors[a] = str(exc)[:200]
            continue
        res.profiles[a] = p
        vaults |= p.vault_addresses
        if node.depth >= depth:
            continue
        for cp, usd, direction in _next_hops(p, top):
            if cp in res.nodes or any(q.address == cp for q in queue):
                continue
            why = (
                "system address (left the chain)" if is_system(cp)
                else "vault (protocol pool)" if cp in vaults
                else f"label: {labels[cp]}" if cp in labels
                else ""
            )  # fmt: skip
            if why:
                res.stopped.setdefault(cp, why)
                continue
            queue.append(Node(cp, node.depth + 1, a, usd, direction))
    return res
