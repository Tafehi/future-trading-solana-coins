"""Telegram for the wallet tracer: one summary message per run (user, 2026-11-17, D-27).

Checked 2026-11-17 against core.telegram.org/bots/api: `sendMessage` takes `chat_id` and `text` (1-4096 characters
after entity parsing) and `parse_mode` (HTML here, so every value goes through `esc`). Same method and limits the
removed bot's `notifications/telegram.py` used for months.

Rules:
- Short addresses only (`0x8747…666b`): the chat is a third-party service, and full addresses stay in `audit/` (PLAN
  §8 spirit). The message names the local report path instead.
- The token travels in the URL path (`/bot<token>/`), so no URL and no exception text is ever logged or raised: a
  failure reports the HTTP status or the exception TYPE only (as DEBUG D-10 for the RPC URL).
- `send` never raises: a Telegram failure must not lose the trace or the local report.
"""

import html
from collections import Counter

import httpx

from app.tools.wallet_trace import links
from app.tools.wallet_trace.classify import INCOMPLETE, LIKELY_BOT, classify
from app.tools.wallet_trace.model import TwinScore
from app.tools.wallet_trace.walk import WalkResult

API_BASE = "https://api.telegram.org"
MAX_TEXT = 4096
TIMEOUT_S = 15.0
NAME = "Wallet tracer"


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def summary(
    res: WalkResult, scores: list[TwinScore], *, chain: str, starts: list[str], calls: int, cache_hits: int, report: str
) -> str:
    """The per-run summary (HTML). Pure, so the exact text is tested."""
    labels = Counter(classify(p).label for p in res.profiles.values())
    lines = [f"<b>[{esc(NAME)}] {esc(chain.upper())} trace</b>",
             f"Start: {esc(', '.join(links.short(s) for s in starts))}",
             f"{len(res.profiles)} wallets, {calls} API calls, {cache_hits} from cache", ""]  # fmt: skip
    lines.append("<b>Verdicts</b>")
    lines += [f"• {esc(k)}: {v}" for k, v in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))]
    bots = [p for p in res.profiles.values() if classify(p).label == LIKELY_BOT]
    lines.append("")
    if bots:
        lines.append(f"<b>⚠️ LIKELY BOT: {len(bots)}</b>")
        lines += [f"• {esc(links.short(p.address))}: {esc(classify(p).reasons[0])}" for p in bots[:10]]
    else:
        lines.append("LIKELY BOT: none")
    groups = links.groups(scores)
    if groups:
        lines += ["", f"<b>Same owner, very likely: {len(groups)} group(s)</b>"]
        lines += [f"• {esc(' + '.join(links.short(a) for a in g))}" for g in groups[:10]]
    incomplete = sum(1 for p in res.profiles.values() if classify(p).label == INCOMPLETE)
    notes = []
    if incomplete:
        notes.append(f"{incomplete} INCOMPLETE (history does not add up)")
    if res.unvisited:
        notes.append(f"call budget used up: {len(res.unvisited)} not visited")
    if res.errors:
        notes.append(f"{len(res.errors)} wallet(s) failed")
    if notes:
        lines += ["", "Note: " + esc("; ".join(notes))]
    lines += ["", f"Full report (laptop): <code>{esc(report)}</code>"]
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 20] + "\n… (cut)"


async def send(token: str, chat_id: str, text: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Send one message. Returns "" on success, else a short reason with no token or URL in it. Never raises."""
    if not token or not chat_id:
        return "no Telegram token or chat id"
    body = {"chat_id": chat_id, "text": text[:MAX_TEXT], "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
            resp = await client.post(f"{API_BASE}/bot{token}/sendMessage", json=body)
    except httpx.HTTPError as exc:
        return f"Telegram not reached ({type(exc).__name__})"
    if resp.status_code != 200:
        try:
            desc = str(resp.json().get("description", ""))[:120]
        except ValueError:
            desc = ""
        return f"Telegram refused: HTTP {resp.status_code} {desc}".strip()
    return ""
