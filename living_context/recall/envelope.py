"""The envelope memory is handed to a model in (POOLS 3.7): data, never instructions.

Every attribute is gateway-set, drawn from a closed alphabet (ids, enums, the label charset) and emitted in a fixed order; every value
is escaped anyway. `title` and `text` are element content in which `<`, `>` and `&` are replaced by entities, so stored content cannot
close the envelope, open another or add an attribute.
"""
from __future__ import annotations

import re

ORIGINS = ("agent", "human", "import", "unknown")   # unknown: a legacy item without provenance, treated as the least trusted
VIAS = ("recall", "injection", "fetch")
_ATTR = re.compile(r"^[a-z0-9 _-]{0,32}$")
_PTR = re.compile(r"^[a-z2-7]{1,400}$")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap(pool: str, label: str, origin: str, via: str, ptr: str, title: str, text: str | None = None) -> str:
    """The envelope. `text=None` gives a title-only envelope (a digest entry of an agent-written leaf)."""
    if origin not in ORIGINS or via not in VIAS or not _ATTR.match(label) or not _PTR.match(ptr) or not re.match(r"^[a-z0-9]{16}$", pool):
        raise ValueError("attribute outside its closed alphabet")
    out = f'<memory pool="{pool}" label="{label}" origin="{origin}" via="{via}" ptr="{ptr}">\n<title>{escape(title)}</title>\n'
    if text is not None:
        out += f"<text>{escape(text)}</text>\n"
    return out + "</memory>"
