"""Secrets Manager -> environment for the web tier (SCALABILITY.md Step 5,
RADIO §12). The reader is injected; no AWS call is ever made (AGENTS §1.2)."""
from __future__ import annotations

import os

from pilgrim.aws_secrets import EXPORTABLE, export_secret_env, secret_region


def test_secret_region():
    assert secret_region("arn:aws:secretsmanager:us-west-1:1:secret:x") == "us-west-1"
    assert secret_region("nonsense") is None


def test_exports_allowlisted_keys_only_and_env_wins(monkeypatch):
    monkeypatch.setenv("PILGRIM_ENV_FILE", "")
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:us-west-1:1:secret:stripe")
    for k in EXPORTABLE:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "from-env")
    doc = {"STRIPE_SECRET_KEY": "sk_test_1", "STRIPE_PUBLISHABLE_KEY": "pk_test_1",
           "STRIPE_WEBHOOK_SECRET": "whsec_from_secret", "PATH": "/evil", "": "x",
           "LITELLM_TOKEN": 123}
    seen = {}

    def reader(arn, profile):
        seen["arn"], seen["profile"] = arn, profile
        return doc
    exported = export_secret_env("STRIPE_SECRET_ARN", "mac-studio", reader=reader)
    assert seen == {"arn": "arn:aws:secretsmanager:us-west-1:1:secret:stripe",
                    "profile": "mac-studio"}
    assert sorted(exported) == ["STRIPE_PUBLISHABLE_KEY", "STRIPE_SECRET_KEY"]
    assert os.environ["STRIPE_SECRET_KEY"] == "sk_test_1"
    assert os.environ["STRIPE_WEBHOOK_SECRET"] == "from-env"      # explicit env wins
    assert os.environ.get("PATH") != "/evil"                      # not allow-listed
    assert "LITELLM_TOKEN" not in os.environ                      # non-string ignored


def test_missing_variable_and_failed_read_are_noops(monkeypatch):
    monkeypatch.setenv("PILGRIM_ENV_FILE", "")
    monkeypatch.delenv("STRIPE_SECRET_ARN", raising=False)
    assert export_secret_env("STRIPE_SECRET_ARN") == []
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:us-west-1:1:secret:stripe")

    def boom(arn, profile):
        raise RuntimeError("AccessDenied")
    assert export_secret_env("STRIPE_SECRET_ARN", reader=boom) == []
