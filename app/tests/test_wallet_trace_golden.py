"""Wallet tracer, golden test: the 2026-11-17 case study (PLAN §2.2) from anonymised fixtures.

Every verdict of PLAN §2.2, both twins, the HYPE balance sum of the funder (0.9156), and no LIKELY BOT.
"""

from functools import cache

import pytest

from app.tests.conftest import fixture
from app.tools.wallet_trace import classify as C
from app.tools.wallet_trace import links
from app.tools.wallet_trace import profile as P
from app.tools.wallet_trace.hl_source import raw_from_answers
from app.tools.wallet_trace.model import Profile

EXPECTED = {
    "holder_staker": C.HOLDER,
    "holder_xmr1_a": C.HOLDER,
    "holder_xmr1_b": C.HOLDER,
    "holder_xmr1_c": C.HOLDER,
    "holder_collect_stake": C.HOLDER,
    "funder_b": C.SPOT_TRADER,
    "twin_of_b": C.SPOT_TRADER,
    "cash_out_one_day": C.CASH_OUT,
    "pass_through": C.PASS_THROUGH,
    "end_holder": C.HOLDER,
    "slow_seller": C.SLOW_SELLER,
    "jeff_seller": C.CASH_OUT,
    "one_shot_a": C.ONE_SHOT,
    "one_shot_b": C.ONE_SHOT,
    "one_shot_c": C.ONE_SHOT,
}


@cache
def case() -> dict[str, Profile]:
    doc = fixture("wallet_trace_hl_case.json")
    meta = P.SpotMeta(doc["spotMeta"])
    out = {}
    for addr, answers in doc["wallets"].items():
        out[doc["roles"][addr]] = P.build(raw_from_answers(addr, answers), meta, doc["allMids"])
    return out


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_golden_verdicts(role: str) -> None:
    v = C.classify(case()[role])
    assert v.label == EXPECTED[role], v.line()
    assert v.reasons and not v.unsure


def test_no_likely_bot_in_the_case() -> None:
    assert all(C.classify(p).label != C.LIKELY_BOT for p in case().values())


def test_every_case_wallet_adds_up() -> None:
    """The history of every case wallet sums to its live balance: nothing missing, so nothing INCOMPLETE."""
    bad = {r: p.balance_gaps for r, p in case().items() if not p.complete}
    assert bad == {}


def test_funder_hype_balance_sum_is_exact() -> None:
    """PLAN §2.2: airdrop + bought - sold - fees - sent - staked + unstaked - sent = 0.9156 (API: 0.91567)."""
    p = case()["funder_b"]
    assert p.balance_sum["HYPE"] == pytest.approx(0.91567001, abs=1e-6)
    assert p.balance_live["HYPE"] == pytest.approx(0.91567001, abs=1e-8)


def test_funder_reasons_match_the_case() -> None:
    """1,077 fills, all spot; 157 orders on 58 days; perp volume $0; no funding."""
    p = case()["funder_b"]
    assert (p.fills, p.spot_fills, p.perp_fills, p.orders, p.active_days, p.funding_events) == (
        1077,
        1077,
        0,
        157,
        58,
        0,
    )
    assert "157 orders on 58 days" in C.classify(p).line()


def test_twins_are_one_group_and_nobody_else() -> None:
    ps = case()
    scores = links.twins(list(ps.values()))
    groups = links.groups(scores)
    assert groups == [sorted([ps["funder_b"].address, ps["twin_of_b"].address])]
    pair = next(t for t in scores if {t.a, t.b} == {ps["funder_b"].address, ps["twin_of_b"].address})
    kinds = [c.kind for c in pair.clues]
    assert kinds.count(links.SMALL_SENDER) == 2 and links.AIRDROP in kinds


def test_shared_airdrops_alone_never_make_a_group() -> None:
    """The second funder shares many airdrop seconds with the funder (every early wallet does), and nothing else."""
    ps = case()
    t = links.twin_score(ps["second_funder"], ps["funder_b"])
    assert {c.kind for c in t.clues} == {links.AIRDROP}
    assert not links.is_twin(t)


def test_perp_moves_followed_by_withdraw_are_exits_not_trading() -> None:
    """On Hyperliquid you can only withdraw from the perp side: toPerp -> withdraw is the exit door (PLAN §2.1)."""
    p = case()["funder_b"]
    assert p.exit_door_pairs > 0 and p.trading_perp_moves == 0


def test_holders_stake_fast() -> None:
    ps = case()
    for role in ("end_holder", "holder_collect_stake"):
        lag = ps[role].stake_lag_minutes
        assert lag is not None and lag <= 5


def test_pass_through_sends_on_within_minutes() -> None:
    p = case()["pass_through"]
    assert p.passthrough_share >= 0.99
    assert p.passthrough_lag_minutes is not None and p.passthrough_lag_minutes <= 3


def test_hlp_is_marked_as_protocol() -> None:
    p = case()["funder_b"]
    assert any("HLP" in n for n in p.notes) and p.vault_addresses


def test_twap_slices_count_as_fills() -> None:
    """A one-shot buyer bought only with a TWAP: its 33 slices are not in userFillsByTime (DEBUG D-9)."""
    p = case()["one_shot_c"]
    assert p.fills == 33 and p.complete
