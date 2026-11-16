#!/usr/bin/env python3
"""Repo checks that no linter covers. Run by precommit.sh and CI. Each check has a `--self-test` that proves it can
fail (a check that has never failed proves nothing).

  python3 scripts/checks.py                 run every check; exit code = number of failures
  python3 scripts/checks.py --self-test     feed each check a known-bad input; exit 1 if any check misses it

Checks:
- user-data-size : gzip(infra/user_data.sh) must stay under EC2's 16 KB user-data limit (with a margin)
- heredocs       : every bash heredoc written by user_data.sh / hostSetup.sh parses (`bash -n`); unit files skipped
- no-secrets     : no tracked text file holds something shaped like a key, token or private key
- claude-size    : CLAUDE.md under 40,000 characters (the harness refuses to load it above that)
- no-environment : no workflow job sets `environment:` (aws-foundation's OIDC roles refuse that subject)
- fixture-keys   : test fixtures hold no free-standing key-like strings: no base58 key (32-44 chars) except the public
                   program ids in ALLOWED_PUBLIC_KEYS, no base58 signature (80-90 chars), and nowhere in the repo a
                   64-hex string (it looks exactly like an Ethereum private key). GitGuardian flagged a public Solana
                   address in a fixture's account list on 2026-11-13 (DEBUG D-6).
- source-not-ignored : no source file (.py .sh .tf .toml .json .yml .md under the repo) is matched by .gitignore —
                   a bare `data/` once kept the whole app/data/ package out of git while every local test passed
                   (DEBUG D-4)
- evidence-read-only : every AWS call in scripts/collect_evidence.sh only reads (sts get-caller-identity, s3 ls,
                   s3 cp FROM s3://); the operator runs it with full rights, so a write there would be real
- host-guards    : the box keeps two guards (2026-11-16): the update timer retries a FAILED bot start, and the host
                   sync re-runs hostSetup.sh while sshd is not masked (DEBUG D-7)
"""

import gzip
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_DATA_LIMIT = 16_384
USER_DATA_MARGIN = 2_048

SECRET_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(
        r"(?i)(api[_-]?secret|passphrase|api[_-]?key)\s*[=:]\s*['\"][A-Za-z0-9+/=_-]{16,}['\"]"
    ),
    re.compile(
        r"https://[a-z0-9-]+\.core\.chainstack\.com/[0-9a-f]{20,}"
    ),  # Chainstack URL with key
]
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".terraform",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}
TEXT_SUFFIXES = {
    ".py",
    ".sh",
    ".tf",
    ".toml",
    ".yml",
    ".yaml",
    ".md",
    ".json",
    ".txt",
    ".cfg",
    "",
}


def check_user_data(text: bytes) -> list[str]:
    size = len(gzip.compress(text))
    if size > USER_DATA_LIMIT - USER_DATA_MARGIN:
        return [
            f"user_data.sh gzips to {size} bytes; keep it under {USER_DATA_LIMIT - USER_DATA_MARGIN}"
        ]
    return []


HEREDOC = re.compile(r"cat >(\S+) <<'EOF'\n(.*?)\nEOF\n", re.DOTALL)


def check_heredocs(text: str, name: str) -> list[str]:
    errors = []
    for target, body in HEREDOC.findall(text):
        if not body.startswith("#!/bin/bash"):
            continue  # systemd units and other non-shell files
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(body + "\n")
        res = subprocess.run(
            ["bash", "-n", f.name], check=False, capture_output=True, text=True
        )
        Path(f.name).unlink()
        if res.returncode != 0:
            errors.append(
                f"{name}: heredoc for {target} does not parse: {res.stderr.strip()}"
            )
    return errors


def check_secrets(text: str, name: str) -> list[str]:
    return [
        f"{name}: looks like a secret ({p.pattern[:30]}...)"
        for p in SECRET_PATTERNS
        if p.search(text)
    ]


def check_claude(text: str) -> list[str]:
    return (
        [f"CLAUDE.md is {len(text)} characters; keep it under 40,000"]
        if len(text) >= 40_000
        else []
    )


def check_no_environment(text: str, name: str) -> list[str]:
    return (
        [f"{name}: a job sets `environment:`; the OIDC roles refuse it"]
        if re.search(r"^\s+environment:", text, re.MULTILINE)
        else []
    )


ALLOWED_PUBLIC_KEYS = {
    "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"
}  # Jupiter Perps program id (public)
B58_KEY = re.compile(r'"([1-9A-HJ-NP-Za-km-z]{32,44})"')
B58_SIG = re.compile(r'"[1-9A-HJ-NP-Za-km-z]{80,90}"')
HEX64 = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")


def check_fixture_keys(text: str, name: str) -> list[str]:
    out = []
    keys = sorted({k for k in B58_KEY.findall(text) if k not in ALLOWED_PUBLIC_KEYS})
    if keys:
        out.append(
            f"{name}: {len(keys)} free-standing base58 key(s), e.g. {keys[0][:6]}... (use a placeholder)"
        )
    if B58_SIG.search(text):
        out.append(f"{name}: a base58 transaction signature (use a placeholder)")
    return out


def check_hex64(text: str, name: str) -> list[str]:
    return (
        [f"{name}: a 64-hex string (looks like a private key; use a placeholder)"]
        if HEX64.search(text)
        else []
    )


SOURCE_SUFFIXES = {
    ".py",
    ".sh",
    ".tf",
    ".toml",
    ".json",
    ".yml",
    ".yaml",
    ".md",
    ".hcl",
}


EVIDENCE_OK = re.compile(r'"\$AWS" (--version|sts get-caller-identity|s3 ls |s3 cp "s3://)')


def check_evidence_read_only(text: str, name: str) -> list[str]:
    errors = []
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(r'"\$(AWS|c)"\s+-*[a-z]', line):  # an invocation of the CLI, not an assignment or a test
            if '"$c" --version' in line:
                continue
            if not EVIDENCE_OK.search(line):
                errors.append(f"evidence-read-only: {name}:{i} is not a read-only AWS call: {line.strip()[:100]}")
        elif re.search(r"(^|[\s;(])aws\s+(s3|s3api|sts|secretsmanager|ec2|iam)\b", line):
            errors.append(f"evidence-read-only: {name}:{i} calls aws directly (use \"$AWS\"): {line.strip()[:100]}")
    return errors


# No FRESH_START guard here: this bot is new and starts empty by design (hostSetup.sh restore comment).
HOST_GUARDS = {
    "hostSetup.sh": ["systemctl is-failed --quiet solana-future-coin"],
    "infra/user_data.sh": ["systemctl is-enabled sshd 2>/dev/null | grep -q masked || RERUN=1"],
}


def check_host_guards(texts: dict[str, str]) -> list[str]:
    return [
        f"host-guards: {name} lost {needle!r}"
        for name, needles in HOST_GUARDS.items()
        for needle in needles
        if needle not in texts.get(name, "")
    ]


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
    return [
        f"{p}: source file is git-ignored (fix .gitignore)"
        for p in ignored_sources(paths)
    ]


def source_paths() -> list[str]:
    out = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if (
            p.is_file()
            and not set(rel.parts) & SKIP_DIRS
            and p.suffix in SOURCE_SUFFIXES
            and rel.parts[0] != ".claude"
        ):
            out.append(str(rel))
    return out


def files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if (
            p.is_file()
            and not set(p.relative_to(ROOT).parts) & SKIP_DIRS
            and p.suffix in TEXT_SUFFIXES
        ):
            out.append(p)
    return out


def run() -> int:
    errors: list[str] = []
    ud = ROOT / "infra" / "user_data.sh"
    errors += check_user_data(ud.read_bytes())
    for name in ("infra/user_data.sh", "hostSetup.sh"):
        errors += check_heredocs((ROOT / name).read_text(), name)
    for p in files():
        if p.name == "checks.py":
            continue
        try:
            errors += check_secrets(
                p.read_text(errors="ignore"), str(p.relative_to(ROOT))
            )
        except OSError:
            continue
    claude = ROOT / "CLAUDE.md"
    if claude.exists():
        errors += check_claude(claude.read_text())
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        errors += check_no_environment(wf.read_text(), str(wf.relative_to(ROOT)))
    errors += check_source_not_ignored(source_paths())
    ev = ROOT / "scripts" / "collect_evidence.sh"
    errors += check_evidence_read_only(ev.read_text(), "scripts/collect_evidence.sh")
    errors += check_host_guards({n: (ROOT / n).read_text() for n in HOST_GUARDS})
    for f in sorted((ROOT / "app" / "tests" / "fixtures").glob("*.json")):
        errors += check_fixture_keys(f.read_text(), str(f.relative_to(ROOT)))
    for p in files():
        if p.name == "checks.py":
            continue
        try:
            errors += check_hex64(
                p.read_text(errors="ignore"), str(p.relative_to(ROOT))
            )
        except OSError:
            continue
    for e in errors:
        print("FAIL", e)
    print(f"checks: {len(errors)} failure(s)")
    return len(errors)


def self_test() -> int:
    missed = []
    import os

    if not check_user_data(os.urandom(20_000)):  # random bytes do not compress
        missed.append("user-data-size")
    if not check_heredocs("cat >/x <<'EOF'\n#!/bin/bash\nif then\nEOF\n", "t"):
        missed.append("heredocs")
    if not check_secrets("TELEGRAM_BOT_TOKEN=123456789:" + "A" * 35, "t"):
        missed.append("no-secrets")
    if not check_claude("x" * 40_000):
        missed.append("claude-size")
    if not check_no_environment("jobs:\n  a:\n    environment: prod\n", "t"):
        missed.append("no-environment")
    fake_key = (
        "Fixture" + "x" * 37
    )  # 44 base58 characters, built here so no key-like literal sits in the repo
    if not check_fixture_keys('{"k": ["' + fake_key + '"]}', "t"):
        missed.append("fixture-keys")
    if check_fixture_keys('{"p": "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"}', "t"):
        missed.append("fixture-keys allow-list")
    if not check_hex64("hash 0x" + "ab" * 32, "t"):
        missed.append("hex64")
    if not check_evidence_read_only('"$AWS" s3 cp "$OUT/x" "s3://bucket/x"\n', "t"):
        missed.append("evidence-read-only (upload)")
    if not check_evidence_read_only('"$AWS" s3 rm "s3://bucket/x"\n', "t"):
        missed.append("evidence-read-only (delete)")
    if check_evidence_read_only('"$AWS" s3 cp "s3://b/k" "$OUT/k"\n"$AWS" s3 ls "s3://b/"\n', "t"):
        missed.append("evidence-read-only allows a download")
    if len(check_host_guards({})) != sum(len(v) for v in HOST_GUARDS.values()):
        missed.append("host-guards")
    if "app/data/kucoin.py" in source_paths() and not _caught_with_pattern(
        "data/", "app/data/kucoin.py"
    ):
        missed.append("source-not-ignored")
    print(
        "self-test:",
        "every check caught its bad input" if not missed else f"MISSED {missed}",
    )
    return 1 if missed else 0


def _caught_with_pattern(pattern: str, path: str) -> bool:
    """Would `pattern` in a .gitignore hide `path`? Checked in a throw-away repo, so the real .gitignore is untouched."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        Path(d, ".gitignore").write_text(pattern + "\n")
        res = subprocess.run(
            ["git", "check-ignore", "--no-index", path],
            check=False,
            cwd=d,
            capture_output=True,
            text=True,
        )
        return res.returncode == 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else run())
