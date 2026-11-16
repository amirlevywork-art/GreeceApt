"""
Log-linear hedonic rent model with a leave-one-out spatial term.

    log(rent) = b0 + Σ b_j·x_j + b_loc·loc_k(x)

``loc_k`` is the mean first-pass residual of the k nearest rentals: a size/floor/condition-
adjusted local rent level, so location is not confounded with what kind of flats an area
happens to list. Ridge (closed form, intercept unpenalized) on standardized columns.

Hyper-parameters (ridge alpha, k) are chosen by 5-fold CV on duplicate-collapsed rentals. The
conservative bound is split-conformal: ``rent_lo = exp(ŷ + q10)`` with q10 the 10th percentile
of out-of-fold log residuals, so ~90 % of flats rent for at least ``rent_lo``.

q10 is taken per group (Mondrian conformal), because the error is not the same everywhere
(Nov 2026, 5,218 rentals): flats with a visual score -0.173, without -0.211; below ground and
scored -0.209 against -0.170 above. A group needs ``MIN_BOUND_GROUP`` rentals, else the overall
q10 applies. A flat whose 10th-nearest rental lies farther than 99 % of rentals' own does
(``sparse_km``; 2 of the 125 ranked flats, Nov 2026) has no local rent evidence: it takes the
lower of its group's q10 and the same group's q10 with whole neighborhoods held out.

Floors are kept monotone below ground: few basements reach the rental market and those that do
are the good ones (36 of 5,218 rentals in Nov 2026, fitted -12.6 % against -20.5 % for the 203
semi-basements), so a basement effect milder than the semi-basement one is capped at it. The
intercept absorbs the shift, so every other flat's prediction is unchanged.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from greeceapt.rent.features import FEATURES, CONTROLS, coords_km, feature_row, knn_mean, matrix

LOCATION = "location"
ALPHAS: tuple[float, ...] = (0.3, 1.0, 3.0, 10.0, 30.0)
KS: tuple[int, ...] = (5, 10, 20, 40)
LOWER_QUANTILE = 0.10
MIN_BOUND_GROUP = 100
SPARSE_NEIGHBOR = 10          # sparse: the 10th-nearest rental farther than rentals' 99th pct
SPARSE_QUANTILE = 0.99
_BELOW_GROUND = frozenset({"basement", "semi_basement"})


def bound_group(listing: Mapping[str, Any], *, visual_override: float | None = None) -> str:
    """``scored|unscored`` × ``above|below``: the conformal group of a flat."""
    from greeceapt.rent.features import accepted_visual

    scored = visual_override is not None or accepted_visual(listing) is not None
    below = listing.get("floor_class") in _BELOW_GROUND
    return f"{'scored' if scored else 'unscored'}/{'below' if below else 'above'}"


def group_quantiles(residual: np.ndarray, groups: Sequence[str]) -> dict[str, float]:
    """q10 per group with at least ``MIN_BOUND_GROUP`` members."""
    labels = np.array(list(groups))
    out = {}
    for g in sorted(set(groups)):
        mask = labels == g
        if mask.sum() >= MIN_BOUND_GROUP:
            out[g] = float(np.quantile(residual[mask], LOWER_QUANTILE))
    return out


def kth_neighbor_km(ref_xy: np.ndarray, query_xy: np.ndarray, k: int, *, exclude_self: bool) -> np.ndarray:
    """Distance from each query point to its k-th nearest reference point."""
    out = np.empty(len(query_xy))
    idx = k if exclude_self else k - 1
    for start in range(0, len(query_xy), 512):
        q = query_xy[start:start + 512]
        d = np.sqrt(((q[:, None, :] - ref_xy[None, :, :]) ** 2).sum(axis=2))
        out[start:start + 512] = np.partition(d, idx, axis=1)[:, idx]
    return out


# ── Ridge ─────────────────────────────────────────────────────────────────────

def ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Return (mean, std, beta_std, intercept) for standardized ridge."""
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std < 1e-12] = 1.0
    Z = (X - mean) / std
    y_mean = y.mean()
    A = Z.T @ Z + alpha * np.eye(Z.shape[1])
    beta = np.linalg.solve(A, Z.T @ (y - y_mean))
    return mean, std, beta, float(y_mean)


def ridge_predict(X: np.ndarray, mean: np.ndarray, std: np.ndarray, beta: np.ndarray, intercept: float) -> np.ndarray:
    return intercept + ((X - mean) / std) @ beta


# ── Model ─────────────────────────────────────────────────────────────────────

@dataclass
class RentModel:
    alpha: float
    k: int
    features: list[str]
    mean: list[float]
    std: list[float]
    beta: list[float]
    intercept: float
    ref_xy: list[list[float]]
    ref_residual: list[float]
    lower_log_residual: float = 0.0
    lower_by_group: dict[str, float] = field(default_factory=dict)
    sparse_lower_by_group: dict[str, float] = field(default_factory=dict)
    sparse_km: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    coefficients: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # raw (unstandardized) coefficient per column
    def raw_coefficients(self) -> dict[str, float]:
        return {name: b / s for name, b, s in zip(self.features, self.beta, self.std)}

    def predict_log(self, listings: Sequence[Mapping[str, Any]], *, visual_override: float | None = None) -> np.ndarray:
        rows = [feature_row(item, for_prediction=True, visual_override=visual_override) for item in listings]
        X = matrix(rows)
        xy = _xy(listings)
        loc = knn_mean(np.array(self.ref_xy), np.array(self.ref_residual), xy, self.k, exclude_self=False)
        X = np.column_stack((X, loc))
        return ridge_predict(X, np.array(self.mean), np.array(self.std), np.array(self.beta), self.intercept)

    def predict_rent(self, listing: Mapping[str, Any], *, visual_override: float | None = None) -> tuple[float, float]:
        """(central monthly rent, conservative monthly rent) in EUR."""
        log_rent = float(self.predict_log([listing], visual_override=visual_override)[0])
        return math.exp(log_rent), math.exp(log_rent + self.lower_bound(listing, visual_override=visual_override))

    def is_sparse(self, listing: Mapping[str, Any]) -> bool:
        """Its 10th-nearest training rental is farther than for 99 % of the rentals themselves."""
        if self.sparse_km is None or not self.ref_xy:
            return False
        distance = kth_neighbor_km(np.array(self.ref_xy), _xy([listing]), SPARSE_NEIGHBOR, exclude_self=False)
        return bool(distance[0] > self.sparse_km)

    def lower_bound(self, listing: Mapping[str, Any], *, visual_override: float | None = None) -> float:
        """Log-residual q10 for this flat's group (overall q10 when the group is too small)."""
        group = bound_group(listing, visual_override=visual_override)
        lower = self.lower_by_group.get(group, self.lower_log_residual)
        if self.is_sparse(listing):
            lower = min(lower, self.sparse_lower_by_group.get(group, lower))
        return lower

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "RentModel":
        return cls(**dict(payload))


def _xy(listings: Sequence[Mapping[str, Any]]) -> np.ndarray:
    lat = np.array([float(item["latitude"]) for item in listings])
    lon = np.array([float(item["longitude"]) for item in listings])
    return coords_km(lat, lon)


def _targets(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.log(np.array([float(r["rent_eur"]) for r in rows]))


def _cap_basement(features: list[str], mean: np.ndarray, std: np.ndarray, beta: np.ndarray,
                  intercept: float) -> tuple[np.ndarray, float, bool]:
    """Raw basement coefficient ≤ raw semi-basement coefficient; other predictions unchanged."""
    b, s = features.index("floor_basement"), features.index("floor_semi_basement")
    raw_basement, raw_semi = beta[b] / std[b], beta[s] / std[s]
    if raw_basement <= raw_semi:
        return beta, intercept, False
    beta = beta.copy()
    shift = (raw_semi - raw_basement) * std[b]          # change in the standardized coefficient
    beta[b] += shift
    return beta, intercept + mean[b] / std[b] * shift, True


def fit(rows: Sequence[Mapping[str, Any]], *, alpha: float, k: int, monotone_floors: bool = True) -> RentModel:
    X = matrix([feature_row(r, for_prediction=False) for r in rows])
    y = _targets(rows)
    xy = _xy(rows)
    m1, s1, b1, c1 = ridge_fit(X, y, alpha)
    residual1 = y - ridge_predict(X, m1, s1, b1, c1)
    loc = knn_mean(xy, residual1, xy, k, exclude_self=True)
    X2 = np.column_stack((X, loc))
    mean, std, beta, intercept = ridge_fit(X2, y, alpha)
    features = list(FEATURES) + [LOCATION]
    constraints: list[str] = []
    if monotone_floors:
        beta, intercept, capped = _cap_basement(features, mean, std, beta, intercept)
        if capped:
            constraints.append("floor_basement capped at floor_semi_basement")
    return RentModel(
        alpha=alpha, k=k, features=features,
        mean=mean.tolist(), std=std.tolist(), beta=beta.tolist(), intercept=intercept,
        ref_xy=xy.tolist(), ref_residual=residual1.tolist(),
        meta={"constraints": constraints} if constraints else {},
    )


def _predict_training_style(model: RentModel, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    """Predict held-out *rentals* with their own controls (furnished, days, price cut) kept,
    so CV error measures the hedonic fit, not the deliberate 'fresh, unfurnished' shift."""
    X = matrix([feature_row(r, for_prediction=False) for r in rows])
    loc = knn_mean(np.array(model.ref_xy), np.array(model.ref_residual), _xy(rows), model.k, exclude_self=False)
    return ridge_predict(np.column_stack((X, loc)), np.array(model.mean), np.array(model.std),
                         np.array(model.beta), model.intercept)


# ── Cross-validation ──────────────────────────────────────────────────────────

def folds(n: int, n_folds: int, seed: int) -> list[np.ndarray]:
    order = np.random.default_rng(seed).permutation(n)
    return [order[i::n_folds] for i in range(n_folds)]


def group_folds(groups: Sequence[str], n_folds: int, seed: int) -> list[np.ndarray]:
    """Whole neighborhoods held out together (extrapolation to unseen areas)."""
    names = sorted(set(groups))
    rng = np.random.default_rng(seed)
    rng.shuffle(names)
    assign = {name: i % n_folds for i, name in enumerate(names)}
    labels = np.array([assign[g] for g in groups])
    return [np.flatnonzero(labels == i) for i in range(n_folds)]


def metrics(y: np.ndarray, pred: np.ndarray) -> dict[str, float]:
    rent, rent_hat = np.exp(y), np.exp(pred)
    ape = np.abs(rent_hat - rent) / rent
    resid = y - pred
    return {
        "mape": float(ape.mean()),
        "median_ape": float(np.median(ape)),
        "within_10pct": float((ape <= 0.10).mean()),
        "r2_log": float(1 - (resid ** 2).sum() / ((y - y.mean()) ** 2).sum()),
        "rmse_log": float(np.sqrt((resid ** 2).mean())),
        "n": int(len(y)),
    }


def out_of_fold(rows: Sequence[Mapping[str, Any]], splits: list[np.ndarray], *, alpha: float, k: int) -> np.ndarray:
    pred = np.empty(len(rows))
    all_idx = np.arange(len(rows))
    for test in splits:
        train = np.setdiff1d(all_idx, test)
        model = fit([rows[i] for i in train], alpha=alpha, k=k)
        pred[test] = _predict_training_style(model, [rows[i] for i in test])
    return pred


def baseline_out_of_fold(rows: Sequence[Mapping[str, Any]], splits: list[np.ndarray], key: str = "hood_l4") -> np.ndarray:
    """Naive benchmark: neighborhood median €/m² × area (city median when the hood is unseen)."""
    pred = np.empty(len(rows))
    all_idx = np.arange(len(rows))
    psqm = np.array([float(r["rent_eur"]) / float(r["area_sqm"]) for r in rows])
    for test in splits:
        train = np.setdiff1d(all_idx, test)
        city = float(np.median(psqm[train]))
        by_hood: dict[str, list[float]] = {}
        for i in train:
            by_hood.setdefault(str(rows[i].get(key)), []).append(psqm[i])
        medians = {h: float(np.median(v)) for h, v in by_hood.items() if len(v) >= 5}
        for i in test:
            pred[i] = math.log(medians.get(str(rows[i].get(key)), city) * float(rows[i]["area_sqm"]))
    return pred


def select_and_fit(rows: Sequence[Mapping[str, Any]], *, seed: int = 7, n_folds: int = 5,
                   alphas: Sequence[float] = ALPHAS, ks: Sequence[int] = KS) -> RentModel:
    """Grid-search (alpha, k) on random 5-fold CV, refit on all rows, attach metrics + bound."""
    y = _targets(rows)
    splits = folds(len(rows), n_folds, seed)
    grid: list[tuple[float, float, int, np.ndarray]] = []
    for alpha in alphas:
        for k in ks:
            pred = out_of_fold(rows, splits, alpha=alpha, k=k)
            grid.append((metrics(y, pred)["rmse_log"], alpha, k, pred))
    grid.sort(key=lambda item: item[0])
    _, alpha, k, pred = grid[0]
    residual = y - pred
    lower = float(np.quantile(residual, LOWER_QUANTILE))

    hood_splits = group_folds([str(r.get("hood_l4")) for r in rows], n_folds, seed)
    hood_pred = out_of_fold(rows, hood_splits, alpha=alpha, k=k)
    groups = [bound_group(r) for r in rows]
    model = fit(rows, alpha=alpha, k=k)
    model.lower_log_residual = lower
    model.lower_by_group = group_quantiles(residual, groups)
    model.sparse_lower_by_group = group_quantiles(y - hood_pred, groups)
    xy = _xy(rows)
    model.sparse_km = float(np.quantile(kth_neighbor_km(xy, xy, SPARSE_NEIGHBOR, exclude_self=True), SPARSE_QUANTILE))
    per_row = np.array([model.lower_by_group.get(g, lower) for g in groups])
    model.metrics = {
        "random_cv": metrics(y, pred),
        "random_cv_lower_bound_coverage": float((residual >= lower).mean()),
        "group_lower_bound_coverage": {
            g: {"n": int(sum(1 for x in groups if x == g)),
                "coverage": float((residual[np.array(groups) == g] >= per_row[np.array(groups) == g]).mean())}
            for g in sorted(set(groups))
        },
        "neighborhood_holdout_cv": metrics(y, hood_pred),
        "baseline_hood_median_psqm": metrics(y, baseline_out_of_fold(rows, splits)),
        "no_location_term": metrics(y, out_of_fold(rows, splits, alpha=alpha, k=len(rows) - 1)),
        "grid": [{"rmse_log": g[0], "alpha": g[1], "k": g[2]} for g in grid],
    }
    model.coefficients = coefficient_table(model, rows)
    return model


# ── Interpretation ────────────────────────────────────────────────────────────

def coefficient_table(model: RentModel, rows: Sequence[Mapping[str, Any]], *, n_boot: int = 200,
                      seed: int = 11) -> list[dict[str, Any]]:
    """Raw coefficients with bootstrap 90 % intervals (location term held at its fitted values)."""
    X = matrix([feature_row(r, for_prediction=False) for r in rows])
    loc = knn_mean(np.array(model.ref_xy), np.array(model.ref_residual), _xy(rows), model.k, exclude_self=True)
    X2 = np.column_stack((X, loc))
    y = _targets(rows)
    rng = np.random.default_rng(seed)
    boot: list[np.ndarray] = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(rows), len(rows))
        _, std, beta, _ = ridge_fit(X2[idx], y[idx], model.alpha)
        boot.append(beta / std)
    draws = np.array(boot)
    raw = model.raw_coefficients()
    table = []
    for j, name in enumerate(model.features):
        lo, hi = np.quantile(draws[:, j], [0.05, 0.95])
        table.append({
            "feature": name,
            "coef": raw[name],
            "ci90": [float(lo), float(hi)],
            "pct_effect": None if name in {"log_area", LOCATION} else math.expm1(raw[name]) * 100,
            "control": name in CONTROLS,
            "share_nonzero": float(X2[:, j].astype(bool).mean()),
        })
    return table
