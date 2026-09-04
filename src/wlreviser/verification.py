"""End-to-end pull request translation verification."""

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import JsonValue

from wlreviser.config import ComponentConfig, ProjectConfig
from wlreviser.errors import MalformedResponseError, PreflightError, safe_error_message
from wlreviser.formats import FormatParseError, diff_catalogs, get_adapter, structural_error
from wlreviser.github import (
    ChangedFile,
    PullRequestMetadata,
    PullRequestRef,
    parse_pull_request_url,
)
from wlreviser.models import (
    AIReview,
    ChangeKind,
    ItemStatus,
    ParsedCatalog,
    PeerTranslation,
    ReportWeblate,
    ReviewRequest,
    ReviewVerdict,
    TranslationChange,
    TranslationIdentity,
    VerificationItem,
    VerificationReport,
    counts_for,
    new_item_id,
)
from wlreviser.weblate import WeblateTranslation, WeblateUnit


class GitHubReader(Protocol):
    async def get_pull_request(self, pull: PullRequestRef) -> PullRequestMetadata: ...

    async def get_changed_files(
        self, pull: PullRequestRef, expected_count: int
    ) -> tuple[ChangedFile, ...]: ...

    async def read_file(self, repository: str, sha: str, path: str) -> str | None: ...


class TranslationReviewer(Protocol):
    async def review(self, request: ReviewRequest) -> AIReview: ...


class WeblateResolver(Protocol):
    def resolve_translation(
        self, component: str, repository_path: str
    ) -> WeblateTranslation | None: ...

    def resolve_unit(
        self,
        translation: WeblateTranslation,
        identity: TranslationIdentity,
        source: tuple[str, ...],
    ) -> WeblateUnit | None: ...


@dataclass(frozen=True)
class _MappedFile:
    component: ComponentConfig
    filename_locale: str
    old_path: str | None
    new_path: str | None

    @property
    def path(self) -> str:
        return (
            self.new_path
            or self.old_path
            or self.component.path_for_locale(self.filename_locale)
        )


@dataclass(frozen=True)
class _PreparedFile:
    mapped: _MappedFile
    source: ParsedCatalog
    old: ParsedCatalog
    new: ParsedCatalog
    changes: tuple[TranslationChange, ...]
    translation: WeblateTranslation | None
    mapping_error: str | None


@dataclass(frozen=True)
class _PendingReview:
    mapped: _MappedFile
    change: TranslationChange
    request: ReviewRequest
    target_metadata: dict[str, JsonValue]
    peers: tuple[PeerTranslation, ...]
    translation: WeblateTranslation | None
    mapping_error: str | None


class VerificationService:
    def __init__(
        self,
        config: ProjectConfig,
        github: GitHubReader,
        weblate: WeblateResolver,
        reviewer: TranslationReviewer,
    ) -> None:
        self._config = config
        self._github = github
        self._weblate = weblate
        self._reviewer = reviewer
        self._ids: set[str] = set()
        self._warnings: set[str] = set()
        self._peer_cache: dict[tuple[str, str], ParsedCatalog | None] = {}
        self._metadata: PullRequestMetadata | None = None

    async def verify(self, pull_request_url: str) -> VerificationReport:
        """Verify every configured added or changed translation unit in a pull request."""
        pull = parse_pull_request_url(pull_request_url)
        if pull.full_name.casefold() != self._config.github.repository.casefold():
            raise PreflightError("pull request repository does not match configuration")
        metadata = await self._github.get_pull_request(pull)
        self._metadata = metadata
        if metadata.base_repository.casefold() != self._config.github.repository.casefold():
            raise PreflightError("pull request base repository does not match configuration")
        changed_files = await self._github.get_changed_files(pull, metadata.changed_files)
        mapped_files = self._map_changed_files(changed_files)

        completed: list[VerificationItem | _PendingReview] = []
        prepared_files: list[_PreparedFile] = []
        for mapped in mapped_files:
            prepared, errors = await self._prepare_file(mapped, metadata)
            completed.extend(errors)
            if prepared is not None:
                prepared_files.append(prepared)

        changed_identities = {
            (prepared.mapped.component.weblate_component, prepared.mapped.filename_locale): {
                change.identity for change in prepared.changes
            }
            for prepared in prepared_files
        }
        for prepared in prepared_files:
            for change in prepared.changes:
                item = await self._classify_change(prepared, change, changed_identities, metadata)
                completed.append(item)

        review_positions = [
            (index, value)
            for index, value in enumerate(completed)
            if isinstance(value, _PendingReview)
        ]
        if review_positions:
            reviewed = await asyncio.gather(
                *(self._complete_review(value) for _, value in review_positions)
            )
            for (index, _), item in zip(review_positions, reviewed, strict=True):
                completed[index] = item

        items = tuple(item for item in completed if isinstance(item, VerificationItem))
        return VerificationReport(
            repository=self._config.github.repository,
            pull_request_url=pull.url,
            pull_request_number=metadata.number,
            base_sha=metadata.base_sha,
            head_sha=metadata.head_sha,
            weblate=ReportWeblate(
                api_url=self._config.weblate.api_url,
                project=self._config.weblate.project,
            ),
            items=items,
            counts=counts_for(items),
            warnings=tuple(sorted(self._warnings)),
        )

    def _map_changed_files(self, changed_files: tuple[ChangedFile, ...]) -> tuple[_MappedFile, ...]:
        mapped: list[_MappedFile] = []
        for changed in changed_files:
            if changed.status == "renamed":
                if changed.previous_filename is None:
                    raise MalformedResponseError("GitHub renamed file has no previous filename")
                old_match = self._config.component_for_path(changed.previous_filename)
                new_match = self._config.component_for_path(changed.filename)
                if old_match is not None and new_match is not None and old_match == new_match:
                    self._append_mapping(
                        mapped,
                        old_match[0],
                        old_match[1],
                        changed.previous_filename,
                        changed.filename,
                    )
                else:
                    if old_match is not None:
                        self._append_mapping(
                            mapped,
                            old_match[0],
                            old_match[1],
                            changed.previous_filename,
                            None,
                        )
                    if new_match is not None:
                        self._append_mapping(
                            mapped,
                            new_match[0],
                            new_match[1],
                            None,
                            changed.filename,
                        )
                continue
            match = self._config.component_for_path(changed.filename)
            if match is None:
                continue
            old_path = None if changed.status == "added" else changed.filename
            new_path = None if changed.status == "removed" else changed.filename
            self._append_mapping(mapped, match[0], match[1], old_path, new_path)
        unique = {
            (
                item.component.weblate_component,
                item.filename_locale,
                item.old_path,
                item.new_path,
            ): item
            for item in mapped
        }
        return tuple(sorted(unique.values(), key=lambda item: (item.path, item.old_path or "")))

    @staticmethod
    def _append_mapping(
        mapped: list[_MappedFile],
        component: ComponentConfig,
        locale: str,
        old_path: str | None,
        new_path: str | None,
    ) -> None:
        if locale != component.source_locale:
            mapped.append(
                _MappedFile(
                    component=component,
                    filename_locale=locale,
                    old_path=old_path,
                    new_path=new_path,
                )
            )

    async def _prepare_file(
        self,
        mapped: _MappedFile,
        metadata: PullRequestMetadata,
    ) -> tuple[_PreparedFile | None, tuple[VerificationItem, ...]]:
        adapter = get_adapter(mapped.component.format)
        errors: list[VerificationItem] = []
        source_content = await self._github.read_file(
            metadata.base_repository,
            metadata.base_sha,
            mapped.component.source_file,
        )
        source_exists = source_content is not None
        if source_content is None and mapped.component.format != "html":
            return None, (
                self._file_error(
                    mapped,
                    f"base source file {mapped.component.source_file!r} is missing",
                ),
            )
        try:
            source = (
                adapter.parse(source_content, mapped.component.source_file)
                if source_content is not None
                else ParsedCatalog()
            )
            old = await self._read_catalog(
                adapter,
                metadata.base_repository,
                metadata.base_sha,
                mapped.old_path,
                required=mapped.old_path is not None,
            )
            new = await self._read_catalog(
                adapter,
                metadata.head_repository,
                metadata.head_sha,
                mapped.new_path,
                required=mapped.new_path is not None,
            )
        except FormatParseError as exc:
            return None, (self._file_error(mapped, safe_error_message(exc)),)
        except FileNotFoundError as exc:
            return None, (self._file_error(mapped, str(exc)),)

        issue_keys: set[str] = set()
        for label, catalog in (("source", source), ("base target", old), ("head target", new)):
            for issue in catalog.issues:
                if issue.key is not None:
                    issue_keys.add(issue.key)
                errors.append(
                    self._file_error(
                        mapped,
                        f"{label} parse issue: {issue.message}",
                        key=issue.key,
                    )
                )
        changes = tuple(
            change
            for change in diff_catalogs(old, new)
            if change.identity.key not in issue_keys
        )
        mapping_error: str | None = None
        try:
            translation = self._weblate.resolve_translation(
                mapped.component.weblate_component,
                mapped.path,
            )
        except Exception as exc:
            translation = None
            mapping_error = safe_error_message(exc)
        if translation is None and mapping_error is None:
            mapping_error = "no Weblate translation matches the repository filename"
        prepared = _PreparedFile(
            mapped=mapped,
            source=source,
            old=old,
            new=new,
            changes=changes,
            translation=translation,
            mapping_error=mapping_error,
        )
        if not source_exists and mapped.component.format == "html":
            self._warnings.add(f"source HTML file is absent for {mapped.path}")
        return prepared, tuple(errors)

    async def _read_catalog(
        self,
        adapter: object,
        repository: str,
        sha: str,
        path: str | None,
        *,
        required: bool,
    ) -> ParsedCatalog:
        if path is None:
            return ParsedCatalog()
        content = await self._github.read_file(repository, sha, path)
        if content is None:
            if required:
                raise FileNotFoundError(f"required repository file {path!r} is missing")
            return ParsedCatalog()
        return adapter.parse(content, path)  # type: ignore[attr-defined,no-any-return]

    async def _classify_change(
        self,
        prepared: _PreparedFile,
        change: TranslationChange,
        changed_identities: dict[tuple[str, str], set[TranslationIdentity]],
        metadata: PullRequestMetadata,
    ) -> VerificationItem | _PendingReview:
        mapped = prepared.mapped
        structural_message = structural_error(
            change,
            prepared.source,
            format_name=mapped.component.format,
            source_exists=bool(prepared.source.units),
        )
        source_unit = prepared.source.units.get(change.identity)
        if (
            structural_message is None
            and change.kind is not ChangeKind.REMOVED
            and source_unit is None
        ):
            structural_message = "changed target unit does not exist in the base source file"
        if structural_message is not None:
            return self._change_error(
                prepared,
                change,
                structural_message,
                source_unit.forms if source_unit else None,
            )
        if change.kind is ChangeKind.REMOVED:
            return self._base_item(
                prepared,
                change,
                status=ItemStatus.PASSED,
                source=source_unit.forms if source_unit else None,
            )
        assert change.new is not None
        assert source_unit is not None
        peers = await self._peer_context(mapped, change.identity, changed_identities, metadata)
        request = ReviewRequest(
            format=mapped.component.format,
            target_locale=(
                prepared.translation.language_code
                if prepared.translation is not None
                else mapped.filename_locale
            ),
            source=source_unit.forms,
            source_context=change.identity.context,
            source_metadata=source_unit.metadata,
            previous_translation=change.old.forms if change.old is not None else None,
            proposed_translation=change.new.forms,
            peer_context=peers,
        )
        return _PendingReview(
            mapped=mapped,
            change=change,
            request=request,
            target_metadata=dict(change.new.metadata),
            peers=peers,
            translation=prepared.translation,
            mapping_error=prepared.mapping_error,
        )

    async def _peer_context(
        self,
        mapped: _MappedFile,
        identity: TranslationIdentity,
        changed_identities: dict[tuple[str, str], set[TranslationIdentity]],
        metadata: PullRequestMetadata,
    ) -> tuple[PeerTranslation, ...]:
        peers: list[PeerTranslation] = []
        for locale in mapped.component.context_locales:
            if locale == mapped.filename_locale:
                continue
            changed = changed_identities.get((mapped.component.weblate_component, locale), set())
            if identity in changed:
                continue
            catalog = await self._peer_catalog(mapped.component, locale, metadata)
            if catalog is not None and (unit := catalog.units.get(identity)) is not None:
                peers.append(PeerTranslation(locale=locale, forms=unit.forms))
        return tuple(peers)

    async def _peer_catalog(
        self,
        component: ComponentConfig,
        locale: str,
        metadata: PullRequestMetadata,
    ) -> ParsedCatalog | None:
        cache_key = (component.weblate_component, locale)
        if cache_key in self._peer_cache:
            return self._peer_cache[cache_key]
        path = component.path_for_locale(locale)
        content = await self._github.read_file(metadata.base_repository, metadata.base_sha, path)
        if content is None:
            self._warnings.add(f"optional peer translation is missing: {path}")
            self._peer_cache[cache_key] = None
            return None
        try:
            catalog = get_adapter(component.format).parse(content, path)
        except FormatParseError:
            self._warnings.add(f"optional peer translation could not be parsed: {path}")
            catalog = None
        self._peer_cache[cache_key] = catalog
        return catalog

    async def _complete_review(self, pending: _PendingReview) -> VerificationItem:
        try:
            review = await self._reviewer.review(pending.request)
        except Exception as exc:
            return self._pending_item(
                pending,
                status=ItemStatus.ERROR,
                error=safe_error_message(exc),
            )
        if review.verdict is ReviewVerdict.OK:
            return self._pending_item(pending, status=ItemStatus.PASSED)

        assert review.reason is not None
        assert review.suggested_translation is not None
        unit: WeblateUnit | None = None
        mapping_error = pending.mapping_error
        multi_form = len(pending.request.proposed_translation) != 1
        if multi_form:
            mapping_error = "automatic correction is unavailable for multi-form translations"
        elif pending.mapped.component.format != "html" and pending.translation is not None:
            try:
                unit = self._weblate.resolve_unit(
                    pending.translation,
                    pending.change.identity,
                    pending.request.source,
                )
            except Exception as exc:
                mapping_error = safe_error_message(exc)
            if unit is None and mapping_error is None:
                mapping_error = "no unique Weblate unit matches the rejected translation"
        applyable = (
            not multi_form
            and pending.mapped.component.format != "html"
            and unit is not None
        )
        return self._pending_item(
            pending,
            status=ItemStatus.REJECTED,
            reason=review.reason,
            suggested_translation=review.suggested_translation,
            error=mapping_error,
            applyable=applyable,
            unit=unit,
        )

    def _pending_item(
        self,
        pending: _PendingReview,
        *,
        status: ItemStatus,
        reason: str | None = None,
        suggested_translation: str | None = None,
        error: str | None = None,
        applyable: bool = False,
        unit: WeblateUnit | None = None,
    ) -> VerificationItem:
        change = pending.change
        return VerificationItem(
            id=self._new_id(),
            status=status,
            change=change.kind,
            component=pending.mapped.component.weblate_component,
            format=pending.mapped.component.format,
            path=pending.mapped.path,
            filename_locale=pending.mapped.filename_locale,
            weblate_locale=(
                pending.translation.language_code if pending.translation else None
            ),
            weblate_locale_name=(
                pending.translation.language_name if pending.translation else None
            ),
            identity=change.identity,
            source=pending.request.source,
            source_metadata=pending.request.source_metadata,
            old_target=change.old.forms if change.old else None,
            pr_target=change.new.forms if change.new else None,
            target_metadata=pending.target_metadata,
            peer_context=pending.peers,
            reason=reason,
            suggested_translation=suggested_translation,
            error=error,
            applyable=applyable,
            translation_url=pending.translation.api_url if pending.translation else None,
            unit_id=unit.id if unit else None,
            unit_url=unit.web_url if unit else None,
        )

    def _base_item(
        self,
        prepared: _PreparedFile,
        change: TranslationChange,
        *,
        status: ItemStatus,
        source: tuple[str, ...] | None,
        error: str | None = None,
    ) -> VerificationItem:
        target = change.new or change.old
        return VerificationItem(
            id=self._new_id(),
            status=status,
            change=change.kind,
            component=prepared.mapped.component.weblate_component,
            format=prepared.mapped.component.format,
            path=prepared.mapped.path,
            filename_locale=prepared.mapped.filename_locale,
            weblate_locale=(
                prepared.translation.language_code if prepared.translation else None
            ),
            weblate_locale_name=(
                prepared.translation.language_name if prepared.translation else None
            ),
            identity=change.identity,
            source=source,
            source_metadata=(
                prepared.source.units[change.identity].metadata
                if change.identity in prepared.source.units
                else {}
            ),
            old_target=change.old.forms if change.old else None,
            pr_target=change.new.forms if change.new else None,
            target_metadata=target.metadata if target else {},
            error=error,
            translation_url=prepared.translation.api_url if prepared.translation else None,
        )

    def _change_error(
        self,
        prepared: _PreparedFile,
        change: TranslationChange,
        message: str,
        source: tuple[str, ...] | None,
    ) -> VerificationItem:
        return self._base_item(
            prepared,
            change,
            status=ItemStatus.ERROR,
            source=source,
            error=message,
        )

    def _file_error(
        self,
        mapped: _MappedFile,
        message: str,
        *,
        key: str | None = None,
    ) -> VerificationItem:
        return VerificationItem(
            id=self._new_id(),
            status=ItemStatus.ERROR,
            change=ChangeKind.UPDATED,
            component=mapped.component.weblate_component,
            format=mapped.component.format,
            path=mapped.path,
            filename_locale=mapped.filename_locale,
            identity=TranslationIdentity(key=key or PurePosixPath(mapped.path).name),
            error=message,
        )

    def _new_id(self) -> str:
        item_id = new_item_id(self._ids)
        self._ids.add(item_id)
        return item_id