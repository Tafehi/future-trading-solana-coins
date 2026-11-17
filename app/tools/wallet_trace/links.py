"""Transfer graph and twin scoring (PLAN §6). Pure.

Twin score between two wallets = points per clue, each clue printed with its evidence. A pair is shown as "same owner,
very likely" when the score reaches `TWIN_MIN_SCORE` AND at least one clue is not an airdrop: an airdrop at the same
second is shared by EVERY eligible wallet, so airdrops alone never make a group, however many there are. For the same
reason all shared airdrops together add at most `TWIN_AIRDROP_MAX_POINTS`: in the case, three unrelated early wallets
shared 3 to 49 airdrop seconds with each other.
"""

from collections import defaultdict
from datetime import UTC, datetime
from itertools import combinations

from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.model import Clue, Edge, Profile, TwinScore

AIRDROP = "same airdrop second"
SMALL_SENDER = "same small sender"
FUNDER = "same funder"
VAULT = "same vault, same week"
BOTH_WAYS = "direct transfers both ways"
MASTER = "sub-account / agent of the same master"


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S")


def edges(profiles: list[Profile]) -> list[Edge]:
    """Every transfer, summed per (src, dst, token). A transfer seen from both ends counts once."""
    acc: dict[tuple[str, str, str], list[float]] = {}
    seen: set[tuple[str, str, str, float, float]] = set()
    for p in profiles:
        for x in p.transfers:
            src, dst = (x.counterparty, p.address) if x.direction == "in" else (p.address, x.counterparty)
            key = (src, dst, x.token, round(x.time, 3), round(x.amount, 9))
            if key in seen:
                continue
            seen.add(key)
            row = acc.setdefault((src, dst, x.token), [0.0, 0.0, 0, x.time, x.time])
            row[0] += x.amount
            row[1] += x.usd
            row[2] += 1
            row[3] = min(row[3], x.time)
            row[4] = max(row[4], x.time)
    return sorted(
        (Edge(s, d, t, r[0], r[1], int(r[2]), r[3], r[4]) for (s, d, t), r in acc.items()), key=lambda e: -e.usd
    )


def twin_score(a: Profile, b: Profile) -> TwinScore:
    ts = TwinScore(a.address, b.address)
    drops_b = {(x.token, int(x.time)) for x in b.airdrops}
    shared = [x for x in sorted(a.airdrops, key=lambda d: d.time) if (x.token, int(x.time)) in drops_b]
    if shared:
        pts = min(S.TWIN_AIRDROP_POINTS * len(shared), S.TWIN_AIRDROP_MAX_POINTS)
        names = ", ".join(x.token for x in shared[:6]) + (" ..." if len(shared) > 6 else "")
        ts.clues.append(Clue(AIRDROP, pts, f"{len(shared)} airdrop(s) to both at the same second ({names}; first "
                                           f"{_t(shared[0].time)})"))  # fmt: skip
    small_a = [x for x in a.transfers if x.direction == "in" and x.usd <= S.TWIN_SMALL_SENDER_USD]
    small_b = [x for x in b.transfers if x.direction == "in" and x.usd <= S.TWIN_SMALL_SENDER_USD]
    senders: set[str] = set()
    for x in small_a:
        if x.counterparty in senders or x.counterparty in (a.address, b.address):
            continue
        match = next((y for y in small_b if y.counterparty == x.counterparty
                      and abs(y.time - x.time) <= S.TWIN_SMALL_SENDER_SECONDS), None)  # fmt: skip
        if match is not None:
            senders.add(x.counterparty)
            ts.clues.append(Clue(SMALL_SENDER, S.TWIN_SMALL_SENDER_POINTS,
                                 f"{short(x.counterparty)} sent {x.amount:g} {x.token} and "
                                 f"{match.amount:g} {match.token} "
                                 f"{abs(match.time - x.time):.0f} s apart"))  # fmt: skip
    if a.funder and a.funder == b.funder:
        ts.clues.append(Clue(FUNDER, S.TWIN_FUNDER_POINTS, f"both first paid by {short(a.funder)}"))
    vaults_b = dict(b.vaults)
    for vault, t in a.vaults:
        if vault in vaults_b and abs(vaults_b[vault] - t) <= S.TWIN_VAULT_DAYS * 86400:
            ts.clues.append(
                Clue(VAULT, S.TWIN_VAULT_POINTS, f"both joined {short(vault)} within {S.TWIN_VAULT_DAYS} days")
            )
    if b.address in a.out_usd and b.address in a.in_usd:
        ts.clues.append(Clue(BOTH_WAYS, S.TWIN_BOTH_WAYS_POINTS, "money moved both ways between them"))
    linked = (
        (a.master and a.master in (b.address, b.master))
        or (b.master and b.master == a.address)
        or b.address in a.subaccounts
        or a.address in b.subaccounts
    )
    if linked:
        ts.clues.append(
            Clue(MASTER, S.TWIN_MASTER_POINTS, "Hyperliquid says one is the master (or co-account) of the other")
        )
    return ts


def is_twin(ts: TwinScore) -> bool:
    return ts.score >= S.TWIN_MIN_SCORE and any(c.kind != AIRDROP for c in ts.clues)


def twins(profiles: list[Profile]) -> list[TwinScore]:
    """Every pair with a positive score, best first."""
    out = [twin_score(a, b) for a, b in combinations(profiles, 2)]
    return sorted((t for t in out if t.score > 0), key=lambda t: -t.score)


def groups(scores: list[TwinScore]) -> list[list[str]]:
    """Connected groups of twins (union-find over the pairs that pass `is_twin`)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for t in scores:
        if is_twin(t):
            parent[find(t.a)] = find(t.b)
    members: dict[str, list[str]] = defaultdict(list)
    for x in list(parent):
        members[find(x)].append(x)
    return sorted((sorted(m) for m in members.values() if len(m) > 1), key=lambda m: (-len(m), m))
