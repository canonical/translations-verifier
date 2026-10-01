"""Whole-file HTML adapter."""

from typing import Literal

from wlreviser.models import ParsedCatalog, TranslationIdentity, TranslationUnit


class HtmlAdapter:
    name: Literal["html"] = "html"

    def parse(self, content: str, path: str) -> ParsedCatalog:
        # Source and target files have different names, so the identity must not depend on them.
        identity = TranslationIdentity(key="document")
        unit = TranslationUnit(identity=identity, forms=(content,))
        return ParsedCatalog(units={identity: unit})
