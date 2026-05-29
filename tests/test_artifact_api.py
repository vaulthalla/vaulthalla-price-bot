from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import zstandard as zstd
from fastapi.testclient import TestClient

from price_bot.api.main import create_app
from price_bot.config import load_settings
from price_bot.normalizer.models import Manifest, ManifestProfileRef
from price_bot.normalizer.rating_profile import stable_json_bytes
from price_bot.publish.artifacts import mirror_artifacts, validate_manifest
from price_bot.publish.signing import write_signature

PROFILE_FIXTURE = Path("tests/fixtures/profiles/r2-standard.json")


@pytest.fixture
def published_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    public_dir = tmp_path / "public" / "v1"
    profile_path = public_dir / "profiles" / "cloudflare-r2" / "global" / "standard.json"
    full_catalog_path = public_dir / "catalog" / "full.json.zst"
    schemas_dir = public_dir / "schemas"
    private_key_path = tmp_path / "ed25519.key"
    public_key_path = tmp_path / "ed25519.pub"

    monkeypatch.setenv("PRICE_BOT_PUBLIC_DIR", str(public_dir))
    monkeypatch.setenv("PRICE_BOT_SIGNING_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("PRICE_BOT_SIGNING_PUBLIC_KEY_PATH", str(public_key_path))

    settings = load_settings()
    profile_bytes = PROFILE_FIXTURE.read_bytes()
    profile_payload = json.loads(profile_bytes)

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_bytes(profile_bytes)
    profile_signature_path = write_signature(profile_path, profile_bytes, settings)

    full_catalog_bytes = zstd.ZstdCompressor(level=1).compress(
        stable_json_bytes({"profiles": [profile_payload]})
    )
    full_catalog_path.parent.mkdir(parents=True, exist_ok=True)
    full_catalog_path.write_bytes(full_catalog_bytes)
    full_catalog_signature_path = write_signature(full_catalog_path, full_catalog_bytes, settings)

    schemas_dir.mkdir(parents=True, exist_ok=True)
    manifest_schema_path = schemas_dir / "manifest-v1.schema.json"
    rating_profile_schema_path = schemas_dir / "rating-profile-v1.schema.json"
    manifest_schema_path.write_bytes(b'{"title":"manifest"}\n')
    rating_profile_schema_path.write_bytes(b'{"title":"rating profile"}\n')

    profile_ref = ManifestProfileRef(
        profile_id=profile_payload["profile_id"],
        provider=profile_payload["provider"]["id"],
        region=profile_payload["scope"]["region"],
        storage_class=profile_payload["scope"]["storage_class"],
        currency=profile_payload["scope"]["currency"],
        href="profiles/cloudflare-r2/global/standard.json",
        sha256=hashlib.sha256(profile_bytes).hexdigest(),
        signature="profiles/cloudflare-r2/global/standard.json.sig",
        confidence_level=profile_payload["confidence"]["level"],
        effective_at=profile_payload["effective_at"],
        expires_at=profile_payload.get("expires_at"),
    )
    manifest = Manifest(
        catalog_version=profile_payload["catalog_version"],
        generated_at=profile_payload["generated_at"],
        profile_count=1,
        profiles=[profile_ref],
        full_catalog={
            "href": "catalog/full.json.zst",
            "sha256": hashlib.sha256(full_catalog_bytes).hexdigest(),
            "signature": "catalog/full.json.zst.sig",
            "content_type": "application/zstd+json",
        },
        schemas={
            "rating_profile_v1": "schemas/rating-profile-v1.schema.json",
            "manifest_v1": "schemas/manifest-v1.schema.json",
        },
    )
    manifest.integrity.signature_ref = "manifest.json.sig"
    manifest.integrity.content_sha256 = hashlib.sha256(
        stable_json_bytes(manifest.model_dump(mode="json", exclude_none=True))
    ).hexdigest()
    manifest_bytes = stable_json_bytes(manifest.model_dump(mode="json", exclude_none=True))
    manifest_path = public_dir / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest_signature_path = write_signature(manifest_path, manifest_bytes, settings)

    return {
        "public_dir": public_dir,
        "profile_path": profile_path,
        "profile_signature_path": profile_signature_path,
        "full_catalog_path": full_catalog_path,
        "full_catalog_signature_path": full_catalog_signature_path,
        "manifest_path": manifest_path,
        "manifest_signature_path": manifest_signature_path,
        "manifest_schema_path": manifest_schema_path,
        "rating_profile_schema_path": rating_profile_schema_path,
        "public_key_path": public_key_path,
    }


def test_raw_artifact_routes_return_exact_bytes(published_artifacts: dict[str, Path]) -> None:
    client = TestClient(create_app())

    response = client.get("/v1/artifacts")
    assert response.status_code == 200
    assert response.json()["manifest"] == "/v1/artifacts/manifest.json"

    response = client.get("/v1/artifacts/manifest.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == published_artifacts["manifest_path"].read_bytes()

    response = client.get("/v1/artifacts/manifest.json.sig")
    assert response.status_code == 200
    assert response.content == published_artifacts["manifest_signature_path"].read_bytes()

    response = client.get("/v1/artifacts/profiles/cloudflare-r2/global/standard.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == published_artifacts["profile_path"].read_bytes()

    response = client.get("/v1/artifacts/profiles/cloudflare-r2/global/standard.json.sig")
    assert response.status_code == 200
    assert response.content == published_artifacts["profile_signature_path"].read_bytes()

    response = client.get("/v1/artifacts/catalog/full.json.zst")
    assert response.status_code == 200
    assert response.content == published_artifacts["full_catalog_path"].read_bytes()

    response = client.get("/v1/artifacts/catalog/full.json.zst.sig")
    assert response.status_code == 200
    assert response.content == published_artifacts["full_catalog_signature_path"].read_bytes()

    response = client.get("/v1/artifacts/schemas/manifest-v1.schema.json")
    assert response.status_code == 200
    assert response.content == published_artifacts["manifest_schema_path"].read_bytes()

    response = client.get("/v1/public-key")
    assert response.status_code == 200
    assert response.content == published_artifacts["public_key_path"].read_bytes()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/artifacts/profiles/%2E%2E/global/standard.json",
        "/v1/artifacts/profiles/cloudflare-r2/%2E%2E/standard.json",
        "/v1/artifacts/profiles/cloudflare-r2/global/%2E%2E.json",
        "/v1/artifacts/profiles/cloudflare-r2%2Fescape/global/standard.json",
        "/v1/artifacts/profiles/cloudflare-r2/global/standard/extra.json",
        "/v1/artifacts/profiles/cloudflare-r2/global/stand%5Card.json",
        "/v1/artifacts/profiles/cloudflare-r2/global/line%0Abreak.json",
        "/v1/artifacts/profiles//global/standard.json",
    ],
)
def test_profile_artifact_path_traversal_fails(
    published_artifacts: dict[str, Path], path: str
) -> None:
    client = TestClient(create_app())
    response = client.get(path)
    assert response.status_code in {400, 404}


def test_existing_api_routes_and_manifest_validation_still_work(
    published_artifacts: dict[str, Path],
) -> None:
    client = TestClient(create_app())

    manifest_response = client.get("/v1/manifest")
    assert manifest_response.status_code == 200
    assert manifest_response.json()["profile_count"] == 1

    profile_response = client.get("/v1/profiles/cloudflare-r2/global/standard")
    assert profile_response.status_code == 200
    assert profile_response.json()["profile_id"] == "cloudflare-r2/global/standard"

    reporting_estimate = client.post(
        "/v1/estimate",
        json={
            "profile": profile_response.json(),
            "usage": {"storage_byte_hours": {"standard": "3650000000000"}},
        },
    )
    assert reporting_estimate.status_code == 200
    assert reporting_estimate.json()["estimate_mode"] == "reporting"
    assert reporting_estimate.json()["free_tier_policy"] == "apply_free_tiers"
    assert reporting_estimate.json()["estimated_cost"] in {"0E-8", "0.00000000"}

    conservative_estimate = client.post(
        "/v1/estimate",
        json={
            "profile": profile_response.json(),
            "usage": {"storage_byte_hours": {"standard": "3650000000000"}},
            "options": {"mode": "budget_conservative", "apply_free_tiers": False},
        },
    )
    assert conservative_estimate.status_code == 200
    assert conservative_estimate.json()["estimate_mode"] == "budget_conservative"
    assert conservative_estimate.json()["free_tier_policy"] == "ignore_account_wide_free_tiers"
    assert conservative_estimate.json()["free_tiers_applied"] is False
    assert conservative_estimate.json()["estimated_cost"] != reporting_estimate.json()["estimated_cost"]

    validate_manifest(published_artifacts["manifest_path"], load_settings())


def test_mirror_artifacts_copies_signed_public_bytes_without_private_key(
    published_artifacts: dict[str, Path],
    tmp_path: Path,
) -> None:
    mirror_dir = tmp_path / "mirror" / "v1"
    settings = load_settings()

    mirror_artifacts(
        published_artifacts["public_dir"],
        mirror_dir,
        settings,
        include_public_key=True,
    )

    for source_path in published_artifacts["public_dir"].rglob("*"):
        if source_path.is_file():
            mirrored = mirror_dir / source_path.relative_to(published_artifacts["public_dir"])
            assert mirrored.read_bytes() == source_path.read_bytes()

    validate_manifest(mirror_dir / "manifest.json", settings)
    assert (mirror_dir / "public-key.pem").read_bytes() == published_artifacts["public_key_path"].read_bytes()
    assert not any(path.name.endswith(".key") for path in mirror_dir.rglob("*"))


def test_public_key_returns_404_when_key_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRICE_BOT_SIGNING_PUBLIC_KEY_PATH", str(tmp_path / "missing.pub"))

    response = TestClient(create_app()).get("/v1/public-key")

    assert response.status_code == 404
