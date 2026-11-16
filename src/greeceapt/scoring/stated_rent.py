"""
The rent a sale ad says the flat is let for **now** — a signed rent, so the floor of its rent.

Decided 2026-11-16: "if the ad says the flat is already rented at a price, that is the lower bound
for that flat". Only a current lease counts ("currently rented for €380", "already leased at
€450/month", "μισθωμένο 500 ευρώ", "current rent €420"); an agent's estimate ("estimated /
potential / expected rental income of €350", "you can rent it 400 €") and a past lease
("previously rented at €300", "the monthly rent was €280", "είχε ενοίκιο 450") do not.

Nov 2026: 38 of 1,279 sale ads name a rent next to a rent word; about 25 are current leases.
"""

from __future__ import annotations

import re

from greeceapt.collect.floor_resolver import normalize_text

MIN_RENT_EUR = 100.0
MAX_RENT_EUR = 2_000.0
MAX_YEARLY_SHARE_OF_PRICE = 0.25   # a stated rent above 25 % a year of the price is a misread

_AMOUNT = r"(?:€\s?(?P<a1>\d{2,4}(?:[.,]\d{2})?)|(?P<a2>\d{2,4}(?:[.,]\d{2})?)\s?(?:€|ευρω|euros?\b|eur\b))"
# A current lease, then the amount within a few words (or the amount, then a current-lease word).
_LEASE = (
    r"(?:(?:currently|already|is|it is|being|has been|now)\s+(?:being\s+)?(?:rented|leased|let)"
    r"|\brented\b|\bleased\b|current rent|the current rent is|monthly rent|rental income"
    r"|μισθωμεν\w*|μισθομεν\w*|ενοικιασμεν\w*|τρεχον μισθωμα|με μισθωμα|ενοικιο|μισθωμα)"
)
_LEASE_THEN_AMOUNT = re.compile(_LEASE + r"[^.€]{0,45}?" + _AMOUNT)
# Words that make the amount an estimate or a past lease, looked for just before it.
_NOT_CURRENT = re.compile(
    r"estimat\w*|potential\w*|expected|expect\b|could|can rent|you can|possib\w*|projected"
    r"|previously|used to|was rented|rent was|had been|formerly|before"
    r"|εκτιμ\w*|δυνητικ\w*|αναμενομεν\w*|μπορει|θα μπορουσε|ειχε|πριν"
)
# "rented / leased / μισθωμένο <amount>" is itself a current lease; a bare "rent" / "rental income"
# word next to an amount counts only when the ad also says the flat is let now.
_DIRECT = re.compile(r"\b(?:rented|leased|let)\b|μισθωμεν|μισθομεν|ενοικιασμεν")
_CURRENTLY_LET = re.compile(
    r"(?:currently|already|is|it is|being|now)\s+(?:being\s+)?(?:rented|leased|let)\b|current rent"
    r"|μισθωμεν\w*|μισθομεν\w*|ενοικιασμεν\w*|tenant (?:who pays|pays|is|can stay|can remain)"
    r"|tenants? (?:can|may) (?:stay|remain)|lease (?:agreement|until|expires)|until \d|μεχρι"
)


def _amount(match: re.Match[str]) -> float:
    text = (match.group("a1") or match.group("a2")).replace(",", ".")
    if text.count(".") == 1 and len(text.split(".")[1]) == 3:   # "1.200" thousands, not decimals
        text = text.replace(".", "")
    return float(text)


def stated_rent(description: object, price_eur: float | None = None) -> float | None:
    """Monthly rent of the current lease stated in the ad, or ``None``."""
    text = normalize_text(description)
    if not text:
        return None
    let_now = bool(_CURRENTLY_LET.search(text))
    for match in _LEASE_THEN_AMOUNT.finditer(text):
        amount_at = match.start("a1") if match.group("a1") else match.start("a2")
        sentence = max(text.rfind(". ", 0, match.start()), text.rfind("\n", 0, match.start())) + 1
        before = text[max(sentence, match.start() - 60):amount_at]
        if _NOT_CURRENT.search(before):
            continue
        if not let_now and not _DIRECT.search(match.group(0)):
            continue
        rent = _amount(match)
        if not MIN_RENT_EUR <= rent <= MAX_RENT_EUR:
            continue
        if price_eur and 12 * rent > MAX_YEARLY_SHARE_OF_PRICE * price_eur:
            continue
        return rent
    return None
