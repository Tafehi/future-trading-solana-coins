"""Wallet tracer: the Hyperliquid client (paging, cache, budget), the walk, the report, the CLI guard, and the guard
that the repo stays a read-only tool. No network: every source takes an httpx.MockTransport."""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.data.http import DataError
from app.tools.wallet_trace import __main__ as cli
from app.tools.wallet_trace import cache as cache_mod
from app.tools.wallet_trace import hl_source, links, report, walk
from app.tools.wallet_trace import profile as P
from app.tools.wallet_trace.model import Profile

W = "0x" + "ab" * 19 + "01"
X = "0x" + "ab" * 19 + "02"
Y = "0x" + "ab" * 19 + "03"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def audit_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throw-away audit/ root, so tests never write into the real audit folder."""
    monkeypatch.setattr(cache_mod, "AUDIT", tmp_path)
    return tmp_path


def ledger_item(t: int) -> dict[str, Any]:
    return {"time": t, "delta": {"type": "deposit", "usdc": "1"}}


def mock(handler: Any) -> httpx.MockTransport:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=handler(json.loads(req.content)))

    return httpx.MockTransport(h)


async def test_paging_follows_the_last_time_and_drops_border_repeats(audit_tmp: Path) -> None:
    """A full page (500) asks again from its last time; the element at the border is not counted twice."""
    starts: list[int] = []

    def answer(body: dict[str, Any]) -> Any:
        starts.append(body["startTime"])
        if body["startTime"] == 0:
            return [ledger_item(t) for t in range(1, 501)]
        return [ledger_item(t) for t in range(500, 520)]

    src = hl_source.HlSource(
        cache_mod.Cache(audit_tmp / "c"), hl_source.CallBudget(10), transport=mock(answer), rps=1000
    )
    out = await src.paged("userNonFundingLedgerUpdates", W, 500)
    await src.close()
    assert starts == [0, 500]
    assert [e["time"] for e in out] == list(range(1, 520))


async def test_a_page_of_one_millisecond_still_moves_on(audit_tmp: Path) -> None:
    starts: list[int] = []

    def answer(body: dict[str, Any]) -> Any:
        starts.append(body["startTime"])
        if body["startTime"] <= 7:  # the API answers the same full page again from startTime 7
            return [{"time": 7, "delta": {"type": "deposit", "usdc": str(i)}} for i in range(500)]
        return []

    src = hl_source.HlSource(
        cache_mod.Cache(audit_tmp / "c"), hl_source.CallBudget(10), transport=mock(answer), rps=1000
    )
    out = await src.paged("userNonFundingLedgerUpdates", W, 500)
    await src.close()
    assert starts == [0, 7, 8] and len(out) == 500


async def test_twap_slices_are_paged_by_their_inner_time(audit_tmp: Path) -> None:
    starts: list[int] = []

    def answer(body: dict[str, Any]) -> Any:
        starts.append(body["startTime"])
        if body["startTime"] == 0:
            return [{"fill": {"time": t, "tid": t}, "twapId": 1} for t in range(1, 2001)]
        return []

    src = hl_source.HlSource(
        cache_mod.Cache(audit_tmp / "c"), hl_source.CallBudget(10), transport=mock(answer), rps=1000
    )
    await src.paged("userTwapSliceFillsByTime", W, 2000)
    await src.close()
    assert starts == [0, 2000]


async def test_a_rerun_costs_zero_calls(audit_tmp: Path) -> None:
    calls = 0

    def answer(body: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return {"role": "user"} if body["type"] == "userRole" else []

    c = cache_mod.Cache(audit_tmp / "c")
    src = hl_source.HlSource(c, hl_source.CallBudget(100), transport=mock(answer), rps=1000)
    await src.fetch(W)
    first = calls
    await src.fetch(W)
    await src.close()
    assert first > 0 and calls == first
    refresh = hl_source.HlSource(cache_mod.Cache(audit_tmp / "c", refresh=True), hl_source.CallBudget(100),
                                 transport=mock(answer), rps=1000)  # fmt: skip
    await refresh.fetch(W)
    await refresh.close()
    assert calls == 2 * first


async def test_call_budget_stops_at_exactly_the_limit(audit_tmp: Path) -> None:
    b = hl_source.CallBudget(2)
    b.spend()
    b.spend()
    with pytest.raises(hl_source.OutOfCalls):
        b.spend()


def test_weights_follow_the_rate_limit_page() -> None:
    assert hl_source._weight("clearinghouseState", 0) == 2
    assert hl_source._weight("userRole", 0) == 60
    assert hl_source._weight("userNonFundingLedgerUpdates", 999) == 20
    assert hl_source._weight("userFillsByTime", 2000) == 120  # 20 + 2000 / 20


async def test_an_http_error_is_a_data_error(audit_tmp: Path) -> None:
    t = httpx.MockTransport(lambda req: httpx.Response(400, text="bad"))
    src = hl_source.HlSource(cache_mod.Cache(audit_tmp / "c"), hl_source.CallBudget(10), transport=t, rps=1000)
    with pytest.raises(DataError):
        await src.query("userRole", W)
    await src.close()


def test_cache_refuses_a_folder_outside_audit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cache_mod.Cache(tmp_path / "elsewhere")


def test_cli_refuses_an_output_folder_outside_audit(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.parse([W, "--out", str(tmp_path)])
    assert cli.parse([W]).out == cli.DEFAULT_OUT


# --- walk ---------------------------------------------------------------------------------------------------------
def _p(addr: str, ins: dict[str, float] | None = None, outs: dict[str, float] | None = None) -> Profile:
    return Profile(address=addr, chain="hl", in_usd=ins or {}, out_usd=outs or {})


async def test_walk_depth_top_stop_list_and_budget() -> None:
    sysaddr = "0x" + "22" * 20
    graph = {
        W: _p(W, ins={X: 100.0, Y: 1.0}, outs={sysaddr: 500.0}),
        X: _p(X, outs={"0x" + "cd" * 20: 5.0}),
        Y: _p(Y),
    }
    fetched: list[str] = []

    async def fetch(a: str) -> Profile:
        fetched.append(a)
        if a not in graph:
            raise hl_source.OutOfCalls("budget")
        return graph[a]

    res = await walk.walk([W], fetch, depth=1, top=1, is_system=hl_source.is_system)
    assert fetched == [W, X]  # top-1 per direction: X in, the system address out is stopped
    assert res.stopped[sysaddr].startswith("system")
    res2 = await walk.walk([W], fetch, depth=2, top=2, is_system=hl_source.is_system)
    assert res2.unvisited == ["0x" + "cd" * 20] and set(res2.profiles) == {W, X, Y}


async def test_walk_stops_at_labels_and_vaults() -> None:
    vault = "0x" + "ef" * 20
    start = _p(W, outs={vault: 10.0, X: 5.0})
    start.vault_addresses.add(vault)

    async def fetch(a: str) -> Profile:
        return start if a == W else _p(a)

    res = await walk.walk([W], fetch, depth=1, top=5, is_system=hl_source.is_system, labels={X: "Some Exchange"})
    assert set(res.profiles) == {W}
    assert res.stopped == {vault: "vault (protocol pool)", X: "label: Some Exchange"}


def test_labels_file(tmp_path: Path) -> None:
    f = tmp_path / "labels.toml"
    f.write_text('[labels]\n"0xABAB" = "Exchange hot wallet"\n')
    assert walk.load_labels(f) == {"0xabab": "Exchange hot wallet"}
    assert walk.load_labels(tmp_path / "missing.toml") == {}


# --- report -------------------------------------------------------------------------------------------------------
def test_report_files_and_contents(audit_tmp: Path) -> None:
    res = walk.WalkResult()
    res.profiles = {W: _p(W, outs={X: 10.0}), X: Profile(address=X, chain="hl", fills=0)}
    res.nodes = {W: walk.Node(W, 0), X: walk.Node(X, 1, W, 10.0, "out")}
    res.unvisited = [Y]
    res.nodes[Y] = walk.Node(Y, 2, X, 1.0, "out")
    path = report.write(audit_tmp / "run", res, links.twins(list(res.profiles.values())), chain="hl", args="t",
                        calls=3, cache_hits=4)  # fmt: skip
    text = path.read_text()
    assert "LIKELY BOT: 0 wallet(s)." in text and "Call budget used up" in text and "Out of scope" in text
    assert (audit_tmp / "run" / "edges.csv").exists() and (audit_tmp / "run" / "tree.txt").read_text().count("\n") == 3
    with pytest.raises(ValueError):
        report.write(REPO / "not-audit", res, [], chain="hl", args="t", calls=0, cache_hits=0)


# --- read-only: the copy-trading bot was removed (2026-11-17, D-26); the repo must stay a research tool ------------
def test_the_tool_imports_with_no_settings_or_secrets() -> None:
    """The tracer runs on a laptop with no env: no bot settings, no secret, no database."""
    code = "import app.tools.wallet_trace.__main__, app.tools.wallet_trace.sol_source; print('ok')"
    argv = [sys.executable, "-c", code]
    out = subprocess.run(argv, cwd=REPO, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "ok"


def test_no_trading_or_bot_code_comes_back() -> None:
    """Nothing places an order or talks to an exchange's private API; the old bot's packages stay gone. Telegram and
    AWS Secrets Manager are allowed since D-27 (user, 2026-11-17), but only in their own modules, and AWS only reads."""
    gone = ["app/main.py", "app/config.py", "app/trading", "app/jobs", "app/core", "app/notifications", "app/record",
            "app/db", "app/api", "infra", ".github/workflows/deploy.yml"]  # fmt: skip
    assert [g for g in gone if (REPO / g).exists()] == []
    words = ("kucoin", "place_order", "/api/v1/orders", '"exchange"', "sign_transaction", "private_key")
    src = [p for p in (REPO / "app").rglob("*.py") if "tests" not in p.parts]
    hits = [f"{p.relative_to(REPO)}: {w}" for p in src for w in words if w in p.read_text().lower()]
    assert hits == []
    aws = sorted(str(p.relative_to(REPO)) for p in src if "boto3" in p.read_text())
    assert aws == ["app/tools/wallet_trace/secrets.py"]
    calls = set(re.findall(r"client\.([a-z_]+)\(", (REPO / "app/tools/wallet_trace/secrets.py").read_text()))
    assert calls == {"get_secret_value"}, calls  # read-only: no put_/create_/delete_ call on AWS


def test_spot_meta_maps_pair_names() -> None:
    meta = P.SpotMeta({"tokens": [{"index": 0, "name": "USDC"}, {"index": 150, "name": "HYPE"}],
                       "universe": [{"name": "@107", "index": 107, "tokens": [150, 0]}]})  # fmt: skip
    assert meta.pairs["@107"] == ("HYPE", "USDC") and meta.price("HYPE", {"@107": "40"}) == 40.0


async def test_a_lasting_429_is_waited_out_then_retried(audit_tmp: Path) -> None:
    """A live depth-2 run drew one 429 that outlasted the HTTP backoff; the client waits and tries again."""
    calls = 0
    slept: list[float] = []

    def h(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls <= 4 else httpx.Response(200, json={"role": "user"})

    async def sleep(s: float) -> None:
        slept.append(s)

    t = httpx.MockTransport(h)
    src = hl_source.HlSource(
        cache_mod.Cache(audit_tmp / "c"), hl_source.CallBudget(5), transport=t, rps=1000, sleep=sleep
    )
    assert await src.query("userRole", W) == {"role": "user"}
    await src.close()
    assert slept == [hl_source.S.HL_429_WAIT_S] and src.budget.used == 1
