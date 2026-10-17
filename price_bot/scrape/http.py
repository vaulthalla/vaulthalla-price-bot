from __future__ import annotations

from typing import BinaryIO

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class PriceBotHttpClient:
    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={
                "User-Agent": "vaulthalla-price-bot/0.1 (+https://vaulthalla.local)",
                "Accept": "application/json,text/markdown,text/html;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )

    @retry(wait=wait_exponential(multiplier=1, min=1, max=16), stop=stop_after_attempt(3))
    def get(self, url: str) -> httpx.Response:
        response = self.client.get(url)
        response.raise_for_status()
        return response

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=16),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    def download_to(self, url: str, sink: BinaryIO, *, max_bytes: int) -> int:
        """Stream ``url`` into ``sink`` without buffering the whole body in memory."""
        sink.seek(0)
        sink.truncate()
        total = 0
        with self.client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"{url} exceeded {max_bytes} bytes")
                sink.write(chunk)
        return total

    def close(self) -> None:
        self.client.close()
