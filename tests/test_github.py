import httpx
import pytest
import respx

from translations_verifier.github import GitHubClient, GitHubError, parse_pr_locator


def test_parse_pr_locator_rejects_other_repository() -> None:
    with pytest.raises(GitHubError, match="expected canonical/example"):
        parse_pr_locator("https://github.com/other/example/pull/42", "canonical/example")


@respx.mock
def test_client_uses_immutable_shas_and_paginates() -> None:
    pr_route = respx.get("https://api.github.com/repos/canonical/example/pulls/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "base": {
                    "ref": "main",
                    "sha": "base-sha",
                    "repo": {"full_name": "canonical/example"},
                },
                "head": {"sha": "head-sha"},
            },
        )
    )
    first_page = [
        {
            "filename": f"l10n/app_{index}.arb",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
        }
        for index in range(100)
    ]
    respx.get(
        "https://api.github.com/repos/canonical/example/pulls/42/files",
        params={"per_page": 100, "page": 1},
    ).mock(return_value=httpx.Response(200, json=first_page))
    respx.get(
        "https://api.github.com/repos/canonical/example/pulls/42/files",
        params={"per_page": 100, "page": 2},
    ).mock(return_value=httpx.Response(200, json=[]))
    file_route = respx.get(
        "https://api.github.com/repos/canonical/example/contents/l10n/app_de.arb",
        params={"ref": "head-sha"},
    ).mock(return_value=httpx.Response(200, content=b'{"hello":"Hallo"}'))

    with GitHubClient("canonical/example", token="secret") as client:
        pull_request = client.get_pull_request("42")
        files = client.list_changed_files(42, max_files=101)
        content = client.get_file("l10n/app_de.arb", pull_request.head_sha)

    assert pull_request.base_sha == "base-sha"
    assert len(files) == 100
    assert content == b'{"hello":"Hallo"}'
    assert pr_route.called
    assert file_route.calls.last.request.headers["authorization"] == "Bearer secret"
    assert file_route.calls.last.request.headers["accept"] == "application/vnd.github.raw+json"


def test_client_rejects_path_traversal() -> None:
    with (
        GitHubClient("canonical/example") as client,
        pytest.raises(GitHubError, match="unsafe repository path"),
    ):
        client.get_file("../secret", "head-sha")
