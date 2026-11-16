"""Shared wallet data types, kept from the parent repo's Hyperliquid module (split 2026-11-14, DECISIONS D-19).

This bot does not read Hyperliquid. The module stays because the shared grading code imports its types:
`core/wallet_stats.py` (`Fill`), `core/signals.py` (`WalletPosition`), `core/wallet_grader.py` (`AccountState`). The
parsers stay because the grading tests build fills from a recorded fixture with them. The Hyperliquid client, the
leaderboard and the portfolio parser left with the Hyperliquid source (they live on in future-trading-kucoins).
"""

from dataclasses import dataclass
from typing import Any

from app.data.http import DataError


@dataclass(frozen=True)
class WalletPosition:
    coin: str
    size: float  # signed: positive long, negative short
    entry_px: float
    leverage: float
    unrealized_pnl: float = 0.0


@dataclass(frozen=True)
class Fill:
    coin: str
    px: float
    sz: float
    side: str  # "B" buy, "A" sell
    time_ms: int
    start_position: float
    direction: str
    closed_pnl: float
    fee: float

    @property
    def signed_size(self) -> float:
        return self.sz if self.side == "B" else -self.sz


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AccountState:
    """`clearinghouseState` summed up: what the account holds and risks right now."""

    account_value: float
    notional: float  # total open position size in USD
    unrealized_pnl: float

    @property
    def leverage(self) -> float:
        """Real leverage of the account (open size / account value), not the per-position setting."""
        return self.notional / self.account_value if self.account_value > 0 else 0.0


def parse_account(body: Any) -> AccountState:
    positions = parse_positions(body)
    summary = body.get("marginSummary") or {}
    return AccountState(
        account_value=_f(summary.get("accountValue")),
        notional=_f(summary.get("totalNtlPos")),
        unrealized_pnl=sum(p.unrealized_pnl for p in positions),
    )


def parse_positions(body: Any) -> list[WalletPosition]:
    """`clearinghouseState` -> open perp positions. Raises DataError on a body without `assetPositions`."""
    if not isinstance(body, dict) or "assetPositions" not in body:
        raise DataError(f"clearinghouseState: unexpected body {str(body)[:160]!r}")
    out: list[WalletPosition] = []
    for item in body["assetPositions"] or []:
        pos = (item or {}).get("position") or {}
        size = _f(pos.get("szi"))
        if size == 0.0 or not pos.get("coin"):
            continue
        lev = pos.get("leverage") or {}
        out.append(
            WalletPosition(
                coin=str(pos["coin"]),
                size=size,
                entry_px=_f(pos.get("entryPx")),
                leverage=_f(lev.get("value"), 1.0) if isinstance(lev, dict) else _f(lev, 1.0),
                unrealized_pnl=_f(pos.get("unrealizedPnl")),
            )
        )
    return out


def parse_fills(body: Any) -> list[Fill]:
    if not isinstance(body, list):
        raise DataError(f"userFillsByTime: unexpected body {str(body)[:160]!r}")
    out: list[Fill] = []
    for raw in body:
        try:
            out.append(
                Fill(
                    coin=str(raw["coin"]),
                    px=float(raw["px"]),
                    sz=float(raw["sz"]),
                    side=str(raw["side"]),
                    time_ms=int(raw["time"]),
                    start_position=float(raw.get("startPosition", 0.0)),
                    direction=str(raw.get("dir", "")),
                    closed_pnl=_f(raw.get("closedPnl")),
                    fee=_f(raw.get("fee")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out
