"""Jupiter Perps (Solana) positions, read straight from the chain. Signal source only: nothing is traded here.

Layout, checked 2026-11-13 against developers.jup.ag/docs/perps/position-account, the public Anchor IDL
(github.com/julianfssen/jupiter-perps-anchor-idl-parsing) AND real mainnet accounts (DEBUG D-2):
- Program id `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu`.
- `Position` account: 8-byte Anchor discriminator (sha256("account:Position")[:8]), then
    owner Pubkey (8) | pool Pubkey (40) | custody Pubkey (72) | collateralCustody Pubkey (104) | openTime i64 (136) |
    updateTime i64 (144) | side u8 (152; 0 None, 1 Long, 2 Short) | price u64 (153) | sizeUsd u64 (161) | ...
  Real accounts are 216 bytes (the IDL sums to 210; the extra 6 bytes come after the fields we read).
- USD amounts and prices use 6 decimals. `custody` is the market: SOL 7xS2gz2b…, ETH AQCGyheW…, BTC 5Pv3gM9J….
- A closed position keeps its account with sizeUsd 0, and a close updates `updateTime`.

Why we never scan everything: mainnet holds ~886,000 Position accounts (2026-11-13), almost all closed. A full scan
is ~280 MB. Instead `updated_request(block)` asks only for accounts whose `updateTime` lies in one 65,536-second
block: a memcmp on the 6 high bytes of the little-endian `updateTime` (offset 146). Measured 2026-11-13: 4,014
accounts, 1.8 MB, 0.9 s for a block 16 hours old. Opens and closes both rewrite `updateTime`, so this is a change feed.
"""

import base64
import hashlib
import struct
from dataclasses import dataclass

ACCOUNT_SIZE = 216
SLICE_OFFSET = 8
SLICE_LENGTH = 161  # owner .. sizeUsd
UPDATE_TIME_OFFSET = 144
BLOCK_SHIFT = 16  # one block = 2**16 s (~18.2 h)
USD_DECIMALS = 1_000_000
MAX_MULTIPLE = 100  # getMultipleAccounts limit

CUSTODY_COIN = {
    "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz": "SOL",
    "AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn": "ETH",
    "5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm": "BTC",
}
SIDES = {1: "long", 2: "short"}
SOURCE = "jupiter"  # the `source` of every Jupiter wallet and signal row (shared by the scout and the trader, D-30)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * pad + out


def discriminator(name: str = "Position") -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


@dataclass(frozen=True)
class JupPosition:
    pubkey: str
    owner: str
    coin: str  # SOL / ETH / BTC
    side: str  # long / short
    size_usd: float  # 0 = closed
    entry_px: float
    open_time: int  # unix seconds
    update_time: int = 0

    @property
    def is_open(self) -> bool:
        return self.size_usd > 0


def decode_slice(pubkey: str, data: bytes) -> JupPosition | None:
    """Bytes 8..169 of a Position account -> the position (open or closed), or None for an unknown market/side."""
    if len(data) < SLICE_LENGTH:
        return None
    custody = b58encode(data[64:96])
    coin = CUSTODY_COIN.get(custody)
    open_time, update_time = struct.unpack_from("<qq", data, 128)
    side = SIDES.get(data[144])
    price, size = struct.unpack_from("<QQ", data, 145)
    if coin is None or side is None:
        return None
    owner = b58encode(data[0:32])
    return JupPosition(pubkey, owner, coin, side, size / USD_DECIMALS, price / USD_DECIMALS, int(open_time),
                       int(update_time))  # fmt: skip


def decode_account(pubkey: str, data: bytes) -> JupPosition | None:
    """A WHOLE Position account (216 bytes, as a websocket notification carries it) -> the position, or None when it is
    not a Position account: the size and the Anchor discriminator must match (a programSubscribe filter on size alone
    could let another 216-byte account type through)."""
    if len(data) != ACCOUNT_SIZE or data[:8] != discriminator():
        return None
    return decode_slice(pubkey, data[SLICE_OFFSET : SLICE_OFFSET + SLICE_LENGTH])


def block_of(ts: float) -> int:
    return int(ts) >> BLOCK_SHIFT


def updated_request(program_id: str, block: int) -> dict[str, object]:
    """Every Position account whose updateTime is in `block`, only the bytes we decode."""
    high = block.to_bytes(8 - BLOCK_SHIFT // 8, "little")
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            program_id,
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "dataSlice": {"offset": SLICE_OFFSET, "length": SLICE_LENGTH},
                "filters": [
                    {"dataSize": ACCOUNT_SIZE},
                    {
                        "memcmp": {
                            "offset": 0,
                            "bytes": base64.b64encode(discriminator()).decode(),
                            "encoding": "base64",
                        }
                    },
                    {
                        "memcmp": {
                            "offset": UPDATE_TIME_OFFSET + BLOCK_SHIFT // 8,
                            "bytes": base64.b64encode(high).decode(),
                            "encoding": "base64",
                        }
                    },
                ],
            },
        ],
    }


def multiple_request(pubkeys: list[str]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getMultipleAccounts",
        "params": [
            pubkeys[:MAX_MULTIPLE],
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "dataSlice": {"offset": SLICE_OFFSET, "length": SLICE_LENGTH},
            },
        ],
    }


def _decode_b64(item: object) -> bytes | None:
    try:
        return base64.b64decode(item[0])  # type: ignore[index]
    except (IndexError, TypeError, ValueError):
        return None


def parse_program_accounts(body: object) -> list[JupPosition]:
    """A getProgramAccounts answer -> positions (open and closed). Raises ValueError on an RPC error body."""
    if not isinstance(body, dict) or "result" not in body:
        raise ValueError(f"getProgramAccounts: unexpected body {str(body)[:200]!r}")
    out: list[JupPosition] = []
    for item in body["result"] or []:
        raw = _decode_b64((item.get("account") or {}).get("data")) if isinstance(item, dict) else None
        pos = decode_slice(str(item.get("pubkey", "")), raw) if raw is not None else None
        if pos is not None:
            out.append(pos)
    return out


def parse_multiple(body: object, pubkeys: list[str]) -> dict[str, JupPosition | None]:
    """A getMultipleAccounts answer -> pubkey: position (None = account gone or unreadable)."""
    if not isinstance(body, dict) or "result" not in body:
        raise ValueError(f"getMultipleAccounts: unexpected body {str(body)[:200]!r}")
    values = (body["result"] or {}).get("value") or []
    out: dict[str, JupPosition | None] = {}
    for key, v in zip(pubkeys, values, strict=False):
        raw = _decode_b64(v.get("data")) if isinstance(v, dict) else None
        out[key] = decode_slice(key, raw) if raw is not None else None
    return out
