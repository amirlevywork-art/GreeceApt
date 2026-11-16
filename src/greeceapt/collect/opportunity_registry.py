"""
Persistent opportunity identities.

An opportunity ID is minted once, from the first URL that created it, and then stored. It
never changes because another advertisement joins the group, so renovation budgets, review
decisions and history can key on it across runs. When two known opportunities turn out to be
one, the older ID survives and the other becomes an alias; when a group is split, the
split-off part gets a new ID with an alias record (``reason="split"``).

All writes of one ``assign_ids`` call happen in a single transaction that the caller commits
only after its outputs are safely written.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import MutableMapping, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id   TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    created_from_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_members (
    url            TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_aliases (
    alias_id     TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    merged_at    TEXT NOT NULL,
    reason       TEXT NOT NULL
);
"""


def mint_id(url: str) -> str:
    return "opp_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class OpportunityRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level="DEFERRED")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── Lookups ───────────────────────────────────────────────────────────────

    def _canonical(self, opportunity_id: str) -> str:
        seen: set[str] = set()
        current = opportunity_id
        while current not in seen:
            seen.add(current)
            # Only a merge redirects. A split record is lineage (the new ID came from the old
            # one); following it sent the split-off part back to the old ID on the next run,
            # where it was taken, so a fresh ID was minted every run (fixed 2026-11-16).
            row = self._conn.execute(
                "SELECT canonical_id FROM opportunity_aliases WHERE alias_id = ? AND reason != 'split'",
                (current,),
            ).fetchone()
            if row is None:
                return current
            current = row[0]
        return current

    def resolve(self, id_or_url: str) -> str | None:
        """Canonical ID for an opportunity ID, alias or member URL; ``None`` if unknown."""
        row = self._conn.execute(
            "SELECT opportunity_id FROM opportunity_members WHERE url = ?", (id_or_url,)
        ).fetchone()
        if row is not None:
            return self._canonical(row[0])
        known = self._conn.execute(
            "SELECT 1 FROM opportunities WHERE opportunity_id = ? "
            "UNION SELECT 1 FROM opportunity_aliases WHERE alias_id = ?",
            (id_or_url, id_or_url),
        ).fetchone()
        return self._canonical(id_or_url) if known else None

    def _created_at(self, opportunity_id: str) -> str:
        row = self._conn.execute(
            "SELECT created_at FROM opportunities WHERE opportunity_id = ?", (opportunity_id,)
        ).fetchone()
        return row[0] if row else "9999"

    # ── Assignment ────────────────────────────────────────────────────────────

    def assign_ids(self, groups: Sequence[Sequence[str]], *, now: datetime) -> dict[str, str]:
        """Return ``{url: opportunity_id}`` for every URL in ``groups`` (uncommitted)."""
        stamp = now.isoformat()
        result: dict[str, str] = {}
        claimed: dict[str, int] = {}  # canonical id → index of the group that kept it
        ordered = sorted((sorted(set(group)) for group in groups if group), key=lambda g: g[0])

        for position, urls in enumerate(ordered):
            known = sorted(
                {cid for cid in (self.resolve(url) for url in urls) if cid is not None},
                key=lambda cid: (self._created_at(cid), cid),
            )
            available = [cid for cid in known if cid not in claimed]
            if available:
                keep = available[0]
                for other in available[1:]:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO opportunity_aliases VALUES (?, ?, ?, ?)",
                        (other, keep, stamp, "merge"),
                    )
            else:
                creator = urls[0]
                keep = mint_id(creator)
                if keep in claimed or self._created_at(keep) != "9999":
                    # The natural ID belongs to a group this one was split from.
                    keep = mint_id(f"{creator}#split@{stamp}")
                for previous in known:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO opportunity_aliases VALUES (?, ?, ?, ?)",
                        (keep, previous, stamp, "split"),
                    )
                self._conn.execute(
                    "INSERT OR IGNORE INTO opportunities VALUES (?, ?, ?)", (keep, stamp, creator)
                )
            claimed[keep] = position
            for url in urls:
                self._conn.execute(
                    """
                    INSERT INTO opportunity_members (url, opportunity_id, first_seen, last_seen)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET opportunity_id = excluded.opportunity_id,
                                                   last_seen = excluded.last_seen
                    """,
                    (url, keep, stamp, stamp),
                )
                result[url] = keep
        return result

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def attach_opportunity_ids(
    registry: OpportunityRegistry,
    listings: Sequence[MutableMapping],
    *,
    now: datetime,
) -> None:
    """Set ``opportunity_id`` on each listing from its member URLs (uncommitted)."""
    from greeceapt.storage.util import normalize_listing_url

    groups: list[list[str]] = []
    for listing in listings:
        urls = list(listing.get("source_urls") or []) or [listing.get("url")]
        group = sorted({normalize_listing_url(str(url)) or str(url) for url in urls if url})
        groups.append(group or [str(listing.get("url"))])
    ids = registry.assign_ids(groups, now=now)
    for listing, group in zip(listings, groups):
        listing["opportunity_id"] = ids[group[0]]
