"""The trader's copy list (D-30): the scout's tracked + watched wallets, held in memory and refreshed from scout.db
once a minute (`app/db/crossread.copy_list`). Nothing is read from the scout during a trade.

- Only wallets on the CURRENT list may open (a wallet that left the list is refused NOT_TRACKED; its open copies keep
  their exits). The sister repo's trader kept acting on dropped wallets' opens — not here.
- The list's age is the scout's last PICK (`jup:picked_at`), not our last read: a scout that still answers but no
  longer picks shows as a stale list. Stale (or never picked) -> new opens are refused LIST_STALE; exits continue.
- `owners_ranked`: tracked first, then watched, each by the scout's grade score; the websocket subscribes in this
  order up to its cap.
"""

from dataclasses import dataclass, field

from app.config import settings
from app.db.crossread import CopyListRead, Entry


@dataclass
class CopyList:
    entries: dict[str, Entry] = field(default_factory=dict)
    picked_at: float | None = None
    scout_fed_at: float | None = None
    read_at: float | None = None  # our last successful read

    def replace(self, read: CopyListRead, *, now: float) -> tuple[set[str], set[str]]:
        """Take a fresh read. Returns (added, dropped) addresses."""
        new = {e.address: e for e in read.entries}
        added, dropped = set(new) - set(self.entries), set(self.entries) - set(new)
        self.entries, self.picked_at, self.scout_fed_at, self.read_at = new, read.picked_at, read.scout_fed_at, now
        return added, dropped

    def get(self, address: str) -> Entry | None:
        return self.entries.get(address)

    def owners_ranked(self) -> list[str]:
        return [
            e.address for e in sorted(self.entries.values(), key=lambda e: (not e.tracked, -e.rank_score, e.address))
        ]

    def age(self, now: float) -> float | None:
        return None if self.picked_at is None else now - self.picked_at

    def stale(self, now: float) -> bool:
        age = self.age(now)
        return age is None or age > settings.copylist_max_age_s

    @property
    def tracked(self) -> int:
        return sum(1 for e in self.entries.values() if e.tracked)
