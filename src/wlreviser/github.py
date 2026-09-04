"""Strict GitHub pull request and immutable content gateway."""

import base64
import binascii
import json
import re
from pathlib import PurePosixPath
from typing import Literal, Self, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wlreviser.errors import ExternalServiceError, MalformedResponseError, PreflightError
from wlreviser.retry import (
    TRANSIENT_HTTP_STATUSES,
    RetryableHttpStatus,
    retry_after_seconds,
    retry_async,
)

_PR_PATH_PATTERN = re.compile(
    r"^/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)$"
)
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


class _GitHubPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _RepositoryPayload(_GitHubPayload):
    full_name: str


class _BranchPayload(_GitHubPayload):
    sha: str
    repo: _RepositoryPayload


class _PullPayload(_GitHubPayload):
    number: int = Field(gt=0)
    html_url: str
    changed_files: int = Field(ge=0)
    base: _BranchPayload
    head: _BranchPayload


class _ChangedFilePayload(_GitHubPayload):
    filename: str
    status: Literal["added", "removed", "modified", "renamed", "copied", "changed", "unchanged"]
    previous_filename: str | None = None


class _ContentPayload(_GitHubPayload):
    type: str
    encoding: str
    content: str


class PullRequestRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    owner: str
    repository: str
    number: int = Field(gt=0)
    url: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


class PullRequestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    number: int = Field(gt=0)
    url: str
    base_repository: str
    base_sha: str
    head_repository: str
    head_sha: str
    changed_files: int = Field(ge=0)


class ChangedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    filename: str
    status: Literal["added", "removed", "modified", "renamed", "copied", "changed", "unchanged"]
    previous_filename: str | None = None


def parse_pull_request_url(url: str) -> PullRequestRef:
    """Parse only an exact public HTTPS github.com pull request URL."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PreflightError("invalid GitHub pull request URL port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PreflightError("pull request URL must be an exact HTTPS github.com URL")
    match = _PR_PATH_PATTERN.fullmatch(parsed.path)
    if match is None:
        raise PreflightError("pull request URL must have the form https://github.com/owner/repo/pull/number")
    return PullRequestRef(
        owner=match.group("owner"),
        repository=match.group("repo"),
        number=int(match.group("number")),
        url=url,
    )


class GitHubClient:
    """Read pull request metadata and UTF-8 files through GitHub REST."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        if not token:
            raise PreflightError("GITHUB_TOKEN is required")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        self._content_cache: dict[tuple[str, str, str], str | None] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_pull_request(self, pull: PullRequestRef) -> PullRequestMetadata:
        response = await self._request(f"/repos/{pull.owner}/{pull.repository}/pulls/{pull.number}")
        payload = self._payload(response, _PullPayload, "pull request")
        if not _SHA_PATTERN.fullmatch(payload.base.sha) or not _SHA_PATTERN.fullmatch(
            payload.head.sha
        ):
            raise MalformedResponseError(
                "GitHub pull request response contains an invalid commit SHA"
            )
        return PullRequestMetadata(
            number=payload.number,
            url=payload.html_url,
            base_repository=payload.base.repo.full_name,
            base_sha=payload.base.sha,
            head_repository=payload.head.repo.full_name,
            head_sha=payload.head.sha,
            changed_files=payload.changed_files,
        )

    async def get_changed_files(
        self,
        pull: PullRequestRef,
        expected_count: int,
    ) -> tuple[ChangedFile, ...]:
        files: list[ChangedFile] = []
        page = 1
        while len(files) < expected_count:
            response = await self._request(
                f"/repos/{pull.owner}/{pull.repository}/pulls/{pull.number}/files",
                params={"per_page": 100, "page": page},
            )
            try:
                raw_value = cast(object, response.json())
            except json.JSONDecodeError as exc:
                raise MalformedResponseError(
                    "GitHub changed-files response is not valid JSON"
                ) from exc
            if not isinstance(raw_value, list):
                raise MalformedResponseError("GitHub changed-files response must be a list")
            raw = cast(list[object], raw_value)
            page_files: list[ChangedFile] = []
            for item in raw:
                try:
                    parsed = _ChangedFilePayload.model_validate(item)
                except ValidationError as exc:
                    raise MalformedResponseError(
                        "GitHub changed-files response is malformed"
                    ) from exc
                page_files.append(
                    ChangedFile(
                        filename=parsed.filename,
                        status=parsed.status,
                        previous_filename=parsed.previous_filename,
                    )
                )
            files.extend(page_files)
            if len(page_files) < 100:
                break
            page += 1
        if len(files) != expected_count:
            raise MalformedResponseError(
                f"GitHub returned {len(files)} changed files, expected {expected_count}"
            )
        return tuple(files)

    async def read_file(self, repository: str, sha: str, path: str) -> str | None:
        """Read and cache one UTF-8 file at an immutable commit, returning None for 404."""
        self._validate_content_coordinates(repository, sha, path)
        cache_key = (repository.casefold(), sha.casefold(), path)
        if cache_key in self._content_cache:
            return self._content_cache[cache_key]
        encoded_path = quote(path, safe="/")
        response = await self._request(
            f"/repos/{repository}/contents/{encoded_path}",
            params={"ref": sha},
            allow_not_found=True,
        )
        if response.status_code == 404:
            result = None
        else:
            payload = self._payload(response, _ContentPayload, "repository content")
            if payload.type != "file" or payload.encoding != "base64":
                raise MalformedResponseError("GitHub content response is not a base64 file")
            try:
                compact = "".join(payload.content.split())
                result = base64.b64decode(compact, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise MalformedResponseError(
                    "GitHub repository file is not valid base64 UTF-8"
                ) from exc
        self._content_cache[cache_key] = result
        return result

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response:
        async def operation() -> httpx.Response:
            response = await self._client.get(path, params=params)
            if response.status_code in TRANSIENT_HTTP_STATUSES:
                raise RetryableHttpStatus(
                    response.status_code,
                    retry_after_seconds(response.headers.get("Retry-After")),
                )
            return response

        try:
            response = await retry_async(operation)
        except RetryableHttpStatus as exc:
            raise ExternalServiceError(f"GitHub returned HTTP {exc.status_code}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ExternalServiceError("could not connect to GitHub") from exc
        if allow_not_found and response.status_code == 404:
            return response
        if response.is_error:
            raise ExternalServiceError(f"GitHub returned HTTP {response.status_code}")
        return response

    @staticmethod
    def _payload[PayloadT: _GitHubPayload](
        response: httpx.Response,
        model: type[PayloadT],
        label: str,
    ) -> PayloadT:
        try:
            raw = response.json()
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(f"GitHub {label} response is not valid JSON") from exc
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise MalformedResponseError(f"GitHub {label} response is malformed") from exc

    @staticmethod
    def _validate_content_coordinates(repository: str, sha: str, path: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid GitHub repository coordinate")
        if not _SHA_PATTERN.fullmatch(sha):
            raise ValueError("invalid GitHub commit SHA")
        pure_path = PurePosixPath(path)
        if (
            pure_path.is_absolute()
            or not path
            or any(part in {".", ".."} for part in pure_path.parts)
        ):
            raise ValueError("invalid repository path")