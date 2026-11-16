"""Telegram message text. Only REAL fills reach Telegram (user, 2026-11-13): no signals, refusals or wallet lists.

- `digest(start, end)`: every DIGEST_MINUTES, the positions opened and closed in [start, end), each with its $ result,
  plus the account. None when nothing was traded in the window (no message is sent).
- `alarm(title, detail)`: problems that need a human, sent at once.

Pump-fun style: HTML, emoji status, one block per trade, every value escaped.
"""

import time
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.db import store
from app.notifications.telegram import duration, esc, mode
from app.trading import ledger

SOURCE_NAME = {"jupiter": "Jupiter Perps"}


def stamp() -> str:
    """`[PAPER] Jupiter-KuCoin`: both bots post to the same chat (split 2026-11-14), so the name says which one."""
    return f"[{mode()}] {esc(settings.bot_name)}"


def usd(x: float) -> str:
    """`+$12.40` / `-$3.05`; `$0` for dust."""
    if abs(x) < 0.005:
        return "$0"
    return f"{'+' if x > 0 else '-'}${abs(x):,.2f}"


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M")


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-2:]}" if len(addr) > 10 else addr


def px(x: float) -> str:
    return f"{x:,.6g}" if x < 1 else f"{x:,.4f}".rstrip("0").rstrip(".")


def opened_block(p: dict[str, Any]) -> str:
    notional = float(p["qty"]) * float(p["entry_px"])
    dot = "🟢" if p["side"] == "long" else "🔴"
    return (
        f"{dot} <b>OPEN #{p['id']}</b> · {esc(p['side'].upper())} {esc(p['coin'])} ({esc(p['symbol'])}) "
        f"{int(p['leverage'])}x · margin ${float(p['margin']):,.0f} · size ${notional:,.0f} "
        f"@ {px(float(p['entry_px']))}\n"
        f"from {esc(p['tier'])} wallet {esc(short(str(p['wallet'])))} "
        f"({esc(SOURCE_NAME.get(p['source'], p['source']))})"
        f" · weight {int(p['copy_weight'])}/10 · fee ${float(p['entry_fee']):,.2f}"
    )


def closed_block(p: dict[str, Any]) -> str:
    net = float(p["net_pnl"] or 0.0)
    margin = float(p["margin"]) or 1.0
    icon = "💰" if net >= 0 else "🔻"
    held = duration(float(p["closed_at"]) - float(p["opened_at"]))
    fees = float(p["entry_fee"]) + float(p["exit_fee"])
    return (
        f"{icon} <b>CLOSE #{p['id']}</b> · {esc(p['side'].upper())} {esc(p['coin'])} · "
        f"<b>{usd(net)} ({net / margin:+.1%} on margin)</b> · {esc(p['reason'])} · held {held}\n"
        f"{px(float(p['entry_px']))} → {px(float(p['exit_px'] or 0))} · fees ${fees:,.2f} · "
        f"funding {usd(float(p['funding']))}"
    )


async def digest(start: float, end: float) -> str | None:
    account = settings.account
    async with store.session() as conn:
        async with conn.execute(
            "SELECT * FROM positions WHERE account = ? AND opened_at >= ? AND opened_at < ? ORDER BY id",
            (account, start, end),
        ) as cur:
            opened = [dict(r) for r in await cur.fetchall()]
        async with conn.execute(
            "SELECT * FROM positions WHERE account = ? AND status = 'CLOSED' AND closed_at >= ? AND closed_at < ? "
            "ORDER BY closed_at",
            (account, start, end),
        ) as cur:
            closed = [dict(r) for r in await cur.fetchall()]
        if not opened and not closed:
            return None
        equity = await ledger.equity(conn, account)
        deposits = await ledger.deposits(conn, account)
        held = await ledger.open_positions(conn, account)
        day_start = end - (end % 86400)
        async with conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) AS d FROM positions WHERE account = ? AND status = 'CLOSED' "
            "AND closed_at >= ? AND closed_at < ?",
            (account, day_start, end),
        ) as cur:
            row = await cur.fetchone()
        today = float(row["d"]) if row else 0.0
    closed_sum = sum(float(p["net_pnl"] or 0.0) for p in closed)
    head = f"🧾 <b>Trades {hhmm(start)}–{hhmm(end)} UTC</b> {stamp()} · {len(opened)} opened · {len(closed)} closed" + (
        f" · closed {usd(closed_sum)}" if closed else ""
    )
    blocks = [head] + [opened_block(p) for p in opened] + [closed_block(p) for p in closed]
    gain = equity - deposits
    unreal = sum(p.unrealized(p.last_mark or p.entry_px) for p in held)
    blocks.append(
        f"💵 Account ${equity:,.2f} ({usd(gain)}, {gain / deposits:+.1%} since start) · today {usd(today)}\n"
        f"{len(held)} open · unrealized {usd(unreal)}"
        + ("" if settings.is_live else "\nSIMULATED · paper money, not real")
    )
    return "\n\n".join(blocks)


def alarm(title: str, detail: str) -> str:
    return f"🚨 <b>{esc(title)}</b> {stamp()}\n{esc(detail[:500])}"


def started(equity: float, tracked: int) -> str:
    return (
        f"🚀 <b>solana-future-coin bot started</b> {stamp()} · account ${equity:,.2f} · {tracked} tracked wallets · "
        f"{settings.leverage}x isolated · report every {settings.digest_minutes} min"
    )


def window_end(now: float | None = None) -> float:
    """The end of the last complete digest window."""
    at = time.time() if now is None else now
    step = settings.digest_minutes * 60
    return at - (at % step)
