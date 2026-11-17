"""Write the trace: `report.md` (verdicts with reasons, twin groups, the tree, what the trace could not see),
`edges.csv` (every transfer edge) and `tree.txt` (the walk). Everything goes under audit/ (git-ignored): it holds real
addresses. Pure apart from writing the three files.
"""

import csv
from datetime import UTC, datetime
from pathlib import Path

from app.tools.wallet_trace import links
from app.tools.wallet_trace.cache import inside_audit
from app.tools.wallet_trace.classify import classify
from app.tools.wallet_trace.model import Profile, TwinScore
from app.tools.wallet_trace.walk import WalkResult

EXPLORER = {
    "arbitrum": "https://arbiscan.io/address/{a}",
    "hyperevm": "https://hyperevmscan.io/address/{a}",
    "sol": "https://solscan.io/account/{a}",
    "hl": "https://app.hyperliquid.xyz/explorer/address/{a}",
}
OUT_OF_SCOPE = (
    "Not followed in v1 (PLAN §4.3): Arbitrum after a Hyperliquid `withdraw`, HyperEVM after a send to a system "
    "address, and exchange hot wallets. Open the explorer links below in a browser."
)


def _d(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d") if ts else "-"


def tree(res: WalkResult) -> str:
    kids: dict[str, list[str]] = {}
    roots = []
    for a, n in res.nodes.items():
        if n.parent:
            kids.setdefault(n.parent, []).append(a)
        else:
            roots.append(a)
    lines: list[str] = []

    def label(a: str) -> str:
        n = res.nodes[a]
        p = res.profiles.get(a)
        v = classify(p).label if p else ("not visited (budget)" if a in res.unvisited else res.errors.get(a, "error"))
        arrow = "" if not n.parent else (f"<- ${n.via_usd:,.0f} " if n.direction == "in" else f"-> ${n.via_usd:,.0f} ")
        return f"{arrow}{a}  [{v}]"

    def rec(a: str, prefix: str, last: bool, top: bool) -> None:
        lines.append(("" if top else prefix + ("└─ " if last else "├─ ")) + label(a))
        cs = kids.get(a, [])
        for i, c in enumerate(cs):
            rec(c, prefix + ("" if top else ("   " if last else "│  ")), i == len(cs) - 1, False)

    for r in roots:
        rec(r, "", True, True)
    return "\n".join(lines)


def _profile_block(p: Profile, chain: str) -> list[str]:
    v = classify(p)
    out = [f"### `{p.address}` — **{v.label}**{' (unsure)' if v.unsure else ''}", ""]
    out += [f"- {r}" for r in v.reasons]
    out.append(
        f"- active {_d(p.first)} .. {_d(p.last)}; deposits ${p.deposits_usd:,.0f}, withdraws ${p.withdraws_usd:,.0f}"
    )
    if p.exit_door_pairs or p.trading_perp_moves:
        out.append(
            f"- moves to perp: {p.exit_door_pairs} exit doors (withdraw soon after), {p.trading_perp_moves} for trading"
        )
    if p.airdrops:
        big = sorted(p.airdrops, key=lambda x: -x.amount)[:3]
        out.append("- airdrops: " + ", ".join(f"{x.amount:,.2f} {x.token} on {_d(x.time)}" for x in big)
                   + (f" (+{len(p.airdrops) - 3} more)" if len(p.airdrops) > 3 else ""))  # fmt: skip
    if p.master:
        out.append(f"- role {p.role}, master `{p.master}`")
    if p.subaccounts:
        out.append(f"- sub-accounts: {', '.join(p.subaccounts)}")
    if p.funder:
        out.append(f"- funder (first SOL in): `{p.funder}`")
    for where, usd in sorted(p.exits.items()):
        link = EXPLORER.get(where, "").format(a=p.address)
        out.append(f"- left the chain to {where}: ${usd:,.0f} — {link}")
    if p.unknown_types:
        out.append(f"- unknown ledger types (kept, not interpreted): {p.unknown_types}")
    out += [f"- note: {n}" for n in p.notes]
    out.append(f"- explorer: {EXPLORER[chain].format(a=p.address)}")
    return out + [""]


def markdown(res: WalkResult, scores: list[TwinScore], *, chain: str, args: str, calls: int, cache_hits: int) -> str:
    profiles = list(res.profiles.values())
    out = ["# Wallet trace", "", f"- run: `{args}`", f"- chain: {chain}; wallets profiled: {len(profiles)}; "
           f"API calls: {calls}; cache hits: {cache_hits}", ""]  # fmt: skip
    counts: dict[str, int] = {}
    for p in profiles:
        counts[classify(p).label] = counts.get(classify(p).label, 0) + 1
    out += ["## Verdicts", "", "| Verdict | Wallets |", "| --- | --- |"]
    out += [f"| {k} | {v} |" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    out += ["", f"LIKELY BOT: {counts.get('LIKELY BOT', 0)} wallet(s).", ""]
    grps = links.groups(scores)
    out += ["## Same owner, very likely", ""]
    if not grps:
        out.append("No group reached the twin score.")
    for g in grps:
        out.append(f"- group: {', '.join(f'`{a}`' for a in g)}")
        for t in scores:
            if t.a in g and t.b in g and links.is_twin(t):
                out.append(f"  - {links.short(t.a)} + {links.short(t.b)}: score {t.score}")
                out += [f"    - {c.kind} (+{c.points}): {c.evidence}" for c in t.clues]
    weak = [t for t in scores if not links.is_twin(t)][:10]
    if weak:
        out += ["", "Weaker links (not a group):"]
        out += [f"- {links.short(t.a)} + {links.short(t.b)}: score {t.score} "
                f"({', '.join(sorted({c.kind for c in t.clues}))})"
                for t in weak]  # fmt: skip
    out += ["", "## Walk", "", "```", tree(res), "```", ""]
    if res.unvisited:
        out += [f"**Call budget used up.** Not visited: {', '.join(f'`{a}`' for a in res.unvisited)}", ""]
    if res.stopped:
        out += ["Stopped at (not followed):", ""] + [f"- `{a}`: {why}" for a, why in sorted(res.stopped.items())] + [""]
    if res.errors:
        out += ["Errors:", ""] + [f"- `{a}`: {e}" for a, e in sorted(res.errors.items())] + [""]
    out += ["## Wallets", ""]
    for p in profiles:
        out += _profile_block(p, chain)
    out += ["## Out of scope", "", OUT_OF_SCOPE, ""]
    return "\n".join(out)


def write(
    out_dir: Path, res: WalkResult, scores: list[TwinScore], *, chain: str, args: str, calls: int, cache_hits: int
) -> Path:
    if not inside_audit(out_dir):
        raise ValueError(f"reports hold real addresses and must stay under audit/ (git-ignored); got {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(
        markdown(res, scores, chain=chain, args=args, calls=calls, cache_hits=cache_hits)
    )
    (out_dir / "tree.txt").write_text(tree(res) + "\n")
    with (out_dir / "edges.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["src", "dst", "token", "amount", "usd", "count", "first_utc", "last_utc"])
        for e in links.edges(list(res.profiles.values())):
            w.writerow([e.src, e.dst, e.token, f"{e.amount:.8g}", f"{e.usd:.2f}", e.count, _d(e.first), _d(e.last)])
    return out_dir / "report.md"
