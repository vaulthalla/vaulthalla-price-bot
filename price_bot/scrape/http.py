from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


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

    def close(self) -> None:
        self.client.close()
