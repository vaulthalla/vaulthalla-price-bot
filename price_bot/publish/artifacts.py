from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import zstandard as zstd
from sqlalchemy.orm import Session

from price_bot.config import Settings
from price_bot.normalizer.models import Manifest, ManifestProfileRef, RatingProfile
from price_bot.normalizer.rating_profile import (
    compute_profile_sha256,
    profile_json_bytes,
    stable_json_bytes,
    utc_now_iso,
)
from price_bot.publish.signing import verify_signature, write_signature


def publish_catalog(session: Session, store, out: Path, settings: Settings) -> Manifest:
    out.mkdir(parents=True, exist_ok=True)
    profiles_dir = out / "profiles"
    schemas_dir = out / "schemas"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    schemas_dir.mkdir(parents=True, exist_ok=True)

    profiles = sorted(store.latest_profiles(session), key=lambda item: item.profile_id)
    if not profiles:
        raise RuntimeError("No profiles found. Run price-bot refresh or normalize first.")
    catalog_version = max(profile.catalog_version for profile in profiles)
    generated_at = utc_now_iso()

    profile_refs: list[ManifestProfileRef] = []
    full_catalog_payload: list[dict] = []
    for profile in profiles:
        rel = _profile_relpath(profile)
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        profile.integrity.signature_ref = f"{rel}.sig"
        profile.integrity.content_sha256 = compute_profile_sha256(profile)
        payload = profile_json_bytes(profile)
        target.write_bytes(payload)
        write_signature(target, payload, settings)
        sha = hashlib.sha256(payload).hexdigest()
        profile_refs.append(
            ManifestProfileRef(
                profile_id=profile.profile_id,
                provider=profile.provider.id,
                region=profile.scope.region,
                storage_class=profile.scope.storage_class,
                currency=profile.scope.currency,
                href=rel,
                sha256=sha,
                signature=f"{rel}.sig",
                confidence_level=profile.confidence.level,
                effective_at=profile.effective_at,
                expires_at=profile.expires_at,
            )
        )
        full_catalog_payload.append(profile.model_dump(mode="json", exclude_none=True))

    full_catalog_rel = "catalog/full.json.zst"
    full_catalog_path = out / full_catalog_rel
    full_catalog_path.parent.mkdir(parents=True, exist_ok=True)
    full_catalog_bytes = stable_json_bytes({"profiles": full_catalog_payload})
    full_catalog_zst = zstd.ZstdCompressor(level=10).compress(full_catalog_bytes)
    full_catalog_path.write_bytes(full_catalog_zst)
    write_signature(full_catalog_path, full_catalog_zst, settings)

    _write_schema(schemas_dir / "rating-profile-v1.schema.json", RatingProfile)
    _write_schema(schemas_dir / "manifest-v1.schema.json", Manifest)

    manifest = Manifest(
        catalog_version=catalog_version,
        generated_at=generated_at,
        profile_count=len(profile_refs),
        profiles=profile_refs,
        full_catalog={
            "href": full_catalog_rel,
            "sha256": hashlib.sha256(full_catalog_zst).hexdigest(),
            "signature": f"{full_catalog_rel}.sig",
            "content_type": "application/zstd+json",
        },
        schemas={
            "rating_profile_v1": "schemas/rating-profile-v1.schema.json",
            "manifest_v1": "schemas/manifest-v1.schema.json",
        },
    )
    manifest_path = out / "manifest.json"
    manifest_payload = stable_json_bytes(manifest.model_dump(mode="json", exclude_none=True))
    manifest.integrity.signature_ref = "manifest.json.sig"
    manifest.integrity.content_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    manifest_payload = stable_json_bytes(manifest.model_dump(mode="json", exclude_none=True))
    manifest_path.write_bytes(manifest_payload)
    write_signature(manifest_path, manifest_payload, settings)
    store.record_publish(
        session,
        catalog_version=catalog_version,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        manifest_uri=str(manifest_path),
        full_catalog_uri=str(full_catalog_path),
        signature_uri=str(manifest_path) + ".sig",
        profile_count=len(profile_refs),
    )
    return manifest


def validate_manifest(manifest_path: Path, settings: Settings) -> None:
    base = manifest_path.parent
    manifest_payload = manifest_path.read_bytes()
    verify_signature(manifest_payload, (manifest_path.with_name(manifest_path.name + ".sig")).read_text().strip(), settings)
    manifest = Manifest.model_validate(json.loads(manifest_payload))
    for ref in manifest.profiles:
        payload = (base / ref.href).read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != ref.sha256:
            raise ValueError(f"Profile hash mismatch for {ref.href}")
        verify_signature(payload, (base / ref.signature).read_text().strip(), settings)
    full_path = base / manifest.full_catalog["href"]
    full_payload = full_path.read_bytes()
    if hashlib.sha256(full_payload).hexdigest() != manifest.full_catalog["sha256"]:
        raise ValueError("Full catalog hash mismatch")
    verify_signature(
        full_payload,
        (base / manifest.full_catalog["signature"]).read_text().strip(),
        settings,
    )


def mirror_artifacts(
    source: Path,
    out: Path,
    settings: Settings,
    *,
    include_public_key: bool = False,
) -> None:
    source = source.resolve()
    out = out.resolve()
    if not (source / "manifest.json").exists():
        raise FileNotFoundError(f"Published artifact manifest not found: {source / 'manifest.json'}")
    if source == out:
        validate_manifest(out / "manifest.json", settings)
        return

    out.mkdir(parents=True, exist_ok=True)
    for name in ["manifest.json", "manifest.json.sig"]:
        shutil.copy2(source / name, out / name)
    for dirname in ["profiles", "schemas", "catalog"]:
        src = source / dirname
        if src.exists():
            shutil.copytree(src, out / dirname, dirs_exist_ok=True)

    if include_public_key:
        public_key = settings.signing_public_key_path
        if public_key.exists():
            shutil.copy2(public_key, out / "public-key.pem")

    validate_manifest(out / "manifest.json", settings)


def _profile_relpath(profile: RatingProfile) -> str:
    return (
        Path("profiles")
        / _safe(profile.provider.id)
        / _safe(profile.scope.region)
        / f"{_safe(profile.scope.storage_class)}.json"
    ).as_posix()


def _safe(value: str) -> str:
    return value.replace("/", "-").replace(" ", "-").lower()


def _write_schema(path: Path, model: type) -> None:
    path.write_bytes(stable_json_bytes(model.model_json_schema()))
