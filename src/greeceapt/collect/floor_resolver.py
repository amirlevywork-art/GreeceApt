"""
Resolve a listing's storey from explicit evidence.

Sources, in priority order:

1. ``structured``      — a field that only ever holds a floor (XE schema.org ``floorLevel``);
2. ``spitogatos_code`` — Spitogatos ``floorNumber``, an offset code (NOT a storey);
3. ``title`` / ``description`` — anchored phrases only ("floor: X", "on the X", "X apartment",
   standalone Greek floor words), so "storage room in the basement" is not floor evidence;
4. ``numeric``         — a bare number, used only when no explicit evidence exists.

Explicit sources never silently override each other: if their floor classes cannot all be
true at once the floor is unknown and ``conflict=True``. A bare number that disagrees with
explicit text is kept in the evidence but ignored (the classic "basement stored as 1st
floor" case).

Canonical values: basement −1.0, semi-basement −0.5, ground 0.0, elevated/raised ground and
mezzanine 0.5, storeys 1, 2, 3 …
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import asdict, dataclass


CLASS_VALUES: dict[str, float] = {
    "basement": -1.0,
    "semi_basement": -0.5,
    "ground": 0.0,
    "elevated_ground": 0.5,
}
_GROUND_CLASSES = frozenset({"ground", "elevated_ground"})

# Spitogatos ``floorNumber`` → (value, compatible classes). Verified 2026-11-05 against the
# "Floor: X" phrase in November descriptions: 1 basement, 2 semi-basement, 3 mostly ground
# (some elevated), 4 elevated ground / mezzanine, 6.. = storey + 5. 0 and 5 carry no evidence.
_SPITOGATOS_SPECIAL: dict[int, tuple[float, frozenset[str]]] = {
    1: (-1.0, frozenset({"basement"})),
    2: (-0.5, frozenset({"semi_basement"})),
    3: (0.0, _GROUND_CLASSES),
    4: (0.5, _GROUND_CLASSES),
}
_SPITOGATOS_UPPER_OFFSET = 5
_SPITOGATOS_MAX_CODE = 30



@dataclass(frozen=True)
class FloorEvidence:
    source: str
    raw_value: str
    parsed_value: float | None
    floor_classes: frozenset[str]

    def to_json(self) -> dict:
        data = asdict(self)
        data["floor_classes"] = sorted(self.floor_classes)
        return data


@dataclass(frozen=True)
class FloorResolution:
    value: float | None
    floor_class: str | None
    conflict: bool
    evidence: tuple[FloorEvidence, ...]
    # Explicit sources disagree only between basement and semi-basement: resolved to the
    # more pessimistic basement (design decision 2026-11-05) instead of an unranked conflict.
    adjacent_conflict: bool = False

    def evidence_json(self) -> list[dict]:
        return [item.to_json() for item in self.evidence]


# ── Normalization ─────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: object) -> str:
    """HTML-unescape, drop tags, NFKC, casefold, strip accents, collapse whitespace."""
    if value is None:
        return ""
    text = _TAG_RE.sub(" ", html.unescape(str(value)))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )
    text = text.replace("’", "'").replace("´", "'").replace("`", "'")
    return _SPACE_RE.sub(" ", text).strip()


# ── Vocabulary (normalized: casefolded, accent-free; Greek final sigma is "σ") ──

# Order matters: longer / more specific phrases first; matched spans are masked.
_CLASS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("semi_basement", r"semi[\s-]?basement"),
    ("semi_basement", r"ημιυπογει\w*"),
    ("semi_basement", r"lower[\s-]ground(?:[\s-]floor)?"),
    ("elevated_ground", r"(?:elevated|raised|upper)[\s-]ground(?:[\s-]floor)?"),
    ("elevated_ground", r"υπερυψωμεν\w*(?:\s+ισογει\w*)?"),
    ("elevated_ground", r"mezzanine(?:\s+(?:floor|level))?"),
    ("elevated_ground", r"ημι[οω]ροφ\w*"),
    ("basement", r"basement"),
    ("basement", r"υπογει\w*"),
    ("ground", r"ground(?:[\s-]floor)?"),
    ("ground", r"ισογει\w*"),
)

_EN_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_GR_ORDINAL_STEMS = {
    "πρωτ": 1, "δευτερ": 2, "τριτ": 3, "τεταρτ": 4, "πεμπτ": 5,
    "εκτ": 6, "εβδομ": 7, "ογδο": 8, "ενατ": 9, "δεκατ": 10,
}
_GR_LETTER_ORDINALS = {"α": 1, "β": 2, "γ": 3, "δ": 4, "ε": 5, "στ": 6, "ζ": 7, "η": 8}

_EN_ORDINAL = r"(?:\d{1,2}\s*(?:st|nd|rd|th)|" + "|".join(_EN_ORDINAL_WORDS) + r")"
_GR_ORDINAL = (
    r"(?:\d{1,2}\s*(?:οσ|ου|ο|η)|(?:" + "|".join(_GR_ORDINAL_STEMS) + r")\w*|"
    r"(?:" + "|".join(_GR_LETTER_ORDINALS) + r")\s*')"
)
_ORDINAL_RE = re.compile(rf"\b(?:{_EN_ORDINAL}|{_GR_ORDINAL})(?!\w)")


def _ordinal_value(token: str) -> int | None:
    token = token.strip()
    digits = re.match(r"(\d{1,2})", token)
    if digits:
        return int(digits.group(1))
    if token in _EN_ORDINAL_WORDS:
        return _EN_ORDINAL_WORDS[token]
    for stem, value in _GR_ORDINAL_STEMS.items():
        if token.startswith(stem):
            return value
    letter = token.rstrip("'").strip()
    return _GR_LETTER_ORDINALS.get(letter)


def _class_evidence(source: str, raw: str, floor_class: str) -> FloorEvidence:
    return FloorEvidence(source, raw, CLASS_VALUES[floor_class], frozenset({floor_class}))


def _upper_evidence(source: str, raw: str, storey: int) -> FloorEvidence | None:
    if storey < 1 or storey > 25:
        return None
    return FloorEvidence(source, raw, float(storey), frozenset({"upper"}))


def _numeric_evidence(source: str, raw: str, value: float) -> FloorEvidence:
    if value <= -1:
        return FloorEvidence(source, raw, -1.0, frozenset({"basement"}))
    if value < 0:
        return FloorEvidence(source, raw, -0.5, frozenset({"semi_basement"}))
    if value < 0.5:
        return FloorEvidence(source, raw, 0.0, frozenset({"ground"}))
    if value < 1:
        return FloorEvidence(source, raw, 0.5, frozenset({"elevated_ground"}))
    return FloorEvidence(source, raw, float(int(value)), frozenset({"upper"}))


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_floor_phrase(value: object, *, source: str) -> FloorEvidence | None:
    """Lenient parse of a field that only ever describes a floor (e.g. "Semi-basement", "3rd")."""
    raw = "" if value is None else str(value).strip()
    text = normalize_text(raw)
    if not text:
        return None
    try:
        return _numeric_evidence(source, raw, float(text))
    except ValueError:
        pass
    for floor_class, pattern in _CLASS_PATTERNS:
        if re.search(rf"(?<!\w){pattern}(?!\w)", text):
            return _class_evidence(source, raw, floor_class)
    match = _ORDINAL_RE.search(text)
    if match:
        storey = _ordinal_value(match.group(0))
        if storey is not None:
            return _upper_evidence(source, raw, storey)
    return None


def decode_spitogatos_floor(code: object) -> FloorEvidence | None:
    """Spitogatos ``floorNumber`` code → evidence; unknown codes give none."""
    if code is None or isinstance(code, bool):
        return None
    try:
        number = float(str(code))
    except (TypeError, ValueError):
        return None
    if not number.is_integer():
        return None
    code_int = int(number)
    if code_int in _SPITOGATOS_SPECIAL:
        value, classes = _SPITOGATOS_SPECIAL[code_int]
        return FloorEvidence("spitogatos_code", str(code_int), value, classes)
    if _SPITOGATOS_UPPER_OFFSET < code_int <= _SPITOGATOS_MAX_CODE:
        return _upper_evidence("spitogatos_code", str(code_int), code_int - _SPITOGATOS_UPPER_OFFSET)
    return None


# Anchored contexts for free text. Each pattern exposes the floor phrase as group "x".
_PHRASE = r"(?P<x>" + "|".join(p for _, p in _CLASS_PATTERNS) + "|" + _ORDINAL_RE.pattern.replace(r"\b", "").replace("(?!\\w)", "") + r")"
_ANCHORED_RES: tuple[re.Pattern[str], ...] = (
    # "floor: basement", "floor - 2nd", "οροφοσ: 1οσ"
    re.compile(rf"(?:floor|level|οροφοσ|οροφου)\s*[:\-]\s*{_PHRASE}(?!\w)"),
    # "2nd floor", "ground floor", "second floor", "2ου οροφου", "α' οροφοσ"
    re.compile(rf"(?<!\w){_PHRASE}\s+(?:floor|οροφ\w*)(?!\w)"),
    # "is on the basement", "located on the 3rd", "situated on level -1 (basement)"
    re.compile(rf"(?:is|located|situated|lies)\s+on\s+the\s+{_PHRASE}(?!\w)"),
    re.compile(rf"(?:is|located|situated|lies)\s+(?:on|at)\s+(?:the\s+)?level\s+-?\d\s*\(\s*{_PHRASE}(?!\w)"),
    # "basement apartment", "semi-basement flat"
    re.compile(rf"(?<!\w){_PHRASE}\s+(?:apartment|flat|studio|maisonette|unit)(?!\w)"),
)
# Greek floor words are used standalone ("ΔΙΑΜΕΡΙΣΜΑ 46 ΤΜ,ΗΜΙΥΠΟΓΕΙΟ,..."), and so is the English
# "semi-basement" ("...SEMI-BASEMENT WITH YARD. 52 M2, 2 BEDROOMS" — Plateia Koliatsou #1,
# Nov 2026, ranked as a ground floor from the Spitogatos code alone). A bare English "basement"
# is not taken standalone: "the building has a basement" is common.
_GREEK_STANDALONE_RE = re.compile(
    r"(?<!\w)(?P<x>ημιυπογει\w*|υπογει\w*|υπερυψωμεν\w*(?:\s+ισογει\w*)?|ημι[οω]ροφ\w*|ισογει\w*"
    r"|semi[\s-]?basement)(?!\w)"
)
_AMENITY_RE = re.compile(
    r"(?:storage|store ?room|αποθηκ|parking|garage|σταθμευσ|γκαραζ|boiler|λεβητ|laundry|πλυστηρ|πιλοτ)"
)
_LOCATIVE_RE = re.compile(r"(?:in the|at the|στο|στον|στην|του)\s*$")
_POSITIVE_CUE_RE = re.compile(r"(?:apartment|flat|studio|located|is|διαμερισμα|βρισκεται|ειναι)")


_UNRELIABLE_UPPER_RE = re.compile(r"(?:is built on the|built on the|up to the|goes up to|elevator|lift|ανελκυστηρ|ασανσερ)")


_AMENITY_AFTER_RE = re.compile(r"^\s*(?:storage|store ?room|αποθηκ|parking|garage|σταθμευσ|γκαραζ|boiler|λεβητ|laundry|πλυστηρ)")


def _excluded(text: str, start: int, floor_class: str, end: int | None = None) -> bool:
    """Skip mentions that do not describe the flat's own floor."""
    if start > 0 and text[start - 1] == "-":
        return True  # "-5th", "under-ground", "basement-ground"
    if end is not None and floor_class in {"basement", "semi_basement"} and _AMENITY_AFTER_RE.search(text[end:end + 25]):
        return True  # "a 5.2 sq.m. basement storage room", "υπογεια αποθηκη"
    before = text[max(0, start - 40):start]
    if floor_class == "upper":
        # An agency template writes "is built on the Nth floor" with garbage N, and
        # "the elevator goes up to the 6th floor" describes the building.
        return bool(_UNRELIABLE_UPPER_RE.search(text[max(0, start - 30):start]))
    if _AMENITY_RE.search(before):
        return True
    if _LOCATIVE_RE.search(before) and not _POSITIVE_CUE_RE.search(text[max(0, start - 25):start]):
        return True
    return False


def parse_floor_text(value: object, *, source: str) -> list[FloorEvidence]:
    """Anchored floor phrases in free text; one evidence per distinct reading."""
    raw = "" if value is None else str(value)
    text = normalize_text(raw)
    if not text:
        return []
    found: list[FloorEvidence] = []
    spans: list[tuple[int, int]] = []

    def add(match: re.Match[str]) -> None:
        start, end = match.span("x")
        if any(start < s_end and end > s_start for s_start, s_end in spans):
            return
        evidence = parse_floor_phrase(match.group("x"), source=source)
        if evidence is None:
            return
        floor_class = next(iter(evidence.floor_classes))
        if _excluded(text, start, floor_class, end):
            return
        spans.append((start, end))
        found.append(FloorEvidence(source, match.group(0), evidence.parsed_value, evidence.floor_classes))

    for pattern in _ANCHORED_RES:
        for match in pattern.finditer(text):
            add(match)
    for match in _GREEK_STANDALONE_RE.finditer(text):
        add(match)

    unique: dict[tuple[float | None, frozenset[str]], FloorEvidence] = {}
    for item in found:
        unique.setdefault((item.parsed_value, item.floor_classes), item)
    return list(unique.values())


# ── Resolution ────────────────────────────────────────────────────────────────

def _class_for_value(value: float) -> str:
    for floor_class, class_value in CLASS_VALUES.items():
        if class_value == value:
            return floor_class
    return "upper"


def resolve_floor(
    *,
    structured_text: object,
    title: object,
    description: object,
    spitogatos_code: object = None,
    numeric_value: object = None,
) -> FloorResolution:
    explicit: list[FloorEvidence] = []
    structured = parse_floor_phrase(structured_text, source="structured")
    if structured is not None:
        explicit.append(structured)
    code = decode_spitogatos_floor(spitogatos_code)
    if code is not None:
        explicit.append(code)
    explicit.extend(parse_floor_text(title, source="title"))
    explicit.extend(parse_floor_text(description, source="description"))

    numeric: FloorEvidence | None = None
    if numeric_value is not None and not isinstance(numeric_value, bool):
        try:
            numeric = _numeric_evidence("numeric", str(numeric_value), float(str(numeric_value)))
        except (TypeError, ValueError):
            numeric = None

    evidence = tuple(explicit + ([numeric] if numeric is not None else []))

    if not explicit:
        if numeric is None:
            return FloorResolution(None, None, False, evidence)
        return FloorResolution(
            numeric.parsed_value, next(iter(numeric.floor_classes)), False, evidence
        )

    classes = frozenset.intersection(*(item.floor_classes for item in explicit))
    if not classes:
        union = frozenset.union(*(item.floor_classes for item in explicit))
        if union <= {"basement", "semi_basement"}:
            return FloorResolution(-1.0, "basement", False, evidence, adjacent_conflict=True)
        if union <= _GROUND_CLASSES:
            # Ground vs elevated ground share rent factor and sale multiplier; take ground.
            return FloorResolution(0.0, "ground", False, evidence)
        return FloorResolution(None, None, True, evidence)
    if classes == {"upper"}:
        storeys = {item.parsed_value for item in explicit}
        if len(storeys) != 1:
            return FloorResolution(None, None, True, evidence)
        storey = storeys.pop()
        return FloorResolution(storey, "upper", False, evidence)
    if len(classes) == 1:
        floor_class = next(iter(classes))
        return FloorResolution(CLASS_VALUES[floor_class], floor_class, False, evidence)
    # Ambiguous ground/elevated-ground code with no text naming either: use its default.
    first_value = explicit[0].parsed_value
    floor_class = _class_for_value(first_value) if first_value is not None else "ground"
    if floor_class not in classes:
        floor_class = sorted(classes)[0]
    return FloorResolution(CLASS_VALUES[floor_class], floor_class, False, evidence)


def apply_floor_resolution(listing: dict) -> FloorResolution:
    """Resolve ``listing``'s floor in place from whatever raw evidence its scraper emitted.

    Spitogatos rows carry ``floor_raw_code``; older Spitogatos files stored the same code in
    ``floor``, so for them ``floor`` is read as a code, never as a storey.
    """
    source = str(listing.get("source") or "")
    is_spitogatos = source == "spitogatos"
    code = listing.get("floor_raw_code")
    numeric = listing.get("floor")
    if is_spitogatos and code is None and numeric is not None:
        code, numeric = numeric, None
    if is_spitogatos:
        numeric = None
    resolution = resolve_floor(
        structured_text=listing.get("floor_level_text"),
        title=listing.get("title"),
        description=listing.get("description"),
        spitogatos_code=code,
        numeric_value=numeric,
    )
    if is_spitogatos and code is not None:
        listing["floor_raw_code"] = code
    listing["floor"] = resolution.value
    listing["floor_class"] = resolution.floor_class
    listing["floor_conflict"] = resolution.conflict
    listing["floor_conflict_adjacent"] = resolution.adjacent_conflict
    listing["floor_evidence"] = resolution.evidence_json()
    return resolution
