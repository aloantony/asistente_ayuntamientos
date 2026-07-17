"""Privacy-preserving identifiers shared by OpenAI assistant runtimes."""

import hashlib
import hmac

from app.core.config import settings


def build_assistant_safety_identifier(user_id: int) -> str:
    """Return a stable HMAC that does not expose the internal user ID."""
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"assistant-user:{user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
