"""Canonical neighborhood aliases (approved 2026-11-05: table A mapped, B and C kept)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greeceapt.storage import neighborhoods as nb

APPROVED_A = {
    "Amerikis Square": "Plateia Amerikis",
    "Attica Square": "Plateia Attikis",
    "Kypseli": "Kipseli",
    "Nea Kypseli": "Nea Kipseli",
    "Koliatsou": "Plateia Koliatsou",
    "Akadimia Platonos": "akadimia Platonos",
    "Vathis Square": "Plateia Vathis",
    "Viktorias Square": "Agiou Meletiou - Victorias Square - Marni",
    "Pedion tou Areos": "Pedion Areos",
    "Neapoli": "Neapoli Exarcheion",
}
KEPT_SEPARATE = [
    "Gyzi", "Koukaki", "Keramikos", "Strefi Hill", "Lambrakis Hill", "Filopappou", "Agios Loukas",
    "Papadiamantis Square", "Nirvana", "Ipirou", "OTE", "Tris Gefires", "Gouva",
    "Agia Paraskevi", "Kallithea", "Zografou", "Peristeri", "Vyronas",
]


@pytest.mark.parametrize(("raw", "canonical"), sorted(APPROVED_A.items()))
def test_approved_aliases(raw, canonical):
    assert nb.canonical_neighborhood(raw) == canonical


@pytest.mark.parametrize("raw", KEPT_SEPARATE)
def test_unapproved_names_pass_through_unchanged(raw):
    assert nb.canonical_neighborhood(raw) == raw


def test_matching_ignores_case_accents_and_spacing():
    assert nb.canonical_neighborhood("  amerikis   square ") == "Plateia Amerikis"
    assert nb.canonical_neighborhood("Viktorias  Square") == "Agiou Meletiou - Victorias Square - Marni"


def test_none_and_blank():
    assert nb.canonical_neighborhood(None) is None
    assert nb.canonical_neighborhood("   ") is None


def test_mapping_is_idempotent_and_targets_are_not_aliases():
    for target in nb.load_aliases().values():
        assert nb.canonical_neighborhood(target) == target
    for raw in APPROVED_A:
        once = nb.canonical_neighborhood(raw)
        assert nb.canonical_neighborhood(once) == once


def test_alias_file_rejects_chains(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"version": 1, "aliases": {"A": "B", "B": "C"}}))
    with pytest.raises(ValueError, match="chain"):
        nb.load_aliases(path)


def test_tier_map_is_keyed_by_canonical_names_only():
    from greeceapt.storage.neighborhoods import NEIGHBORHOOD_CANONICAL

    for key in NEIGHBORHOOD_CANONICAL:
        assert nb.canonical_neighborhood(key) == key, key
    assert NEIGHBORHOOD_CANONICAL["Kipseli"] is not None
    assert "Kypseli" not in NEIGHBORHOOD_CANONICAL


def test_merge_applies_aliases_and_keeps_raw_name():
    from greeceapt.collect.run_all import canonicalize_listing_neighborhood

    row = {"neighborhood": "Amerikis Square"}
    canonicalize_listing_neighborhood(row)
    assert row == {"neighborhood": "Plateia Amerikis", "neighborhood_raw": "Amerikis Square"}


def test_november_name_count_shrinks(tmp_path):
    rows = json.loads((Path(__file__).parent / "fixtures" / "november_ranking_input_2026.json").read_text())
    before = {row["neighborhood"] for row in rows}
    after = {nb.canonical_neighborhood(row["neighborhood"]) for row in rows}
    assert len(after) == len(before) - len(APPROVED_A)
