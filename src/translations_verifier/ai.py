from __future__ import annotations

import json
import time
from typing import Any

import httpx
from pydantic import ValidationError

from translations_verifier.config import AiConfig
from translations_verifier.models import AiUsage, AiVerdict, TranslationOperation
from translations_verifier.prompts import SYSTEM_PROMPT, build_review_payload


class AiReviewError(RuntimeError):
    pass


class _AiSchemaError(AiReviewError):
    pass


class AiReviewer:
    def __init__(
        self, config: AiConfig, api_key: str, *, client: httpx.Client | None = None
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.endpoint.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "translations-verifier"},
            timeout=config.timeout_seconds,
        )

    def __enter__(self) -> AiReviewer:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def review(
        self,
        operation: TranslationOperation,
        context: dict[str, str] | None = None,
    ) -> tuple[AiVerdict, AiUsage]:
        user_payload = build_review_payload(operation, context)
        if len(user_payload) > self.config.max_input_chars_per_key:
            raise AiReviewError("AI input exceeds the configured per-key character limit")

        schema_retries_remaining = min(self.config.max_retries, 1)
        while True:
            response = self._complete(user_payload)
            try:
                return self._validate_response(response, operation.key)
            except _AiSchemaError:
                if schema_retries_remaining == 0:
                    raise
                schema_retries_remaining -= 1

    def _complete(self, user_payload: str) -> httpx.Response:
        schema_mode = True
        malformed_attempts = 0
        while True:
            body = self._request_body(user_payload, schema_mode=schema_mode)
            response = self._post(body)
            if (
                response.status_code == 400
                and schema_mode
                and self.config.allow_json_object_fallback
            ):
                schema_mode = False
                continue
            if not response.is_success:
                raise AiReviewError(f"AI provider returned HTTP {response.status_code}")
            try:
                self._extract_content(response)
            except AiReviewError:
                if malformed_attempts >= min(self.config.max_retries, 1):
                    raise
                malformed_attempts += 1
                continue
            return response

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.post("/chat/completions", json=body)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.config.max_retries:
                    raise AiReviewError("AI provider request failed after retries") from exc
                time.sleep(0.25 * (2**attempt))
                continue
            if (
                response.status_code in {429, 500, 502, 503, 504}
                and attempt < self.config.max_retries
            ):
                time.sleep(0.25 * (2**attempt))
                continue
            return response
        raise AiReviewError("AI provider request failed")

    def _request_body(self, user_payload: str, *, schema_mode: bool) -> dict[str, Any]:
        if schema_mode:
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "translation_verdict",
                    "strict": True,
                    "schema": AiVerdict.model_json_schema(),
                },
            }
        else:
            response_format = {"type": "json_object"}
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "response_format": response_format,
            "stream": False,
        }

    def _validate_response(
        self,
        response: httpx.Response,
        expected_key: str,
    ) -> tuple[AiVerdict, AiUsage]:
        content, payload = self._extract_content(response)
        try:
            verdict = AiVerdict.model_validate_json(content)
        except ValidationError as exc:
            raise _AiSchemaError(
                "AI provider returned a response outside the required schema"
            ) from exc
        if verdict.key != expected_key:
            raise AiReviewError("AI provider returned a verdict for an unexpected key")

        raw_usage = payload.get("usage", {})
        if not isinstance(raw_usage, dict):
            raw_usage = {}
        prompt_tokens = _nonnegative_int(raw_usage.get("prompt_tokens"))
        completion_tokens = _nonnegative_int(raw_usage.get("completion_tokens"))
        total_tokens = _nonnegative_int(raw_usage.get("total_tokens"))
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens
        return verdict, AiUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    def _extract_content(self, response: httpx.Response) -> tuple[str, dict[str, Any]]:
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
            content = message["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AiReviewError("AI provider returned a malformed response envelope") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(choice, dict)
            or not isinstance(message, dict)
        ):
            raise AiReviewError("AI provider returned a malformed response envelope")
        if message.get("tool_calls") or message.get("function_call"):
            raise AiReviewError("AI provider attempted to return a tool call")
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise AiReviewError(f"AI review did not complete: {choice['finish_reason']}")
        if not isinstance(content, str):
            raise AiReviewError("AI provider returned non-text content")
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AiReviewError("AI provider returned invalid JSON content") from exc
        if not isinstance(decoded, dict):
            raise AiReviewError("AI provider returned non-object JSON content")
        return content, payload


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
