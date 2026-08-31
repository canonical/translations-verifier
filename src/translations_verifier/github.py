from __future__ import annotations

import re
import time
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from translations_verifier.models import ChangedFile, PullRequestInfo


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self,
        repository: str,
        token: str | None = None,
        *,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "translations-verifier",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.repository = repository
        self.max_retries = max_retries
        self._client = httpx.Client(base_url=api_url, headers=headers, timeout=timeout)

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_pull_request(self, locator: str) -> PullRequestInfo:
        repository, number = parse_pr_locator(locator, self.repository)
        response = self._request("GET", f"/repos/{repository}/pulls/{number}")
        payload = _json_object(response, "pull request")
        try:
            base = payload["base"]
            head = payload["head"]
            base_repo = base["repo"]["full_name"]
            return PullRequestInfo(
                repository=str(base_repo),
                number=number,
                base_ref=str(base["ref"]),
                base_sha=str(base["sha"]),
                head_sha=str(head["sha"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("GitHub returned malformed pull request metadata") from exc

    def list_changed_files(self, number: int, max_files: int) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{self.repository}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubError("GitHub returned malformed changed-file metadata")
            for item in payload:
                if not isinstance(item, dict):
                    raise GitHubError("GitHub returned malformed changed-file entry")
                path = normalize_repository_path(str(item.get("filename", "")))
                previous = item.get("previous_filename")
                files.append(
                    ChangedFile(
                        path=path,
                        status=str(item.get("status", "unknown")),
                        previous_path=(
                            normalize_repository_path(str(previous)) if previous else None
                        ),
                        additions=int(item.get("additions", 0)),
                        deletions=int(item.get("deletions", 0)),
                        changes=int(item.get("changes", 0)),
                    )
                )
                if len(files) > max_files:
                    raise GitHubError(f"pull request exceeds the {max_files} changed-file limit")
            if len(payload) < 100:
                return files
            page += 1

    def get_file(self, path: str, sha: str, *, allow_missing: bool = False) -> bytes | None:
        normalized = normalize_repository_path(path)
        encoded_path = quote(normalized, safe="/")
        response = self._request(
            "GET",
            f"/repos/{self.repository}/contents/{encoded_path}",
            params={"ref": sha},
            headers={"Accept": "application/vnd.github.raw+json"},
            allowed_statuses={404} if allow_missing else set(),
        )
        if response.status_code == 404:
            return None
        return response.content

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allowed_statuses: set[int] | None = None,
    ) -> httpx.Response:
        allowed_statuses = allowed_statuses or set()
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(method, path, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_retries:
                    raise GitHubError("GitHub request failed after retries") from exc
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code in allowed_statuses or response.is_success:
                return response
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(0.25 * (2**attempt))
                continue
            raise GitHubError(f"GitHub request failed with HTTP {response.status_code} for {path}")
        raise GitHubError("GitHub request failed")


def parse_pr_locator(locator: str, configured_repository: str) -> tuple[str, int]:
    if locator.isdigit():
        number = int(locator)
        if number < 1:
            raise GitHubError("pull request number must be positive")
        return configured_repository, number

    parsed = urlparse(locator)
    match = re.fullmatch(r"/([^/]+)/([^/]+)/pull/(\d+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not match:
        raise GitHubError(
            "pull request must be a number or https://github.com/owner/repo/pull/NUMBER"
        )
    repository = f"{match.group(1)}/{match.group(2)}"
    if repository.casefold() != configured_repository.casefold():
        raise GitHubError(f"pull request belongs to {repository}, expected {configured_repository}")
    return configured_repository, int(match.group(3))


def normalize_repository_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise GitHubError(f"unsafe repository path {path!r}")
    return str(candidate)


def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubError(f"GitHub returned invalid JSON for {label}") from exc
    if not isinstance(payload, dict):
        raise GitHubError(f"GitHub returned malformed {label} metadata")
    return payload
