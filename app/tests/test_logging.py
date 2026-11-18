"""Log redaction: no credential that travels inside a URL may reach a log line (app/utils/logging.py).

Keys below are placeholders, never real-looking strings (CLAUDE.md rule 1, scripts/checks.py).
"""

import logging

from app.utils.logging import RedactSecrets, redact


def test_redact_hides_the_chainstack_key_in_the_rpc_url() -> None:
    url = "https://solana-mainnet.core.chainstack.com/PLACEHOLDERKEY"
    out = redact(f"POST {url} failed")
    assert "PLACEHOLDERKEY" not in out
    assert "chainstack.com/***" in out


def test_redact_hides_the_other_url_secrets() -> None:
    assert redact("https://api.telegram.org/bot123:PLACEHOLDER/sendMessage") == (
        "https://api.telegram.org/bot***/sendMessage"
    )
    assert redact("https://hc-ping.com/PLACEHOLDER-UUID") == "https://hc-ping.com/***"
    assert redact("https://x.io/?api-key=PLACEHOLDER&a=1") == "https://x.io/?api-key=***&a=1"


def test_redact_leaves_plain_text_alone() -> None:
    text = "wallet_trace: 11 wallets, 190 API calls"
    assert redact(text) == text


def test_redact_filter_cleans_log_arguments() -> None:
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "rpc %s", ("https://n.core.chainstack.com/PLACEHOLDERKEY",), None
    )
    assert RedactSecrets().filter(record) is True
    assert "PLACEHOLDERKEY" not in record.getMessage()
