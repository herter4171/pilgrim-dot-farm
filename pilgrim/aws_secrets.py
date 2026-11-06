"""Secrets Manager -> environment, for the Fargate web tier (SCALABILITY.md
Step 5; RADIO §12).

The ECS task definition carries *ARNs*, not values: `DB_PASS_SECRET_ARN`
(RDS-managed `{username, password}`, read by `pilgrim.db`) and
`STRIPE_SECRET_ARN`, a JSON secret holding the three Stripe keys. At start the
app reads such a secret with the task role (or the station's `media.aws_profile`
on the Mac, where the same code runs if the variable is set) and exports the
allow-listed keys into `os.environ`, so `config.stripe_env()` and
`load_secret()` see them exactly as they would see a `.env` line. Values are
never logged; only the key names are (AGENTS §1.7, §1.11).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from pilgrim.config import load_secret

log = logging.getLogger("radio.secrets")

STRIPE_SECRET_ARN_ENV = "STRIPE_SECRET_ARN"
# What a JSON secret may set. Anything else in the document is ignored, so a
# secret cannot inject arbitrary environment (PATH, PILGRIM_CONFIG_OVERLAY...).
EXPORTABLE = frozenset({
    "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET",
    "PAID_OVERRIDE_TOKEN", "LITELLM_TOKEN", "BRAVE_SEARCH_KEY", "PILGRIM_DJ_TOKEN",
})


def secret_region(arn: str) -> str | None:
    parts = arn.split(":")
    return parts[3] if len(parts) > 4 and parts[0] == "arn" else None


def read_secret_json(arn: str, profile: str | None = None) -> dict[str, Any]:
    """The secret's JSON document, read with `profile` when that profile
    exists, else the default credential chain (a task role)."""
    import boto3
    from botocore.exceptions import ProfileNotFound

    region = secret_region(arn)
    try:
        session = boto3.Session(profile_name=profile or None, region_name=region)
    except ProfileNotFound:
        session = boto3.Session(region_name=region)
    body = session.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("secret is not a JSON object")
    return data


def export_secret_env(arn_env: str, profile: str | None = None, *,
                      allowed: frozenset[str] = EXPORTABLE,
                      reader=read_secret_json) -> list[str]:
    """If `$arn_env` names a secret, export its allow-listed keys that are not
    already set in the environment. Returns the names exported (never values).
    A missing variable is a no-op; a failed read is logged and leaves the
    environment untouched, so the app starts with that feature off rather
    than not at all."""
    arn = load_secret(arn_env)
    if not arn:
        return []
    try:
        data = reader(arn, profile)
    except Exception as e:  # noqa: BLE001 - degrade, never crash on a secret
        log.warning("secrets.read_failed", extra={"env": arn_env,
                                                  "error": f"{type(e).__name__}: {str(e)[:160]}"})
        return []
    exported: list[str] = []
    skipped = [k for k in data if k not in allowed]
    for key, value in data.items():
        if key not in allowed or not isinstance(value, str) or not value:
            continue
        if os.environ.get(key, "").strip():
            continue           # an explicit environment value wins
        os.environ[key] = value
        exported.append(key)
    log.info("secrets.exported", extra={"env": arn_env, "keys": sorted(exported),
                                        "ignored_keys": sorted(skipped)})
    return exported
