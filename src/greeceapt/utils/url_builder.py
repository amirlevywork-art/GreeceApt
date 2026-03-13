import urllib.parse

BASE_URL_XE = "https://www.xe.gr/en/property/results"

ATHENS_CENTER_ID = "ChIJ8UNwBh-9oRQR3Y1mdkU1Nic"


def build_xe_url(
    min_price: int | None = None,
    max_price: int | None = None,
    geo_place_id: str = ATHENS_CENTER_ID,
    building_type: str = "apartment",
    has_photos: bool = False,
    page: int | None = None,
):
    params: dict[str, object] = {
        "transaction_name": "buy",
        "item_type": "re_residence",
        "geo_place_ids[]": [geo_place_id],
        "building_type_options[]": [building_type],
    }

    if min_price is not None:
        params["minimum_price"] = min_price
    if max_price is not None:
        params["maximum_price"] = max_price
    if has_photos:
        params["has_photos"] = "true"

    if page is not None:
        params["page"] = page

    query_str = urllib.parse.urlencode(params, doseq=True)
    return f"{BASE_URL_XE}?{query_str}"
