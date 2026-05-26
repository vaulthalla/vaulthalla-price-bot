from __future__ import annotations

import json

from price_bot.normalizer.rating_profile import base_profile, meter, op_map, storage_rule
from price_bot.providers.base import ProviderFetcher, SourceDefinition

CURATED = b"""{
  "serviceCode": "AmazonS3",
  "source": "aws-price-list-fixture",
  "region": "us-east-1",
  "currency": "USD",
  "standard": {
    "storage_gb_month": "0.023",
    "put_copy_post_list_1000": "0.005",
    "get_head_1000": "0.0004",
    "egress_gb": "0.09"
  },
  "standard_ia": {
    "storage_gb_month": "0.0125",
    "put_copy_post_list_1000": "0.01",
    "get_head_1000": "0.001",
    "retrieval_gb": "0.01",
    "egress_gb": "0.09",
    "minimum_storage_duration_days": 30,
    "minimum_billable_object_size_bytes": 131072
  }
}"""


class AwsS3Fetcher(ProviderFetcher):
    provider_key = "aws-s3"
    display_name = "AWS S3"

    def sources(self) -> list[SourceDefinition]:
        return [
            SourceDefinition(
                kind="api",
                url="https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/index.json",
                fixture_name="aws_s3.json",
            )
        ]

    def curated_payload(self) -> bytes:
        return CURATED

    def build_profiles(self, payload: bytes, catalog_version: str, source_snapshot_sha256: str):
        data = json.loads(payload)
        if data.get("serviceCode") != "AmazonS3":
            raise ValueError("AWS S3 parser expected serviceCode AmazonS3")
        region = data["region"]
        return [
            self._storage_class(
                "standard",
                region,
                data["standard"],
                catalog_version,
                source_snapshot_sha256,
                min_days=None,
                min_object_bytes=None,
                retrieval=False,
            ),
            self._storage_class(
                "standard-ia",
                region,
                data["standard_ia"],
                catalog_version,
                source_snapshot_sha256,
                min_days=int(data["standard_ia"]["minimum_storage_duration_days"]),
                min_object_bytes=int(data["standard_ia"]["minimum_billable_object_size_bytes"]),
                retrieval=True,
            ),
        ]

    def _storage_class(
        self,
        storage_class: str,
        region: str,
        values: dict,
        catalog_version: str,
        source_snapshot_sha256: str,
        *,
        min_days: int | None,
        min_object_bytes: int | None,
        retrieval: bool,
    ):
        profile = base_profile(
            provider_id=self.provider_key,
            display_name=self.display_name,
            region=region,
            storage_class=storage_class,
            catalog_version=catalog_version,
            source_snapshot_sha256=source_snapshot_sha256,
            confidence_level="high",
            confidence_score="0.86",
            confidence_reasons=[
                "fixture follows AWS Price List API shape",
                "official AWS Price List Bulk API source path configured",
            ],
            provenance={
                "source": "AWS Price List API/Bulk API fixture",
                "live_api_path": "AmazonS3 current offer index",
                "todo": "Add selective live extraction over AWS offer dimensions before enabling routine bulk fetches.",
            },
        )
        profile.meters = {
            "storage": meter(
                "storage",
                "storage_byte_hours",
                "byte_hour",
                values["storage_gb_month"],
                "gb_month",
                billing_unit="gb_month",
            ),
            "request_write": meter(
                "request_write",
                "request",
                "operation",
                values["put_copy_post_list_1000"],
                "request_1000",
                billing_unit="request_1000",
            ),
            "request_read": meter(
                "request_read",
                "request",
                "operation",
                values["get_head_1000"],
                "request_1000",
                billing_unit="request_1000",
            ),
            "egress": meter(
                "egress", "egress_bytes", "byte", values["egress_gb"], "gb", billing_unit="gb"
            ),
            "free_operations": meter("free_operations", "request", "operation", "0", "operation"),
        }
        if retrieval:
            profile.meters["retrieval"] = meter(
                "retrieval",
                "retrieval_bytes",
                "byte",
                values["retrieval_gb"],
                "gb",
                billing_unit="gb",
            )
        profile.operation_map = {
            "ListObjectsV2": op_map("request_write"),
            "HeadObject": op_map("request_read"),
            "GetObject": op_map("request_read"),
            "PutObject": op_map("request_write"),
            "CopyObject": op_map("request_write"),
            "CreateMultipartUpload": op_map("request_write"),
            "UploadPart": op_map("request_write"),
            "UploadPartCopy": op_map("request_write"),
            "CompleteMultipartUpload": op_map("request_write"),
            "AbortMultipartUpload": op_map("free_operations"),
            "DeleteObject": op_map("free_operations"),
            "LifecycleTransition": op_map("request_write"),
            "RestoreObject": op_map("request_write"),
        }
        if min_days or min_object_bytes:
            profile.storage_rules = [
                storage_rule(
                    min_days=min_days,
                    min_object_bytes=min_object_bytes,
                    overhead_bytes=40960 if "glacier" in storage_class else None,
                )
            ]
        return profile
