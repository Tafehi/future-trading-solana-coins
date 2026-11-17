"""Wallet tracer, Solana: profiles from anonymised mainnet transactions (2026-11-17), the swap and transfer decoding,
the funder, Jupiter Perps episodes, and the URL never reaching an error message."""

from functools import cache
from typing import Any

import httpx
import pytest

from app.data import jupiter_events as je
from app.data.http import DataError
from app.tests.conftest import fixture
from app.tools.wallet_trace import cache as cache_mod
from app.tools.wallet_trace import classify as C
from app.tools.wallet_trace import sol_profile
from app.tools.wallet_trace.hl_source import CallBudget
from app.tools.wallet_trace.sol_source import QUOTES, VENUES, SolRaw, SolSource

W = "fixture-key-001"
CP = "fixture-key-002"
MINT = "fixture-key-003"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL = next(m for m, n in QUOTES.items() if n == "SOL")


def raw_of(doc: dict[str, Any], addr: str) -> SolRaw:
    w = doc["wallets"][addr]
    events = [je.CloseEvent(**e) for e in w["perp_events"]]
    sigs = [{"signature": f"s{i}", "err": None} for i in range(w["n_signatures"])]
    return SolRaw(address=addr, signatures=sigs, history_end=w["history_end"], txs=w["txs"],
                  balance_lamports=(w["balance"] or {}).get("value"), perp_events=events,
                  perp_positions=w["perp_positions"], sol_usd=doc["sol_usd"])  # fmt: skip


@cache
def case() -> dict[str, Any]:
    doc = fixture("wallet_trace_sol_case.json")
    return {role: sol_profile.build(raw_of(doc, a)) for a, role in doc["roles"].items()}


def test_jupiter_trader_is_a_perp_trader() -> None:
    """PLAN phase 4: a live Jupiter Perps trader (found from recent closes, 2026-11-17) gets PERP TRADER."""
    p = case()["jupiter_trader"]
    v = C.classify(p)
    assert v.label == C.PERP_TRADER, v.line()
    assert p.jup_episodes >= 5 and p.perp_volume > 0 and p.perp_share == 1.0


def test_pumpfun_trader_gets_a_verdict_with_reasons() -> None:
    """PLAN phase 5: a live pump.fun trader gets a spot verdict with its swaps counted (partial history: unsure)."""
    p = case()["pumpfun_trader"]
    v = C.classify(p)
    assert v.label == C.SPOT_TRADER and v.unsure and v.reasons
    assert p.swaps > 0 and p.swap_tokens > 0 and p.spot_buy_volume > 0 and p.spot_sell_volume > 0


def test_funder_is_found_when_the_history_starts() -> None:
    assert case()["jupiter_trader"].funder.startswith("fixture-key-")


def _tx(t: int, *, programs: list[str], pre_tok: float, post_tok: float, sol_change: int, fee: int = 5000,
        extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "blockTime": t,
        "meta": {"err": None, "fee": fee, "preBalances": [10**10, 0], "postBalances": [10**10 + sol_change, 0],
                 "preTokenBalances": [{"accountIndex": 1, "owner": W, "mint": MINT,
                                       "uiTokenAmount": {"uiAmountString": str(pre_tok)}}],
                 "postTokenBalances": [{"accountIndex": 1, "owner": W, "mint": MINT,
                                        "uiTokenAmount": {"uiAmountString": str(post_tok)}}],
                 "innerInstructions": []},
        "transaction": {"message": {"accountKeys": [{"pubkey": W}, {"pubkey": "fixture-key-009"}],
                                    "instructions": [{"programId": p, "accounts": [], "data": ""} for p in programs]
                                    + (extra or [])}},
    }  # fmt: skip


def test_a_swap_is_read_from_the_wallets_own_balances() -> None:
    buy = _tx(1000, programs=[PUMP], pre_tok=0, post_tok=500, sol_change=-(10**9) - 5000)
    sell = _tx(2000, programs=[PUMP], pre_tok=500, post_tok=0, sol_change=2_330_525 - 5000)  # a real 0.0023 SOL sell
    p = sol_profile.build(SolRaw(address=W, signatures=[{"err": None}] * 2, history_end=True, txs=[buy, sell],
                                 sol_usd=100.0))  # fmt: skip
    assert p.swaps == 2 and p.swap_tokens == 1
    assert p.spot_buy_volume == pytest.approx(100.0)
    assert p.spot_sell_volume == pytest.approx(0.2330525)


def test_without_a_known_venue_it_is_not_a_swap() -> None:
    tx = _tx(1000, programs=["fixture-key-077"], pre_tok=0, post_tok=500, sol_change=-(10**9))
    p = sol_profile.build(SolRaw(address=W, signatures=[{"err": None}], history_end=True, txs=[tx], sol_usd=100.0))
    assert p.swaps == 0 and C.classify(p).label == C.HOLDER


def test_sol_transfers_and_the_funder() -> None:
    def transfer(t: int, src: str, dst: str, lam: int) -> dict[str, Any]:
        ix = {"program": "system", "parsed": {"type": "transfer", "info": {"source": src, "destination": dst,
                                                                            "lamports": lam}}}  # fmt: skip
        return _tx(t, programs=[], pre_tok=0, post_tok=0, sol_change=0, extra=[ix])

    txs = [transfer(1000, CP, W, 2 * 10**9), transfer(2000, "fixture-key-004", W, 10**9), transfer(3000, W, CP, 10**8)]
    p = sol_profile.build(SolRaw(address=W, signatures=[{"err": None}] * 3, history_end=True, txs=txs, sol_usd=50.0))
    assert p.funder == CP
    assert p.in_usd == {CP: 100.0, "fixture-key-004": 50.0} and p.out_usd == {CP: 5.0}
    cut = sol_profile.build(SolRaw(address=W, signatures=[{"err": None}] * 3, history_end=False, txs=txs, sol_usd=50.0))
    assert cut.funder == "" and cut.partial


def test_reading_fewer_transactions_than_exist_is_partial() -> None:
    p = sol_profile.build(SolRaw(address=W, signatures=[{"err": None}] * 5, history_end=True, txs=[], sol_usd=1.0))
    assert p.partial and C.classify(p).unsure
    full = sol_profile.build(SolRaw(address=W, signatures=[], history_end=True, txs=[], sol_usd=1.0))
    assert not full.partial


def test_venues_include_pump_and_raydium() -> None:
    assert {"pump.fun", "PumpSwap", "Raydium AMM v4", "Jupiter"} <= set(VENUES.values())


async def test_an_rpc_error_never_carries_the_url(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL can hold a key (Chainstack puts it in the path): an error must not repeat it."""
    monkeypatch.setattr(cache_mod, "AUDIT", tmp_path)
    secret_url = "https://node.example/" + "k" * 32

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}")

    src = SolSource(cache_mod.Cache(tmp_path / "c"), CallBudget(5), url=secret_url,
                    transport=httpx.MockTransport(boom), rps=1000)  # fmt: skip
    with pytest.raises(DataError) as err:
        await src.signatures(W)
    await src.close()
    assert "k" * 32 not in str(err.value) and "node.example" not in str(err.value)


def test_rpc_choice_flag_then_secret_then_public() -> None:
    """D-27: --rpc-url wins, then SOLANA_RPC_URL from the secret, then the free public RPC. The label hides the URL."""
    from app.tools.wallet_trace import __main__ as cli
    from app.tools.wallet_trace import secrets as sm
    from app.tools.wallet_trace import settings as st

    keyed = "https://node.example/" + "k" * 32
    with_secret = sm.Secrets(solana_rpc_url=keyed, source="secrets-manager:solana-future-bot")
    url, label, max_tx = cli.rpc_choice(cli.parse(["x", "--chain", "sol"]), with_secret)
    assert url == keyed and "k" * 32 not in label and max_tx == st.SOL_MAX_TX_PER_WALLET_PAID
    url, label, _ = cli.rpc_choice(
        cli.parse(["x", "--chain", "sol", "--rpc-url", "https://other.example/"]), with_secret
    )
    assert url == "https://other.example/" and label == "--rpc-url"
    url, label, max_tx = cli.rpc_choice(cli.parse(["x", "--chain", "sol"]), sm.Secrets())
    assert url == "https://api.mainnet-beta.solana.com" and max_tx == st.SOL_MAX_TX_PER_WALLET
