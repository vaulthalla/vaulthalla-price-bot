from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from price_bot.api.main import create_app
from price_bot.cli import app
from price_bot.config import load_settings

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "providers"


def test_cli_estimate_fixture() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "estimate",
            "--profile",
            "tests/fixtures/profiles/r2-standard.json",
            "--usage",
            "tests/fixtures/usage/r2-standard-free-tier.json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"estimated_cost": "0.00000000"' in result.output or '"estimated_cost": "0E-8"' in result.output


def test_refresh_publish_validate_and_api_with_database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = load_settings().database_url
    if not database_url:
        pytest.skip("DATABASE_URL not configured")

    public_dir = tmp_path / "public" / "v1"
    env = {
        "DATABASE_URL": database_url,
        "PRICE_BOT_RAW_DIR": str(tmp_path / "raw"),
        "PRICE_BOT_PUBLIC_DIR": str(public_dir),
        "PRICE_BOT_SIGNING_PRIVATE_KEY_PATH": str(tmp_path / "ed25519.key"),
        "PRICE_BOT_SIGNING_PUBLIC_KEY_PATH": str(tmp_path / "ed25519.pub"),
        "PRICE_BOT_LIVE": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    runner = CliRunner()
    for command in [
        ["db", "init"],
        ["refresh", "--fixture-dir", str(FIXTURE_DIR)],
        ["publish", "--out", str(public_dir)],
        ["validate", str(public_dir / "manifest.json")],
    ]:
        result = runner.invoke(app, command, env=env)
        assert result.exit_code == 0, result.output

    client = TestClient(create_app())
    health = client.get("/health")
    assert health.status_code == 200
    manifest = client.get("/v1/manifest")
    assert manifest.status_code == 200
    assert manifest.json()["profile_count"] >= 16
    profile = client.get("/v1/profiles/cloudflare-r2/global/standard")
    assert profile.status_code == 200
    estimate = client.post(
        "/v1/estimate",
        json={
            "provider": "cloudflare-r2",
            "region": "global",
            "storage_class": "standard",
            "usage": {
                "storage_byte_hours": {"standard": "3650000000000"},
                "provider_operation_counts": {"PutObject": "1000"},
            },
        },
    )
    assert estimate.status_code == 200
    assert estimate.json()["currency"] == "USD"
