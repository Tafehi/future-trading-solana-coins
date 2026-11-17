"""Raw Hyperliquid answers -> Profile (PLAN §5). Pure: no I/O, no clock.

The balance check (`complete`) replays every movement of every token and compares the sum with the live balance. If
they differ by more than a tolerance, history is missing (fills cap, paging, an event type the ledger does not show),
and the wallet is `INCOMPLETE` instead of guessed. Rules learned on the 2026-11-17 case (DEBUG D-8):

- Staking comes from `delegatorHistory` (`cDeposit` takes HYPE from spot, a FINALIZED `withdrawal` gives it back);
  the ledger's `cStakingTransfer` rows are only a fallback, because the ledger missed some.
- `deposit` (from Arbitrum) lands on the perp side; `withdraw` leaves from it. USDC is checked as one figure (spot +
  perp account value), because the split between the two sides moves with every class transfer.
- `send` names its sides: `sourceDex`/`destinationDex` "spot" = spot, "" = the perp side.
- A liquidation takes the margin that is left, and no fill or ledger row shows it (a real wallet: -55.80 USDC, exactly
  its margin). So a wallet with a liquidation fill gets a note for USDC, not INCOMPLETE.
- A gap worth less than `BALANCE_USD_TOL` at the current price is noted, not failed: some airdrop dust left a real
  wallet with no ledger event at all.
"""

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.hl_source import HLP_NAME, HlRaw, is_system
from app.tools.wallet_trace.model import Airdrop, Profile, Transfer

KNOWN_TYPES = {
    "deposit", "withdraw", "spotTransfer", "send", "internalTransfer", "accountClassTransfer", "cStakingTransfer",
    "spotGenesis", "vaultDeposit", "vaultWithdraw", "subAccountTransfer", "borrowLend", "accountActivationGas",
}  # fmt: skip
USD_TOKENS = {"USDC", "USDH", "USDT0", "USDE", "USDHL"}
EXIT_ARBITRUM = "arbitrum"
EXIT_HYPEREVM = "hyperevm"


def _f(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


class SpotMeta:
    """`spotMeta` -> pair names (`@N` and canonical names) -> (base, quote); token -> its USDC pair."""

    def __init__(self, meta: dict[str, Any]) -> None:
        tokens = {int(t["index"]): str(t["name"]) for t in meta.get("tokens") or []}
        self.pairs: dict[str, tuple[str, str]] = {}
        self.usdc_pair: dict[str, str] = {}
        for u in meta.get("universe") or []:
            base, quote = (tokens.get(int(i), f"#{i}") for i in u["tokens"])
            for name in (str(u["name"]), f"@{u['index']}"):
                self.pairs[name] = (base, quote)
            if quote == "USDC" and base not in self.usdc_pair:
                self.usdc_pair[base] = f"@{u['index']}"

    def is_spot(self, coin: str) -> bool:
        return coin in self.pairs or coin.startswith("@")

    def price(self, token: str, mids: dict[str, Any]) -> float | None:
        if token in USD_TOKENS:
            return 1.0
        pair = self.usdc_pair.get(token)
        if pair is None:
            return None
        p = mids.get(pair)
        name = next((n for n, bq in self.pairs.items() if bq == (token, "USDC") and not n.startswith("@")), None)
        if p is None and name:
            p = mids.get(name)
        return _f(p) if p is not None else None


def _round_lot(size: float) -> bool:
    if size < S.ROUND_LOT_MIN:
        return False
    steps = size / S.ROUND_LOT_STEP
    return abs(steps - round(steps)) < 1e-6


def merged_fills(raw: HlRaw) -> list[dict[str, Any]]:
    """Normal fills plus TWAP slices (they are not in `userFillsByTime`), one per trade id, oldest first."""
    out: dict[Any, dict[str, Any]] = {}
    for f in list(raw.fills) + [x.get("fill") or {} for x in raw.twap_fills]:
        if f:
            out.setdefault((f.get("tid"), f.get("time"), f.get("coin"), f.get("sz")), f)
    return sorted(out.values(), key=lambda f: int(f["time"]))


def staking_events(raw: HlRaw) -> list[tuple[int, str, float]]:
    """(time ms, "dep"|"wd", amount): delegatorHistory first, plus any ledger cStakingTransfer it lacks."""
    out: list[tuple[int, str, float]] = []
    for e in raw.staking:
        d = e.get("delta") or {}
        if "cDeposit" in d:
            out.append((int(e["time"]), "dep", _f(d["cDeposit"].get("amount"))))
        elif "withdrawal" in d and d["withdrawal"].get("phase") == "finalized":
            out.append((int(e["time"]), "wd", _f(d["withdrawal"].get("amount"))))
    have = {(t // 1000, k, round(a, 6)) for t, k, a in out}
    for e in raw.ledger:
        d = e.get("delta") or {}
        if d.get("type") != "cStakingTransfer" or str(d.get("token", "HYPE")) != "HYPE":
            continue
        ev = (int(e["time"]), "dep" if d.get("isDeposit") else "wd", _f(d.get("amount")))
        if (ev[0] // 1000, ev[1], round(ev[2], 6)) not in have:
            out.append(ev)
    return sorted(out)


def build(raw: HlRaw, meta: SpotMeta, mids: dict[str, Any] | None = None) -> Profile:
    """Every PLAN §5 number for one Hyperliquid wallet."""
    a = raw.address.lower()
    mids = mids or {}
    p = Profile(address=a, chain="hl")
    spot: defaultdict[str, float] = defaultdict(float)  # summed spot balance per token
    moved: defaultdict[str, float] = defaultdict(float)  # everything that moved per token (for the relative tolerance)
    usdc_perp = 0.0
    times: list[float] = []
    unknown: Counter[str] = Counter()
    transfers: list[Transfer] = []
    to_perp: list[int] = []
    withdraw_times: list[int] = []
    withdraws: list[tuple[int, float]] = []

    def add(token: str, amount: float) -> None:
        spot[token] += amount
        moved[token] += abs(amount)

    def transfer(t: int, cp: str, token: str, amount: float, usd: float, direction: str, kind: str) -> None:
        cp = cp.lower()
        transfers.append(Transfer(t / 1000, cp, token, amount, usd, direction, kind))
        if is_system(cp):
            if direction == "out":
                p.exits[EXIT_HYPEREVM] = p.exits.get(EXIT_HYPEREVM, 0.0) + usd
            return
        book = p.in_usd if direction == "in" else p.out_usd
        book[cp] = book.get(cp, 0.0) + usd

    for e in sorted(raw.ledger, key=lambda x: int(x["time"])):
        t = int(e["time"])
        d = e.get("delta") or {}
        kind = str(d.get("type"))
        times.append(t)
        if kind not in KNOWN_TYPES:
            unknown[kind] += 1
            continue
        token = str(d.get("token") or "USDC")
        amount = _f(d.get("amount"))
        usdc = _f(d.get("usdc"))
        src = str(d.get("user") or "").lower()
        dst = str(d.get("destination") or "").lower()
        if kind == "spotGenesis":
            add(token, amount)
            p.airdrops.append(Airdrop(token, amount, t / 1000))
        elif kind == "deposit":
            usdc_perp += usdc
            moved["USDC"] += usdc
            p.deposits_usd += usdc
        elif kind == "withdraw":
            usdc_perp -= usdc + _f(d.get("fee"))
            moved["USDC"] += usdc
            p.withdraws_usd += usdc
            p.exits[EXIT_ARBITRUM] = p.exits.get(EXIT_ARBITRUM, 0.0) + usdc
            withdraw_times.append(t)
            withdraws.append((t, usdc))
        elif kind == "accountClassTransfer":
            sign = 1 if d.get("toPerp") else -1
            usdc_perp += sign * usdc
            add("USDC", -sign * usdc)
            if d.get("toPerp"):
                to_perp.append(t)
        elif kind in ("spotTransfer", "send"):
            usd = _f(d.get("usdcValue")) or (amount if token in USD_TOKENS else 0.0)
            src_side = "spot" if kind == "spotTransfer" else ("spot" if d.get("sourceDex") == "spot" else "perp")
            dst_side = "spot" if kind == "spotTransfer" else ("spot" if d.get("destinationDex") == "spot" else "perp")
            if src == a:
                if src_side == "spot":
                    add(token, -amount)
                else:
                    usdc_perp -= amount
                fee_token = str(d.get("feeToken") or "USDC")
                if _f(d.get("fee")):
                    add(fee_token, -_f(d.get("fee")))
                if _f(d.get("nativeTokenFee")):
                    add("HYPE", -_f(d.get("nativeTokenFee")))
            if dst == a:
                if dst_side == "spot":
                    add(token, amount)
                else:
                    usdc_perp += amount
            if src == a and dst == a:
                if src_side == "spot" and dst_side == "perp":
                    to_perp.append(t)  # spot -> perp on the same wallet: the same door as a class transfer
            elif src == a:
                transfer(t, dst, token, amount, usd, "out", kind)
            elif dst == a:
                transfer(t, src, token, amount, usd, "in", kind)
        elif kind in ("internalTransfer", "subAccountTransfer"):
            if src == a:
                usdc_perp -= usdc + (_f(d.get("fee")) if kind == "internalTransfer" else 0.0)
                transfer(t, dst, "USDC", usdc, usdc, "out", kind)
            if dst == a:
                usdc_perp += usdc
                transfer(t, src, "USDC", usdc, usdc, "in", kind)
            moved["USDC"] += usdc
        elif kind == "vaultDeposit":
            vault = str(d.get("vault") or "").lower()
            usdc_perp -= usdc
            moved["USDC"] += usdc
            p.vault_addresses.add(vault)
            if vault not in {v for v, _ in p.vaults}:
                p.vaults.append((vault, t / 1000))
            transfer(t, vault, "USDC", usdc, usdc, "out", kind)
        elif kind == "vaultWithdraw":
            vault = str(d.get("vault") or "").lower()
            net = _f(d.get("netWithdrawnUsd"))
            usdc_perp += net
            moved["USDC"] += net
            p.vault_addresses.add(vault)
            transfer(t, vault, "USDC", net, net, "in", kind)
        elif kind == "borrowLend":
            op = str(d.get("operation"))
            add(token, -amount if op in ("supply", "repay") else amount)
        elif kind == "accountActivationGas":
            add(token, -amount)

    stakes = staking_events(raw)
    for t, k, amount in stakes:
        add("HYPE", -amount if k == "dep" else amount)
        times.append(t)

    # --- fills ---
    fills = merged_fills(raw)
    p.fills = len(fills)
    p.fills_capped = len(fills) >= S.HL_FILLS_VISIBLE
    days: set[str] = set()
    sell_days: set[str] = set()
    orders: set[Any] = set()
    sell_size: dict[Any, float] = defaultdict(float)
    perp_fill_times: list[int] = []
    liquidated = False
    for f in fills:
        t = int(f["time"])
        times.append(t)
        days.add(_day(t))
        orders.add(f.get("oid"))
        coin = str(f["coin"])
        px, sz = _f(f["px"]), _f(f["sz"])
        buy = f.get("side") == "B"
        fee_token = str(f.get("feeToken") or "USDC")
        if meta.is_spot(coin):
            base, quote = meta.pairs.get(coin, (coin, "USDC"))
            notional = px * sz * (meta.price(quote, mids) or 1.0 if quote not in USD_TOKENS else 1.0)
            p.spot_fills += 1
            p.spot_volume += notional
            if buy:
                p.spot_buy_volume += notional
            else:
                p.spot_sell_volume += notional
                sell_size[f.get("oid")] += sz
                sell_days.add(_day(t))
            add(base, sz if buy else -sz)
            add(quote, -px * sz if buy else px * sz)
            add(fee_token, -_f(f.get("fee")))
        else:
            p.perp_fills += 1
            p.perp_volume += px * sz
            perp_fill_times.append(t)
            if f.get("liquidation"):
                liquidated = True
            usdc_perp += _f(f.get("closedPnl")) - (_f(f.get("fee")) if fee_token == "USDC" else 0.0)
            if fee_token != "USDC":
                add(fee_token, -_f(f.get("fee")))
            moved["USDC"] += abs(_f(f.get("closedPnl")))
    p.orders = len(orders)
    p.active_days = len(days)
    p.sell_orders = len(sell_size)
    p.round_lot_sells = sum(1 for s in sell_size.values() if _round_lot(s))
    p.sell_days = len(sell_days)

    p.funding_events = len(raw.funding)
    for e in raw.funding:
        usdc_perp += _f((e.get("delta") or {}).get("usdc"))
        times.append(int(e["time"]))

    # --- exit door vs trading (toPerp moves) ---
    door_ms = S.EXIT_DOOR_MINUTES * 60_000
    for t in to_perp:
        nxt_w = next((w for w in withdraw_times if w >= t), None)
        nxt_f = next((f for f in perp_fill_times if f >= t), None)
        if nxt_f is not None and (nxt_w is None or nxt_f < nxt_w):
            p.trading_perp_moves += 1
        elif nxt_w is not None and nxt_w - t <= door_ms:
            p.exit_door_pairs += 1

    # --- transfers: pass-through, one-shot, stake lag ---
    transfers.sort(key=lambda x: x.time)
    p.transfers = transfers
    window = S.PASSTHROUGH_MINUTES * 60
    for tin in (x for x in transfers if x.direction == "in" and x.usd >= S.BIG_TRANSFER_USD and x.amount > 0):
        outs = [
            x
            for x in transfers
            if x.direction == "out" and x.token == tin.token and tin.time < x.time <= tin.time + window
        ]
        share = min(1.0, sum(x.amount for x in outs) / tin.amount)
        if share > p.passthrough_share:
            p.passthrough_share = share
            p.passthrough_lag_minutes = (outs[-1].time - tin.time) / 60 if outs else None
    hype_in = [x for x in transfers if x.direction == "in" and x.token == "HYPE" and x.usd >= S.BIG_TRANSFER_USD]
    if hype_in:
        first_dep = next((t for t, k, _ in stakes if k == "dep" and t / 1000 >= hype_in[0].time), None)
        if first_dep is not None:
            p.stake_lag_minutes = (first_dep / 1000 - hype_in[0].time) / 60
    p.one_shot = _one_shot(raw, fills, meta, transfers, p)
    big_in = next((x.time for x in transfers if x.direction == "in" and x.usd >= S.BIG_TRANSFER_USD), None)
    if big_in is not None:
        after = [(w_usd, w_t) for w_t, w_usd in withdraws if w_t / 1000 >= big_in]
        if after:
            p.in_to_exit_days = (max(after)[1] / 1000 - big_in) / 86400

    # --- account now ---
    p.staked_now = _f(raw.deleg.get("delegated")) + _f(raw.deleg.get("totalPendingWithdrawal"))
    role = raw.role or {}
    p.role = str(role.get("role") or "")
    data = role.get("data") or {}
    p.master = str(data.get("master") or data.get("user") or "").lower()
    p.subaccounts = [str(s.get("subAccountUser", "")).lower() for s in raw.subs or [] if isinstance(s, dict)]
    p.unknown_types = dict(unknown)
    if times:
        p.first, p.last = min(times) / 1000, max(times) / 1000
        p.lifetime_days = (p.last - p.first) / 86400

    _balance_check(raw, meta, mids, p, spot, moved, usdc_perp, liquidated)
    if p.fills_capped:
        p.complete = False
        p.notes.append(f"{p.fills} fills: only the newest {S.HL_FILLS_VISIBLE:,} exist on the API")
    for vault, _ in p.vaults:
        info = raw.vault_details.get(vault) or {}
        name = str(info.get("name") or "unnamed vault")
        if HLP_NAME in name:
            p.notes.append(f"deposited in {name}: Hyperliquid's own vault, not a person")
        else:
            p.notes.append(f"deposited in vault '{name}' (leader {info.get('leader') or '?'}): someone else trades it")
    return p


def _one_shot(raw: HlRaw, fills: list[dict[str, Any]], meta: SpotMeta, transfers: list[Transfer], p: Profile) -> bool:
    """Money in -> only buys -> one transfer out of >= ONE_SHOT_OUT_SHARE of the bought token, all inside the window."""
    spot_fills = [f for f in fills if meta.is_spot(str(f["coin"]))]
    if not spot_fills or any(f.get("side") != "B" for f in spot_fills):
        return False
    bought: defaultdict[str, float] = defaultdict(float)
    for f in spot_fills:
        bought[meta.pairs.get(str(f["coin"]), (str(f["coin"]), ""))[0]] += _f(f["sz"])
    token, amount = max(bought.items(), key=lambda kv: kv[1])
    outs = [x for x in transfers if x.direction == "out" and x.token == token]
    if len(outs) != 1 or outs[0].amount < S.ONE_SHOT_OUT_SHARE * amount:
        return False
    money_in = [int(e["time"]) / 1000 for e in raw.ledger if (e.get("delta") or {}).get("type") == "deposit"]
    money_in += [x.time for x in transfers if x.direction == "in" and x.token in USD_TOKENS]
    start = min(money_in + [int(spot_fills[0]["time"]) / 1000])
    return outs[0].time - start <= S.ONE_SHOT_WINDOW_HOURS * 3600


def _balance_check(
    raw: HlRaw,
    meta: SpotMeta,
    mids: dict[str, Any],
    p: Profile,
    spot: dict[str, float],
    moved: dict[str, float],
    usdc_perp: float,
    liquidated: bool = False,
) -> None:
    live: dict[str, float] = {}
    for b in (raw.spot or {}).get("balances") or []:
        live[str(b["coin"])] = live.get(str(b["coin"]), 0.0) + _f(b.get("total"))
    perp_state = raw.perp or {}
    account_value = _f((perp_state.get("marginSummary") or {}).get("accountValue"))
    open_positions = bool(perp_state.get("assetPositions"))
    summed = dict(spot)
    summed["USDC"] = summed.get("USDC", 0.0) + usdc_perp
    live["USDC"] = live.get("USDC", 0.0) + account_value
    if not raw.spot and not raw.perp:
        p.notes.append("no live balance answer: balance check skipped")
        return
    for token in sorted(set(summed) | set(live)):
        gap = live.get(token, 0.0) - summed.get(token, 0.0)
        tol = max(S.BALANCE_ABS_TOL, S.BALANCE_REL_TOL * moved.get(token, 0.0))
        if abs(gap) <= tol:
            continue
        if token == "USDC" and open_positions:
            p.notes.append(f"USDC not checked: open perp positions (unrealised PnL), gap {gap:,.2f}")
            continue
        if token == "USDC" and liquidated:
            p.notes.append(f"USDC not checked: liquidated (the margin left went to the liquidation), gap {gap:,.2f}")
            continue
        price = meta.price(token, mids)
        if price is not None and abs(gap) * price < S.BALANCE_USD_TOL:
            p.notes.append(f"{token}: small unexplained gap {gap:,.4f} (~${abs(gap) * price:,.2f})")
            continue
        p.balance_gaps[token] = gap
    if p.balance_gaps:
        p.complete = False
    p.balance_sum = summed
    p.balance_live = live
