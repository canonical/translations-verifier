"""Built-in translation format adapters."""

from wlreviser.formats.arb import ArbAdapter
from wlreviser.formats.base import (
    FormatAdapter,
    FormatParseError,
    diff_catalogs,
    get_adapter,
    structural_error,
)
from wlreviser.formats.html import HtmlAdapter
from wlreviser.formats.po import PoAdapter

__all__ = [
    "ArbAdapter",
    "FormatAdapter",
    "FormatParseError",
    "HtmlAdapter",
    "PoAdapter",
    "diff_catalogs",
    "get_adapter",
    "structural_error",
]