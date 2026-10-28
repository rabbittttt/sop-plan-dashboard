"""Shared presentation order; catalog identities and stored values are unchanged."""

BRAND_ORDER = ("智界", "享界", "尊界", "尚界", "问界")
_BRAND_RANK = {brand: rank for rank, brand in enumerate(BRAND_ORDER)}


def brand_order_key(brand):
    return _BRAND_RANK.get(brand, len(BRAND_ORDER)), brand
