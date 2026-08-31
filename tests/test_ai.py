import json

import httpx
import pytest
import respx

from translations_verifier.ai import AiReviewer, AiReviewError
from translations_verifier.arb import parse_arb
from translations_verifier.config import AiConfig
from translations_verifier.diff import diff_translations


def _config(**overrides: object) -> AiConfig:
    return AiConfig.model_validate(
        {
            "endpoint": "https://model.example/v1",
            "model": "review-model",
            "api_key_env": "MODEL_API_KEY",
            **overrides,
        }
    )


def _operation():
    source = parse_arb(b'{"hello":"Hello"}', "l10n/app_en.arb")
    target = parse_arb(b'{"hello":"Hallo"}', "l10n/app_de.arb")
    return diff_translations(source=source, before=None, after=target, locale="de")[0]


def _response(**overrides: object) -> dict[str, object]:
    verdict = {
        "key": "hello",
        "acceptable": True,
        "categories": [],
        "rationale": "Meaning is preserved.",
        "confidence": 0.94,
        "human_review_note": None,
        "suggested_translation": None,
    }
    verdict.update(overrides)
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


@respx.mock
def test_review_sends_isolated_bounded_request() -> None:
    route = respx.post("https://model.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_response())
    )

    with AiReviewer(_config(), "secret") as reviewer:
        verdict, usage = reviewer.review(_operation(), {"fr": "Bonjour"})

    request = route.calls.last.request
    body = json.loads(request.content)
    assert verdict.acceptable is True
    assert usage.total_tokens == 120
    assert body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    verdict_schema = body["response_format"]["json_schema"]["schema"]
    assert "suggested_translation" in verdict_schema["required"]
    assert "tools" not in body
    assert request.headers["authorization"] == "Bearer secret"
    user_data = json.loads(body["messages"][1]["content"])
    assert user_data["base_branch_context"] == {"fr": "Bonjour"}


@respx.mock
def test_review_falls_back_to_json_object_on_schema_rejection() -> None:
    route = respx.post("https://model.example/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(400, json={"error": "unsupported response format"}),
            httpx.Response(200, json=_response()),
        ]
    )

    with AiReviewer(_config(), "secret") as reviewer:
        verdict, _ = reviewer.review(_operation())

    assert verdict.acceptable is True
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [body["response_format"]["type"] for body in bodies] == [
        "json_schema",
        "json_object",
    ]


@respx.mock
def test_review_rejects_tool_calls() -> None:
    respx.post("https://model.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"content": "{}", "tool_calls": [{"id": "bad"}]},
                    }
                ]
            },
        )
    )

    with (
        AiReviewer(_config(max_retries=0), "secret") as reviewer,
        pytest.raises(AiReviewError, match="tool call"),
    ):
        reviewer.review(_operation())


@respx.mock
def test_rejected_review_requires_suggested_translation() -> None:
    respx.post("https://model.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_response(
                acceptable=False,
                categories=["meaning"],
                rationale="The greeting is mistranslated.",
            ),
        )
    )

    with (
        AiReviewer(_config(max_retries=0), "secret") as reviewer,
        pytest.raises(AiReviewError, match="required schema"),
    ):
        reviewer.review(_operation())


@respx.mock
def test_rejected_review_returns_suggested_translation() -> None:
    respx.post("https://model.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_response(
                acceptable=False,
                categories=["meaning"],
                rationale="The greeting is mistranslated.",
                suggested_translation="Hallo",
            ),
        )
    )

    with AiReviewer(_config(), "secret") as reviewer:
        verdict, _ = reviewer.review(_operation())

    assert verdict.suggested_translation == "Hallo"
