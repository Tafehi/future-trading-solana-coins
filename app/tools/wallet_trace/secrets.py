"""Secrets for the wallet tracer, read from AWS Secrets Manager with the user's own AWS login (user, 2026-11-17, D-27).

The secret is `solana-future-bot` (eu-central-1), a JSON object of key/value pairs. It is the same secret the removed
copy bot used; the tracer reads only these keys and ignores the rest (the exchange keys are never touched):

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`  -> the per-run Telegram summary
- `SOLANA_RPC_URL`                          -> the Solana RPC (Chainstack; the key is in the URL path)

Checked 2026-11-17 against the boto3 docs (`SecretsManager.Client.get_secret_value(SecretId=...)` returns
`SecretString`, "a JSON structure of key/value pairs" for a console-made secret; errors such as
`ResourceNotFoundException` come as `botocore.exceptions.ClientError`) and the removed bot's `hostSetup.sh`, which read
the same secret the same way. Not run against AWS by the assistant (CLAUDE.md rule 1): the user's first run is the test.

Rules: a value is never printed or logged. A missing secret, no AWS login, or no boto3 is not fatal: the tracer still
runs, on the public RPC and without Telegram, and says why once. Env vars win over the secret (a test or a one-off run
can set them without AWS).
"""

import json
import os
from dataclasses import dataclass
from typing import Any

DEFAULT_SECRET = "solana-future-bot"
DEFAULT_REGION = "eu-central-1"
KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SOLANA_RPC_URL")
UNSET = {"", "unset", "none", "null"}


@dataclass(frozen=True)
class Secrets:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    solana_rpc_url: str = ""
    source: str = "none"  # "env", "secrets-manager:<name>", "env+secrets-manager:<name>" or "none"
    problem: str = ""  # why the secret could not be read (no value inside)

    @property
    def telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def _clean(v: object) -> str:
    s = str(v or "").strip()
    return "" if s.lower() in UNSET else s


def fetch_secret(name: str, region: str, *, client: Any = None) -> dict[str, str]:
    """The secret's JSON as a dict. `client` is injectable (tests use a fake). Raises on any AWS error."""
    if client is None:
        import boto3  # laptop only (requirements-dev.txt), never in the bot image: imported when the secret is read

        client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=name)
    raw = resp.get("SecretString")
    if not raw:
        raise ValueError("the secret has no SecretString")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("the secret is not a JSON object of key/value pairs")
    return {str(k): str(v) for k, v in data.items()}


def load(
    *,
    name: str = DEFAULT_SECRET,
    region: str = DEFAULT_REGION,
    use_aws: bool = True,
    env: dict[str, str] | None = None,
    client: Any = None,
) -> Secrets:
    """Env first, then the secret for whatever is still missing. Never raises."""
    env = dict(os.environ) if env is None else env
    vals = {k: _clean(env.get(k)) for k in KEYS}
    source = "env" if any(vals.values()) else "none"
    problem = ""
    if use_aws and not all(vals.values()):
        try:
            data = fetch_secret(name, region, client=client)
        except Exception as exc:  # noqa: BLE001 — any failure only means "no secret": report the TYPE, never a value
            problem = f"could not read secret {name} ({type(exc).__name__}); public RPC, no Telegram"
        else:
            for k in KEYS:
                if not vals[k]:
                    vals[k] = _clean(data.get(k))
            source = f"{'env+' if source == 'env' else ''}secrets-manager:{name}"
    return Secrets(vals["TELEGRAM_BOT_TOKEN"], vals["TELEGRAM_CHAT_ID"], vals["SOLANA_RPC_URL"], source, problem)
