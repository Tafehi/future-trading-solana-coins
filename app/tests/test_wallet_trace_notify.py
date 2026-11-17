"""Wallet tracer: secrets from AWS Secrets Manager and the per-run Telegram summary (user, 2026-11-17, D-27).

No AWS and no network: Secrets Manager is a fake client, Telegram an httpx.MockTransport. Tokens below are placeholders.
"""

import json
from typing import Any

import httpx
import pytest

from app.tools.wallet_trace import __main__ as cli
from app.tools.wallet_trace import cache as cache_mod
from app.tools.wallet_trace import secrets as sm
from app.tools.wallet_trace import telegram as tg
from app.tools.wallet_trace import walk
from app.tools.wallet_trace.links import Clue, TwinScore
from app.tools.wallet_trace.model import Profile

TOKEN = "123:PLACEHOLDER"
W = "0x" + "ab" * 19 + "01"
X = "0x" + "ab" * 19 + "02"


class FakeSecrets:
    def __init__(self, value: dict[str, str] | None = None, error: Exception | None = None) -> None:
        self.value, self.error = value, error
        self.asked: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803 (boto3's own argument name)
        self.asked.append(SecretId)
        if self.error:
            raise self.error
        return {"SecretString": json.dumps(self.value)}


SECRET = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "-100", "SOLANA_RPC_URL": "https://n.example/KEY",
          "KUCOIN_API_KEY": "PLACEHOLDER"}  # fmt: skip


def test_secret_gives_telegram_and_rpc_and_ignores_the_rest() -> None:
    fake = FakeSecrets(SECRET)
    s = sm.load(env={}, client=fake)
    assert fake.asked == ["solana-future-bot"]
    assert (s.telegram_token, s.telegram_chat_id, s.solana_rpc_url) == (TOKEN, "-100", "https://n.example/KEY")
    assert s.telegram and s.source == "secrets-manager:solana-future-bot" and not s.problem
    assert "KUCOIN" not in repr(s)


def test_env_wins_and_unset_counts_as_missing() -> None:
    s = sm.load(env={"TELEGRAM_BOT_TOKEN": "999:ENV", "TELEGRAM_CHAT_ID": "unset"}, client=FakeSecrets(SECRET))
    assert s.telegram_token == "999:ENV" and s.telegram_chat_id == "-100"
    assert s.source == "env+secrets-manager:solana-future-bot"


def test_env_alone_never_calls_aws() -> None:
    fake = FakeSecrets(SECRET)
    full = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "1", "SOLANA_RPC_URL": "https://e.example/"}
    assert sm.load(env=full, client=fake).source == "env" and fake.asked == []
    assert sm.load(env={}, use_aws=False, client=fake).source == "none" and fake.asked == []


def test_a_failed_secret_read_is_not_fatal_and_leaks_nothing() -> None:
    s = sm.load(env={}, client=FakeSecrets(error=RuntimeError(f"AccessDenied for {TOKEN}")))
    assert not s.telegram and not s.solana_rpc_url
    assert "RuntimeError" in s.problem and TOKEN not in s.problem


def test_a_secret_that_is_not_a_json_object_is_refused() -> None:
    class Bad(FakeSecrets):
        def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803
            return {"SecretString": "[1, 2]"}

    assert "ValueError" in sm.load(env={}, client=Bad()).problem


def _result() -> tuple[walk.WalkResult, list[TwinScore]]:
    res = walk.WalkResult()
    bot = Profile(address=W, chain="hl", fills=9000, orders=400, active_days=40, perp_fills=9000, perp_volume=1e6)
    res.profiles = {W: bot, X: Profile(address=X, chain="hl")}
    res.unvisited = ["0x" + "cd" * 20]
    scores = [TwinScore(W, X, [Clue("same funder", 3, "e"), Clue("sub-account / agent of the same master", 5, "e")])]
    return res, scores


def test_summary_text() -> None:
    res, scores = _result()
    text = tg.summary(res, scores, chain="hl", starts=[W], calls=12, cache_hits=3, report="/x/audit/r/report.md")
    assert text.startswith("<b>[Wallet tracer] HL trace</b>")
    assert "2 wallets, 12 API calls, 3 from cache" in text
    assert "• HOLDER: 1" in text and "• LIKELY BOT: 1" in text and "⚠️ LIKELY BOT: 1" in text
    assert "Same owner, very likely: 1 group(s)" in text and "call budget used up: 1 not visited" in text
    assert W not in text and "0xabab…ab01" in text  # short forms only: full addresses stay in audit/
    assert "<code>/x/audit/r/report.md</code>" in text and len(text) <= tg.MAX_TEXT


def test_summary_escapes_html_and_respects_the_length_limit() -> None:
    res = walk.WalkResult()
    res.profiles = {f"0x{i:040x}": Profile(address=f"0x{i:040x}", chain="hl") for i in range(5)}
    text = tg.summary(res, [], chain="sol", starts=["<b>"], calls=0, cache_hits=0, report="a" * 5000)
    assert "&lt;b&gt;" in text and len(text) <= tg.MAX_TEXT


async def test_send_posts_html_to_the_bot_api() -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    assert await tg.send(TOKEN, "-100", "<b>hi</b>", transport=httpx.MockTransport(h)) == ""
    body = json.loads(seen[0].content)
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert body == {"chat_id": "-100", "text": "<b>hi</b>", "parse_mode": "HTML", "disable_web_page_preview": True}


async def test_send_failures_never_raise_and_never_show_the_token() -> None:
    refused = httpx.MockTransport(lambda r: httpx.Response(400, json={"description": "chat not found"}))
    out = await tg.send(TOKEN, "-1", "x", transport=refused)
    assert out == "Telegram refused: HTTP 400 chat not found"

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}")

    out = await tg.send(TOKEN, "-1", "x", transport=httpx.MockTransport(boom))
    assert out == "Telegram not reached (ConnectError)" and TOKEN not in out
    assert await tg.send("", "-1", "x") == "no Telegram token or chat id"


async def test_a_run_sends_one_summary(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end on fakes: one trace -> report on disk -> exactly one Telegram message."""
    monkeypatch.setattr(cache_mod, "AUDIT", tmp_path)
    sent: list[dict[str, Any]] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.telegram.org":
            sent.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True})
        body = json.loads(req.content)
        answer: Any = (
            {"role": "user"}
            if body["type"] == "userRole"
            else {}
            if body["type"]
            in ("spotMeta", "allMids", "clearinghouseState", "spotClearinghouseState", "delegatorSummary")
            else []
        )
        return httpx.Response(200, json=answer)

    a = cli.parse([W, "--depth", "0", "--out", str(tmp_path / "wallet_trace")])
    sec = sm.Secrets(TOKEN, "-100", "", "env")
    path = await cli.run(a, transport=httpx.MockTransport(h), sec=sec)
    assert path.exists() and len(sent) == 1 and "HOLDER: 1" in sent[0]["text"]
    sent.clear()
    a2 = cli.parse([W, "--depth", "0", "--out", str(tmp_path / "wallet_trace"), "--no-telegram"])
    await cli.run(a2, transport=httpx.MockTransport(h), sec=sec)
    assert sent == []
