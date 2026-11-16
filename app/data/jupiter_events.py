"""Jupiter Perps close events, decoded from a transaction (item 1: wallet history from the chain).

Jupiter's program emits Anchor events by a self-CPI: an inner instruction of the perps program whose data is
    reversed(sha256("anchor:event")[:8]) | sha256("event:<Name>")[:8] | borsh fields
Checked against mainnet 2026-11-13 (DEBUG D-5): a closing transaction (`InstantDecreasePosition2`) carries
`InstantDecreasePositionEvent`; decoded with the IDL field order it gives the right owner, position key, prices and a
PnL that matches the price move to the cent (fixture `jupiter_close_tx.json`). The event held ONE byte more than the IDL
lists, so trailing bytes are allowed. `DecreasePositionEvent` (keeper path) and `LiquidateFullPositionEvent` are decoded
from the same IDL; the first live run must confirm them (BUG B-14).

USD and prices have 6 decimals. `position_size_usd` is the size AFTER the event: 0 = the position is fully closed.
"""

import hashlib
import struct
from dataclasses import dataclass
from typing import Any

from app.data.jupiter_perps import CUSTODY_COIN, SIDES, USD_DECIMALS, b58encode

# Anchor's EVENT_IX_TAG is the u64 0x1d9acb512ea545e4 (= sha256("anchor:event")[:8]) written LITTLE-endian, so the
# bytes on the wire are that hash prefix REVERSED: e4 45 a5 2e 51 cb 9a 1d (DEBUG D-5; seen in the mainnet fixture).
EVENT_IX = hashlib.sha256(b"anchor:event").digest()[:8][::-1]

# (name, type) in IDL order. pk = 32-byte key, opk = Option<key>, ou64 = Option<u64>.
_DECREASE_INSTANT = [
    ("position_key", "pk"), ("side", "u8"), ("custody", "pk"), ("collateral_custody", "pk"),
    ("position_size_usd", "u64"), ("position_mint", "pk"), ("desired_mint", "pk"), ("has_profit", "bool"),
    ("pnl_delta", "u64"), ("owner", "pk"), ("pool", "pk"), ("size_usd_delta", "u64"), ("transfer_amount_usd", "u64"),
    ("transfer_token", "u64"), ("price", "u64"), ("price_slippage", "u64"), ("fee_usd", "u64"), ("open_time", "i64"),
    ("referral", "opk"), ("position_fee_usd", "u64"), ("funding_fee_usd", "u64"),
    ("original_collateral_usd", "u64"), ("collateral_usd", "u64"), ("price_impact_fee_usd", "u64"),
    ("position_open_time", "i64"), ("position_price", "u64"),
]  # fmt: skip
_DECREASE = [
    ("position_key", "pk"), ("side", "u8"), ("custody", "pk"), ("collateral_custody", "pk"),
    ("position_size_usd", "u64"), ("position_mint", "pk"), ("request_key", "pk"), ("request_mint", "pk"),
    ("request_change", "u8"), ("request_type", "u8"), ("has_profit", "bool"), ("pnl_delta", "u64"), ("owner", "pk"),
    ("pool", "pk"), ("size_usd_delta", "u64"), ("transfer_amount_usd", "u64"), ("transfer_token", "ou64"),
    ("price", "u64"), ("price_slippage", "ou64"), ("fee_usd", "u64"), ("open_time", "i64"), ("referral", "opk"),
    ("position_fee_usd", "u64"), ("funding_fee_usd", "u64"), ("price_impact_fee_usd", "u64"),
    ("original_collateral_usd", "u64"), ("collateral_usd", "u64"), ("position_open_time", "i64"),
    ("position_price", "u64"),
]  # fmt: skip
_LIQUIDATE = [
    ("position_key", "pk"), ("side", "u8"), ("custody", "pk"), ("collateral_custody", "pk"),
    ("collateral_mint", "pk"), ("position_mint", "pk"), ("position_size_usd", "u64"), ("has_profit", "bool"),
    ("pnl_delta", "u64"), ("owner", "pk"), ("pool", "pk"), ("transfer_amount_usd", "u64"), ("transfer_token", "u64"),
    ("price", "u64"), ("fee_usd", "u64"), ("liquidation_fee_usd", "u64"), ("open_time", "i64"),
    ("position_fee_usd", "u64"), ("funding_fee_usd", "u64"), ("price_impact_fee_usd", "u64"),
    ("original_collateral_usd", "u64"), ("collateral_usd", "u64"), ("position_open_time", "i64"),
    ("position_price", "u64"),
]  # fmt: skip

LAYOUTS: dict[bytes, tuple[str, list[tuple[str, str]]]] = {
    hashlib.sha256(f"event:{name}".encode()).digest()[:8]: (name, fields)
    for name, fields in (
        ("InstantDecreasePositionEvent", _DECREASE_INSTANT),
        ("DecreasePositionEvent", _DECREASE),
        ("LiquidateFullPositionEvent", _LIQUIDATE),
    )
}
LIQUIDATION = "LiquidateFullPositionEvent"


@dataclass(frozen=True)
class CloseEvent:
    name: str
    signature: str
    block_time: int
    position_key: str
    owner: str
    coin: str
    side: str
    size_after_usd: float
    size_delta_usd: float  # the size this event removed (a liquidation removes everything)
    pnl_usd: float  # signed, before fees
    fee_usd: float
    price: float
    position_open_time: int
    position_price: float

    @property
    def closes(self) -> bool:
        return self.size_after_usd == 0 or self.name == LIQUIDATION


def _read(data: bytes, fields: list[tuple[str, str]]) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    o = 0
    try:
        for name, kind in fields:
            if kind == "pk":
                if len(data) < o + 32:
                    return None
                out[name] = b58encode(data[o : o + 32])
                o += 32
            elif kind in ("u8", "bool"):
                out[name] = data[o] if kind == "u8" else data[o] == 1
                o += 1
            elif kind == "u64":
                out[name] = struct.unpack_from("<Q", data, o)[0]
                o += 8
            elif kind == "i64":
                out[name] = struct.unpack_from("<q", data, o)[0]
                o += 8
            elif kind in ("opk", "ou64"):
                tag = data[o]
                o += 1
                if tag:
                    size = 32 if kind == "opk" else 8
                    out[name] = b58encode(data[o : o + 32]) if kind == "opk" else struct.unpack_from("<Q", data, o)[0]
                    o += size
                else:
                    out[name] = None
    except (IndexError, struct.error):
        return None
    return out


def decode_event(data: bytes, *, signature: str = "", block_time: int = 0) -> CloseEvent | None:
    """One inner-instruction payload -> a close event, or None (another event, unknown market, or too short)."""
    if data[:8] != EVENT_IX or data[8:16] not in LAYOUTS:
        return None
    name, fields = LAYOUTS[data[8:16]]
    f = _read(data[16:], fields)
    if f is None:
        return None
    coin = CUSTODY_COIN.get(str(f["custody"]))
    side = SIDES.get(int(f["side"]))
    if coin is None or side is None:
        return None
    sign = 1.0 if f["has_profit"] else -1.0
    if name == LIQUIDATION:  # the event has no delta: the whole position (its size field) is gone
        size_after, delta = 0.0, f["position_size_usd"]
    else:
        size_after, delta = f["position_size_usd"] / USD_DECIMALS, f.get("size_usd_delta")
    return CloseEvent(
        name=name, signature=signature, block_time=block_time, position_key=str(f["position_key"]),
        owner=str(f["owner"]), coin=coin, side=side, size_after_usd=size_after,
        size_delta_usd=(delta / USD_DECIMALS) if delta is not None else size_after,
        pnl_usd=sign * f["pnl_delta"] / USD_DECIMALS, fee_usd=f["fee_usd"] / USD_DECIMALS,
        price=f["price"] / USD_DECIMALS, position_open_time=int(f["position_open_time"]),
        position_price=f["position_price"] / USD_DECIMALS,
    )  # fmt: skip


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def events_from_transaction(tx: Any, program_id: str) -> list[CloseEvent]:
    """A `getTransaction` result (encoding json) -> every close event of the perps program in it."""
    if not isinstance(tx, dict) or (tx.get("meta") or {}).get("err") is not None:
        return []
    msg = (tx.get("transaction") or {}).get("message") or {}
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys = list(msg.get("accountKeys") or []) + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    sig = str(((tx.get("transaction") or {}).get("signatures") or [""])[0])
    when = int(tx.get("blockTime") or 0)
    out: list[CloseEvent] = []
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            idx = ix.get("programIdIndex")
            if not isinstance(idx, int) or idx >= len(keys) or keys[idx] != program_id:
                continue
            try:
                data = b58decode(str(ix.get("data") or ""))
            except ValueError:
                continue
            ev = decode_event(data, signature=sig, block_time=when)
            if ev is not None:
                out.append(ev)
    return out


@dataclass(frozen=True)
class ChainEpisode:
    owner: str
    position_key: str
    coin: str
    side: str
    size_usd: float
    entry_px: float
    exit_px: float
    opened_at: float
    closed_at: float
    pnl: float  # net of the close fees


def episodes(events: list[CloseEvent]) -> list[ChainEpisode]:
    """Close events -> complete episodes. Pure.

    Grouped by (position account, position open time): one lifetime of a position. An episode needs a full close
    (size after 0, or a liquidation); a lifetime with only partial decreases (still open) is skipped. PnL = the sum of
    the signed PnL of every decrease minus its fees (borrow included in `fee_usd`; open fees are not in these events, so
    the PnL is slightly optimistic — the same for every wallet). Size = the largest size seen before a decrease.
    """
    groups: dict[tuple[str, int], list[CloseEvent]] = {}
    for ev in events:
        groups.setdefault((ev.position_key, ev.position_open_time), []).append(ev)
    out: list[ChainEpisode] = []
    for (key, opened), evs in groups.items():
        evs = sorted(evs, key=lambda e: (e.block_time, e.size_after_usd * -1))
        final = next((e for e in reversed(evs) if e.closes), None)
        if final is None:
            continue
        lifetime = [e for e in evs if e.block_time <= final.block_time]
        out.append(
            ChainEpisode(
                owner=final.owner, position_key=key, coin=final.coin, side=final.side,
                size_usd=max(e.size_after_usd + e.size_delta_usd for e in lifetime),
                entry_px=final.position_price, exit_px=final.price, opened_at=float(opened),
                closed_at=float(final.block_time), pnl=sum(e.pnl_usd - e.fee_usd for e in lifetime),
            )
        )  # fmt: skip
    return sorted(out, key=lambda e: e.closed_at)
