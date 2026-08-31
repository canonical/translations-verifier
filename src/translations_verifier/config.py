from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Any, TypeVar
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigurationError(ValueError):
    pass


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarkupPolicy(StrictConfigModel):
    mode: str = "none"
    allowed_tags: tuple[str, ...] = ()
    allowed_attributes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    immutable_attributes: tuple[str, ...] = ("href", "src")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in {"none", "xml", "html"}:
            raise ValueError("mode must be one of: none, xml, html")
        return value


class DeterministicPolicy(StrictConfigModel):
    variable_patterns: tuple[str, ...] = ()
    immutable_tokens: tuple[str, ...] = ()
    preserve_urls: bool = True
    preserve_command_flags: bool = True
    preserve_whitespace: bool = True
    allow_bidi_isolates: bool = False
    markup: MarkupPolicy = Field(default_factory=MarkupPolicy)

    @field_validator("variable_patterns")
    @classmethod
    def validate_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid variable pattern {pattern!r}: {exc}") from exc
        return patterns


class LimitsConfig(StrictConfigModel):
    max_changed_files: int = Field(default=100, ge=1, le=1000)
    max_changed_keys: int = Field(default=1000, ge=1, le=100_000)
    max_file_bytes: int = Field(default=2_000_000, ge=1, le=20_000_000)
    max_total_bytes: int = Field(default=20_000_000, ge=1, le=100_000_000)


class TranslationSetConfig(StrictConfigModel):
    name: str
    directory: str
    filename_pattern: str
    source_locale: str = "en"
    include_locales: tuple[str, ...] = ()
    exclude_locales: tuple[str, ...] = ()
    context_locales: tuple[str, ...] = ()
    max_context_chars: int = Field(default=4000, ge=0, le=50_000)

    @field_validator("directory")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        normalized = _validate_relative_path(value.rstrip("/"))
        return normalized

    @field_validator("filename_pattern")
    @classmethod
    def validate_filename_pattern(cls, value: str) -> str:
        fields = [field for _, field, _, _ in Formatter().parse(value) if field]
        if fields != ["locale"]:
            raise ValueError("filename_pattern must contain exactly one {locale} field")
        if "/" in value or "\\" in value:
            raise ValueError("filename_pattern must be a filename, not a path")
        return value

    @model_validator(mode="after")
    def validate_locale_filters(self) -> TranslationSetConfig:
        overlap = set(self.include_locales) & set(self.exclude_locales)
        if overlap:
            raise ValueError(f"locales cannot be both included and excluded: {sorted(overlap)}")
        if self.source_locale in self.context_locales:
            raise ValueError("source_locale must not be repeated in context_locales")
        return self

    def path_for_locale(self, locale: str) -> str:
        return f"{self.directory}/{self.filename_pattern.format(locale=locale)}"

    def locale_from_path(self, path: str) -> str | None:
        prefix, suffix = self.filename_pattern.split("{locale}")
        parent = str(PurePosixPath(path).parent)
        filename = PurePosixPath(path).name
        if (
            parent != self.directory
            or not filename.startswith(prefix)
            or not filename.endswith(suffix)
        ):
            return None
        end = len(filename) - len(suffix) if suffix else len(filename)
        locale = filename[len(prefix) : end]
        if not locale or "/" in locale or "\\" in locale:
            return None
        if self.include_locales and locale not in self.include_locales:
            return None
        if locale in self.exclude_locales:
            return None
        return locale


class ProjectConfig(StrictConfigModel):
    repository: str
    main_branch: str = "main"
    translation_sets: tuple[TranslationSetConfig, ...]
    policy: DeterministicPolicy = Field(default_factory=DeterministicPolicy)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        stripped = value.removesuffix(".git").rstrip("/")
        if stripped.startswith("https://github.com/"):
            stripped = stripped.removeprefix("https://github.com/")
        parts = stripped.split("/")
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
            raise ValueError("repository must be owner/name or https://github.com/owner/name")
        return "/".join(parts)

    @model_validator(mode="after")
    def validate_translation_sets(self) -> ProjectConfig:
        if not self.translation_sets:
            raise ValueError("at least one translation set is required")
        names = [item.name for item in self.translation_sets]
        if len(names) != len(set(names)):
            raise ValueError("translation set names must be unique")
        identities = [(item.directory, item.filename_pattern) for item in self.translation_sets]
        if len(identities) != len(set(identities)):
            raise ValueError("translation sets must not have identical path patterns")
        return self


class AiConfig(StrictConfigModel):
    endpoint: str
    model: str
    api_key_env: str
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=1, ge=0, le=3)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_output_tokens: int = Field(default=4000, ge=50, le=10000)
    max_input_tokens_per_run: int = Field(default=200_000, ge=100, le=1_000_000)
    max_input_chars_per_key: int = Field(default=12_000, ge=1500, le=100_000)
    allow_insecure_http: bool = False
    allow_json_object_fallback: bool = True

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError("api_key_env must be an uppercase environment variable name")
        return value

    @model_validator(mode="after")
    def validate_endpoint(self) -> AiConfig:
        parsed = urlparse(self.endpoint)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint must be an absolute HTTP(S) URL")
        if (
            parsed.scheme != "https"
            and parsed.hostname not in local_hosts
            and not self.allow_insecure_http
        ):
            raise ValueError("non-local AI endpoints must use HTTPS")
        return self

    def resolve_api_key(self) -> str:
        value = os.environ.get(self.api_key_env)
        if not value:
            raise ConfigurationError(f"environment variable {self.api_key_env} is not set")
        return value


ConfigType = TypeVar("ConfigType", bound=BaseModel)


def load_project_config(path: Path) -> ProjectConfig:
    return _load_yaml(path, ProjectConfig)


def load_ai_config(path: Path) -> AiConfig:
    return _load_yaml(path, AiConfig)


def config_fingerprint(config: BaseModel) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_yaml(path: Path, model: type[ConfigType]) -> ConfigType:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"could not read {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping")
    try:
        return model.model_validate(loaded)
    except ValueError as exc:
        raise ConfigurationError(f"invalid configuration in {path}: {exc}") from exc


def _validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("path must be a normalized repository-relative path")
    return str(path)
