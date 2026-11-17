"""Raw Solana answers -> Profile (PLAN §4.2, §5). Pure.

- **Transfers:** parsed `system` transfers (SOL) and `spl-token` transfer / transferChecked (the token owner comes from
  the pre/post token balances: SPL instructions name token ACCOUNTS, not wallets).
- **Swaps (spot):** a transaction that calls a known venue (pump.fun, PumpSwap, Raydium, Jupiter) is a swap. Its sides
  come from the wallet's own balance change in that transaction: token balances (pre/post, owner = wallet) and the SOL
  balance, minus the network fee when the wallet paid it (moves below `SOL_DUST_LAMPORTS` ignored). A real pump.fun
  sell on 2026-11-17 moved only 0.0023 SOL, so the threshold must stay tiny. Tokens up = buy, down = sell. Valued in USD
  with the SOL price given by the caller (USDC/USDT at 1).
- **Funder:** the sender of the oldest incoming SOL transfer, only when the history reached the wallet's first
  transaction (otherwise unknown and noted).
- **Futures:** Jupiter Perps close events -> episodes (`data/jupiter_events.episodes`, the bot's own decoder).
- **partial:** Solana has no full-history balance replay here (only the newest transactions are read), so a wallet is
  `partial` (label kept, marked unsure) when its signatures were cut or not every signature was decoded. It is never
  `INCOMPLETE`: that verdict is kept for a history that contradicts the live balance (Hyperliquid).
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.data import jupiter_events as je
from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.model import Profile, Transfer
from app.tools.wallet_trace.sol_source import QUOTES, VENUES, SolRaw

LAMPORTS = 1_000_000_000


def _keys(tx: dict[str, Any]) -> list[str]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    out = []
    for k in msg.get("accountKeys") or []:
        out.append(str(k.get("pubkey")) if isinstance(k, dict) else str(k))
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return out + [str(x) for x in (loaded.get("writable") or []) + (loaded.get("readonly") or [])]


def _instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    out = list(msg.get("instructions") or [])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        out += list(group.get("instructions") or [])
    return [ix for ix in out if isinstance(ix, dict)]


def _programs(tx: dict[str, Any]) -> set[str]:
    return {str(ix.get("programId") or "") for ix in _instructions(tx)}


def _token_owner(tx: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Token account address -> (owner wallet, mint), from the pre/post token balances."""
    keys = _keys(tx)
    meta = tx.get("meta") or {}
    out: dict[str, tuple[str, str]] = {}
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        i = b.get("accountIndex")
        if isinstance(i, int) and i < len(keys):
            out[keys[i]] = (str(b.get("owner") or ""), str(b.get("mint") or ""))
    return out


def token_deltas(tx: dict[str, Any], wallet: str) -> dict[str, float]:
    """mint -> change of the wallet's own token balance in this transaction."""
    meta = tx.get("meta") or {}
    out: defaultdict[str, float] = defaultdict(float)
    for sign, rows in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in rows:
            if b.get("owner") == wallet:
                amt = (b.get("uiTokenAmount") or {}).get("uiAmountString") or "0"
                out[str(b.get("mint"))] += sign * float(amt)
    return {k: v for k, v in out.items() if abs(v) > 0}


def sol_delta(tx: dict[str, Any], wallet: str) -> float:
    """Lamports change of the wallet's own SOL account (the fee included when it paid)."""
    keys = _keys(tx)
    if wallet not in keys:
        return 0.0
    i = keys.index(wallet)
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    return float(post[i] - pre[i]) if i < len(pre) and i < len(post) else 0.0


def build(raw: SolRaw) -> Profile:
    w = raw.address
    p = Profile(address=w, chain="sol")
    usd_of = {"SOL": raw.sol_usd, "USDC": 1.0, "USDT": 1.0}
    times: list[float] = []
    transfers: list[Transfer] = []
    days: set[str] = set()
    sell_days: set[str] = set()
    swap_tokens: set[str] = set()
    for tx in sorted(raw.txs, key=lambda t: int(t.get("blockTime") or 0)):
        if (tx.get("meta") or {}).get("err") is not None:
            continue
        t = float(tx.get("blockTime") or 0)
        times.append(t)
        owners = _token_owner(tx)
        venues = {VENUES[x] for x in _programs(tx) if x in VENUES}
        if venues:
            moves = token_deltas(tx, w)
            lam = sol_delta(tx, w)
            if _keys(tx)[:1] == [w]:  # the fee payer: the network fee is not part of the trade
                lam += float((tx.get("meta") or {}).get("fee") or 0)
            quote_usd = abs(lam) / LAMPORTS * raw.sol_usd if abs(lam) >= S.SOL_DUST_LAMPORTS else 0.0
            for mint in [m for m in moves if m in QUOTES]:
                quote_usd += abs(moves.pop(mint)) * usd_of[QUOTES[mint]] if QUOTES[mint] != "SOL" else 0.0
            base = [(m, d) for m, d in moves.items() if m not in QUOTES]
            if base and quote_usd > 0:
                mint, d = max(base, key=lambda md: abs(md[1]))
                p.swaps += 1
                p.fills += 1
                p.spot_fills += 1
                swap_tokens.add(mint)
                day = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")
                days.add(day)
                p.spot_volume += quote_usd
                if d > 0:
                    p.spot_buy_volume += quote_usd
                else:
                    p.spot_sell_volume += quote_usd
                    p.sell_orders += 1
                    sell_days.add(day)
                continue  # a swap's own token moves are not wallet-to-wallet transfers
        for ix in _instructions(tx):
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            kind = str(parsed.get("type"))
            if ix.get("program") == "system" and kind in ("transfer", "transferWithSeed"):
                src, dst, lam = str(info.get("source")), str(info.get("destination")), float(info.get("lamports") or 0)
                amt = lam / LAMPORTS
                if dst == w and src != w:
                    transfers.append(Transfer(t, src, "SOL", amt, amt * raw.sol_usd, "in", "sol"))
                elif src == w and dst != w:
                    transfers.append(Transfer(t, dst, "SOL", amt, amt * raw.sol_usd, "out", "sol"))
            elif ix.get("program") == "spl-token" and kind in ("transfer", "transferChecked"):
                src_owner, mint = owners.get(str(info.get("source")), ("", str(info.get("mint") or "")))
                dst_owner, mint2 = owners.get(str(info.get("destination")), ("", mint))
                mint = mint or mint2
                tok = info.get("tokenAmount") or {}
                amt = float(tok.get("uiAmountString") or info.get("amount") or 0) if tok else 0.0
                usd = amt * usd_of[QUOTES[mint]] if mint in QUOTES else 0.0
                name = QUOTES.get(mint, mint)
                if dst_owner == w and src_owner and src_owner != w:
                    transfers.append(Transfer(t, src_owner, name, amt, usd, "in", "spl"))
                elif src_owner == w and dst_owner and dst_owner != w:
                    transfers.append(Transfer(t, dst_owner, name, amt, usd, "out", "spl"))
    p.transfers = transfers
    for x in transfers:
        book = p.in_usd if x.direction == "in" else p.out_usd
        book[x.counterparty] = book.get(x.counterparty, 0.0) + x.usd
    first_sol_in = next((x for x in transfers if x.direction == "in" and x.token == "SOL"), None)
    if raw.history_end and first_sol_in is not None:
        p.funder = first_sol_in.counterparty
    elif not raw.history_end:
        p.notes.append(f"more than {len(raw.signatures):,} transactions: the oldest (and the funder) were not read")
    p.swap_tokens = len(swap_tokens)
    p.active_days = len(days)
    p.sell_days = len(sell_days)
    p.orders = p.swaps

    # Futures: Jupiter Perps episodes from the close events.
    eps = je.episodes(raw.perp_events)
    p.jup_episodes = len(eps)
    p.funding_events = len(raw.perp_events)
    p.perp_volume = sum(e.size_usd for e in eps)
    p.perp_fills = len(raw.perp_events)
    p.fills += p.perp_fills
    for e in eps:
        times += [e.opened_at, e.closed_at]
        days.add(datetime.fromtimestamp(e.closed_at, UTC).strftime("%Y-%m-%d"))
    p.active_days = len(days)
    if raw.perp_positions and not eps:
        p.notes.append(f"{raw.perp_positions} Jupiter Perps position account(s), no closed episode in the window read")

    if times:
        p.first, p.last = min(times), max(times)
        p.lifetime_days = (p.last - p.first) / 86400
    ok = [s for s in raw.signatures if s.get("err") is None]
    read_all = len(raw.txs) >= len(ok)
    p.partial = not (raw.history_end and read_all)
    if not read_all:
        p.notes.append(f"decoded the newest {len(raw.txs)} of {len(ok)} transactions: counts are a lower bound")
    if raw.balance_lamports is not None:
        p.balance_live = {"SOL": raw.balance_lamports / LAMPORTS}
    return p
