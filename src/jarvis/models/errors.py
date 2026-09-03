"""Model error taxonomy (ADR 0005).

Every error carries provider + model (+ status_code when an HTTP response
existed). Retry policy: ONLY RateLimitError and ConnectionError are retried
(max 2, exponential backoff) — owned by the runtime/strategy layer, not the
adapter.
"""

from __future__ import annotations


class ModelError(Exception):
    """Base for all model-provider failures."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.status_code = status_code

    @property
    def kind(self) -> str:
        return "model"


class ModelConnectionError(ModelError):
    kind = "connection"


class ModelAuthError(ModelError):
    kind = "auth"


class ModelRateLimitError(ModelError):
    kind = "rate_limit"

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        provider: str = "",
        model: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model, status_code=status_code)
        self.retry_after = retry_after


class ModelBadRequestError(ModelError):
    kind = "bad_request"


class ModelTimeoutError(ModelError):
    kind = "timeout"


class ModelAbortedError(ModelError):
    """The call was cancelled via the CancellationToken."""

    kind = "aborted"


class ModelStreamError(ModelError):
    kind = "stream"


RETRYABLE_ERRORS = (ModelRateLimitError, ModelConnectionError)

__all__ = [
    "ModelAbortedError",
    "ModelAuthError",
    "ModelBadRequestError",
    "ModelConnectionError",
    "ModelError",
    "ModelRateLimitError",
    "ModelStreamError",
    "ModelTimeoutError",
    "RETRYABLE_ERRORS",
]
