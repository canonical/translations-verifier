"""Whole-file HTML adapter."""

from pathlib import PurePosixPath
from typing import Literal

from wlreviser.models import ParsedCatalog, TranslationIdentity, TranslationUnit


class HtmlAdapter:
    name: Literal["html"] = "html"

    def parse(self, content: str, path: str) -> ParsedCatalog:
        identity = TranslationIdentity(key=PurePosixPath(path).name)
        unit = TranslationUnit(identity=identity, forms=(content,))
        return ParsedCatalog(units={identity: unit})