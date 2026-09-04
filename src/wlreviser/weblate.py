"""Weblate gateway backed by the official wlc client."""

from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Protocol, TypeVar, cast, runtime_checkable

import requests
from pydantic import BaseModel, ConfigDict, Field
from wlc import Weblate
from wlc.exceptions import (
    WeblateDeniedError,
    WeblateException,
    WeblatePermissionError,
    WeblateThrottlingError,
)

from wlreviser.config import WeblateConfig
from wlreviser.errors import ExternalServiceError, MalformedResponseError, PreflightError
from wlreviser.models import TranslationIdentity
from wlreviser.retry import TRANSIENT_HTTP_STATUSES, retry_after_seconds, retry_sync

ResultT = TypeVar("ResultT")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class WeblateTranslation(_StrictModel):
    project: str
    component: str
    filename: str
    language_code: str
    language_name: str
    api_url: str
    web_url: str


class WeblateUnit(_StrictModel):
    id: int = Field(gt=0)
    api_url: str
    web_url: str
    context: str
    source: tuple[str, ...] = Field(min_length=1)


class WeblateMappingError(ExternalServiceError):
    """A repository translation or unit does not map uniquely to Weblate."""


class WeblateBackend(Protocol):
    def list_translations(self, project: str, component: str) -> Iterable[WeblateTranslation]: ...

    def search_units(
        self, translation: WeblateTranslation, query: str
    ) -> Iterable[WeblateUnit]: ...

    def update_unit(self, unit_id: int, target: list[str], state: int) -> None: ...

    def repository_operation(self, project: str, component: str, operation: str) -> None: ...


@runtime_checkable
class _WlcResource(Protocol):
    def get_data(self) -> object: ...


def _resource_data(value: object, label: str) -> dict[str, object]:
    raw = value.get_data() if isinstance(value, _WlcResource) else value
    if not isinstance(raw, dict):
        raise MalformedResponseError(f"{label} is not an object")
    return cast(dict[str, object], raw)


def _normalize_filename(filename: str) -> str:
    while filename.startswith("./"):
        filename = filename[2:]
    path = PurePosixPath(filename)
    if path.is_absolute() or not filename or any(part in {".", ".."} for part in path.parts):
        raise MalformedResponseError("Weblate returned an invalid translation filename")
    return path.as_posix()


def _as_forms(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        forms: list[str] = []
        values = cast(list[object] | tuple[object, ...], value)
        for item in values:
            if not isinstance(item, str):
                raise MalformedResponseError("Weblate returned invalid translation text forms")
            forms.append(item)
        if forms:
            return tuple(forms)
    raise MalformedResponseError("Weblate returned invalid translation text forms")


def _query_term(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}:"{escaped}"'


class WlcBackend:
    """Small synchronous adapter over public wlc APIs."""

    def __init__(self, api_url: str, token: str) -> None:
        self.client = Weblate(key=token, url=api_url, retries=0, timeout=30)

    def list_translations(self, project: str, component: str) -> Iterable[WeblateTranslation]:
        path = f"components/{project}/{component}/translations/"
        translations = cast(Iterable[object], self.client.list_translations(path))
        for translation in translations:
            data = _resource_data(translation, "Weblate translation metadata")
            language_data = _resource_data(
                data.get("language"),
                "Weblate translation language metadata",
            )
            try:
                yield WeblateTranslation(
                    project=project,
                    component=component,
                    filename=_normalize_filename(str(data["filename"])),
                    language_code=str(data["language_code"]),
                    language_name=str(language_data["name"]),
                    api_url=str(data["url"]),
                    web_url=str(data["web_url"]),
                )
            except KeyError as exc:
                raise MalformedResponseError("Weblate translation metadata is incomplete") from exc

    def search_units(
        self, translation: WeblateTranslation, query: str
    ) -> Iterable[WeblateUnit]:
        path = f"{translation.api_url.rstrip('/')}/units/"
        units = cast(Iterable[object], self.client.list_units(path, params={"q": query}))
        for unit in units:
            data = _resource_data(unit, "Weblate unit metadata")
            try:
                raw_id = data["id"]
                if not isinstance(raw_id, (int, str)):
                    raise TypeError
                yield WeblateUnit(
                    id=int(raw_id),
                    api_url=str(data["url"]),
                    web_url=str(data["web_url"]),
                    context=str(data["context"]),
                    source=_as_forms(data["source"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MalformedResponseError("Weblate unit metadata is incomplete") from exc

    def update_unit(self, unit_id: int, target: list[str], state: int) -> None:
        self.client.raw_request(
            "patch",
            f"units/{unit_id}/",
            data={"target": target, "state": state},
        )

    def repository_operation(self, project: str, component: str, operation: str) -> None:
        self.client.post(
            f"components/{project}/{component}/repository/",
            operation=operation,
        )


def _request_cause(error: BaseException) -> requests.RequestException | None:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, requests.RequestException):
            return current
        current = current.__cause__
    return None


def _is_weblate_transient(error: BaseException) -> bool:
    if isinstance(error, WeblateThrottlingError):
        return True
    cause = _request_cause(error)
    if isinstance(cause, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(cause, requests.HTTPError) and cause.response is not None:
        return cause.response.status_code in TRANSIENT_HTTP_STATUSES
    return False


def _weblate_retry_delay(error: BaseException) -> float:
    if isinstance(error, WeblateThrottlingError):
        return retry_after_seconds(error.retry_after)
    cause = _request_cause(error)
    if isinstance(cause, requests.HTTPError) and cause.response is not None:
        return retry_after_seconds(cause.response.headers.get("Retry-After"))
    return 0


class WeblateGateway:
    """Resolve and mutate only configured Weblate translations and units."""

    def __init__(
        self,
        config: WeblateConfig,
        token: str,
        *,
        backend: WeblateBackend | None = None,
    ) -> None:
        if not token:
            raise PreflightError("WEBLATE_TOKEN is required")
        self.config = config
        self._backend = backend or WlcBackend(config.api_url, token)

    def resolve_translation(
        self,
        component: str,
        repository_path: str,
    ) -> WeblateTranslation | None:
        """Match an exact normalized repository filename from component translation metadata."""
        expected = _normalize_filename(repository_path)
        translations = self._call(
            lambda: tuple(self._backend.list_translations(self.config.project, component))
        )
        matches = [
            translation
            for translation in translations
            if _normalize_filename(translation.filename) == expected
        ]
        if len(matches) > 1:
            raise WeblateMappingError(
                f"multiple Weblate translations map to repository path {repository_path!r}"
            )
        return matches[0] if matches else None

    def resolve_unit(
        self,
        translation: WeblateTranslation,
        identity: TranslationIdentity,
        source: tuple[str, ...],
    ) -> WeblateUnit | None:
        """Resolve a unique unit by exact search terms, source forms, and context."""
        terms = [_query_term("key", identity.key)]
        if identity.context is not None:
            terms.append(_query_term("context", identity.context))
        candidates = self._call(
            lambda: tuple(self._backend.search_units(translation, " ".join(terms)))
        )
        expected_context = identity.context or identity.key
        matches = [
            unit
            for unit in candidates
            if unit.source == source and unit.context == expected_context
        ]
        if len(matches) > 1:
            raise WeblateMappingError(
                f"multiple Weblate units map to translation key {identity.key!r}"
            )
        return matches[0] if matches else None

    def update_unit(self, unit_id: int, target: tuple[str, ...]) -> None:
        """Write a report suggestion directly, deliberately without a stale-state GET."""
        self._call(lambda: self._backend.update_unit(unit_id, list(target), 20))

    def commit_component(self, component: str) -> None:
        self._repository_operation(component, "commit")

    def push_component(self, component: str) -> None:
        self._repository_operation(component, "push")

    def _repository_operation(self, component: str, operation: str) -> None:
        self._call(
            lambda: self._backend.repository_operation(
                self.config.project,
                component,
                operation,
            )
        )

    @staticmethod
    def _call(operation: Callable[[], ResultT]) -> ResultT:
        try:
            return retry_sync(
                operation,
                transient=_is_weblate_transient,
                delay=_weblate_retry_delay,
            )
        except WeblateDeniedError as exc:
            raise ExternalServiceError("Weblate authentication failed") from exc
        except WeblatePermissionError as exc:
            raise ExternalServiceError("Weblate permission denied") from exc
        except MalformedResponseError:
            raise
        except WeblateException as exc:
            cause = _request_cause(exc)
            if isinstance(cause, requests.HTTPError) and cause.response is not None:
                raise ExternalServiceError(
                    f"Weblate returned HTTP {cause.response.status_code}"
                ) from exc
            raise ExternalServiceError("Weblate operation failed") from exc
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ExternalServiceError("could not connect to Weblate") from exc