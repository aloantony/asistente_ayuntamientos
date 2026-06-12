"""Privacy/AI Gateway v1.

Single choke point for every call to the external AI API. Principles
(README §2, ADR-009): conversation text and structured requirement fields
typed by the user are the only data sent; original documents and stored
municipal files are never read or transmitted by this module. Logging is
metadata-only (model, token usage, stop reason) — never message content.
"""

import logging

import anthropic

from app.core.config import settings

logger = logging.getLogger(__name__)


class AssistantUnavailableError(Exception):
    """The AI gateway is not configured or the upstream API failed."""


class AIGateway:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    @property
    def enabled(self) -> bool:
        return bool(settings.anthropic_api_key)

    def _get_client(self) -> anthropic.Anthropic:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> anthropic.types.Message:
        client = self._get_client()
        try:
            response = client.messages.create(
                model=settings.assistant_model,
                max_tokens=settings.assistant_max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=messages,
                tools=tools,
            )
        except anthropic.APIStatusError as error:
            logger.error(
                "Assistant API error: status=%s type=%s",
                error.status_code,
                getattr(error, "type", None),
            )
            raise AssistantUnavailableError("Assistant API request failed") from error
        except anthropic.APIConnectionError as error:
            logger.error("Assistant API connection error")
            raise AssistantUnavailableError(
                "Assistant API connection failed"
            ) from error

        logger.info(
            "Assistant completion: model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            response.model,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return response


gateway = AIGateway()
