"""Floor resolution from explicit evidence (English, Greek, Spitogatos codes)."""

from __future__ import annotations

import json

import pytest

from greeceapt.collect.floor_resolver import (
    FloorResolution,
    decode_spitogatos_floor,
    parse_floor_phrase,
    resolve_floor,
)


@pytest.mark.parametrize(
    ("phrase", "value", "floor_class"),
    [
        ("Basement", -1.0, "basement"),
        ("Semi-basement", -0.5, "semi_basement"),
        ("semi basement", -0.5, "semi_basement"),
        ("Ground floor", 0.0, "ground"),
        ("Elevated ground floor", 0.5, "elevated_ground"),
        ("Raised ground floor", 0.5, "elevated_ground"),
        ("Mezzanine", 0.5, "elevated_ground"),
        ("1st", 1.0, "upper"),
        ("2nd floor", 2.0, "upper"),
        ("3rd", 3.0, "upper"),
        ("ΥΠΟΓΕΙΟ", -1.0, "basement"),
        ("Ημιυπόγειο", -0.5, "semi_basement"),
        ("Ισόγειο", 0.0, "ground"),
        ("Υπερυψωμένο", 0.5, "elevated_ground"),
        ("Ημιόροφος", 0.5, "elevated_ground"),
        ("1ος", 1.0, "upper"),
        ("2ος όροφος", 2.0, "upper"),
    ],
)
def test_structured_phrases(phrase, value, floor_class):
    evidence = parse_floor_phrase(phrase, source="structured")
    assert evidence is not None
    assert evidence.parsed_value == value
    assert floor_class in evidence.floor_classes


def test_semi_basement_is_never_read_as_basement():
    assert parse_floor_phrase("ΗΜΙΥΠΟΓΕΙΟ", source="structured").floor_classes == frozenset(
        {"semi_basement"}
    )
    assert parse_floor_phrase("semi-basement", source="structured").floor_classes == frozenset(
        {"semi_basement"}
    )


@pytest.mark.parametrize(
    ("code", "value", "classes"),
    [
        (1, -1.0, {"basement"}),
        (2, -0.5, {"semi_basement"}),
        (3, 0.0, {"ground", "elevated_ground"}),
        (4, 0.5, {"ground", "elevated_ground"}),
        (6, 1.0, {"upper"}),
        (7, 2.0, {"upper"}),
        (8, 3.0, {"upper"}),
        (12, 7.0, {"upper"}),
    ],
)
def test_spitogatos_code_table(code, value, classes):
    evidence = decode_spitogatos_floor(code)
    assert evidence is not None
    assert evidence.parsed_value == value
    assert evidence.floor_classes == frozenset(classes)


@pytest.mark.parametrize("code", [0, 5, None, "", "x", 99, -3])
def test_unknown_spitogatos_codes_give_no_evidence(code):
    assert decode_spitogatos_floor(code) is None


def test_code_and_matching_text_agree():
    resolution = resolve_floor(
        structured_text=None,
        title=None,
        description="Athens, Ampelokipoi, Studio For Sale, 46 sq.m., Floor: Basement, 1 Level(s)",
        spitogatos_code=1,
    )
    assert resolution == FloorResolution(
        value=-1.0, floor_class="basement", conflict=False, evidence=resolution.evidence
    )
    assert len(resolution.evidence) == 2


def test_code_six_is_first_floor():
    resolution = resolve_floor(
        structured_text=None, title=None, description="Floor: 1st, 2 bedrooms", spitogatos_code=6
    )
    assert (resolution.value, resolution.floor_class, resolution.conflict) == (1.0, "upper", False)


def test_ambiguous_code_takes_the_class_named_by_text():
    resolution = resolve_floor(
        structured_text=None,
        title=None,
        description="Apartment For sale, floor: Elevated ground floor, in Kipseli",
        spitogatos_code=3,
    )
    assert (resolution.value, resolution.floor_class) == (0.5, "elevated_ground")


def test_ambiguous_code_without_text_uses_its_default():
    assert resolve_floor(
        structured_text=None, title=None, description=None, spitogatos_code=3
    ).floor_class == "ground"
    assert resolve_floor(
        structured_text=None, title=None, description=None, spitogatos_code=4
    ).floor_class == "elevated_ground"


def test_structured_basement_and_description_second_floor_conflict():
    resolution = resolve_floor(
        structured_text="Basement", title=None, description="Bright apartment on the 2nd floor."
    )
    assert resolution.value is None
    assert resolution.floor_class is None
    assert resolution.conflict is True


def test_text_basement_overrides_incompatible_bare_number():
    resolution = resolve_floor(
        structured_text=None,
        title=None,
        description="Furnished basement apartment for sale, renovated in 2023.",
        numeric_value=1,
    )
    assert (resolution.value, resolution.floor_class, resolution.conflict) == (-1.0, "basement", False)
    assert any(item.source == "numeric" for item in resolution.evidence)


def test_bare_number_is_used_only_without_explicit_evidence():
    resolution = resolve_floor(structured_text=None, title=None, description=None, numeric_value=-0.5)
    assert (resolution.value, resolution.floor_class) == (-0.5, "semi_basement")


@pytest.mark.parametrize(
    "description",
    [
        "Apartment on the 2nd floor with a storage room in the basement.",
        "Διαμέρισμα 2ου ορόφου με αποθήκη στο υπόγειο.",
        "2nd floor flat, parking space in the basement of the building.",
    ],
)
def test_basement_storage_mentions_are_not_floor_evidence(description):
    resolution = resolve_floor(structured_text=None, title=None, description=description)
    assert (resolution.value, resolution.floor_class, resolution.conflict) == (2.0, "upper", False)


def test_greek_uppercase_semibasement_token_in_description():
    resolution = resolve_floor(
        structured_text=None,
        title=None,
        description="ΑΜΠΕΛΟΚΗΠΟΙ ΔΙΑΜΕΡΙΣΜΑ 46 ΤΜ,ΗΜΙΥΠΟΓΕΙΟ,ΚΑΤΑΣΚΕΥΗΣ 1976,ΑΝΑΚΑΙΝΙΣΗ 2023.",
    )
    assert (resolution.value, resolution.floor_class) == (-0.5, "semi_basement")


def test_unanchored_ordinals_are_ignored():
    resolution = resolve_floor(
        structured_text=None, title=None, description="Close to the 1st metro exit and 3rd school."
    )
    assert resolution.value is None and resolution.conflict is False


def test_html_entities_and_tags_are_normalized():
    resolution = resolve_floor(
        structured_text=None, title=None, description="For sale,<br /> floor:&nbsp;Semi-basement"
    )
    assert resolution.floor_class == "semi_basement"


def test_evidence_is_json_serializable():
    resolution = resolve_floor(
        structured_text="3rd", title=None, description="Floor: 3rd", spitogatos_code=8
    )
    payload = json.loads(json.dumps(resolution.evidence_json()))
    assert {item["source"] for item in payload} == {"structured", "spitogatos_code", "description"}


@pytest.mark.parametrize(
    "description",
    [
        "This property is now available for sale. This apartment is built on the 5th floor and sized about 56sqm.",
        "This studio is built on the -5th floor and sized about 32sqm.",
        "It will be sold renovated. The elevator goes up to the 6th floor.",
        "An under-ground apartment of 37 sq.m. is available.",
    ],
)
def test_template_and_amenity_floor_mentions_are_ignored(description):
    resolution = resolve_floor(structured_text=None, title=None, description=description)
    assert resolution.evidence == ()


def test_lower_ground_floor_means_semi_basement():
    assert parse_floor_phrase("Lower ground floor", source="structured").floor_classes == frozenset(
        {"semi_basement"}
    )


def test_basement_versus_semi_basement_resolves_to_basement_with_flag():
    resolution = resolve_floor(
        structured_text=None,
        title=None,
        description="Bright semi-basement apartment, fully renovated.",
        spitogatos_code=1,
    )
    assert (resolution.value, resolution.floor_class, resolution.conflict) == (-1.0, "basement", False)
    assert resolution.adjacent_conflict is True


def test_other_disagreements_stay_conflicts():
    resolution = resolve_floor(
        structured_text=None, title=None, description="Floor: ground floor", spitogatos_code=2
    )
    assert resolution.conflict is True
    assert resolution.adjacent_conflict is False


def test_ground_and_elevated_ground_mentions_are_compatible():
    resolution = resolve_floor(
        structured_text=None,
        title=None,
        description="Raised ground floor apartment. The building entrance is on the ground floor.",
        spitogatos_code=4,
    )
    assert (resolution.floor_class, resolution.conflict) == ("ground", False)


def test_level_minus_one_and_a_standalone_semi_basement_are_floor_evidence():
    """Nov 2026: two #1 flats ranked as ground floors from the Spitogatos code alone — the ads
    say "situated on level -1 (basement)" and "...SEMI-BASEMENT WITH YARD"."""
    from greeceapt.collect.floor_resolver import parse_floor_text, resolve_floor

    def classes(text):
        return [sorted(e.floor_classes) for e in parse_floor_text(text, source="description")]

    assert classes("This fully renovated 34 m² apartment is situated on level -1 (basement).") == [["basement"]]
    assert classes("...SEMI-BASEMENT WITH YARD. 52 M2, 2 BEDROOMS, RENOVATED") == [["semi_basement"]]
    assert classes("a wc, and a 5.2 sq.m. basement storage room") == []          # storage, not the flat
    assert classes("the building has a basement") == []                          # bare English: not taken
    assert classes("storage room in the semi-basement") == []
    # text against the ground-floor code: unclear, so the flat is left unranked until checked
    assert resolve_floor(structured_text=None, title=None, spitogatos_code=3,
                         description="...SEMI-BASEMENT WITH YARD.").conflict


def test_rescore_rereads_the_floor_but_keeps_a_floor_the_evidence_no_longer_gives():
    from greeceapt.scoring.ranking import refresh_floor

    stored_ground = {"source": "spitogatos", "floor_raw_code": 3.0, "floor": 0.0, "floor_class": "ground",
                     "floor_conflict": False, "description": "...SEMI-BASEMENT WITH YARD. 52 M2"}
    refresh_floor(stored_ground)
    assert stored_ground["floor_conflict"] and stored_ground["floor_class"] is None   # text vs code: unclear
    legacy = {"source": "spitogatos", "floor_raw_code": 0.0, "floor": 6, "floor_class": "upper",
              "floor_conflict": False, "description": ""}
    refresh_floor(legacy)
    assert legacy["floor_class"] == "upper" and legacy["floor"] == 6                    # nothing better: kept
