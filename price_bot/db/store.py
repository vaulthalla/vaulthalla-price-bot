from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import zstandard as zstd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from price_bot.config import Settings, require_database_url
from price_bot.db.models import (
    CatalogPublish,
    OperationMeterMap,
    PriceMeter,
    PriceProfile,
    PriceTier,
    Provider,
    ProviderSource,
    RawPriceSnapshot,
    SourceFetchRun,
    StorageRuleRow,
)
from price_bot.normalizer.models import (
    Confidence,
    Integrity,
    Meter,
    OperationMapEntry,
    ProfileScope,
    ProviderInfo,
    RateTier,
    RatingProfile,
    StorageRule,
    VersioningCapability,
)
from price_bot.normalizer.rating_profile import finalize_profile_integrity, utc_now_iso


@dataclass(frozen=True)
class SnapshotRecord:
    sha256: str
    blob_uri: str


class Store:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, future=True)
        self.session_factory = sessionmaker(self.engine, future=True, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Store:
        return cls(require_database_url(settings))

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session

    def seed_provider_sources(self, session: Session) -> None:
        from price_bot.providers import all_fetchers

        for fetcher in all_fetchers():
            provider = self.ensure_provider(
                session, fetcher.provider_key, fetcher.display_name, fetcher.api_family
            )
            for source in fetcher.sources():
                source_row = self.ensure_source(
                    session,
                    provider,
                    source.kind,
                    source.url,
                    source.region,
                    source.priority,
                )
                source_row.enabled = source.enabled
        session.flush()

    def ensure_provider(
        self, session: Session, provider_key: str, display_name: str, api_family: str
    ) -> Provider:
        row = session.scalar(select(Provider).where(Provider.provider_key == provider_key))
        if row is None:
            row = Provider(
                provider_key=provider_key,
                display_name=display_name,
                api_family=api_family,
            )
            session.add(row)
            session.flush()
        return row

    def ensure_source(
        self,
        session: Session,
        provider: Provider,
        source_kind: str,
        source_url: str | None,
        region: str | None,
        priority: int = 100,
    ) -> ProviderSource:
        row = session.scalar(
            select(ProviderSource).where(
                ProviderSource.provider_id == provider.id,
                ProviderSource.source_kind == source_kind,
                ProviderSource.source_url == source_url,
                ProviderSource.region == region,
            )
        )
        if row is None:
            row = ProviderSource(
                provider_id=provider.id,
                source_kind=source_kind,
                source_url=source_url,
                region=region,
                priority=priority,
            )
            session.add(row)
            session.flush()
        return row

    def record_fetch_result(self, session: Session, fetcher, result, raw_dir: Path) -> SnapshotRecord:
        provider = self.ensure_provider(
            session, fetcher.provider_key, fetcher.display_name, fetcher.api_family
        )
        source = self.ensure_source(
            session,
            provider,
            result.source.kind,
            result.source.url,
            result.source.region,
            result.source.priority,
        )
        payload = result.payload
        raw_sha = hashlib.sha256(payload).hexdigest()
        today = datetime.now(UTC)
        blob_path = (
            raw_dir
            / fetcher.provider_key
            / f"{today.year:04d}"
            / f"{today.month:02d}"
            / f"{today.day:02d}"
            / f"{raw_sha}.zst"
        )
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if not blob_path.exists():
            blob_path.write_bytes(zstd.ZstdCompressor(level=10).compress(payload))

        existing_snapshot = session.scalar(
            select(RawPriceSnapshot).where(RawPriceSnapshot.sha256 == raw_sha)
        )
        snapshot = SnapshotRecord(sha256=raw_sha, blob_uri=str(blob_path))
        if existing_snapshot is None:
            session.add(
                RawPriceSnapshot(
                    provider_id=provider.id,
                    source_id=source.id,
                    fetched_at=today,
                    sha256=raw_sha,
                    blob_uri=str(blob_path),
                    content_type=result.content_type,
                    compressed_size_bytes=blob_path.stat().st_size,
                    uncompressed_size_bytes=len(payload),
                    metadata_=result.metadata,
                )
            )
        status = "unchanged" if existing_snapshot is not None else "success"
        session.add(
            SourceFetchRun(
                source_id=source.id,
                finished_at=today,
                status=status,
                http_status=result.http_status,
                raw_sha256=raw_sha,
                raw_blob_uri=str(blob_path),
                raw_size_bytes=len(payload),
                metadata_=result.metadata,
            )
        )
        session.flush()
        return snapshot

    def latest_snapshot(self, session: Session, provider_key: str) -> RawPriceSnapshot | None:
        return session.scalar(
            select(RawPriceSnapshot)
            .join(Provider, Provider.id == RawPriceSnapshot.provider_id)
            .where(Provider.provider_key == provider_key)
            .order_by(RawPriceSnapshot.fetched_at.desc(), RawPriceSnapshot.id.desc())
            .limit(1)
        )

    def read_snapshot_payload(self, blob_uri: str) -> bytes:
        return zstd.ZstdDecompressor().decompress(Path(blob_uri).read_bytes())

    def upsert_profiles(self, session: Session, profiles: list[RatingProfile]) -> None:
        for profile in profiles:
            finalize_profile_integrity(profile)
            provider = self.ensure_provider(
                session,
                profile.provider.id,
                profile.provider.display_name,
                profile.provider.api_family,
            )
            existing = session.scalar(
                select(PriceProfile).where(
                    PriceProfile.profile_key == profile.profile_id,
                    PriceProfile.catalog_version == profile.catalog_version,
                )
            )
            if existing is not None:
                session.delete(existing)
                session.flush()

            row = PriceProfile(
                profile_key=profile.profile_id,
                provider_id=provider.id,
                region=profile.scope.region,
                storage_class=profile.scope.storage_class,
                currency=profile.scope.currency,
                catalog_version=profile.catalog_version,
                effective_at=_parse_iso(profile.effective_at),
                expires_at=_parse_iso(profile.expires_at) if profile.expires_at else None,
                confidence_level=profile.confidence.level,
                confidence_score=profile.confidence.score,
                profile_sha256=profile.integrity.content_sha256,
                source_snapshot_sha256=profile.provenance.get("source_snapshot_sha256"),
                # Profile-level fields without their own columns ride in the provenance JSON blob
                # (create_all never ALTERs existing tables) and are popped back out in
                # profile_from_row. Anything not stored here is silently dropped at publish.
                provenance={
                    **profile.provenance,
                    "confidence": profile.confidence.model_dump(mode="json"),
                    "known_exclusions": profile.known_exclusions,
                    "generated_at": profile.generated_at,
                    **(
                        {"versioning": profile.versioning.model_dump(mode="json", exclude_none=True)}
                        if profile.versioning is not None
                        else {}
                    ),
                },
            )
            session.add(row)
            session.flush()

            for meter_key, profile_meter in profile.meters.items():
                meter_row = PriceMeter(
                    profile_id=row.id,
                    meter_key=meter_key,
                    unit=profile_meter.unit,
                    billing_unit=profile_meter.billing_unit,
                    rounding_rule=profile_meter.rounding_rule,
                    free_tier_amount=profile_meter.free_tier_amount,
                    free_tier_unit=profile_meter.free_tier_unit,
                    free_tier_scope=profile_meter.free_tier_scope,
                    rules={
                        **profile_meter.rules,
                        "meter_type": profile_meter.meter_type,
                    },
                )
                session.add(meter_row)
                session.flush()
                for profile_tier in profile_meter.tiers:
                    session.add(
                        PriceTier(
                            meter_id=meter_row.id,
                            tier_start=profile_tier.tier_start,
                            tier_end=profile_tier.tier_end,
                            rate=profile_tier.rate,
                            rate_unit=profile_tier.rate_unit,
                        )
                    )

            for operation_key, mapping in profile.operation_map.items():
                session.add(
                    OperationMeterMap(
                        profile_id=row.id,
                        operation_key=operation_key,
                        meter_key=mapping.meter_key,
                        multiplier=mapping.multiplier,
                        rules=mapping.rules,
                    )
                )

            for rule in profile.storage_rules:
                session.add(
                    StorageRuleRow(
                        profile_id=row.id,
                        minimum_storage_duration_days=rule.minimum_storage_duration_days,
                        minimum_billable_object_size_bytes=rule.minimum_billable_object_size_bytes,
                        metadata_overhead_bytes_per_object=rule.metadata_overhead_bytes_per_object,
                        early_delete_penalty_rule=rule.early_delete_penalty_rule,
                        retrieval_rule=rule.retrieval_rule,
                    )
                )
        session.flush()

    def latest_profiles(self, session: Session) -> list[RatingProfile]:
        rows = session.scalars(
            select(PriceProfile)
            .join(Provider)
            .order_by(PriceProfile.profile_key, PriceProfile.created_at.desc(), PriceProfile.id.desc())
        ).all()
        selected: dict[str, PriceProfile] = {}
        for row in rows:
            selected.setdefault(row.profile_key, row)
        return [self.profile_from_row(row) for row in selected.values()]

    def profile_by_scope(
        self, session: Session, provider_key: str, region: str, storage_class: str
    ) -> RatingProfile | None:
        row = session.scalar(
            select(PriceProfile)
            .join(Provider)
            .where(
                Provider.provider_key == provider_key,
                PriceProfile.region == region,
                PriceProfile.storage_class == storage_class,
            )
            .order_by(PriceProfile.created_at.desc(), PriceProfile.id.desc())
            .limit(1)
        )
        return self.profile_from_row(row) if row else None

    def profile_from_row(self, row: PriceProfile) -> RatingProfile:
        provenance = dict(row.provenance or {})
        confidence_raw = provenance.pop("confidence", {})
        known_exclusions = provenance.pop("known_exclusions", [])
        generated_at = provenance.pop("generated_at", None) or utc_now_iso()
        versioning_raw = provenance.pop("versioning", None)

        meters: dict[str, Meter] = {}
        for meter_row in row.meters:
            rules = dict(meter_row.rules or {})
            meter_type = rules.pop("meter_type", "usage")
            meters[meter_row.meter_key] = Meter(
                meter_key=meter_row.meter_key,
                meter_type=meter_type,
                unit=meter_row.unit,
                billing_unit=meter_row.billing_unit,
                rounding_rule=meter_row.rounding_rule,
                free_tier_amount=meter_row.free_tier_amount,
                free_tier_unit=meter_row.free_tier_unit,
                free_tier_scope=meter_row.free_tier_scope or "none",
                rules=rules,
                tiers=[
                    RateTier(
                        tier_start=tier_row.tier_start,
                        tier_end=tier_row.tier_end,
                        rate=tier_row.rate,
                        rate_unit=tier_row.rate_unit,
                    )
                    for tier_row in sorted(meter_row.tiers, key=lambda tier: tier.tier_start)
                ],
            )

        operation_map = {
            item.operation_key: OperationMapEntry(
                meter_key=item.meter_key, multiplier=item.multiplier, rules=item.rules
            )
            for item in row.operation_maps
        }

        storage_rules = [
            StorageRule(
                minimum_storage_duration_days=item.minimum_storage_duration_days,
                minimum_billable_object_size_bytes=item.minimum_billable_object_size_bytes,
                metadata_overhead_bytes_per_object=item.metadata_overhead_bytes_per_object,
                early_delete_penalty_rule=item.early_delete_penalty_rule,
                retrieval_rule=item.retrieval_rule,
            )
            for item in row.storage_rules
        ]

        profile = RatingProfile(
            profile_id=row.profile_key,
            catalog_version=row.catalog_version,
            generated_at=generated_at,
            effective_at=_iso(row.effective_at),
            expires_at=_iso(row.expires_at) if row.expires_at else None,
            provider=ProviderInfo(
                id=row.provider.provider_key,
                display_name=row.provider.display_name,
                api_family=row.provider.api_family,
            ),
            scope=ProfileScope(
                region=row.region,
                storage_class=row.storage_class,
                currency=row.currency,
            ),
            confidence=Confidence(
                level=confidence_raw.get("level", row.confidence_level),
                score=Decimal(str(confidence_raw.get("score", row.confidence_score or "0"))),
                reasons=confidence_raw.get("reasons", []),
                unknowns=confidence_raw.get("unknowns", []),
            ),
            operation_map=operation_map,
            meters=meters,
            storage_rules=storage_rules,
            known_exclusions=known_exclusions,
            provenance=provenance,
            versioning=(
                VersioningCapability.model_validate(versioning_raw) if versioning_raw else None
            ),
            integrity=Integrity(content_sha256=row.profile_sha256),
        )
        finalize_profile_integrity(profile)
        return profile

    def record_publish(
        self,
        session: Session,
        *,
        catalog_version: str,
        manifest_sha256: str,
        manifest_uri: str,
        full_catalog_uri: str,
        signature_uri: str,
        profile_count: int,
    ) -> None:
        existing = session.scalar(
            select(CatalogPublish).where(CatalogPublish.catalog_version == catalog_version)
        )
        if existing is not None:
            existing.status = "published"
            existing.manifest_sha256 = manifest_sha256
            existing.manifest_uri = manifest_uri
            existing.full_catalog_uri = full_catalog_uri
            existing.signature_uri = signature_uri
            existing.profile_count = profile_count
            existing.published_at = datetime.now(UTC)
        else:
            session.add(
                CatalogPublish(
                    catalog_version=catalog_version,
                    status="published",
                    manifest_sha256=manifest_sha256,
                    manifest_uri=manifest_uri,
                    full_catalog_uri=full_catalog_uri,
                    signature_uri=signature_uri,
                    profile_count=profile_count,
                    published_at=datetime.now(UTC),
                )
            )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)
