"""Strict, non-secret project configuration."""

import re
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from wlreviser.errors import PreflightError

TranslationFormat = Literal["arb", "po", "html"]

_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?$"
)
_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOCALE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")


class ConfigError(PreflightError):
    """Raised when a project configuration cannot be loaded."""


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


def _validate_endpoint(value: str, field_name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain a query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"{field_name} must use HTTPS unless it targets localhost")
    path = f"{parsed.path.rstrip('/')}/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validate_repository_path(value: str, field_name: str, *, template: bool) -> str:
    expected_placeholders = 1 if template else 0
    if value.count("{locale}") != expected_placeholders:
        requirement = "exactly one {locale} placeholder" if template else "no placeholders"
        raise ValueError(f"{field_name} must contain {requirement}")
    if "{" in value.replace("{locale}", "") or "}" in value.replace("{locale}", ""):
        raise ValueError(f"{field_name} contains an unsupported placeholder")
    path = PurePosixPath(value)
    if path.is_absolute() or not value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must be a normalized repository-relative path")
    return value


class WeblateConfig(StrictConfigModel):
    api_url: str
    project: str

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        return _validate_endpoint(value, "weblate.api_url")

    @field_validator("project")
    @classmethod
    def validate_project(cls, value: str) -> str:
        if not _SLUG_PATTERN.fullmatch(value):
            raise ValueError("weblate.project must be a valid slug")
        return value


class GitHubConfig(StrictConfigModel):
    repository: str

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not _REPOSITORY_PATTERN.fullmatch(value):
            raise ValueError("github.repository must have the form owner/repository")
        return value


class AIConfig(StrictConfigModel):
    api_url: str
    model: str = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1, le=32)

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        return _validate_endpoint(value, "ai.api_url")


class ComponentConfig(StrictConfigModel):
    weblate_component: str
    source_locale: str
    context_locales: list[str] = Field(default_factory=list)
    format: TranslationFormat
    source_file: str
    path: str
    push_after_commit: bool = True

    @field_validator("weblate_component")
    @classmethod
    def validate_component(cls, value: str) -> str:
        if not _SLUG_PATTERN.fullmatch(value):
            raise ValueError("weblate_component must be a valid slug")
        return value

    @field_validator("source_locale")
    @classmethod
    def validate_source_locale(cls, value: str) -> str:
        if not _LOCALE_PATTERN.fullmatch(value):
            raise ValueError("source_locale is invalid")
        return value

    @field_validator("context_locales")
    @classmethod
    def validate_context_locales(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("context_locales must not contain duplicates")
        if any(not _LOCALE_PATTERN.fullmatch(value) for value in values):
            raise ValueError("context_locales contains an invalid locale")
        return values

    @field_validator("source_file")
    @classmethod
    def validate_source_file(cls, value: str) -> str:
        return _validate_repository_path(value, "source_file", template=False)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_repository_path(value, "path", template=True)

    @model_validator(mode="after")
    def validate_locale_roles(self) -> Self:
        if self.source_locale in self.context_locales:
            raise ValueError("source_locale must not also appear in context_locales")
        return self

    def path_for_locale(self, locale: str) -> str:
        """Return the repository path for a filename locale."""
        if not _LOCALE_PATTERN.fullmatch(locale):
            raise ValueError("locale is invalid")
        return self.path.replace("{locale}", locale)

    def locale_from_path(self, repository_path: str) -> str | None:
        """Extract the filename locale when this component owns a path."""
        prefix, suffix = self.path.split("{locale}")
        if not repository_path.startswith(prefix) or not repository_path.endswith(suffix):
            return None
        end = len(repository_path) - len(suffix) if suffix else len(repository_path)
        locale = repository_path[len(prefix) : end]
        return locale if _LOCALE_PATTERN.fullmatch(locale) else None


class ProjectConfig(StrictConfigModel):
    weblate: WeblateConfig
    github: GitHubConfig
    ai: AIConfig
    components: list[ComponentConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_component_ownership(self) -> Self:
        component_names = [item.weblate_component.casefold() for item in self.components]
        templates = [item.path for item in self.components]
        source_files = [item.source_file for item in self.components]
        if len(component_names) != len(set(component_names)):
            raise ValueError("weblate_component values must be unique")
        if len(templates) != len(set(templates)):
            raise ValueError("component path templates must be unique")
        if len(source_files) != len(set(source_files)):
            raise ValueError("component source_file values must be unique")
        return self

    def component_for_path(self, repository_path: str) -> tuple[ComponentConfig, str] | None:
        """Resolve a configured target path to its component and filename locale."""
        matches = [
            (component, locale)
            for component in self.components
            if (locale := component.locale_from_path(repository_path)) is not None
        ]
        if len(matches) > 1:
            raise ConfigError(f"multiple components own repository path {repository_path!r}")
        return matches[0] if matches else None


def load_config(path: Path) -> ProjectConfig:
    """Load a strict project configuration from YAML."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read configuration: {exc}") from exc
    try:
        return ProjectConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"invalid configuration: {details}") from exc