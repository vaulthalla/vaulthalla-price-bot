from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

JsonType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    api_family: Mapped[str] = mapped_column(Text, nullable=False, default="s3-compatible")
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sources: Mapped[list[ProviderSource]] = relationship(back_populates="provider")


class ProviderSource(Base):
    __tablename__ = "provider_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id"), nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

    provider: Mapped[Provider] = relationship(back_populates="sources")

    __table_args__ = (UniqueConstraint("provider_id", "source_kind", "source_url", "region"),)


class SourceFetchRun(Base):
    __tablename__ = "source_fetch_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("provider_source.id"), nullable=False)
    started_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    raw_sha256: Mapped[str | None] = mapped_column(Text)
    raw_blob_uri: Mapped[str | None] = mapped_column(Text)
    raw_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False, default=dict)


class RawPriceSnapshot(Base):
    __tablename__ = "raw_price_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("provider_source.id"), nullable=False)
    fetched_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    sha256: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    blob_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    compressed_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uncompressed_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False, default=dict)


class PriceProfile(Base):
    __tablename__ = "price_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_key: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id"), nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    storage_class: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="USD")
    catalog_version: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    confidence_level: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric)
    profile_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot_sha256: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

    provider: Mapped[Provider] = relationship()
    meters: Mapped[list[PriceMeter]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    operation_maps: Mapped[list[OperationMeterMap]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    storage_rules: Mapped[list[StorageRuleRow]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("profile_key", "catalog_version"),)


class PriceMeter(Base):
    __tablename__ = "price_meter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("price_profile.id", ondelete="CASCADE"))
    meter_key: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    billing_unit: Mapped[str | None] = mapped_column(Text)
    rounding_rule: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    free_tier_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    free_tier_unit: Mapped[str | None] = mapped_column(Text)
    free_tier_scope: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)

    profile: Mapped[PriceProfile] = relationship(back_populates="meters")
    tiers: Mapped[list[PriceTier]] = relationship(back_populates="meter", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("profile_id", "meter_key"),)


class PriceTier(Base):
    __tablename__ = "price_tier"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("price_meter.id", ondelete="CASCADE"))
    tier_start: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=Decimal("0"))
    tier_end: Mapped[Decimal | None] = mapped_column(Numeric)
    rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate_unit: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

    meter: Mapped[PriceMeter] = relationship(back_populates="tiers")


class OperationMeterMap(Base):
    __tablename__ = "operation_meter_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("price_profile.id", ondelete="CASCADE"))
    operation_key: Mapped[str] = mapped_column(Text, nullable=False)
    meter_key: Mapped[str] = mapped_column(Text, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=Decimal("1"))
    rules: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)

    profile: Mapped[PriceProfile] = relationship(back_populates="operation_maps")

    __table_args__ = (UniqueConstraint("profile_id", "operation_key"),)


class StorageRuleRow(Base):
    __tablename__ = "storage_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("price_profile.id", ondelete="CASCADE"))
    minimum_storage_duration_days: Mapped[int | None] = mapped_column(Integer)
    minimum_billable_object_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    metadata_overhead_bytes_per_object: Mapped[int | None] = mapped_column(BigInteger)
    early_delete_penalty_rule: Mapped[dict | None] = mapped_column(JsonType)
    retrieval_rule: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped[PriceProfile] = relationship(back_populates="storage_rules")


class CatalogPublish(Base):
    __tablename__ = "catalog_publish"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_version: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    generated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str | None] = mapped_column(Text)
    manifest_uri: Mapped[str | None] = mapped_column(Text)
    full_catalog_uri: Mapped[str | None] = mapped_column(Text)
    signature_uri: Mapped[str | None] = mapped_column(Text)
    profile_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False, default=dict)
