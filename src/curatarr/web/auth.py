"""Validate Telegram launch data; never trust client-supplied user IDs."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthenticationError(ValueError):
    """Invalid, expired, or unauthorized launch data."""


def validate_init_data(raw: str, token: str, allowed_users: frozenset[int], *,
                       now: float | None = None, max_age: int = 3600) -> int:
    try:
        if not raw or len(raw) > 8192:
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError
        received = fields.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError
        timestamp = int(fields["auth_date"])
        age = (time.time() if now is None else now) - timestamp
        if age < -30 or age > max_age:
            raise ValueError
        user_id = json.loads(fields["user"])["id"]
        if type(user_id) is not int or user_id not in allowed_users:
            raise ValueError
        return user_id
    except (ValueError, KeyError, TypeError, OverflowError):
        raise AuthenticationError("Open Curatarr again from your authorized Telegram account.") from None
