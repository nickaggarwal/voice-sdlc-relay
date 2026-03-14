"""Authentication helpers for the relay server."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException


def verify_secret(secret: str | None) -> None:
    """Verify the provided secret matches the configured RELAY_SECRET.

    Uses constant-time comparison to prevent timing attacks.

    Raises:
        HTTPException: 401 if the secret is missing or invalid.
    """
    expected = os.environ.get("RELAY_SECRET", "change-me-in-production")
    if secret is None:
        raise HTTPException(status_code=401, detail="Missing secret parameter")
    if not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=401, detail="Invalid secret")
