"""Logging setup.

- Idempotent: `_configured` stops a second setup adding a duplicate handler (every line would double).
- The file handler fails soft: if the log file cannot be written, log to stdout only and keep running.
- No credential reaches a log line. The Chainstack key (`SOLANA_RPC_URL`), the heartbeat URL and the Telegram bot token
  travel inside URLs, and `httpx` logs URLs at INFO. So URL-logging libraries run at WARNING, and every handler
  has `RedactSecrets` as a second guard (`BUG.md` T-49).
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "[%(asctime)s] %(levelname)s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False

# Credential shapes that travel inside URLs. Each keeps enough to say WHICH secret it was.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(api[-_]key=)[^&\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(hc-ping\.com/)[A-Za-z0-9-]+"), r"\1***"),
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), r"\1***"),
    # Chainstack puts the key in the URL path: https://<node>.core.chainstack.com/<key>
    (re.compile(r"(\.chainstack\.com/)[A-Za-z0-9_-]+"), r"\1***"),
)

# Libraries that log a request URL at INFO. Raised to WARNING so their per-request lines never exist.
_URL_LOGGERS = ("httpx", "httpcore", "websockets", "urllib3")


def redact(text: str) -> str:
    """`text` with every known credential shape masked. Pure, so the patterns can be tested directly."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecrets(logging.Filter):
    """Masks credential shapes in the fully formatted message, arguments included."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def _setup_root_logger() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    console.addFilter(RedactSecrets())
    root.addHandler(console)

    log_dir = os.environ.get("LOG_DIR", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "app.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        file_handler.addFilter(RedactSecrets())
        root.addHandler(file_handler)
    except (PermissionError, OSError):
        # Read-only filesystem or a bind mount we do not own — stdout only.
        pass

    _configured = True


def get_logger(name: str) -> logging.Logger:
    _setup_root_logger()
    return logging.getLogger(name)
