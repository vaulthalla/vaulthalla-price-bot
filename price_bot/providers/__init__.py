from __future__ import annotations

from price_bot.providers.aws_s3 import AwsS3Fetcher
from price_bot.providers.backblaze_b2 import BackblazeB2Fetcher
from price_bot.providers.ceph import CephFetcher
from price_bot.providers.cloudflare_r2 import CloudflareR2Fetcher
from price_bot.providers.digitalocean_spaces import DigitalOceanSpacesFetcher
from price_bot.providers.minio import MinioFetcher
from price_bot.providers.other import OtherFetcher
from price_bot.providers.storj import StorjFetcher
from price_bot.providers.wasabi import WasabiFetcher


def all_fetchers():
    return [
        AwsS3Fetcher(),
        CloudflareR2Fetcher(),
        WasabiFetcher(),
        BackblazeB2Fetcher(),
        DigitalOceanSpacesFetcher(),
        StorjFetcher(),
        MinioFetcher(),
        CephFetcher(),
        OtherFetcher(),
    ]


def selected_fetchers(provider_key: str | None):
    fetchers = all_fetchers()
    if provider_key is None:
        return fetchers
    selected = [fetcher for fetcher in fetchers if fetcher.provider_key == provider_key]
    if not selected:
        keys = ", ".join(fetcher.provider_key for fetcher in fetchers)
        raise ValueError(f"Unknown provider {provider_key!r}. Expected one of: {keys}")
    return selected
