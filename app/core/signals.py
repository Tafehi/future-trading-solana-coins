"""Position changes -> signals, and the copy trigger. Pure.

`diff(previous, current)` compares two snapshots of one wallet's open positions:
- OPEN: flat before, open now.     CLOSE: open before, flat now.
- FLIP: the sign changed; it is emitted as CLOSE then OPEN so exits run before the new entry.
- ADD / REDUCE: same side, size changed. Recorded only; copies follow opens and closes.

The first snapshot of a wallet is a BASELINE: positions it already holds are old news, and copying them
would buy at a price the wallet chose hours or days ago. `diff(None, ...)` therefore returns nothing.

Ported from ai-kucoin-future; the SILVER confluence rule is dropped (GOLD only).
"""

from dataclasses import dataclass

from app.data.hyperliquid import WalletPosition

OPEN = "OPEN"
CLOSE = "CLOSE"
ADD = "ADD"
REDUCE = "REDUCE"

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class Signal:
    wallet: str
    coin: str
    kind: str
    side: str
    size: float
    entry_px: float
    leverage: float


def side_of(size: float) -> str:
    return LONG if size > 0 else SHORT


def diff(wallet: str, previous: dict[str, WalletPosition] | None, current: list[WalletPosition]) -> list[Signal]:
    if previous is None:
        return []
    now = {p.coin: p for p in current}
    out: list[Signal] = []
    for coin, old in previous.items():
        new = now.get(coin)
        if new is None:
            out.append(Signal(wallet, coin, CLOSE, side_of(old.size), 0.0, old.entry_px, old.leverage))
        elif new.size * old.size < 0:
            out.append(Signal(wallet, coin, CLOSE, side_of(old.size), 0.0, old.entry_px, old.leverage))
            out.append(Signal(wallet, coin, OPEN, side_of(new.size), new.size, new.entry_px, new.leverage))
        elif abs(new.size) > abs(old.size):
            out.append(Signal(wallet, coin, ADD, side_of(new.size), new.size, new.entry_px, new.leverage))
        elif abs(new.size) < abs(old.size):
            out.append(Signal(wallet, coin, REDUCE, side_of(new.size), new.size, new.entry_px, new.leverage))
    for coin, new in now.items():
        if coin not in previous:
            out.append(Signal(wallet, coin, OPEN, side_of(new.size), new.size, new.entry_px, new.leverage))
    return out
