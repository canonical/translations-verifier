from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from translations_verifier.ai import AiReviewer, AiReviewError, estimate_tokens
from translations_verifier.arb import ArbParseError, parse_arb
from translations_verifier.checks import has_blocking_findings, validate_operation
from translations_verifier.config import (
    AiConfig,
    ProjectConfig,
    TranslationSetConfig,
    config_fingerprint,
)
from translations_verifier.diff import diff_translations
from translations_verifier.github import GitHubClient, GitHubError
from translations_verifier.models import (
    AiCategory,
    AiReviewRecord,
    AiReviewState,
    AiUsage,
    ArbDocument,
    ChangedFile,
    Finding,
    OperationType,
    OverallStatus,
    Severity,
    TranslationOperation,
    VerificationReport,
)
from translations_verifier.prompts import build_review_payload


@dataclass(frozen=True)
class _TargetFile:
    changed_file: ChangedFile
    translation_set: TranslationSetConfig
    locale: str


class _VerificationLimitError(RuntimeError):
    pass


class Verifier:
    def __init__(
        self,
        project: ProjectConfig,
        github: GitHubClient,
        *,
        ai_config: AiConfig | None = None,
        ai_reviewer: AiReviewer | None = None,
        deterministic_only: bool = False,
    ) -> None:
        self.project = project
        self.github = github
        self.ai_config = ai_config
        self.ai_reviewer = ai_reviewer
        self.deterministic_only = deterministic_only
        self._file_cache: dict[tuple[str, str], bytes | None] = {}
        self._total_bytes = 0

    def verify(self, pr_locator: str) -> VerificationReport:
        pull_request = self.github.get_pull_request(pr_locator)
        findings: list[Finding] = []
        incomplete_reasons: list[str] = []
        operations: list[TranslationOperation] = []
        ai_reviews: list[AiReviewRecord] = []

        if pull_request.repository.casefold() != self.project.repository.casefold():
            raise GitHubError(
                "GitHub pull request metadata does not match the configured repository"
            )
        if pull_request.base_ref != self.project.main_branch:
            findings.append(
                _file_finding(
                    "policy.unexpected_base",
                    f"pull request targets {pull_request.base_ref!r}, expected "
                    f"{self.project.main_branch!r}",
                    ".github",
                )
            )

        try:
            changed_files = self.github.list_changed_files(
                pull_request.number, self.project.limits.max_changed_files
            )
        except GitHubError as exc:
            incomplete_reasons.append(str(exc))
            changed_files = []
        targets, path_findings, ignored_count = self._classify_files(changed_files)
        findings.extend(path_findings)

        for target in targets:
            try:
                file_operations, file_findings = self._process_target(
                    target,
                    pull_request.base_sha,
                    pull_request.head_sha,
                )
            except (GitHubError, _VerificationLimitError) as exc:
                incomplete_reasons.append(f"could not acquire {target.changed_file.path}: {exc}")
                continue
            findings.extend(file_findings)
            operations.extend(file_operations)
            if len(operations) > self.project.limits.max_changed_keys:
                key_limit = self.project.limits.max_changed_keys
                incomplete_reasons.append(f"pull request exceeds the {key_limit} changed-key limit")
                operations = operations[:key_limit]
                break

        eligible: list[tuple[TranslationOperation, _TargetFile]] = []
        target_by_path = {target.changed_file.path: target for target in targets}
        for operation in operations:
            operation_findings = validate_operation(operation, self.project.policy)
            findings.extend(operation_findings)
            if operation.operation in {
                OperationType.INSERTED,
                OperationType.CHANGED,
            } and not has_blocking_findings(operation_findings):
                operation_target = target_by_path.get(operation.path)
                if operation_target is not None:
                    eligible.append((operation, operation_target))

        ai_usage = AiUsage()
        if not self.deterministic_only:
            ai_reviews, ai_findings, ai_usage, ai_incomplete = self._review_eligible(
                eligible, pull_request.base_sha
            )
            findings.extend(ai_findings)
            incomplete_reasons.extend(ai_incomplete)

        status = _status(findings, incomplete_reasons)
        return VerificationReport(
            repository=pull_request.repository,
            pull_request=pull_request.number,
            base_sha=pull_request.base_sha,
            head_sha=pull_request.head_sha,
            status=status,
            project_config_fingerprint=config_fingerprint(self.project),
            ai_config_fingerprint=(
                config_fingerprint(self.ai_config) if self.ai_config is not None else None
            ),
            operations=operations,
            findings=findings,
            ai_reviews=ai_reviews,
            ai_usage=ai_usage,
            incomplete_reasons=incomplete_reasons,
            ignored_file_count=ignored_count,
        )

    def _classify_files(
        self, changed_files: Iterable[ChangedFile]
    ) -> tuple[list[_TargetFile], list[Finding], int]:
        targets: list[_TargetFile] = []
        findings: list[Finding] = []
        ignored = 0
        for changed_file in sorted(changed_files, key=lambda item: item.path):
            if not changed_file.path.endswith(".arb"):
                ignored += 1
                continue
            matches: list[tuple[TranslationSetConfig, str]] = []
            for translation_set in self.project.translation_sets:
                locale = translation_set.locale_from_path(changed_file.path)
                if locale is not None:
                    matches.append((translation_set, locale))
            if not matches:
                findings.append(
                    _file_finding(
                        "policy.unmatched_arb",
                        "changed ARB file does not match a configured translation set",
                        changed_file.path,
                    )
                )
                continue
            if len(matches) > 1:
                findings.append(
                    _file_finding(
                        "policy.ambiguous_arb",
                        "changed ARB file matches more than one translation set",
                        changed_file.path,
                    )
                )
                continue
            translation_set, locale = matches[0]
            if locale == translation_set.source_locale:
                findings.append(
                    _file_finding(
                        "policy.source_changed",
                        "source-locale changes are outside verifier scope",
                        changed_file.path,
                    )
                )
                continue
            if changed_file.status == "renamed" or changed_file.previous_path:
                findings.append(
                    _file_finding(
                        "policy.renamed_arb",
                        "renamed ARB files are outside verifier scope",
                        changed_file.path,
                    )
                )
                continue
            targets.append(_TargetFile(changed_file, translation_set, locale))
        return targets, findings, ignored

    def _process_target(
        self,
        target: _TargetFile,
        base_sha: str,
        head_sha: str,
    ) -> tuple[list[TranslationOperation], list[Finding]]:
        path = target.changed_file.path
        source_path = target.translation_set.path_for_locale(target.translation_set.source_locale)
        try:
            source_data = self._get_file(source_path, base_sha, allow_missing=False)
            before_data = self._get_file(path, base_sha, allow_missing=True)
            after_data = (
                None
                if target.changed_file.status == "removed"
                else self._get_file(path, head_sha, allow_missing=False)
            )
            if source_data is None:
                raise GitHubError(f"required English source {source_path} is missing")
            source = parse_arb(source_data, source_path)
            before = parse_arb(before_data, path) if before_data is not None else None
            after = (
                parse_arb(after_data, path)
                if after_data is not None
                else ArbDocument(path=path, messages={})
            )
        except ArbParseError as exc:
            return [], [_file_finding(exc.rule_id, str(exc), path, line=exc.line)]

        return (
            diff_translations(source=source, before=before, after=after, locale=target.locale),
            [],
        )

    def _review_eligible(
        self,
        eligible: list[tuple[TranslationOperation, _TargetFile]],
        base_sha: str,
    ) -> tuple[list[AiReviewRecord], list[Finding], AiUsage, list[str]]:
        reviews: list[AiReviewRecord] = []
        findings: list[Finding] = []
        incomplete: list[str] = []
        total_usage = AiUsage()
        if not eligible:
            return reviews, findings, total_usage, incomplete
        if self.ai_config is None or self.ai_reviewer is None:
            return reviews, findings, total_usage, ["AI review was required but not configured"]

        input_tokens = 0
        for index, (operation, target) in enumerate(eligible):
            context = self._context_for(operation, target, base_sha)
            estimated = estimate_tokens(build_review_payload(operation, context))
            if input_tokens + estimated > self.ai_config.max_input_tokens_per_run:
                reason = "AI input token estimate limit reached"
                incomplete.append(reason)
                reviews.extend(_skipped_reviews(eligible[index:], reason))
                break
            input_tokens += estimated
            try:
                verdict, usage = self.ai_reviewer.review(operation, context)
            except AiReviewError as exc:
                reason = str(exc)
                incomplete.append(f"AI review failed for {operation.key!r}: {reason}")
                reviews.append(
                    AiReviewRecord(
                        key=operation.key,
                        path=operation.path,
                        state=AiReviewState.FAILED,
                        reason=reason,
                    )
                )
                continue
            reviews.append(
                AiReviewRecord(
                    key=operation.key,
                    path=operation.path,
                    state=AiReviewState.REVIEWED,
                    verdict=verdict,
                    usage=usage,
                )
            )
            total_usage = _add_usage(total_usage, usage)
            if not verdict.acceptable:
                categories = verdict.categories or [AiCategory.OTHER]
                for category in categories:
                    findings.append(
                        Finding(
                            rule_id=f"ai.{category}",
                            severity=Severity.ERROR,
                            message=verdict.rationale,
                            path=operation.path,
                            key=operation.key,
                            line=operation.after.line if operation.after else None,
                            evidence={
                                "confidence": verdict.confidence,
                                "human_review_note": verdict.human_review_note,
                                "suggested_translation": verdict.suggested_translation,
                            },
                        )
                    )
        return reviews, findings, total_usage, incomplete

    def _context_for(
        self,
        operation: TranslationOperation,
        target: _TargetFile,
        base_sha: str,
    ) -> dict[str, str]:
        context: dict[str, str] = {}
        used_chars = 0
        for locale in target.translation_set.context_locales:
            if locale == target.locale:
                continue
            path = target.translation_set.path_for_locale(locale)
            try:
                content = self._get_file(path, base_sha, allow_missing=True)
                if content is None:
                    continue
                document = parse_arb(content, path)
            except (GitHubError, ArbParseError):
                continue
            message = document.messages.get(operation.key)
            if message is None or message.value in {
                operation.source.value if operation.source else "",
                operation.after.value if operation.after else "",
            }:
                continue
            if used_chars + len(message.value) > target.translation_set.max_context_chars:
                break
            context[locale] = message.value
            used_chars += len(message.value)
        return context

    def _get_file(self, path: str, sha: str, *, allow_missing: bool) -> bytes | None:
        cache_key = (sha, path)
        if cache_key in self._file_cache:
            return self._file_cache[cache_key]
        content = self.github.get_file(path, sha, allow_missing=allow_missing)
        if content is not None:
            if len(content) > self.project.limits.max_file_bytes:
                raise _VerificationLimitError(f"{path} exceeds the configured per-file byte limit")
            self._total_bytes += len(content)
            if self._total_bytes > self.project.limits.max_total_bytes:
                raise _VerificationLimitError(
                    "fetched ARB data exceeds the configured total byte limit"
                )
        self._file_cache[cache_key] = content
        return content


def _skipped_reviews(
    eligible: list[tuple[TranslationOperation, _TargetFile]], reason: str
) -> list[AiReviewRecord]:
    return [
        AiReviewRecord(
            key=operation.key,
            path=operation.path,
            state=AiReviewState.SKIPPED,
            reason=reason,
        )
        for operation, _ in eligible
    ]


def _add_usage(left: AiUsage, right: AiUsage) -> AiUsage:
    return AiUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


def _status(findings: list[Finding], incomplete_reasons: list[str]) -> OverallStatus:
    if any(finding.severity is Severity.ERROR for finding in findings):
        return OverallStatus.UNACCEPTABLE
    if incomplete_reasons:
        return OverallStatus.INCOMPLETE
    return OverallStatus.ACCEPTABLE


def _file_finding(
    rule_id: str,
    message: str,
    path: str,
    *,
    line: int | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=message,
        path=path,
        line=line,
    )
