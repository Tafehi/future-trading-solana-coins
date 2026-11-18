"""Hyperliquid coin <-> KuCoin USDT-margined contract.

Rules, in order:
1. `BTC` is `XBT` on KuCoin.
2. A Hyperliquid `kXYZ` coin is priced per 1000 XYZ. KuCoin lists either `1000XYZ` (same scale) or plain
   `XYZ` (1/1000 of the price). `price_scale` converts a Hyperliquid price to the KuCoin contract's price.
3. Otherwise the same base name.

A coin with no match is simply not tradeable here; it is never guessed.
"""

from dataclasses import dataclass

from app.data.kucoin import Contract


@dataclass(frozen=True)
class Mapping:
    coin: str  # Hyperliquid name
    contract: Contract
    price_scale: float  # KuCoin price = Hyperliquid price x price_scale

    @property
    def symbol(self) -> str:
        return self.contract.symbol


def build(coins: list[str], contracts: list[Contract]) -> dict[str, Mapping]:
    by_base = {c.base: c for c in contracts if c.status == "Open"}
    out: dict[str, Mapping] = {}
    for coin in coins:
        if coin == "BTC" and "XBT" in by_base:
            out[coin] = Mapping(coin, by_base["XBT"], 1.0)
        elif coin.startswith("k") and len(coin) > 1 and coin[1:].isupper():
            plain = coin[1:]
            if f"1000{plain}" in by_base:
                out[coin] = Mapping(coin, by_base[f"1000{plain}"], 1.0)
            elif plain in by_base:
                out[coin] = Mapping(coin, by_base[plain], 0.001)
        elif coin in by_base:
            out[coin] = Mapping(coin, by_base[coin], 1.0)
    return out
