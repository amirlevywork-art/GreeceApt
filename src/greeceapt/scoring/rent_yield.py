"""
Euro rent and yield for a sale listing from the calibrated rent model (``greeceapt.rent``).

Consistent with ``yield_model``: when a renovation is charged in ``all_in_cost`` the flat earns
*post-renovation* rent at the renovation's target visual condition (``estimate.condition_score``,
8.0). Otherwise it earns rent at its own visual condition. Condition comes from the photos only.

**Rent.** The model learns *asking* rents. A flat lets for less: the first-ranked flat of Nov 2026
(Ampelokipoi, 46 m² basement) was predicted €536 central / €441 conservative and lets for €400
(decided 2026-11-16). ``asking_to_achieved`` (10 %) takes asked down to signed rent, so the ranked
conservative rent is ``rent_lo = model 10th percentile × 0.90`` (€397 there). When the sale ad says
the flat is let now for a rent (``stated_rent``), that signed rent is its floor (decided
2026-11-16): ``rent_lo = max(rent_lo, stated)``, and the central rent is never below it.

**Costs, per year** (``CostAssumptions``; decided 2026-11-16: every cost, a management company and
the 15 % tax in the ranked yield)::

    rent                 12 × monthly rent
    − vacancy            0.5 month a year (a new tenant every few years)
    = collected
    − management         1 month's rent a year (Athens firms quote 8–12 % of the rent)
    − maintenance        5 % of the rent
    − insurance          €100
    − ENFIA              €3.5 per m² (central Athens zone values; the bill depends on the address)
    − accountant         €300 (E1/E2 filing for a non-resident owner)
    − income tax         15 % on 95 % of the collected rent (first bracket, up to €12,000 a year)
    = net                → net_yield_lo = net / all_in_cost   (ranked)

``net_yield_lo_before_tax`` is the same before income tax; ``gross_yield`` is a year of the same
conservative signed rent over the cost, before anything (decided 2026-11-16: the three yields use
one rent, so they differ only by the costs and the tax). The rent is for the flat let unfurnished (the model
fits "furnished" as a control and predicts without it): furnishing is left to whoever lets it
(decided 2026-11-16), so it changes neither rent nor cost. Financing and Israeli tax on foreign
income (credit for the Greek tax) are not modelled.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from greeceapt.scoring.stated_rent import stated_rent
from greeceapt.scoring.yield_model import YieldEstimate


@dataclass(frozen=True)
class CostAssumptions:
    asking_to_achieved: float = 0.10     # signed rent = asking rent × (1 − this)
    vacancy_months: float = 0.5          # empty months a year
    management_months: float = 1.0       # management company: months of rent a year
    maintenance_rate: float = 0.05       # share of the year's rent
    insurance_eur: float = 100.0
    enfia_eur_per_sqm: float = 3.5
    accountant_eur: float = 300.0
    income_tax_rate: float = 0.15
    income_tax_deduction: float = 0.05   # flat deduction before the tax rate

    def to_json(self) -> dict[str, float]:
        return asdict(self)


DEFAULT_COSTS = CostAssumptions()


def annual_costs(monthly_rent: float, area_sqm: float, costs: CostAssumptions = DEFAULT_COSTS) -> dict[str, float]:
    """The year in euros for a flat letting at ``monthly_rent`` (already the signed rent)."""
    gross = 12.0 * monthly_rent
    vacancy = costs.vacancy_months * monthly_rent
    collected = gross - vacancy
    management = costs.management_months * monthly_rent
    maintenance = costs.maintenance_rate * gross
    enfia = costs.enfia_eur_per_sqm * area_sqm
    before_tax = collected - management - maintenance - costs.insurance_eur - enfia - costs.accountant_eur
    tax = costs.income_tax_rate * (1.0 - costs.income_tax_deduction) * collected
    return {
        "rent": gross, "vacancy": vacancy, "collected": collected, "management": management,
        "maintenance": maintenance, "insurance": costs.insurance_eur, "enfia": enfia,
        "accountant": costs.accountant_eur, "before_tax": before_tax, "income_tax": tax,
        "net": before_tax - tax,
    }


@dataclass(frozen=True)
class RentYield:
    rent_hat: float | None               # central signed rent a month
    rent_lo: float | None                # conservative signed rent a month (ranked)
    gross_yield: float | None            # 12 × rent_lo / cost
    net_yield_lo: float | None           # after every cost and income tax (ranked)
    rent_basis: str | None  # as_is | post_renovation
    unavailable_reason: str | None = None
    # few rentals around it: the rent rests on distant evidence (and a wider conservative bound)
    few_rentals_nearby: bool = False
    net_yield_lo_before_tax: float | None = None
    costs: dict[str, float] = field(default_factory=dict)   # the conservative year, plus asked rents


def _unavailable(reason: str) -> RentYield:
    return RentYield(None, None, None, None, None, reason)


def _coordinate(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def estimate_rent_yield(listing: Mapping[str, Any], estimate: YieldEstimate, model,
                        costs: CostAssumptions = DEFAULT_COSTS, *, with_lease: bool = True) -> RentYield:
    """``with_lease=False``: the market rent, ignoring a lease in the ad (the tenant has left)."""
    if model is None:
        return _unavailable("no_rent_model")
    if estimate.exclusion_reason is not None or not estimate.all_in_cost:
        return _unavailable("unranked")
    lat, lon = _coordinate(listing.get("latitude")), _coordinate(listing.get("longitude"))
    if lat is None or lon is None:
        return _unavailable("no_coordinates")
    subject = dict(listing, latitude=lat, longitude=lon)
    # its own score, or the renovation's target score (shifted by a stress scenario, if any)
    asked_hat, asked_lo = model.predict_rent(subject, visual_override=estimate.condition_score)
    basis = "post_renovation" if estimate.renovation_level != "none" else "as_is"
    signed = 1.0 - costs.asking_to_achieved
    rent_hat, rent_lo = asked_hat * signed, asked_lo * signed
    model_lo = rent_lo
    cost = estimate.all_in_cost
    lease = stated_rent(listing.get("description"), _coordinate(listing.get("price_eur"))) if with_lease else None
    if lease is not None:            # a tenant already pays this: the rent cannot be lower
        rent_lo = max(rent_lo, lease)
        rent_hat = max(rent_hat, rent_lo)
    area = float(listing.get("area_sqm") or 0.0)
    year = annual_costs(rent_lo, area, costs)
    return RentYield(
        rent_hat=rent_hat,
        rent_lo=rent_lo,
        gross_yield=12.0 * rent_lo / cost,
        net_yield_lo=year["net"] / cost,
        rent_basis=basis,
        few_rentals_nearby=bool(getattr(model, "is_sparse", lambda _: False)(subject)),
        net_yield_lo_before_tax=year["before_tax"] / cost,
        costs={**year, "rent_asked_hat": asked_hat, "rent_asked_lo": asked_lo, "rent_model_lo": model_lo,
               **({"rent_stated": lease} if lease is not None else {})},
    )


def renovation_alternative(listing: Mapping[str, Any], renovated: YieldEstimate, model,
                           costs: CostAssumptions = DEFAULT_COSTS) -> dict[str, Any] | None:
    """The year if the buyer renovates instead of keeping the sitting tenant (decided 2026-11-16).

    The tenant leaves, the estimated renovation is paid, and the flat is let again at the model's
    rent for its renovated condition. Shown beside the ranked "keep the tenant" figures.
    """
    rent = estimate_rent_yield(listing, renovated, model, costs, with_lease=False)
    if rent.net_yield_lo is None:
        return None
    return {
        "renovation_level": renovated.renovation_level,
        "renovation_eur": renovated.renovation_budget,
        "condition_after": renovated.condition_score,
        "all_in_cost": renovated.all_in_cost,
        "rent_lo": rent.rent_lo,
        "rent_hat": rent.rent_hat,
        "gross_yield": rent.gross_yield,
        "net_yield_lo_before_tax": rent.net_yield_lo_before_tax,
        "net_yield_lo": rent.net_yield_lo,
        "net": rent.costs.get("net"),
    }
