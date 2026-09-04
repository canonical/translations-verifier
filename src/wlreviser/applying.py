"""Apply selected verification suggestions to Weblate."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from wlreviser.config import ProjectConfig
from wlreviser.errors import PreflightError, safe_error_message
from wlreviser.models import ItemStatus, VerificationItem, VerificationReport
from wlreviser.weblate import WeblateGateway


class ApplyStatus(StrEnum):
    UPDATED = "updated"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApplyItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    status: ApplyStatus
    component: str | None = None
    message: str | None = None


class ComponentOperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    component: str
    commit_error: str | None = None
    push_skipped: bool = False
    push_error: str | None = None


class ApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    items: tuple[ApplyItemResult, ...]
    components: tuple[ComponentOperationResult, ...]

    @property
    def updated(self) -> int:
        return sum(item.status is ApplyStatus.UPDATED for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status is ApplyStatus.FAILED for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status is ApplyStatus.SKIPPED for item in self.items)


class ApplyService:
    def __init__(self, config: ProjectConfig, gateway: WeblateGateway) -> None:
        self._config = config
        self._gateway = gateway

    def apply(self, report: VerificationReport, selectors: list[str]) -> ApplyResult:
        """Apply valid selections and independently commit/push affected components."""
        self._validate_report_coordinates(report)
        if not selectors:
            raise PreflightError("at least one finding ID or 'all' is required")
        all_selectors = [selector for selector in selectors if selector.casefold() == "all"]
        if all_selectors and (len(selectors) != 1 or selectors[0].casefold() != "all"):
            raise PreflightError("'all' cannot be combined with finding IDs")

        items_by_id = {item.id: item for item in report.items}
        selected: list[tuple[str, VerificationItem | None]]
        if all_selectors:
            selected = [(item.id, item) for item in report.items if item.applyable]
        else:
            selected = []
            seen: set[str] = set()
            for selector in selectors:
                if selector in seen:
                    selected.append((selector, None))
                else:
                    selected.append((selector, items_by_id.get(selector)))
                    seen.add(selector)

        results: list[ApplyItemResult] = []
        affected_components: set[str] = set()
        applied_ids: set[str] = set()
        for selector, item in selected:
            if selector in applied_ids or item is None:
                message = (
                    "duplicate finding ID" if selector in applied_ids else "unknown finding ID"
                )
                results.append(
                    ApplyItemResult(id=selector, status=ApplyStatus.SKIPPED, message=message)
                )
                continue
            applied_ids.add(selector)
            non_applyable_reason = self._non_applyable_reason(item)
            if non_applyable_reason is not None:
                results.append(
                    ApplyItemResult(
                        id=item.id,
                        status=ApplyStatus.SKIPPED,
                        component=item.component,
                        message=non_applyable_reason,
                    )
                )
                continue
            assert item.unit_id is not None
            assert item.suggested_translation is not None
            try:
                self._gateway.update_unit(item.unit_id, (item.suggested_translation,))
            except Exception as exc:
                results.append(
                    ApplyItemResult(
                        id=item.id,
                        status=ApplyStatus.FAILED,
                        component=item.component,
                        message=safe_error_message(exc),
                    )
                )
                continue
            results.append(
                ApplyItemResult(
                    id=item.id,
                    status=ApplyStatus.UPDATED,
                    component=item.component,
                )
            )
            affected_components.add(item.component)

        component_results: list[ComponentOperationResult] = []
        configured_components = {
            item.weblate_component: item for item in self._config.components
        }
        for component in sorted(affected_components):
            commit_error: str | None = None
            push_error: str | None = None
            push_skipped = not configured_components[component].push_after_commit
            try:
                self._gateway.commit_component(component)
            except Exception as exc:
                commit_error = safe_error_message(exc)
            if not push_skipped:
                try:
                    self._gateway.push_component(component)
                except Exception as exc:
                    push_error = safe_error_message(exc)
            component_results.append(
                ComponentOperationResult(
                    component=component,
                    commit_error=commit_error,
                    push_skipped=push_skipped,
                    push_error=push_error,
                )
            )
        return ApplyResult(items=tuple(results), components=tuple(component_results))

    def _validate_report_coordinates(self, report: VerificationReport) -> None:
        if report.repository.casefold() != self._config.github.repository.casefold():
            raise PreflightError("report repository does not match configuration")
        if (
            report.weblate.api_url != self._config.weblate.api_url
            or report.weblate.project != self._config.weblate.project
        ):
            raise PreflightError("report Weblate coordinates do not match configuration")
        configured = {item.weblate_component: item for item in self._config.components}
        for item in report.items:
            component = configured.get(item.component)
            if component is None or component.format != item.format:
                raise PreflightError(
                    f"report component {item.component!r} does not match configuration"
                )
            match = self._config.component_for_path(item.path)
            if match is None or match[0].weblate_component != item.component:
                raise PreflightError(f"report path {item.path!r} does not match configuration")

    @staticmethod
    def _non_applyable_reason(item: VerificationItem) -> str | None:
        if item.status is not ItemStatus.REJECTED:
            return f"finding status is {item.status.value}"
        if not item.applyable:
            return "finding is not applyable"
        if item.format == "html":
            return "HTML findings require manual correction"
        if item.unit_id is None or not item.suggested_translation:
            return "finding has no complete Weblate suggestion mapping"
        return None