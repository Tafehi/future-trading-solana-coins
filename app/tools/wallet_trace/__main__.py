"""CLI: python -m app.tools.wallet_trace <addr> [<addr> ...] [--chain hl|sol] [--depth 2] [--top 5] [--max-calls 500]
            [--refresh] [--rpc-url URL] [--sol-usd PRICE] [--out audit/wallet_trace] [--no-telegram] [--no-aws]

Read-only research tool for the laptop (PLAN.md). Hyperliquid: the public info API, no key. Solana: the Chainstack URL
from the secret `solana-future-bot` (user, 2026-11-17, D-27), else the free public RPC; `--rpc-url` overrides both. No
URL is ever printed. Writes report.md, edges.csv and tree.txt to `<out>/<UTC time>/` under audit/ (git-ignored: real
addresses), then sends a short summary to Telegram (token and chat id from the same secret).
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.data.http import DataError
from app.tools.wallet_trace import hl_source, links, report, sol_profile, sol_source, telegram
from app.tools.wallet_trace import profile as hl_profile
from app.tools.wallet_trace import secrets as secrets_mod
from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.cache import AUDIT, Cache, inside_audit
from app.tools.wallet_trace.classify import classify
from app.tools.wallet_trace.model import Profile
from app.tools.wallet_trace.walk import load_labels, walk

DEFAULT_OUT = AUDIT / "wallet_trace"
LABELS = AUDIT / "wallet_trace" / "labels.toml"


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m app.tools.wallet_trace", description=__doc__.split("\n\n")[1])
    ap.add_argument("addrs", nargs="+")
    ap.add_argument("--chain", choices=("hl", "sol"), default="hl")
    ap.add_argument("--depth", type=int, default=S.DEFAULT_DEPTH)
    ap.add_argument("--top", type=int, default=S.DEFAULT_TOP, help="counterparties followed per direction (by USD)")
    ap.add_argument("--max-calls", type=int, default=S.DEFAULT_MAX_CALLS)
    ap.add_argument("--refresh", action="store_true", help="ignore the cache (answers are still saved)")
    ap.add_argument("--rpc-url", default=None, help="Solana RPC (default: SOLANA_RPC_URL from the secret, else public)")
    ap.add_argument("--sol-usd", type=float, default=None, help="SOL price for USD values (default: Hyperliquid's mid)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-telegram", action="store_true", help="do not send the Telegram summary")
    ap.add_argument("--no-aws", action="store_true", help="do not read AWS Secrets Manager (env vars still count)")
    ap.add_argument("--secret", default=secrets_mod.DEFAULT_SECRET, help="Secrets Manager secret name")
    a = ap.parse_args(argv)
    if not inside_audit(a.out):
        ap.error(f"--out must be under {AUDIT} (reports hold real addresses and audit/ is git-ignored)")
    return a


def rpc_choice(a: argparse.Namespace, sec: secrets_mod.Secrets) -> tuple[str, str, int]:
    """(url, label for the screen, transactions per wallet). The label never contains the URL."""
    if a.rpc_url:
        return a.rpc_url, "--rpc-url", S.SOL_MAX_TX_PER_WALLET_PAID
    if sec.solana_rpc_url:
        return sec.solana_rpc_url, f"SOLANA_RPC_URL from {sec.source}", S.SOL_MAX_TX_PER_WALLET_PAID
    return S.SOL_DEFAULT_RPC, "free public RPC", S.SOL_MAX_TX_PER_WALLET


async def _sol_price(src: sol_source.SolSource, given: float | None) -> float:
    if given is not None:
        return given
    try:
        return await src.sol_usd()
    except DataError:
        print("could not read a SOL price; pass --sol-usd. USD values of SOL moves are 0.", file=sys.stderr)
        return 0.0


async def run(
    a: argparse.Namespace,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sec: secrets_mod.Secrets | None = None,
) -> Path:
    if sec is None:
        need_aws = not a.no_aws and (not a.no_telegram or (a.chain == "sol" and not a.rpc_url))
        sec = secrets_mod.load(name=a.secret, use_aws=need_aws)
    if sec.problem:
        print(sec.problem, file=sys.stderr)
    cache = Cache(a.out / "cache", refresh=a.refresh)
    budget = hl_source.CallBudget(a.max_calls)
    labels = load_labels(LABELS)
    if a.chain == "hl":
        hl = hl_source.HlSource(cache, budget, transport=transport)
        try:
            meta = hl_profile.SpotMeta(await hl.spot_meta())
            mids = await hl.mids()

            async def fetch(addr: str) -> Profile:
                return hl_profile.build(await hl.fetch(addr), meta, mids)

            res = await walk([x.lower() for x in a.addrs], fetch, depth=a.depth, top=a.top,
                             is_system=hl_source.is_system, labels=labels)  # fmt: skip
        finally:
            await hl.close()
    else:
        url, label, max_tx = rpc_choice(a, sec)
        print(f"Solana RPC: {label}", file=sys.stderr)
        rps = S.SOL_CALLS_PER_SECOND_PAID if url != S.SOL_DEFAULT_RPC else S.SOL_CALLS_PER_SECOND
        sol = sol_source.SolSource(cache, budget, url=url, transport=transport, rps=rps, max_tx=max_tx)
        try:
            price = await _sol_price(sol, a.sol_usd)

            async def fetch(addr: str) -> Profile:
                return sol_profile.build(await sol.fetch(addr, sol_usd=price))

            res = await walk(
                list(a.addrs), fetch, depth=a.depth, top=a.top, is_system=sol_source.is_system, labels=labels
            )
        finally:
            await sol.close()
    scores = links.twins(list(res.profiles.values()))
    out_dir = a.out / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args = " ".join(["--chain", a.chain, "--depth", str(a.depth), "--top", str(a.top)] + [f"{x[:6]}…" for x in a.addrs])
    path = report.write(out_dir, res, scores, chain=a.chain, args=args, calls=budget.used, cache_hits=cache.hits)
    for p in res.profiles.values():
        print(f"{p.address}  {classify(p).line()[:160]}")
    print(f"\n{len(res.profiles)} wallets, {budget.used} API calls, {cache.hits} cache hits. Report: {path}")
    if res.unvisited:
        print(f"Call budget used up: {len(res.unvisited)} wallet(s) not visited (listed in the report).")
    if not a.no_telegram:
        if sec.telegram:
            text = telegram.summary(res, scores, chain=a.chain, starts=list(a.addrs), calls=budget.used,
                                    cache_hits=cache.hits, report=str(path))  # fmt: skip
            problem = await telegram.send(sec.telegram_token, sec.telegram_chat_id, text, transport=transport)
            print(f"Telegram: {problem or 'summary sent'}")
        else:
            print("Telegram: skipped (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in the secret or env)")
    return path


def main(argv: list[str] | None = None) -> None:
    asyncio.run(run(parse(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
