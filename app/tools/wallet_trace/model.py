"""Chain-neutral data types for the wallet tracer. Pure dataclasses; no I/O."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Edge:
    """Value moved between two wallets (summed over every transfer of one token)."""

    src: str
    dst: str
    token: str
    amount: float
    usd: float
    count: int
    first: float  # unix seconds
    last: float


@dataclass(frozen=True)
class Airdrop:
    token: str
    amount: float
    time: float


@dataclass(frozen=True)
class Transfer:
    """One movement in or out of the wallet. `direction` is "in" or "out" seen from the profiled wallet."""

    time: float
    counterparty: str
    token: str
    amount: float
    usd: float
    direction: str
    kind: str  # the source's own type name (spotTransfer, send, internalTransfer, sol, spl ...)


@dataclass
class Profile:
    """The numbers of PLAN §5 for one wallet. Every field has a safe empty value, so a wallet with no history still
    makes a profile (and is a HOLDER)."""

    address: str
    chain: str
    fills: int = 0
    orders: int = 0
    active_days: int = 0
    first: float = 0.0
    last: float = 0.0
    perp_fills: int = 0
    spot_fills: int = 0
    perp_volume: float = 0.0
    spot_volume: float = 0.0
    spot_buy_volume: float = 0.0
    spot_sell_volume: float = 0.0
    funding_events: int = 0
    jup_episodes: int = 0
    sell_orders: int = 0
    round_lot_sells: int = 0
    sell_days: int = 0
    swaps: int = 0
    swap_tokens: int = 0
    exit_door_pairs: int = 0
    trading_perp_moves: int = 0
    deposits_usd: float = 0.0
    withdraws_usd: float = 0.0
    in_usd: dict[str, float] = field(default_factory=dict)
    out_usd: dict[str, float] = field(default_factory=dict)
    transfers: list[Transfer] = field(default_factory=list)
    staked_now: float = 0.0
    stake_lag_minutes: float | None = None
    lifetime_days: float = 0.0
    in_to_exit_days: float | None = None  # first big incoming transfer -> the biggest withdraw after it
    passthrough_share: float = 0.0
    passthrough_lag_minutes: float | None = None
    one_shot: bool = False
    airdrops: list[Airdrop] = field(default_factory=list)
    vaults: list[tuple[str, float]] = field(default_factory=list)  # (vault, first deposit time)
    master: str = ""  # sub-account master or agent owner (Hyperliquid userRole)
    role: str = ""
    subaccounts: list[str] = field(default_factory=list)
    vault_addresses: set[str] = field(default_factory=set)  # counterparties that are vaults (stop the walk there)
    exits: dict[str, float] = field(default_factory=dict)  # where money left the chain: "arbitrum" / "hyperevm" -> USD
    funder: str = ""  # Solana: who paid for the first fee
    complete: bool = True  # False = the history CONTRADICTS the live balance (or the API cut it): verdict INCOMPLETE
    partial: bool = False  # True = only the newest part was read (Solana window): the label stands, marked unsure
    balance_gaps: dict[str, float] = field(default_factory=dict)  # token -> live minus summed (only the bad ones)
    balance_sum: dict[str, float] = field(default_factory=dict)  # token -> summed from history
    balance_live: dict[str, float] = field(default_factory=dict)  # token -> live (spot; USDC = spot + perp value)
    unknown_types: dict[str, int] = field(default_factory=dict)
    fills_capped: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def perp_share(self) -> float:
        total = self.perp_volume + self.spot_volume
        return self.perp_volume / total if total else 0.0

    @property
    def sell_share(self) -> float:
        return self.spot_sell_volume / self.spot_volume if self.spot_volume else 0.0

    @property
    def buy_share(self) -> float:
        return self.spot_buy_volume / self.spot_volume if self.spot_volume else 0.0

    @property
    def round_lot_share(self) -> float:
        return self.round_lot_sells / self.sell_orders if self.sell_orders else 0.0

    @property
    def fills_per_day(self) -> float:
        return self.fills / self.active_days if self.active_days else 0.0


@dataclass(frozen=True)
class Verdict:
    label: str
    reasons: list[str]
    unsure: bool = False  # INCOMPLETE history: the label below it is a best guess

    def line(self) -> str:
        return f"{self.label}{' (unsure)' if self.unsure else ''}: {'; '.join(self.reasons)}"


@dataclass(frozen=True)
class Clue:
    kind: str
    points: int
    evidence: str


@dataclass
class TwinScore:
    a: str
    b: str
    clues: list[Clue] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(c.points for c in self.clues)
