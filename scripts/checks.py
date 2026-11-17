#!/usr/bin/env python3
"""Repo checks that no linter covers. Run by precommit.sh and verify_clone.sh. Each check has a `--self-test` that
proves it can fail (a check that has never failed proves nothing).

  python3 scripts/checks.py                 run every check; exit code = number of failures
  python3 scripts/checks.py --self-test     feed each check a known-bad input; exit 1 if any check misses it

Checks:
- no-secrets     : no tracked text file holds something shaped like a key, token or private key
- claude-size    : CLAUDE.md under 40,000 characters (the harness refuses to load it above that)
- fixture-keys   : test fixtures hold no free-standing key-like strings: no base58 key (32-44 chars) except the public
                   program ids / mints in ALLOWED_PUBLIC_KEYS, no base58 signature (80-90 chars) (DEBUG D-6)
- hex64          : nowhere in the repo a 64-hex string (it looks exactly like an Ethereum private key; trade hashes
                   are deleted from fixtures, DEBUG D-6)
- case-addresses : no file outside audit/ names a real wallet of the case study (audit/*/wallets.txt and
                   sol_wallets.txt): reports and fixtures must stay anonymised (PLAN §8). Skipped on a fresh clone,
                   where audit/ does not exist
- source-not-ignored : no source file (.py .sh .toml .json .yml .md under the repo) is matched by .gitignore — a bare
                   `data/` once kept the whole app/data/ package out of git while every local test passed (DEBUG D-4)
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SECRET_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"(?i)(api[_-]?secret|passphrase|api[_-]?key)\s*[=:]\s*['\"][A-Za-z0-9+/=_-]{16,}['\"]"),
    re.compile(r"https://[a-z0-9-]+\.core\.chainstack\.com/[0-9a-f]{20,}"),  # Chainstack URL with key
]
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "audit",  # real case data and tracer output (git-ignored, never committed)
}
TEXT_SUFFIXES = {".py", ".sh", ".toml", ".yml", ".yaml", ".md", ".json", ".txt", ".cfg", ""}
SOURCE_SUFFIXES = {".py", ".sh", ".toml", ".json", ".yml", ".yaml", ".md"}

# PUBLIC program ids and mints only (each checked executable / a mint on mainnet, 2026-11-17; wallet_trace/sol_source.py).
ALLOWED_PUBLIC_KEYS = {
    "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu",  # Jupiter Perps
    "11111111111111111111111111111111",  # System program
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # Raydium LaunchLab
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter aggregator v6
    "So11111111111111111111111111111111111111112",  # wrapped SOL mint
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC mint
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT mint
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022 program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account program
    "ComputeBudget111111111111111111111111111111",  # Compute Budget program
}
B58_KEY = re.compile(r'"([1-9A-HJ-NP-Za-km-z]{32,44})"')
B58_SIG = re.compile(r'"[1-9A-HJ-NP-Za-km-z]{80,90}"')
HEX64 = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}|(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{43,44}(?![1-9A-HJ-NP-Za-km-z])")


def check_secrets(text: str, name: str) -> list[str]:
    return [f"{name}: looks like a secret ({p.pattern[:30]}...)" for p in SECRET_PATTERNS if p.search(text)]


def check_claude(text: str) -> list[str]:
    return [f"CLAUDE.md is {len(text)} characters; keep it under 40,000"] if len(text) >= 40_000 else []


def check_fixture_keys(text: str, name: str) -> list[str]:
    out = []
    keys = sorted({k for k in B58_KEY.findall(text) if k not in ALLOWED_PUBLIC_KEYS})
    if keys:
        out.append(f"{name}: {len(keys)} free-standing base58 key(s), e.g. {keys[0][:6]}... (use a placeholder)")
    if B58_SIG.search(text):
        out.append(f"{name}: a base58 transaction signature (use a placeholder)")
    return out


def check_hex64(text: str, name: str) -> list[str]:
    return [f"{name}: a 64-hex string (looks like a private key; use a placeholder)"] if HEX64.search(text) else []


def case_addresses() -> set[str]:
    """Every real wallet address listed in the case files under audit/ (empty on a fresh clone)."""
    out: set[str] = set()
    for f in list((ROOT / "audit").glob("*/wallets.txt")) + list((ROOT / "audit").glob("*/sol_wallets.txt")):
        out |= {a.lower() if a.startswith("0x") else a for a in ADDRESS.findall(f.read_text())}
    return out


def check_case_addresses(text: str, name: str, real: set[str]) -> list[str]:
    found = {a.lower() if a.startswith("0x") else a for a in ADDRESS.findall(text)} & real
    return [f"{name}: names {len(found)} real case wallet(s), e.g. {sorted(found)[0][:6]}… (anonymise)"] if found else []


def ignored_sources(paths: list[str]) -> list[str]:
    """Paths (relative to ROOT) that .gitignore matches although they are source. Uses git itself."""
    if not paths:
        return []
    res = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        check=False,
        cwd=ROOT,
        input="\n".join(paths),
        capture_output=True,
        text=True,
    )
    return [line for line in res.stdout.splitlines() if line.strip()]


def check_source_not_ignored(paths: list[str]) -> list[str]:
    return [f"{p}: source file is git-ignored (fix .gitignore)" for p in ignored_sources(paths)]


def source_paths() -> list[str]:
    out = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if p.is_file() and not set(rel.parts) & SKIP_DIRS and p.suffix in SOURCE_SUFFIXES and rel.parts[0] != ".claude":
            out.append(str(rel))
    return out


def files() -> list[Path]:
    return [
        p
        for p in ROOT.rglob("*")
        if p.is_file() and not set(p.relative_to(ROOT).parts) & SKIP_DIRS and p.suffix in TEXT_SUFFIXES
    ]


def run() -> int:
    errors: list[str] = []
    real = case_addresses()
    for p in files():
        if p.name == "checks.py":
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        name = str(p.relative_to(ROOT))
        errors += check_secrets(text, name)
        errors += check_hex64(text, name)
        errors += check_case_addresses(text, name, real)
    claude = ROOT / "CLAUDE.md"
    if claude.exists():
        errors += check_claude(claude.read_text())
    errors += check_source_not_ignored(source_paths())
    for f in sorted((ROOT / "app" / "tests" / "fixtures").glob("*.json")):
        errors += check_fixture_keys(f.read_text(), str(f.relative_to(ROOT)))
    for e in errors:
        print("FAIL", e)
    print(f"checks: {len(errors)} failure(s){'' if real else ' (case-addresses skipped: no audit/ case files)'}")
    return len(errors)


def self_test() -> int:
    missed = []
    if not check_secrets("TELEGRAM_BOT_TOKEN=123456789:" + "A" * 35, "t"):
        missed.append("no-secrets")
    if not check_claude("x" * 40_000):
        missed.append("claude-size")
    fake_key = "Fixture" + "x" * 37  # 44 base58 characters, built here so no key-like literal sits in the repo
    if not check_fixture_keys('{"k": ["' + fake_key + '"]}', "t"):
        missed.append("fixture-keys")
    if check_fixture_keys('{"p": "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"}', "t"):
        missed.append("fixture-keys allow-list")
    if not check_hex64("hash 0x" + "ab" * 32, "t"):
        missed.append("hex64")
    real = {"0x" + "12" * 20}
    if not check_case_addresses("sent to 0x" + "12" * 20, "t", real) or check_case_addresses("0x" + "ab" * 20, "t", real):
        missed.append("case-addresses")
    if "app/data/http.py" in source_paths() and not _caught_with_pattern("data/", "app/data/http.py"):
        missed.append("source-not-ignored")
    print("self-test:", "every check caught its bad input" if not missed else f"MISSED {missed}")
    return 1 if missed else 0


def _caught_with_pattern(pattern: str, path: str) -> bool:
    """Would `pattern` in a .gitignore hide `path`? Checked in a throw-away repo, so the real .gitignore is untouched."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        Path(d, ".gitignore").write_text(pattern + "\n")
        res = subprocess.run(
            ["git", "check-ignore", "--no-index", path], check=False, cwd=d, capture_output=True, text=True
        )
        return res.returncode == 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else run())
