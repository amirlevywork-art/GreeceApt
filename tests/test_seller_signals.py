"""Display-only negotiation signals: price cut and days on market."""

from __future__ import annotations

from datetime import date

import pytest

from greeceapt.scoring.seller_signals import seller_signals

AS_OF = date(2026, 11, 2)


def test_price_cut_from_pre_reduction_price():
    signals = seller_signals({"price_eur": 54_000, "price_pre_reduction": 60_000}, as_of=AS_OF)
    assert signals.price_cut_pct == pytest.approx(10.0)
    assert "price_cut" in signals.flags


def test_small_cut_is_reported_but_not_flagged():
    signals = seller_signals({"price_eur": 58_000, "price_pre_reduction": 60_000}, as_of=AS_OF)
    assert signals.price_cut_pct == pytest.approx(3.3333, abs=1e-3)
    assert "price_cut" not in signals.flags


def test_price_increase_or_missing_is_not_a_cut():
    assert seller_signals({"price_eur": 60_000, "price_pre_reduction": 55_000}, as_of=AS_OF).price_cut_pct is None
    assert seller_signals({"price_eur": 60_000}, as_of=AS_OF).price_cut_pct is None


def test_days_on_market_uses_explicit_as_of():
    signals = seller_signals({"earliest_publication_date": "2026-07-01"}, as_of=AS_OF)
    assert signals.days_on_market == 124
    assert "long_on_market" in signals.flags
    assert seller_signals({"publication_date": "2026-10-20"}, as_of=AS_OF).days_on_market == 13


def test_future_or_bad_dates_are_ignored():
    assert seller_signals({"earliest_publication_date": "2027-01-01"}, as_of=AS_OF).days_on_market is None
    assert seller_signals({"earliest_publication_date": "soon"}, as_of=AS_OF).days_on_market is None
