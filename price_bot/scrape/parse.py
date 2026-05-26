from __future__ import annotations

import re
from decimal import Decimal

from bs4 import BeautifulSoup


def text_from_html(payload: bytes) -> str:
    soup = BeautifulSoup(payload, "lxml")
    return soup.get_text("\n", strip=True)


def require_phrases(text: str, phrases: list[str]) -> None:
    lowered = text.lower()
    missing = [phrase for phrase in phrases if phrase.lower() not in lowered]
    if missing:
        raise ValueError(f"Parser sentinel phrase(s) missing: {', '.join(missing)}")


def decimal_after(pattern: str, text: str, *, label: str) -> Decimal:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError(f"Could not parse decimal for {label}")
    return Decimal(match.group(1).replace(",", ""))
