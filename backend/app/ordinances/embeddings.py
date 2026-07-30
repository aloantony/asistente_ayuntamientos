import hashlib
import json
import math
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from app.core.config import settings
from app.core.http import urlopen_without_redirects


class EmbeddingsUnavailableError(Exception):
    pass


def embed_text(text: str) -> tuple[str | None, str, str]:
    """Return (vector string, model, status) for storage in pgvector/text.

    The local hash runtime is deterministic and network-free. It is good enough
    for tests and development, while production can switch to an EU-hosted
    OpenAI-compatible embeddings endpoint through configuration.
    """
    clean_text = text.strip()
    if settings.embeddings_runtime == "disabled" or not clean_text:
        return None, settings.embeddings_model, "disabled"
    if settings.embeddings_runtime == "openai_compatible":
        return (
            _format_vector(_embed_openai_compatible(clean_text)),
            settings.embeddings_model,
            "ready",
        )
    return (
        _format_vector(_embed_local_hash(clean_text, settings.embeddings_dimensions)),
        settings.embeddings_model,
        "ready",
    )


def vector_similarity(first: str | None, second: str | None) -> float:
    first_vector = _parse_vector(first)
    second_vector = _parse_vector(second)
    if not first_vector or not second_vector or len(first_vector) != len(second_vector):
        return 0.0
    dot = sum(a * b for a, b in zip(first_vector, second_vector))
    first_norm = math.sqrt(sum(value * value for value in first_vector))
    second_norm = math.sqrt(sum(value * value for value in second_vector))
    if first_norm == 0 or second_norm == 0:
        return 0.0
    return dot / (first_norm * second_norm)


def _embed_openai_compatible(text: str) -> list[float]:
    if not settings.embeddings_base_url or not settings.embeddings_api_key:
        raise EmbeddingsUnavailableError("Embeddings provider is not configured")

    payload = {
        "model": settings.embeddings_model,
        "input": text,
    }
    request = urlrequest.Request(
        f"{settings.embeddings_base_url.rstrip('/')}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.embeddings_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen_without_redirects(
            request,
            timeout=settings.embeddings_timeout_seconds,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urlerror.HTTPError, urlerror.URLError, TimeoutError) as error:
        raise EmbeddingsUnavailableError("Embeddings request failed") from error
    try:
        embedding = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as error:
        raise EmbeddingsUnavailableError("Embeddings response is invalid") from error
    if not isinstance(embedding, list) or not all(
        isinstance(value, (int, float)) for value in embedding
    ):
        raise EmbeddingsUnavailableError("Embeddings response is invalid")
    return [float(value) for value in embedding]


def _embed_local_hash(text: str, dimensions: int) -> list[float]:
    vector = [0.0 for _ in range(dimensions)]
    for raw_word in text.lower().split():
        word = "".join(character for character in raw_word if character.isalnum())
        if not word:
            continue
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        weight = 1.0 + (digest[5] / 255.0)
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _format_vector(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def _parse_vector(value: str | None) -> list[float]:
    if not value:
        return []
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    vector: list[float] = []
    for item in parsed:
        if not isinstance(item, (int, float)):
            return []
        vector.append(float(item))
    return vector
