"""Wallet tracer: every classify rule and the profile timing rules, tested AT their boundary values (rule 7)."""

import dataclasses

import pytest

from app.tools.wallet_trace import classify as C
from app.tools.wallet_trace import links
from app.tools.wallet_trace import profile as P
from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.hl_source import HlRaw, is_system
from app.tools.wallet_trace.model import Airdrop, Profile, Transfer

W = "0x" + "ab" * 19 + "01"
X = "0x" + "ab" * 19 + "02"
Y = "0x" + "ab" * 19 + "03"
META = P.SpotMeta({"tokens": [{"index": 0, "name": "USDC"}, {"index": 150, "name": "HYPE"}],
                   "universe": [{"name": "@107", "index": 107, "tokens": [150, 0]}]})  # fmt: skip


def prof(**kw: object) -> Profile:
    base = Profile(address=W, chain="hl", fills=50, orders=40, active_days=10, spot_fills=50, spot_volume=1000.0,
                   spot_buy_volume=500.0, spot_sell_volume=500.0, lifetime_days=100.0)  # fmt: skip
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def bot(**kw: object) -> Profile:
    p: dict[str, object] = dict(fills=6000, orders=300, active_days=30, perp_fills=6000, perp_volume=1e6)
    p.update(spot_volume=0.0, spot_buy_volume=0.0, spot_sell_volume=0.0, funding_events=50)
    p.update(kw)
    return prof(**p)


# --- rule 2: HOLDER -------------------------------------------------------------------------------------------------
def test_holder_is_zero_fills_and_one_fill_is_not() -> None:
    assert C.classify(prof(fills=0)).label == C.HOLDER
    assert C.classify(prof(fills=1)).label != C.HOLDER


# --- rule 3: PASS-THROUGH -------------------------------------------------------------------------------------------
def test_pass_through_share_boundary() -> None:
    assert C.classify(prof(fills=9, passthrough_share=S.PASSTHROUGH_SHARE)).label == C.PASS_THROUGH
    assert C.classify(prof(fills=9, passthrough_share=S.PASSTHROUGH_SHARE - 0.001)).label != C.PASS_THROUGH


def test_pass_through_fills_boundary() -> None:
    assert C.classify(prof(fills=S.PASSTHROUGH_MAX_FILLS - 1, passthrough_share=1.0)).label == C.PASS_THROUGH
    assert C.classify(prof(fills=S.PASSTHROUGH_MAX_FILLS, passthrough_share=1.0)).label != C.PASS_THROUGH


def _ledger_transfer(t_ms: int, amount: float, *, incoming: bool, usd: float) -> dict[str, object]:
    src, dst = (X, W) if incoming else (W, Y)
    return {"time": t_ms, "delta": {"type": "spotTransfer", "token": "HYPE", "amount": str(amount),
                                    "usdcValue": str(usd), "user": src, "destination": dst, "fee": "0"}}  # fmt: skip


def test_passthrough_window_is_inclusive_at_exactly_the_limit() -> None:
    t0 = 1_700_000_000_000
    edge = int(S.PASSTHROUGH_MINUTES * 60_000)
    at = HlRaw(W, ledger=[_ledger_transfer(t0, 100, incoming=True, usd=5000),
                          _ledger_transfer(t0 + edge, 95, incoming=False, usd=4750)])  # fmt: skip
    late = HlRaw(W, ledger=[_ledger_transfer(t0, 100, incoming=True, usd=5000),
                            _ledger_transfer(t0 + edge + 1000, 95, incoming=False, usd=4750)])  # fmt: skip
    assert P.build(at, META).passthrough_share == pytest.approx(0.95)
    assert P.build(late, META).passthrough_share == 0.0


# --- rule 4: CASH-OUT -----------------------------------------------------------------------------------------------
def test_cash_out_fast_exit_boundary() -> None:
    kw = dict(spot_sell_volume=600.0, spot_buy_volume=400.0, withdraws_usd=300.0)
    assert C.classify(prof(in_to_exit_days=S.CASH_OUT_MAX_LIFETIME_DAYS, **kw)).label == C.CASH_OUT
    assert C.classify(prof(in_to_exit_days=S.CASH_OUT_MAX_LIFETIME_DAYS + 0.01, **kw)).label != C.CASH_OUT


def test_cash_out_sell_share_boundary() -> None:
    kw = dict(withdraws_usd=900.0, in_to_exit_days=None)
    sells = S.CASH_OUT_SELL_SHARE * 1000
    assert C.classify(prof(spot_sell_volume=sells, spot_buy_volume=1000 - sells, **kw)).label == C.CASH_OUT
    assert C.classify(prof(spot_sell_volume=sells - 1, spot_buy_volume=1001 - sells, **kw)).label != C.CASH_OUT


def test_cash_out_withdraw_share_boundary() -> None:
    kw = dict(spot_sell_volume=1000.0, spot_buy_volume=0.0, in_to_exit_days=1.0)
    half = S.CASH_OUT_WITHDRAW_SHARE * 1000
    assert C.classify(prof(withdraws_usd=half, **kw)).label == C.CASH_OUT
    assert C.classify(prof(withdraws_usd=half - 0.01, **kw)).label != C.CASH_OUT


def test_cash_out_short_life_boundary() -> None:
    kw = dict(spot_sell_volume=600.0, spot_buy_volume=400.0, withdraws_usd=300.0, in_to_exit_days=None)
    assert C.classify(prof(lifetime_days=S.CASH_OUT_MAX_LIFETIME_DAYS, **kw)).label == C.CASH_OUT
    assert C.classify(prof(lifetime_days=S.CASH_OUT_MAX_LIFETIME_DAYS + 0.01, **kw)).label != C.CASH_OUT


# --- rule 5: ONE-SHOT BUYER ---------------------------------------------------------------------------------------
def _one_shot_raw(out_share: float, *, late_s: float = 0) -> HlRaw:
    t0 = 1_700_000_000_000
    fill = {"coin": "@107", "px": "10", "sz": "100", "side": "B", "time": t0 + 60_000, "oid": 1, "fee": "0",
            "feeToken": "HYPE", "closedPnl": "0", "tid": 1}  # fmt: skip
    ledger = [{"time": t0, "delta": {"type": "deposit", "usdc": "1000"}},
              {"time": t0 + 10_000, "delta": {"type": "accountClassTransfer", "usdc": "1000", "toPerp": False}},
              _ledger_transfer(t0 + 120_000 + int(late_s * 1000), 100 * out_share, incoming=False,
                               usd=900)]  # fmt: skip
    return HlRaw(W, ledger=ledger, fills=[fill])


def test_one_shot_out_share_boundary() -> None:
    assert P.build(_one_shot_raw(S.ONE_SHOT_OUT_SHARE), META).one_shot
    assert not P.build(_one_shot_raw(S.ONE_SHOT_OUT_SHARE - 0.01), META).one_shot


def test_one_shot_window_boundary() -> None:
    window = S.ONE_SHOT_WINDOW_HOURS * 3600 - 120  # the out transfer is 120 s after the deposit at late_s = 0
    assert P.build(_one_shot_raw(1.0, late_s=window), META).one_shot
    assert not P.build(_one_shot_raw(1.0, late_s=window + 1), META).one_shot


# --- rule 6: LIKELY BOT ---------------------------------------------------------------------------------------------
def test_bot_at_exactly_300_orders_and_30_days() -> None:
    assert C.classify(bot()).label == C.LIKELY_BOT
    assert C.classify(bot(orders=S.BOT_MIN_ORDERS - 1)).label != C.LIKELY_BOT
    assert C.classify(bot(active_days=S.BOT_MIN_ACTIVE_DAYS - 1, fills=5800)).label != C.LIKELY_BOT


def test_bot_fills_per_day_boundary() -> None:
    days = S.BOT_MIN_ACTIVE_DAYS
    assert C.classify(bot(fills=int(S.BOT_MIN_FILLS_PER_DAY * days))).label == C.LIKELY_BOT
    assert C.classify(bot(fills=int(S.BOT_MIN_FILLS_PER_DAY * days) - 1)).label != C.LIKELY_BOT


def test_bot_perp_share_boundary() -> None:
    half = dict(perp_volume=1000.0, spot_volume=1000.0, spot_sell_volume=500.0, spot_buy_volume=500.0)
    assert C.classify(bot(**half)).label == C.LIKELY_BOT
    assert C.classify(bot(perp_volume=999.0, spot_volume=1001.0, spot_sell_volume=500.0)).label != C.LIKELY_BOT


def test_solana_swap_bot_boundary() -> None:
    kw = dict(chain="sol", perp_volume=0.0, perp_fills=0, spot_volume=1e5, spot_buy_volume=5e4, spot_sell_volume=5e4)
    assert C.classify(bot(swaps=300, swap_tokens=20, **kw)).label == C.LIKELY_BOT
    assert C.classify(bot(swaps=299, swap_tokens=20, **kw)).label != C.LIKELY_BOT
    assert C.classify(bot(swaps=300, swap_tokens=19, **kw)).label != C.LIKELY_BOT


# --- rule 7: PERP TRADER --------------------------------------------------------------------------------------------
def test_perp_trader_needs_funding_on_hyperliquid() -> None:
    kw = dict(perp_volume=600.0, spot_volume=400.0, spot_sell_volume=200.0, spot_buy_volume=200.0)
    assert C.classify(prof(funding_events=1, **kw)).label == C.PERP_TRADER
    assert C.classify(prof(funding_events=0, **kw)).label != C.PERP_TRADER


def test_perp_trader_share_boundary() -> None:
    kw = dict(funding_events=5, spot_sell_volume=500.0, spot_buy_volume=500.0, spot_volume=1000.0)
    assert C.classify(prof(perp_volume=1000.0, **kw)).label == C.PERP_TRADER
    assert C.classify(prof(perp_volume=999.0, **kw)).label != C.PERP_TRADER


def test_jupiter_perp_trader_episode_boundary() -> None:
    kw = dict(chain="sol", perp_volume=1e4, spot_volume=0.0, spot_buy_volume=0.0, spot_sell_volume=0.0)
    assert C.classify(prof(jup_episodes=S.PERP_MIN_JUP_EPISODES, **kw)).label == C.PERP_TRADER
    assert C.classify(prof(jup_episodes=S.PERP_MIN_JUP_EPISODES - 1, **kw)).label != C.PERP_TRADER


# --- rule 8: SLOW SELLER --------------------------------------------------------------------------------------------
def _slow(**kw: object) -> Profile:
    p: dict[str, object] = dict(
        spot_sell_volume=800.0, spot_buy_volume=200.0, sell_orders=10, round_lot_sells=5, sell_days=5
    )
    p.update(kw)
    return prof(**p)


def test_slow_seller_boundaries() -> None:
    assert C.classify(_slow()).label == C.SLOW_SELLER
    assert C.classify(_slow(spot_sell_volume=799.0, spot_buy_volume=201.0)).label != C.SLOW_SELLER
    assert C.classify(_slow(round_lot_sells=4)).label != C.SLOW_SELLER
    assert C.classify(_slow(sell_days=S.SLOW_SELL_MIN_DAYS - 1)).label != C.SLOW_SELLER


def test_round_lot_sizes() -> None:
    assert [P._round_lot(x) for x in (100, 250, 300, 500, 1000)] == [True] * 5
    assert [P._round_lot(x) for x in (99.99, 50, 120, 1000.5)] == [False] * 4


# --- rule 1: INCOMPLETE ---------------------------------------------------------------------------------------------
def test_incomplete_wins_and_keeps_the_best_guess() -> None:
    v = C.classify(prof(fills=0, complete=False, balance_gaps={"HYPE": 5.0}))
    assert v.label == C.INCOMPLETE and v.unsure and "HOLDER" in v.line()


def test_partial_keeps_the_label_but_marks_it_unsure() -> None:
    v = C.classify(prof(fills=0, partial=True))
    assert v.label == C.HOLDER and v.unsure


# --- profile timing: the exit door ------------------------------------------------------------------------------
def _door(gap_ms: int, *, fill_first: bool = False) -> HlRaw:
    t0 = 1_700_000_000_000
    ledger = [{"time": t0, "delta": {"type": "accountClassTransfer", "usdc": "100", "toPerp": True}},
              {"time": t0 + gap_ms, "delta": {"type": "withdraw", "usdc": "99", "fee": "1"}}]  # fmt: skip
    fills = []
    if fill_first:
        fills = [{"coin": "BTC", "px": "1", "sz": "1", "side": "B", "time": t0 + 1000, "oid": 1, "fee": "0",
                  "feeToken": "USDC", "closedPnl": "0", "tid": 1}]  # fmt: skip
    return HlRaw(W, ledger=ledger, fills=fills)


def test_exit_door_at_exactly_the_limit() -> None:
    edge = int(S.EXIT_DOOR_MINUTES * 60_000)
    assert P.build(_door(edge), META).exit_door_pairs == 1
    assert P.build(_door(edge + 1), META).exit_door_pairs == 0


def test_perp_fill_before_the_withdraw_is_trading() -> None:
    p = P.build(_door(60_000, fill_first=True), META)
    assert (p.trading_perp_moves, p.exit_door_pairs) == (1, 0)


# --- profile: balance check and unknown types -------------------------------------------------------------------
def test_unknown_ledger_types_are_counted_not_dropped() -> None:
    raw = HlRaw(
        W, ledger=[{"time": 1, "delta": {"type": "brandNewThing"}}, {"time": 2, "delta": {"type": "brandNewThing"}}]
    )
    assert P.build(raw, META).unknown_types == {"brandNewThing": 2}


def test_a_missing_movement_makes_the_wallet_incomplete() -> None:
    raw = HlRaw(W, ledger=[_ledger_transfer(1_000, 100, incoming=True, usd=5000)],
                spot={"balances": [{"coin": "HYPE", "total": "60"}]})  # fmt: skip
    p = P.build(raw, META, {"@107": "40"})
    assert not p.complete and p.balance_gaps["HYPE"] == pytest.approx(-40)


def test_a_dust_gap_is_noted_not_failed() -> None:
    raw = HlRaw(W, ledger=[_ledger_transfer(1_000, 100, incoming=True, usd=5000)],
                spot={"balances": [{"coin": "HYPE", "total": "99.9"}]})  # fmt: skip
    p = P.build(raw, META, {"@107": "40"})  # 0.1 HYPE = $4 < BALANCE_USD_TOL
    assert p.complete and any("HYPE" in n for n in p.notes)


def test_delegator_history_fills_what_the_ledger_misses() -> None:
    """DEBUG D-8: a staking deposit seen only in delegatorHistory must still leave spot."""
    raw = HlRaw(W, ledger=[_ledger_transfer(1_000, 100, incoming=True, usd=5000)],
                staking=[{"time": 2_000, "delta": {"cDeposit": {"amount": "100"}}}],
                spot={"balances": []})  # fmt: skip
    assert P.build(raw, META).complete


def test_staking_from_both_sources_is_counted_once() -> None:
    raw = HlRaw(W, ledger=[_ledger_transfer(1_000, 100, incoming=True, usd=5000),
                           {"time": 2_000, "delta": {"type": "cStakingTransfer", "token": "HYPE", "amount": "100",
                                                     "isDeposit": True}}],
                staking=[{"time": 2_000, "delta": {"cDeposit": {"amount": "100"}}}], spot={"balances": []})  # fmt: skip
    assert P.build(raw, META).complete


# --- system addresses (docs 2026-11-17) ---------------------------------------------------------------------------
def test_system_addresses() -> None:
    assert is_system("0x" + "22" * 20)
    assert is_system("0x2000000000000000000000000000000000000000")
    assert is_system("0x20000000000000000000000000000000000000c8")  # token index 200
    assert not is_system("0x2100000000000000000000000000000000000000")
    assert not is_system(W)


def test_a_send_to_a_system_address_is_an_exit_not_a_counterparty() -> None:
    sysaddr = "0x2000000000000000000000000000000000000000"
    raw = HlRaw(W, ledger=[{"time": 1, "delta": {"type": "send", "token": "USDC", "amount": "10", "usdcValue": "10",
                                                  "user": W, "destination": sysaddr, "sourceDex": "spot",
                                                  "destinationDex": "spot"}}])  # fmt: skip
    p = P.build(raw, META)
    assert sysaddr not in p.out_usd and p.exits["hyperevm"] == 10


# --- twins --------------------------------------------------------------------------------------------------------
def test_twin_score_boundary_and_airdrop_cap() -> None:
    a = prof(airdrops=[Airdrop("HYPE", 1, 1000.0), Airdrop("JEFF", 1, 2000.0)])
    b = dataclasses.replace(prof(address=X), airdrops=list(a.airdrops))
    t = links.twin_score(a, b)
    assert t.score == S.TWIN_AIRDROP_MAX_POINTS and not links.is_twin(t)
    small = [Transfer(5000.0, Y, "HYPE", 0.0001, 0.003, "in", "send")]
    a2 = dataclasses.replace(a, transfers=small)
    b2 = dataclasses.replace(b, transfers=[Transfer(5000.0 + S.TWIN_SMALL_SENDER_SECONDS, Y, "HYPE", 0.0001, 0.003,
                                                    "in", "send")])  # fmt: skip
    t2 = links.twin_score(a2, b2)
    assert t2.score == S.TWIN_AIRDROP_MAX_POINTS + S.TWIN_SMALL_SENDER_POINTS
    b3 = dataclasses.replace(
        b2, transfers=[dataclasses.replace(b2.transfers[0], time=5001.0 + S.TWIN_SMALL_SENDER_SECONDS)]
    )
    assert links.twin_score(a2, b3).score == S.TWIN_AIRDROP_MAX_POINTS


def test_twin_needs_the_minimum_score() -> None:
    a = prof(funder=Y)
    b = prof(address=X, funder=Y, master=W)
    t = links.twin_score(a, b)
    assert t.score == S.TWIN_FUNDER_POINTS + S.TWIN_MASTER_POINTS and links.is_twin(t)
    assert not links.is_twin(links.twin_score(prof(funder=Y), prof(address=X, funder=Y)))


def test_twin_at_exactly_the_minimum_score() -> None:
    """Score 6 exactly (funder 3 + airdrops 3) is a twin; 5 (funder 3 + one small sender 2) is not."""
    assert S.TWIN_FUNDER_POINTS + S.TWIN_AIRDROP_MAX_POINTS == S.TWIN_MIN_SCORE
    drop = [Airdrop("HYPE", 1, 1000.0)]
    t = links.twin_score(prof(funder=Y, airdrops=drop), prof(address=X, funder=Y, airdrops=list(drop)))
    assert t.score == S.TWIN_MIN_SCORE and links.is_twin(t)
    small = [Transfer(5000.0, "0x" + "cd" * 20, "HYPE", 0.0001, 0.003, "in", "send")]
    t5 = links.twin_score(prof(funder=Y, transfers=small), prof(address=X, funder=Y, transfers=list(small)))
    assert t5.score == S.TWIN_MIN_SCORE - 1 and not links.is_twin(t5)


def test_airdrops_alone_never_make_a_twin_even_above_the_score() -> None:
    """Airdrops are capped, but even a score raised to the minimum by airdrop clues alone is not a twin."""
    t = links.TwinScore(W, X, [links.Clue(links.AIRDROP, S.TWIN_MIN_SCORE, "many airdrops")])
    assert t.score >= S.TWIN_MIN_SCORE and not links.is_twin(t)


def test_a_liquidation_gap_in_usdc_is_noted_not_incomplete() -> None:
    """A liquidation takes the margin left, with no row for it (a real wallet: -55.80 USDC, exactly its margin)."""
    t0 = 1_700_000_000_000
    fills = [{"coin": "JUP", "px": "1", "sz": "100", "side": "B", "time": t0 + 1000, "oid": 1, "fee": "0",
              "feeToken": "USDC", "closedPnl": "0", "tid": 1},
             {"coin": "JUP", "px": "0.5", "sz": "100", "side": "A", "time": t0 + 2000, "oid": 2, "fee": "0",
              "feeToken": "USDC", "closedPnl": "-50", "tid": 2, "liquidation": {"method": "market"}}]  # fmt: skip
    raw = HlRaw(W, ledger=[{"time": t0, "delta": {"type": "deposit", "usdc": "100"}}], fills=fills,
                spot={"balances": []}, perp={"marginSummary": {"accountValue": "0"}, "assetPositions": []})  # fmt: skip
    p = P.build(raw, META)
    assert p.complete and any("liquidated" in n for n in p.notes)
    raw.fills[1].pop("liquidation")
    assert not P.build(raw, META).complete
