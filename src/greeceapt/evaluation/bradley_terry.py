"""
Bradley-Terry ranking from pairwise comparisons — pure Python, no numpy/scipy.

Given "A beats B" judgements, recovers a latent strength theta per item such that
P(i beats j) = sigma(theta_i - theta_j). Fitted by the standard MM (minorize-maximize)
iteration, which is monotonic and needs no learning rate.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Comparison:
    """One pairwise judgement. ``winner`` is ``"a"``, ``"b"`` or ``"="`` (tie)."""
    a_id: str
    b_id: str
    winner: str


def _win_matrix(comparisons: Iterable[Comparison]) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Return (item_ids, wins) where wins[i][j] is the (fractional) times i beat j."""
    wins: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    items: set[str] = set()
    for c in comparisons:
        items.add(c.a_id)
        items.add(c.b_id)
        if c.winner == "a":
            wins[c.a_id][c.b_id] += 1.0
        elif c.winner == "b":
            wins[c.b_id][c.a_id] += 1.0
        else:  # tie: half a win each way
            wins[c.a_id][c.b_id] += 0.5
            wins[c.b_id][c.a_id] += 0.5
    return sorted(items), wins


def fit_bradley_terry(
    comparisons: Iterable[Comparison],
    *,
    max_iter: int = 500,
    tol: float = 1e-9,
    reg: float = 1.0,
) -> dict[str, float]:
    """
    Fit Bradley-Terry strengths. Returns ``{item_id: theta}``, mean-centred (``theta = log p``).

    ``reg`` adds one virtual win and one virtual loss for every item against a phantom
    opponent of strength 1, so an item that never won still gets a finite, sensible theta
    instead of collapsing to zero. Without it the MM update divides by zero for such items.
    """
    items, wins = _win_matrix(comparisons)
    if not items:
        return {}

    # total games between each unordered pair (i,j), including ties as 1 game
    games: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for i in items:
        for j, w in wins[i].items():
            games[i][j] += w
            games[j][i] += w

    p = {i: 1.0 for i in items}
    total_wins = {i: sum(wins[i].values()) for i in items}

    for _ in range(max_iter):
        new_p: dict[str, float] = {}
        for i in items:
            # numerator: real wins + one virtual win
            num = total_wins[i] + reg
            # denominator: sum over opponents of games/(p_i+p_j), + virtual game vs strength 1
            den = reg / (p[i] + 1.0)
            for j in items:
                n_ij = games[i][j]
                if n_ij:
                    den += n_ij / (p[i] + p[j])
            new_p[i] = num / den if den > 0 else p[i]

        # renormalise to geometric mean 1 (keeps the scale from drifting)
        logmean = sum(math.log(v) for v in new_p.values()) / len(new_p)
        scale = math.exp(-logmean)
        new_p = {k: v * scale for k, v in new_p.items()}

        delta = max(abs(new_p[i] - p[i]) / max(p[i], 1e-12) for i in items)
        p = new_p
        if delta < tol:
            break

    return {i: math.log(p[i]) for i in items}


def connected_components(comparisons: Iterable[Comparison]) -> list[set[str]]:
    """
    Distinct connected components of the comparison graph.

    Theta values are only comparable *within* a component. If labelling produces two
    disconnected clusters, their scales are arbitrary relative to each other and any
    cross-cluster comparison is meaningless — regularisation hides this, so it must be
    checked explicitly (``--stats`` asserts this is 1).
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    seen = False
    for c in comparisons:
        seen = True
        union(c.a_id, c.b_id)
    if not seen:
        return []

    groups: dict[str, set[str]] = defaultdict(set)
    for node in list(parent):
        groups[find(node)].add(node)
    return sorted(groups.values(), key=lambda g: (-len(g), sorted(g)[0]))
