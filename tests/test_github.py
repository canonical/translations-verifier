import base64

import httpx
import pytest

from wlreviser.errors import PreflightError
from wlreviser.github import GitHubClient, parse_pull_request_url

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/canonical/repo/pull/1",
        "https://github.example/canonical/repo/pull/1",
        "https://github.com/canonical/repo/pull/0",
        "https://github.com/canonical/repo/pull/1/",
        "https://github.com/canonical/repo/pull/1?diff=split",
        "https://user@github.com/canonical/repo/pull/1",
        "https://github.com:443/canonical/repo/pull/1",
    ],
)
def test_parse_pull_request_url_rejects_noncanonical_urls(url: str) -> None:
    with pytest.raises(PreflightError):
        parse_pull_request_url(url)


async def test_github_uses_fork_metadata_paginates_and_caches_content() -> None:
    content_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal content_requests
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "html_url": "https://github.com/canonical/repo/pull/7",
                    "changed_files": 101,
                    "base": {"sha": BASE_SHA, "repo": {"full_name": "canonical/repo"}},
                    "head": {"sha": HEAD_SHA, "repo": {"full_name": "contributor/repo"}},
                },
            )
        if request.url.path.endswith("/pulls/7/files"):
            page = int(request.url.params["page"])
            count = 100 if page == 1 else 1
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": f"l10n/app_{page}_{index}.arb",
                        "status": "modified",
                    }
                    for index in range(count)
                ],
            )
        if "/contents/" in request.url.path:
            content_requests += 1
            assert request.url.params["ref"] == HEAD_SHA
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(b'{"hello": "Hallo"}').decode(),
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    pull = parse_pull_request_url("https://github.com/canonical/repo/pull/7")
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = GitHubClient("secret", http_client)
        metadata = await client.get_pull_request(pull)
        files = await client.get_changed_files(pull, metadata.changed_files)
        first = await client.read_file(
            metadata.head_repository, metadata.head_sha, "l10n/app_de.arb"
        )
        second = await client.read_file(
            metadata.head_repository, metadata.head_sha, "l10n/app_de.arb"
        )

    assert metadata.base_repository == "canonical/repo"
    assert metadata.head_repository == "contributor/repo"
    assert len(files) == 101
    assert first == second == '{"hello": "Hallo"}'
    assert content_requests == 1


async def test_github_retries_one_transient_status_without_exposing_body() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "0"}, text="sensitive body")
        return httpx.Response(
            200,
            json={
                "number": 1,
                "html_url": "https://github.com/canonical/repo/pull/1",
                "changed_files": 0,
                "base": {"sha": BASE_SHA, "repo": {"full_name": "canonical/repo"}},
                "head": {"sha": HEAD_SHA, "repo": {"full_name": "canonical/repo"}},
            },
        )

    pull = parse_pull_request_url("https://github.com/canonical/repo/pull/1")
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http_client:
        metadata = await GitHubClient("secret", http_client).get_pull_request(pull)

    assert metadata.number == 1
    assert calls == 2