from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from price_bot.config import load_settings
from price_bot.db.store import Store
from price_bot.normalizer.estimator import estimate_cost
from price_bot.normalizer.models import RatingProfile, UsageInput
from price_bot.providers import all_fetchers

router = APIRouter()


def _file_response(path: Path, *, media_type: str, missing_detail: str) -> FileResponse:
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=missing_detail)
    return FileResponse(path, media_type=media_type)


def _safe_artifact_segment(value: str) -> str:
    if (
        not value
        or "/" in value
        or "\\" in value
        or ".." in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise HTTPException(status_code=400, detail="unsafe artifact path component")
    return value


def _profile_artifact_path(
    provider: str, region: str, storage_class: str, suffix: str = ".json"
) -> Path:
    provider = _safe_artifact_segment(provider)
    region = _safe_artifact_segment(region)
    storage_class = _safe_artifact_segment(storage_class)
    return load_settings().public_dir / "profiles" / provider / region / f"{storage_class}{suffix}"


@router.get("/health")
def health() -> dict[str, Any]:
    settings = load_settings()
    db_status = "not_configured"
    if settings.database_url:
        try:
            store = Store.from_settings(settings)
            with store.session() as session:
                session.execute(text("select 1"))
            db_status = "ok"
        except Exception as exc:  # pragma: no cover - exact DB failure is environment-specific
            db_status = f"error: {exc}"
    manifest_path = settings.public_dir / "manifest.json"
    latest_catalog_version = None
    if manifest_path.exists():
        latest_catalog_version = json.loads(manifest_path.read_text()).get("catalog_version")
    return {"status": "ok", "db": db_status, "latest_catalog_version": latest_catalog_version}


@router.get("/v1/manifest")
def manifest() -> dict[str, Any]:
    path = load_settings().public_dir / "manifest.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="manifest not published")
    return json.loads(path.read_text())


@router.get("/v1/profiles/{provider}/{region}/{storage_class}")
def profile(provider: str, region: str, storage_class: str) -> dict[str, Any]:
    settings = load_settings()
    path = settings.public_dir / "profiles" / provider / region / f"{storage_class}.json"
    if path.exists():
        return json.loads(path.read_text())
    if settings.database_url:
        store = Store.from_settings(settings)
        with store.session() as session:
            row_profile = store.profile_by_scope(session, provider, region, storage_class)
            if row_profile:
                return row_profile.model_dump(mode="json", exclude_none=True)
    raise HTTPException(status_code=404, detail="profile not found")


class ResolveProfileRequest(BaseModel):
    provider: str
    region: str
    storage_classes: list[str]
    include_bundle: bool = False


@router.post("/v1/resolve-profile")
def resolve_profile(request: ResolveProfileRequest) -> dict[str, Any]:
    manifest_payload = manifest()
    refs = [
        item
        for item in manifest_payload["profiles"]
        if item["provider"] == request.provider
        and item["region"] == request.region
        and item["storage_class"] in request.storage_classes
    ]
    response: dict[str, Any] = {"profiles": refs}
    if request.include_bundle:
        base = load_settings().public_dir
        response["bundle"] = [json.loads((base / item["href"]).read_text()) for item in refs]
    return response


class EstimateRequest(BaseModel):
    profile: dict[str, Any] | None = None
    provider: str | None = None
    region: str | None = None
    storage_class: str | None = None
    usage: UsageInput = Field(default_factory=UsageInput)


@router.post("/v1/estimate")
def estimate(request: EstimateRequest) -> dict[str, Any]:
    if request.profile is not None:
        rating_profile = RatingProfile.model_validate(request.profile)
    else:
        if not (request.provider and request.region and request.storage_class):
            raise HTTPException(
                status_code=400,
                detail="provide either profile payload or provider/region/storage_class",
            )
        rating_profile = RatingProfile.model_validate(
            profile(request.provider, request.region, request.storage_class)
        )
    return estimate_cost(rating_profile, request.usage).model_dump(mode="json")


@router.get("/v1/artifacts")
def artifact_index() -> dict[str, Any]:
    return {
        "manifest": "/v1/artifacts/manifest.json",
        "manifest_signature": "/v1/artifacts/manifest.json.sig",
        "full_catalog": "/v1/artifacts/catalog/full.json.zst",
        "full_catalog_signature": "/v1/artifacts/catalog/full.json.zst.sig",
        "schemas": {
            "manifest_v1": "/v1/artifacts/schemas/manifest-v1.schema.json",
            "rating_profile_v1": "/v1/artifacts/schemas/rating-profile-v1.schema.json",
        },
        "public_key": "/v1/public-key",
    }


@router.get("/v1/artifacts/manifest.json")
def manifest_artifact() -> FileResponse:
    return _file_response(
        load_settings().public_dir / "manifest.json",
        media_type="application/json",
        missing_detail="manifest artifact not published",
    )


@router.get("/v1/artifacts/manifest.json.sig")
def manifest_signature_artifact() -> FileResponse:
    return _file_response(
        load_settings().public_dir / "manifest.json.sig",
        media_type="text/plain",
        missing_detail="manifest signature not published",
    )


@router.get("/v1/artifacts/profiles/{provider}/{region}/{storage_class}.json")
def profile_artifact(provider: str, region: str, storage_class: str) -> FileResponse:
    return _file_response(
        _profile_artifact_path(provider, region, storage_class),
        media_type="application/json",
        missing_detail="profile artifact not found",
    )


@router.get("/v1/artifacts/profiles/{provider}/{region}/{storage_class}.json.sig")
def profile_signature_artifact(provider: str, region: str, storage_class: str) -> FileResponse:
    return _file_response(
        _profile_artifact_path(provider, region, storage_class, suffix=".json.sig"),
        media_type="text/plain",
        missing_detail="profile signature not found",
    )


@router.get("/v1/artifacts/schemas/{schema_name}")
def schema_artifact(schema_name: str) -> FileResponse:
    schema_name = _safe_artifact_segment(schema_name)
    if not schema_name.endswith(".schema.json"):
        raise HTTPException(status_code=400, detail="unsupported schema artifact")
    return _file_response(
        load_settings().public_dir / "schemas" / schema_name,
        media_type="application/schema+json",
        missing_detail="schema artifact not found",
    )


@router.get("/v1/artifacts/catalog/full.json.zst")
def full_catalog_artifact() -> FileResponse:
    return _file_response(
        load_settings().public_dir / "catalog" / "full.json.zst",
        media_type="application/zstd+json",
        missing_detail="full catalog not published",
    )


@router.get("/v1/artifacts/catalog/full.json.zst.sig")
def full_catalog_signature_artifact() -> FileResponse:
    return _file_response(
        load_settings().public_dir / "catalog" / "full.json.zst.sig",
        media_type="text/plain",
        missing_detail="full catalog signature not published",
    )


@router.get("/v1/public-key")
def public_key() -> FileResponse:
    return _file_response(
        load_settings().signing_public_key_path,
        media_type="text/plain",
        missing_detail="signing public key not configured",
    )


@router.get("/v1/providers")
def providers() -> dict[str, Any]:
    return {
        "providers": [
            {
                "provider": fetcher.provider_key,
                "display_name": fetcher.display_name,
                "sources": [source.__dict__ for source in fetcher.sources()],
            }
            for fetcher in all_fetchers()
        ]
    }


@router.get("/v1/catalog/full.json.zst")
def full_catalog() -> FileResponse:
    path = load_settings().public_dir / "catalog" / "full.json.zst"
    if not path.exists():
        raise HTTPException(status_code=404, detail="full catalog not published")
    return FileResponse(path, media_type="application/zstd+json")
