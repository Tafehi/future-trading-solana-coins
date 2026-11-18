"""Who is tracked (copied with money) and who is watched (shadow-copied only). Pure; one rule for both sources.

Inputs per wallet: its grade (tier, grade score) and its shadow record (closed shadow copies in SHADOW_WINDOW_DAYS,
their mean net return in price %). Rules (user, 2026-11-13, items 4 and 5):

1. Eligible: GOLD, and SILVER when COPY_SILVER.
2. Excluded from tracking: a wallet with SHADOW_MIN_TRADES or more shadow copies whose mean is at or below
   SHADOW_MIN_MEAN_RETURN_PCT — copying it loses, whatever its own record says. It stays WATCHED, so it can earn its
   way back as the window rolls.
3. Order: PROVEN wallets first (enough shadow copies, mean above the line), best mean first; then unproven GOLD by
   grade score; then unproven SILVER by grade score.
4. The first `max_total` are tracked, with at most `max_silver` SILVER among them. Everything else eligible is
   watched (the caller caps the watch list where watching costs something).
"""

from dataclasses import dataclass

from app.config import settings

GOLD = "GOLD"
SILVER = "SILVER"


@dataclass(frozen=True)
class Candidate:
    address: str
    tier: str
    grade_score: float
    shadow_n: int = 0
    shadow_mean: float | None = None


def eligible_tiers() -> tuple[str, ...]:
    return (GOLD, SILVER) if settings.copy_silver else (GOLD,)


def proven(c: Candidate) -> bool:
    return (
        c.shadow_n >= settings.shadow_min_trades
        and c.shadow_mean is not None
        and c.shadow_mean > settings.shadow_min_mean_return_pct
    )


def failed(c: Candidate) -> bool:
    return (
        c.shadow_n >= settings.shadow_min_trades
        and c.shadow_mean is not None
        and c.shadow_mean <= settings.shadow_min_mean_return_pct
    )


def _key(c: Candidate) -> tuple[int, float, int, float, str]:
    is_proven = proven(c)
    return (
        1 if is_proven else 0,
        (c.shadow_mean or 0.0) if is_proven else 0.0,
        1 if c.tier == GOLD else 0,
        c.grade_score,
        c.address,
    )


def pick(cands: list[Candidate], *, max_total: int, max_silver: int) -> tuple[list[str], list[str]]:
    """(tracked, watched) addresses, each in rank order."""
    tiers = eligible_tiers()
    pool = [c for c in cands if c.tier in tiers]
    use_shadow = settings.shadow_enabled
    ranked = sorted(pool, key=_key if use_shadow else (lambda c: (0, 0.0, c.tier == GOLD, c.grade_score, c.address)),
                    reverse=True)  # fmt: skip
    tracked: list[str] = []
    silver = 0
    for c in ranked:
        if len(tracked) >= max_total:
            break
        if use_shadow and failed(c):
            continue
        if c.tier == SILVER:
            if silver >= max_silver:
                continue
            silver += 1
        tracked.append(c.address)
    chosen = set(tracked)
    watched = [c.address for c in ranked if c.address not in chosen] if use_shadow else []
    return tracked, watched
