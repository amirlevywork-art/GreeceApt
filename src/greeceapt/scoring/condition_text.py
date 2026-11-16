"""
Publisher-stated condition ("needs renovation" / "renovated").

XE exposes it as ``condition`` in ``characteristics_list``; otherwise only anchored English or
Greek phrases in the description count. Mentions of renovating the *building* (facade, common
areas) or of a *possibility* of renovation are not a statement about the flat.
"""

from __future__ import annotations

import re
from typing import Literal, Mapping

from greeceapt.collect.floor_resolver import normalize_text

# Patterns are written in normalized form: casefolded, accent-free, final sigma "ς" → "σ".

PublisherCondition = Literal["needs_renovation", "renovated"]

_NEEDS_RE = re.compile(
    r"needs? (?:a )?(?:full |complete |total |some )?renovation"
    r"|in need of (?:a )?(?:full |complete )?renovation"
    r"|(?:to be|must be) renovated"
    r"|requires? (?:a )?(?:full |complete )?renovation"
    r"|(?:apartment|flat|studio|property) for renovation"
    r"|προσ ανακαινιση"
    r"|χρηζει ανακαινισησ"
    r"|θελει ανακαινιση"
    r"|χρειαζεται ανακαινιση"
    r"|για ανακαινιση"
    r"|unfinished|ημιτελ\w*|under construction|υπο κατασκευη"
)
_RENOVATED_RE = re.compile(
    r"(?<!to be )(?<!must be )\b(?:fully |newly |totally |completely |recently )?renovated\b"
    r"|refurbished"
    r"|ανακαινισμεν\w*"
    r"|ανακαινιση (?:19|20)\d\d"
)
_NOT_ABOUT_FLAT_RE = re.compile(
    r"(?:of|to) the (?:building|facade|common areas?|entrance|staircase)"
    r"|possibility of|option of|potential for|δυνατοτητα"
    r"|πολυκατοικια|οψη|κοινοχρηστ"
)


_NEGATED_RE = re.compile(r"(?:\bnot|n't|\bno need to|\bwithout|\bδεν|\bχωρισ)\s+$")
# "300 m from the Kypseli Square metro station (line 4), which is currently under construction":
# the Athens metro Line 4 is being built through the centre and many ads mention it (6 flats,
# Nov 2026, were charged a full renovation for it). Construction near transport words is not the flat.
_CONSTRUCTION_RE = re.compile(r"unfinished|ημιτελ\w*|under construction|υπο κατασκευη")
_INFRASTRUCTURE_RE = re.compile(r"metro|station|\bline \d|\btram|γραμμη|σταθμ|μετρο|τραμ")


def construction_is_about_infrastructure(text: str, match: re.Match[str]) -> bool:
    """An "under construction" / "unfinished" match that describes a metro station or line."""
    if not _CONSTRUCTION_RE.fullmatch(match.group(0)):
        return False
    return bool(_INFRASTRUCTURE_RE.search(text[max(0, match.start() - 90):match.end() + 20]))


def _mentions(pattern: re.Pattern[str], text: str) -> bool:
    for match in pattern.finditer(text):
        window = text[max(0, match.start() - 30):match.end() + 30]
        if _NOT_ABOUT_FLAT_RE.search(window):
            continue
        if _NEGATED_RE.search(text[max(0, match.start() - 15):match.start()]):
            continue  # "does not require renovation"
        if construction_is_about_infrastructure(text, match):
            continue  # the metro is under construction, not the flat
        return True
    return False


def publisher_condition(listing: Mapping[str, object]) -> PublisherCondition | None:
    structured = normalize_text(listing.get("condition"))
    if structured:
        if "needs renovation" in structured or "προσ ανακαινιση" in structured:
            return "needs_renovation"
        if "renovated" in structured or "ανακαινισμεν" in structured:
            return "renovated"
    text = normalize_text(listing.get("description"))
    if not text:
        return None
    if _mentions(_NEEDS_RE, text):
        return "needs_renovation"
    if _mentions(_RENOVATED_RE, text):
        return "renovated"
    return None


# Premises that are themselves a shop / store room / commercial space (sometimes "convertible" or
# already converted), listed among homes. Only the property's own description counts: "close to
# shops", the agency's "office" and the feature tag "For Commercial Use" on flats do not.
_COMMERCIAL_RE = re.compile(
    r"\b\d+\s*(?:sqm|sq\.? ?m\.?|m2|τ\.?μ\.?)\s+(?:commercial space|shops?|stores?)\b"
    r"|\b(?:shops?|stores?|commercial space)\s+(?:of \d|suitable|that (?:can|has|have)"
    r"|which (?:can|has)|is offered|for sale)"
    r"|\bfor sale from an? (?:shop|store)\b"
    r"|\bstore ?room\b"
    r"|πρωην καταστημα|πωλειται καταστημα|καταστημα \d|επαγγελματικοσ χωροσ|επαγγελματικη στεγη"
)


def commercial_premises(description: object) -> bool:
    return bool(_COMMERCIAL_RE.search(normalize_text(description)))
