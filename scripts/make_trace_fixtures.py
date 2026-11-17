#!/usr/bin/env python3
"""Build the ANONYMISED wallet-tracer fixtures from the case study (PLAN §8). Run by hand, never by CI:

    .venv/bin/python scripts/make_trace_fixtures.py

Reads the tracer's own cache of the case (`audit/wallet-trace-case-2026-11-17/hl_full/`, written by a live run of
`hl_source`; git-ignored, real addresses) and writes `app/tests/fixtures/wallet_trace_hl_case.json`:

- every 40-hex address becomes `0x` + `ab` x 19 + a 2-digit index (same input -> same placeholder, so links still work).
  System addresses (`0x2222…`, `0x2000…`) are public protocol constants and stay as they are;
- every `hash` field is deleted (64-hex: `scripts/checks.py` hex64 rejects them anywhere in the repo), and so is every
  other 64-hex string and `tokenId` / `evmContract` (spotMeta) that is not needed;
- `spotMeta` is cut to the tokens and pairs the case uses; `allMids` to their pairs;
- amounts, times, coins and types are kept.

The roles file maps each placeholder to its case role (holder, twin ...), so the golden test reads like PLAN §2.2.
The run is deterministic: running it twice gives the same file. It FAILS (exit 1) if any 40-hex or 64-hex string that
is not a placeholder or a system address survives.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # `app` imports when run as a script
CASE = ROOT / "audit" / "wallet-trace-case-2026-11-17"
SNAP = CASE / "hl_full" / "hl"
OUT = ROOT / "app" / "tests" / "fixtures" / "wallet_trace_hl_case.json"
SOL_SNAP = CASE / "sol_probe" / "sol"
SOL_WALLETS = CASE / "sol_wallets.txt"
SOL_OUT = ROOT / "app" / "tests" / "fixtures" / "wallet_trace_sol_case.json"
B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
B58_SIG = re.compile(r"[1-9A-HJ-NP-Za-km-z]{80,90}")
SOL_KEEP_TX = 25  # newest parsed transactions kept per wallet (enough for the rules; the rest stays in audit/)
ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
HEX64 = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
PLACEHOLDER = re.compile(r"0x(?:ab){19}[0-9]{2}")

# Case role per address prefix (from wallets.txt; prefixes only, as in PLAN.md).
ROLES = {
    "0x0000": "holder_staker", "0x0028": "holder_xmr1_a", "0x0009": "holder_collect_stake",
    "0x0064": "holder_xmr1_b", "0x006c": "holder_xmr1_c", "0x8747": "funder_b", "0x8f4f": "twin_of_b",
    "0x921e": "cash_out_one_day", "0xf0bd": "slow_seller", "0xc8aa": "pass_through", "0xb9b9": "end_holder",
    "0xe931": "jeff_receiver", "0xf580": "jeff_seller", "0x7e5f": "second_funder", "0x02bb": "one_shot_a",
    "0x029b": "one_shot_b", "0x8738": "one_shot_c", "0x4710": "receiver_of_holder", "0x7c1f": "early_sender",
    "0xc17c": "hype_flipper",
}  # fmt: skip
DROP_KEYS = {"hash", "tokenId", "evmContract", "deployerTradingFeeShare", "fullName", "cloid", "crossed", "startPosition",
             "dir", "twapId", "nonce", "nSamples", "fundingRate", "szi", "entryNtl", "hold", "lockedUntilTimestamp"}
# Queries the profile does not read (kept in the cache for the live tool, not in git).
DROP_QUERIES = {"portfolio"}
# Wallets of the case with no verdict in PLAN §2.2: not needed by the golden test, so not copied into git.
SKIP_ROLES = {"jeff_receiver", "receiver_of_holder", "early_sender", "hype_flipper"}


def is_system(a: str) -> bool:
    a = a.lower()
    return a == "0x" + "22" * 20 or (a.startswith("0x20") and set(a[4:26]) <= {"0"})


class Anon:
    def __init__(self) -> None:
        self.map: dict[str, str] = {}

    def addr(self, a: str) -> str:
        a = a.lower()
        if is_system(a):
            return a
        if a not in self.map:
            n = len(self.map) + 1
            if n > 99:
                raise SystemExit("more than 99 addresses: widen the placeholder scheme")
            self.map[a] = "0x" + "ab" * 19 + f"{n:02d}"
        return self.map[a]

    def walk(self, x: object) -> object:
        if isinstance(x, dict):
            return {self.walk(k) if isinstance(k, str) else k: self.walk(v) for k, v in x.items() if k not in DROP_KEYS}
        if isinstance(x, list):
            return [self.walk(v) for v in x]
        if isinstance(x, str):
            return ADDR.sub(lambda m: self.addr(m.group(0)), x)
        return x


def answer(p: Path) -> object:
    return json.loads(p.read_text())["answer"]


def main() -> int:
    if not SNAP.exists():
        print(f"no case snapshot at {SNAP}: run the tracer once on the case wallets first")
        return 1
    anon = Anon()
    wallets = sorted(d for d in SNAP.iterdir() if d.is_dir() and d.name != "_global"
                     and (d / "userNonFundingLedgerUpdates.json").exists())  # fmt: skip
    wallets = [d for d in wallets if ROLES.get(d.name[:6], "other") not in SKIP_ROLES]
    for d in wallets:  # stable numbering: the case wallets first, in sorted order
        anon.addr(d.name)
    out_wallets: dict[str, dict[str, object]] = {}
    roles: dict[str, str] = {}
    coins: set[str] = set()
    tokens: set[str] = set()
    for d in wallets:
        ans = {f.stem: answer(f) for f in sorted(d.glob("*.json")) if f.stem not in DROP_QUERIES}
        vaults = {str(e["delta"].get("vault") or "").lower() for e in ans.get("userNonFundingLedgerUpdates") or []}
        details = {v: answer(SNAP / v / "vaultDetails.json") for v in sorted(vaults) if (SNAP / v / "vaultDetails.json").exists()}
        if details:
            ans["vaultDetails"] = details
        for f in list(ans.get("userFillsByTime") or []) + [x["fill"] for x in ans.get("userTwapSliceFillsByTime") or []]:
            coins.add(str(f["coin"]))
            tokens.add(str(f.get("feeToken") or ""))
        for e in ans.get("userNonFundingLedgerUpdates") or []:
            tokens.add(str(e["delta"].get("token") or ""))
        for b in (ans.get("spotClearinghouseState") or {}).get("balances") or []:
            tokens.add(str(b["coin"]))
        ph = anon.addr(d.name)
        out_wallets[ph] = anon.walk(ans)  # type: ignore[assignment]
        roles[ph] = ROLES.get(d.name[:6], "other")
    meta = answer(SNAP / "_global" / "spotMeta.json")
    mids = answer(SNAP / "_global" / "allMids.json")
    assert isinstance(meta, dict) and isinstance(mids, dict)
    by_index = {int(t["index"]): t for t in meta["tokens"]}
    keep_pairs = [u for u in meta["universe"] if u["name"] in coins or f"@{u['index']}" in coins
                  or by_index[u["tokens"][0]]["name"] in tokens]  # fmt: skip
    keep_tokens = sorted({i for u in keep_pairs for i in u["tokens"]} | {int(t["index"]) for t in meta["tokens"]
                                                                        if t["name"] in tokens})  # fmt: skip
    small_meta = {"universe": keep_pairs, "tokens": [by_index[i] for i in keep_tokens]}
    pair_names = {u["name"] for u in keep_pairs} | {f"@{u['index']}" for u in keep_pairs}
    small_mids = {k: v for k, v in mids.items() if k in pair_names}
    doc = {
        "note": "Anonymised Hyperliquid case study 2026-11-17 (PLAN §2), built by scripts/make_trace_fixtures.py. "
        "Placeholders 0xabab..NN stand for real wallets; system addresses are public constants. No hashes.",
        "roles": roles,
        "spotMeta": anon.walk(small_meta),
        "allMids": small_mids,
        "wallets": out_wallets,
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"
    leftovers = sorted({a for a in ADDR.findall(text) if not PLACEHOLDER.fullmatch(a) and not is_system(a)})
    if leftovers or HEX64.search(text):
        print(f"FAIL: {len(leftovers)} real address(es) or a 64-hex string survived; nothing written")
        return 1
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(out_wallets)} wallets, {len(anon.map)} placeholders, {len(text):,} bytes")
    return 0


class SolAnon:
    """Base58 keys -> `fixture-key-NNN` (the same input gets the same placeholder; the hyphens keep it from looking like
    a key to `checks.py fixture-keys`). Public program ids and mints in `ALLOWED_PUBLIC_KEYS` stay; signatures go."""

    def __init__(self, public: set[str]) -> None:
        self.public = public
        self.map: dict[str, str] = {}

    def key(self, k: str) -> str:
        if k in self.public:
            return k
        if k not in self.map:
            n = len(self.map) + 1
            self.map[k] = f"fixture-key-{n:03d}"
        return self.map[k]

    def walk(self, x: Any) -> Any:
        if isinstance(x, dict):
            drop = {"signatures", "recentBlockhash", "logMessages", "returnData", "rewards"}
            return {k: self.walk(v) for k, v in x.items() if k not in drop}
        if isinstance(x, list):
            return [self.walk(v) for v in x]
        if isinstance(x, str):
            if B58_SIG.fullmatch(x):
                return "sig-removed"
            return B58.sub(lambda m: self.key(m.group(0)), x)
        return x


def sol_main() -> int:
    import dataclasses

    from app.data import jupiter_events as je
    from app.tools.wallet_trace.sol_source import JUPITER_PERPS, QUOTES, VENUES

    sys.path.insert(0, str(ROOT / "scripts"))
    from checks import ALLOWED_PUBLIC_KEYS, check_fixture_keys  # type: ignore[import-not-found]

    if not SOL_WALLETS.exists():
        print(f"no Solana probe list at {SOL_WALLETS}: skipped")
        return 0
    public = set(VENUES) | set(QUOTES) | {JUPITER_PERPS} | {"11111111111111111111111111111111"}
    missing = public - set(ALLOWED_PUBLIC_KEYS)
    if missing:
        print(f"FAIL: add these PUBLIC program ids / mints to scripts/checks.py ALLOWED_PUBLIC_KEYS first: {sorted(missing)}")
        return 1
    anon = SolAnon(set(ALLOWED_PUBLIC_KEYS))
    wallets: dict[str, Any] = {}
    roles: dict[str, str] = {}
    for line in SOL_WALLETS.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        role, addr = line.split()
        d = SOL_SNAP / addr
        sigs = []
        for page in sorted(d.glob("signatures_*.json")):
            sigs += answer(page)  # type: ignore[arg-type]
        ok = [s_ for s_ in sigs if s_.get("err") is None]
        have = [s_ for s_ in ok if (SOL_SNAP / "_tx" / f"{s_['signature']}_jsonParsed.json").exists()]
        keep = have[:SOL_KEEP_TX] + [s_ for s_ in have[-3:] if s_ not in have[:SOL_KEEP_TX]]  # newest + the funder's
        parsed = [answer(SOL_SNAP / "_tx" / f"{s_['signature']}_jsonParsed.json") for s_ in keep]
        positions = answer(d / "jup_positions.json") if (d / "jup_positions.json").exists() else []
        # Jupiter events carry the owner INSIDE their bytes, so the fixture holds the DECODED events (the decoder itself
        # is tested on real bytes in test_jupiter_history.py), with owner and position key swapped for placeholders.
        perp_events = []
        for pos in positions or []:  # type: ignore[union-attr]
            pk = str(pos["pubkey"])
            for s_ in answer(SOL_SNAP / pk / "signatures_0.json") or []:  # type: ignore[union-attr]
                f = SOL_SNAP / "_tx" / f"{s_['signature']}_json.json"
                if s_.get("err") is None and f.exists():
                    for ev in je.events_from_transaction(answer(f), JUPITER_PERPS):
                        if ev.owner == addr:
                            perp_events.append({**dataclasses.asdict(ev), "signature": ""})
        bal = answer(d / "balance.json") if (d / "balance.json").exists() else None
        ph = anon.key(addr)
        roles[ph] = role
        wallets[ph] = anon.walk({"n_signatures": len(ok), "history_end": len(sigs) % 1000 != 0, "txs": parsed,
                                 "perp_events": perp_events, "perp_positions": len(positions or []), "balance": bal})  # fmt: skip
    doc = {"note": "Anonymised Solana probe wallets 2026-11-17 (a Jupiter Perps trader, a pump.fun trader), built by "
           "scripts/make_trace_fixtures.py. Keys are Fixture...N<k> placeholders; signatures removed.",
           "roles": roles, "sol_usd": 73.4, "wallets": wallets}  # fmt: skip
    text = json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"
    errors = check_fixture_keys(text, SOL_OUT.name)
    if errors or HEX64.search(text):
        print(f"FAIL: {errors or 'a 64-hex string'}; nothing written")
        return 1
    SOL_OUT.write_text(text)
    print(f"wrote {SOL_OUT.relative_to(ROOT)}: {len(wallets)} wallets, {len(anon.map)} placeholders, {len(text):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main() or sol_main())
