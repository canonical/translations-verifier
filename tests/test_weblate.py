from collections.abc import Iterable

import pytest
import requests

from wlreviser.config import WeblateConfig
from wlreviser.errors import ExternalServiceError
from wlreviser.models import TranslationIdentity
from wlreviser.weblate import (
    WeblateBackend,
    WeblateGateway,
    WeblateMappingError,
    WeblateTranslation,
    WeblateUnit,
    WlcBackend,
)


def translation(
    filename: str,
    language_code: str = "zh_Hant",
    *,
    api_language: str | None = None,
) -> WeblateTranslation:
    api_language = api_language or language_code
    return WeblateTranslation(
        project="project",
        component="component",
        filename=filename,
        language_code=language_code,
        language_name="Chinese (Traditional)",
        api_url=f"https://weblate.example/api/translations/project/component/{api_language}/",
        web_url=f"https://weblate.example/projects/project/component/{language_code}/",
    )


class FakeBackend(WeblateBackend):
    def __init__(self) -> None:
        self.translations: list[WeblateTranslation] = []
        self.units: list[WeblateUnit] = []
        self.queries: list[str] = []
        self.updates: list[tuple[int, list[str], int]] = []
        self.operations: list[tuple[str, str, str]] = []

    def list_translations(self, project: str, component: str) -> Iterable[WeblateTranslation]:
        return self.translations

    def search_units(self, translation: WeblateTranslation, query: str) -> Iterable[WeblateUnit]:
        self.queries.append(query)
        return self.units

    def update_unit(self, unit_id: int, target: list[str], state: int) -> None:
        self.updates.append((unit_id, target, state))

    def repository_operation(self, project: str, component: str, operation: str) -> None:
        self.operations.append((project, component, operation))


def gateway(backend: WeblateBackend) -> WeblateGateway:
    return WeblateGateway(
        WeblateConfig(api_url="https://weblate.example/api", project="project"),
        "token",
        backend=backend,
    )


def test_resolves_filename_locale_from_exact_translation_metadata() -> None:
    backend = FakeBackend()
    backend.translations = [translation("./l10n/app_zh_TW.arb")]

    resolved = gateway(backend).resolve_translation("component", "l10n/app_zh_TW.arb")

    assert resolved is not None
    assert resolved.language_code == "zh_Hant"


def test_rejects_ambiguous_translation_mapping() -> None:
    backend = FakeBackend()
    backend.translations = [
        translation("l10n/app_de.arb", "de"),
        translation("l10n/app_de.arb", "de_DE"),
    ]

    with pytest.raises(WeblateMappingError, match="multiple Weblate translations"):
        gateway(backend).resolve_translation("component", "l10n/app_de.arb")


def test_resolves_unique_unit_and_updates_without_reading_it_again() -> None:
    backend = FakeBackend()
    resolved_translation = translation("l10n/app_de.arb", "de")
    backend.units = [
        WeblateUnit(
            id=42,
            api_url="https://weblate.example/api/units/42/",
            web_url="https://weblate.example/translate/project/component/de/?checksum=x",
            context="menu",
            source=("Files",),
        )
    ]
    client = gateway(backend)

    unit = client.resolve_unit(
        resolved_translation,
        TranslationIdentity(key="files-key", context="menu"),
        ("Files",),
    )
    assert unit is not None
    client.update_unit(unit.id, ("Dateien",))
    client.commit_component("component")
    client.push_component("component")

    assert backend.queries == ['key:"files-key" context:"menu"']
    assert backend.updates == [(42, ["Dateien"], 20)]
    assert backend.operations == [
        ("project", "component", "commit"),
        ("project", "component", "push"),
    ]


def test_wlc_unit_search_uses_canonical_translation_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WlcBackend("https://weblate.example/api/", "token")
    paths: list[str] = []

    def list_units(path: str, params: object = None) -> tuple[()]:
        paths.append(path)
        return ()

    monkeypatch.setattr(backend.client, "list_units", list_units)

    tuple(
        backend.search_units(
            translation("l10n/app_zh.arb", "zh", api_language="zh_Hans"),
            'key:"welcome"',
        )
    )

    assert paths == [
        "https://weblate.example/api/translations/project/component/zh_Hans/units/"
    ]


def test_retries_connection_failure_once() -> None:
    class FlakyBackend(FakeBackend):
        calls = 0

        def list_translations(self, project: str, component: str) -> Iterable[WeblateTranslation]:
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError("temporary")
            return [translation("l10n/app_de.arb", "de")]

    backend = FlakyBackend()

    assert gateway(backend).resolve_translation("component", "l10n/app_de.arb") is not None
    assert backend.calls == 2


def test_sanitizes_weblate_exception_text() -> None:
    class FailingBackend(FakeBackend):
        def update_unit(self, unit_id: int, target: list[str], state: int) -> None:
            from wlc.exceptions import WeblateException

            raise WeblateException("response body contains SENTINEL_TOKEN")

    with pytest.raises(ExternalServiceError, match="Weblate operation failed") as raised:
        gateway(FailingBackend()).update_unit(1, ("text",))

    assert "SENTINEL_TOKEN" not in str(raised.value)


def test_reports_weblate_http_status_without_response_body() -> None:
    class FailingBackend(FakeBackend):
        def update_unit(self, unit_id: int, target: list[str], state: int) -> None:
            from wlc.exceptions import WeblateException

            response = requests.Response()
            response.status_code = 404
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise WeblateException("response body contains SENTINEL_TOKEN") from exc

    with pytest.raises(ExternalServiceError, match="Weblate returned HTTP 404") as raised:
        gateway(FailingBackend()).update_unit(1, ("text",))

    assert "SENTINEL_TOKEN" not in str(raised.value)