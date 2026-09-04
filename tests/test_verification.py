from wlreviser.config import ProjectConfig
from wlreviser.github import ChangedFile, PullRequestMetadata, PullRequestRef
from wlreviser.models import AIReview, ItemStatus, ReviewRequest, ReviewVerdict, TranslationIdentity
from wlreviser.verification import VerificationService
from wlreviser.weblate import WeblateTranslation, WeblateUnit

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "weblate": {"api_url": "https://weblate.example/api", "project": "project"},
            "github": {"repository": "canonical/repo"},
            "ai": {"api_url": "http://localhost:11434/v1", "model": "model"},
            "components": [
                {
                    "weblate_component": "app",
                    "source_locale": "en",
                    "context_locales": ["fr"],
                    "format": "arb",
                    "source_file": "l10n/app_en.arb",
                    "path": "l10n/app_{locale}.arb",
                }
            ],
        }
    )


class FakeGitHub:
    def __init__(self, *, structural_removal: bool = False) -> None:
        self.structural_removal = structural_removal
        self.reads: list[tuple[str, str, str]] = []

    async def get_pull_request(self, pull: PullRequestRef) -> PullRequestMetadata:
        return PullRequestMetadata(
            number=pull.number,
            url=pull.url,
            base_repository="canonical/repo",
            base_sha=BASE_SHA,
            head_repository="contributor/repo",
            head_sha=HEAD_SHA,
            changed_files=1 if self.structural_removal else 2,
        )

    async def get_changed_files(
        self, pull: PullRequestRef, expected_count: int
    ) -> tuple[ChangedFile, ...]:
        if self.structural_removal:
            return (ChangedFile(filename="l10n/app_de.arb", status="modified"),)
        return (
            ChangedFile(filename="l10n/app_de.arb", status="modified"),
            ChangedFile(filename="l10n/app_fr.arb", status="modified"),
        )

    async def read_file(self, repository: str, sha: str, path: str) -> str | None:
        self.reads.append((repository, sha, path))
        values = {
            ("canonical/repo", BASE_SHA, "l10n/app_en.arb"): '{"hello":"Welcome","bye":"Bye"}',
            ("canonical/repo", BASE_SHA, "l10n/app_de.arb"): (
                '{"hello":"Willkommen","bye":"Tschüss"}'
            ),
            ("canonical/repo", BASE_SHA, "l10n/app_fr.arb"): (
                '{"hello":"Bienvenue","bye":"Au revoir"}'
            ),
            ("contributor/repo", HEAD_SHA, "l10n/app_de.arb"): (
                '{"bye":"Tschüss"}'
                if self.structural_removal
                else '{"hello":"Willkommn","bye":"Tschüss"}'
            ),
            ("contributor/repo", HEAD_SHA, "l10n/app_fr.arb"): (
                '{"hello":"Bienvenue","bye":"Adieu"}'
            ),
        }
        return values.get((repository, sha, path))


class FakeReviewer:
    def __init__(self) -> None:
        self.requests: list[ReviewRequest] = []

    async def review(self, request: ReviewRequest) -> AIReview:
        self.requests.append(request)
        if request.source == ("Welcome",):
            return AIReview(
                verdict=ReviewVerdict.REJECT,
                reason="The translation contains a typo.",
                suggested_translation="Willkommen",
            )
        return AIReview(verdict=ReviewVerdict.OK, reason=None, suggested_translation=None)


class FakeWeblate:
    def resolve_translation(
        self, component: str, repository_path: str
    ) -> WeblateTranslation | None:
        locale = repository_path.removeprefix("l10n/app_").removesuffix(".arb")
        return WeblateTranslation(
            project="project",
            component=component,
            filename=repository_path,
            language_code={"de": "de", "fr": "fr_FR"}[locale],
            language_name={"de": "German", "fr": "French"}[locale],
            api_url=f"https://weblate.example/api/translations/project/app/{locale}/",
            web_url=f"https://weblate.example/projects/project/app/{locale}/",
        )

    def resolve_unit(
        self,
        translation: WeblateTranslation,
        identity: TranslationIdentity,
        source: tuple[str, ...],
    ) -> WeblateUnit | None:
        return WeblateUnit(
            id=10,
            api_url="https://weblate.example/api/units/10/",
            web_url="https://weblate.example/translate/project/app/de/?checksum=x",
            context=identity.key,
            source=source,
        )


def plural_config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "weblate": {"api_url": "https://weblate.example/api", "project": "project"},
            "github": {"repository": "canonical/repo"},
            "ai": {"api_url": "http://localhost:11434/v1", "model": "model"},
            "components": [
                {
                    "weblate_component": "messages",
                    "source_locale": "en",
                    "format": "po",
                    "source_file": "l10n/messages_en.po",
                    "path": "l10n/messages_{locale}.po",
                }
            ],
        }
    )


def plural_po(one: str, many: str) -> str:
    return f'''msgid ""
msgstr "Project-Id-Version: test\\n"

msgid "files"
msgid_plural "files-plural"
msgstr[0] "{one}"
msgstr[1] "{many}"
'''


class PluralGitHub:
    async def get_pull_request(self, pull: PullRequestRef) -> PullRequestMetadata:
        return PullRequestMetadata(
            number=pull.number,
            url=pull.url,
            base_repository="canonical/repo",
            base_sha=BASE_SHA,
            head_repository="canonical/repo",
            head_sha=HEAD_SHA,
            changed_files=1,
        )

    async def get_changed_files(
        self, pull: PullRequestRef, expected_count: int
    ) -> tuple[ChangedFile, ...]:
        return (ChangedFile(filename="l10n/messages_de.po", status="modified"),)

    async def read_file(self, repository: str, sha: str, path: str) -> str | None:
        values = {
            (BASE_SHA, "l10n/messages_en.po"): plural_po("One file", "Many files"),
            (BASE_SHA, "l10n/messages_de.po"): plural_po("Eine Datei", "Viele Dateien"),
            (HEAD_SHA, "l10n/messages_de.po"): plural_po("Eine Datei", "Viele Datein"),
        }
        return values.get((sha, path))


class PluralWeblate:
    def resolve_translation(
        self, component: str, repository_path: str
    ) -> WeblateTranslation:
        return WeblateTranslation(
            project="project",
            component=component,
            filename=repository_path,
            language_code="de",
            language_name="German",
            api_url="https://weblate.example/api/translations/project/messages/de/",
            web_url="https://weblate.example/projects/project/messages/de/",
        )

    def resolve_unit(
        self,
        translation: WeblateTranslation,
        identity: TranslationIdentity,
        source: tuple[str, ...],
    ) -> WeblateUnit | None:
        raise AssertionError("multi-form corrections must not resolve an apply target")


class PluralReviewer:
    async def review(self, request: ReviewRequest) -> AIReview:
        return AIReview(
            verdict=ReviewVerdict.REJECT,
            reason="The plural translation contains a typo.",
            suggested_translation="Viele Dateien",
        )


async def test_verification_uses_snapshots_peer_context_and_deterministic_results() -> None:
    github = FakeGitHub()
    reviewer = FakeReviewer()
    service = VerificationService(config(), github, FakeWeblate(), reviewer)

    report = await service.verify("https://github.com/canonical/repo/pull/7")

    assert report.base_sha == BASE_SHA
    assert report.head_sha == HEAD_SHA
    assert report.counts.rejected == 1
    assert report.counts.passed == 1
    assert [item.path for item in report.items] == ["l10n/app_de.arb", "l10n/app_fr.arb"]
    rejection = report.items[0]
    assert rejection.status is ItemStatus.REJECTED
    assert rejection.applyable is True
    assert rejection.unit_id == 10
    assert rejection.weblate_locale == "de"
    assert reviewer.requests[0].peer_context[0].locale == "fr"
    assert reviewer.requests[0].peer_context[0].forms == ("Bienvenue",)
    assert ("canonical/repo", BASE_SHA, "l10n/app_en.arb") in github.reads
    assert ("contributor/repo", HEAD_SHA, "l10n/app_de.arb") in github.reads


async def test_structurally_invalid_removal_is_error_without_ai() -> None:
    reviewer = FakeReviewer()
    service = VerificationService(
        config(),
        FakeGitHub(structural_removal=True),
        FakeWeblate(),
        reviewer,
    )

    report = await service.verify("https://github.com/canonical/repo/pull/7")

    assert report.counts.errors == 1
    assert report.items[0].change.value == "removed"
    assert "still exists" in (report.items[0].error or "")
    assert reviewer.requests == []


async def test_rejected_multi_form_translation_is_not_applyable() -> None:
    service = VerificationService(
        plural_config(), PluralGitHub(), PluralWeblate(), PluralReviewer()
    )

    result = await service.verify("https://github.com/canonical/repo/pull/7")

    assert result.counts.rejected == 1
    rejection = result.items[0]
    assert rejection.suggested_translation == "Viele Dateien"
    assert rejection.applyable is False
    assert rejection.unit_id is None
    assert rejection.error == (
        "automatic correction is unavailable for multi-form translations"
    )