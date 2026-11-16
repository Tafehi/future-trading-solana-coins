"""In-memory view of the markets, refreshed by the cold loop (ported from ai-kucoin-future, spot removed).

Holds KuCoin's open USDT contracts and the coin -> contract mapping. Readers get the last good refresh; a failed
refresh keeps the old view and logs, so one bad KuCoin answer does not empty the universe.

The only coins this bot copies are Jupiter Perps' fixed markets: SOL, ETH, BTC (`JUPITER_COINS`). Since the split
(2026-11-14) no Hyperliquid coin list is fetched.
"""

import time

from app.data import symbol_map
from app.data.http import DataError
from app.data.kucoin import Contract, KucoinFutures
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Jupiter Perps trades only these. `symbol_map` maps them to KuCoin contracts (BTC -> XBTUSDTM).
JUPITER_COINS = ("SOL", "ETH", "BTC")


class MarketCache:
    def __init__(self) -> None:
        self.contracts: dict[str, Contract] = {}
        self.mapping: dict[str, symbol_map.Mapping] = {}
        self.refreshed_at = 0.0

    async def refresh(self, kf: KucoinFutures) -> bool:
        try:
            contracts = await kf.contracts()
        except DataError as exc:
            logger.warning("market: refresh failed, keeping the last view: %s", exc)
            return False
        if not contracts:
            logger.warning("market: KuCoin returned no contracts, keeping the last view")
            return False
        self.load(contracts)
        return True

    def load(self, contracts: list[Contract]) -> None:
        coins = sorted(JUPITER_COINS)
        self.contracts = {c.symbol: c for c in contracts}
        self.mapping = symbol_map.build(coins, contracts)
        self.refreshed_at = time.time()

    def contract(self, symbol: str) -> Contract | None:
        return self.contracts.get(symbol)

    def listed_coins(self) -> set[str]:
        return set(self.mapping)

    def top_by_turnover(self, n: int) -> list[str]:
        ranked = sorted(self.contracts.values(), key=lambda c: c.turnover_24h, reverse=True)
        return [c.symbol for c in ranked[:n]]
