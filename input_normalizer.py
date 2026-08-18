"""
input_normalizer.py -- unchanged from the version already in this
conversation; recreated here only so the pipeline files that import from
it (output_row_builder.py, run_menu_creation.py) can be smoke-tested
together in this sandbox.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd


CANONICAL_ALIASES: Dict[str, List[str]] = {
    'name': ['item name', 'menu item', 'current menu item', 'menu_item_name', 'item', 'product name', 'dish name', 'dish', 'menu_item', 'name'],
    'category': ['category', 'current category', 'department', 'family_group_name', 'menu group', 'menu_group', 'food category', 'item category', 'course', 'type', 'dept', 'product_group_id'],
    'ingredients': ['ingredients', 'current ingredients', 'recipe', 'recipe_name', 'ingredient list'],
    'price': ['price', 'selling price', 'current price ($)', 'avg menu price ($)', 'menu_item_price', 'unit price', 'menu price', 'sale price', 'item price', 'price ($)'],
    'annual_qty': ['quantity sold', 'total qty sold', 'total qty sold (annual)', 'qty sold', 'units sold', 'store_qty_sold', 'qty', 'quantity', 'annual quantity', 'annual qty', 'units', 'volume'],
    'theoretical_cost': ['theoretical cost', 'theoretical cost ($)', 'cost', 'unit cost', 'cogs', 'food cost', 'item cost', 'theoretical_cost', 'cost of goods sold'],
    'ingredient_cost': ['ingredient cost', 'ingredient cost ($)', 'ingredient_cost'],
    'prep_cost': ['prep cost', 'prep cost ($)', 'prep_cost', 'labor cost'],
    'zcta': ['zcta', 'zip', 'zip code', 'postal code', 'zipcode'],
}

REQUIRED_MINIMUM = ('name', 'price', 'annual_qty')

CATEGORY_FC_BENCHMARK = {
    'APPETIZER': 0.28, 'BREAKFAST': 0.28, 'BREAKFAST SIDE': 0.22, 'SOUP': 0.28,
    'SALAD': 0.28, 'SAND / WRAP': 0.30, 'SIDE / OTHER': 0.22,
    'ENTREE': 0.2715, 'BURGER': 0.1614,
}
DEFAULT_FC_BENCHMARK = 0.25

CATEGORY_KEYWORDS = [
    ('BURGER', ('burger',)), ('SALAD', ('salad',)),
    ('SOUP', ('soup', 'chowder', 'bisque', 'chili')),
    ('BREAKFAST', ('pancake', 'waffle', 'omelet', 'omelette', 'breakfast', 'biscuit', 'french toast')),
    ('SAND / WRAP', ('sandwich', 'wrap', 'sub', 'panini', 'burrito')),
    ('APPETIZER', ('wings', 'pretzel', 'nachos', 'dip', 'sticks', 'poppers')),
    ('SIDE / OTHER', ('fries', 'chips', 'side', 'slaw', 'guacamole')),
]
FALLBACK_CATEGORY = 'UNKNOWN'


def _normalize_header(h) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(h).strip().lower()).strip()


def _build_alias_lookup(aliases_dict=None) -> Dict[str, str]:
    aliases_dict = aliases_dict if aliases_dict is not None else CANONICAL_ALIASES
    lookup = {}
    for canonical, aliases in aliases_dict.items():
        lookup[_normalize_header(canonical)] = canonical
        for a in aliases:
            lookup[_normalize_header(a)] = canonical
    return lookup


_ALIAS_LOOKUP = _build_alias_lookup()


def detect_columns(headers) -> Dict[str, str]:
    found = {}
    for h in headers:
        if h is None:
            continue
        norm = _normalize_header(h)
        canonical = _ALIAS_LOOKUP.get(norm)
        if canonical and canonical not in found:
            found[canonical] = h
    return found


def missing_required_fields(column_map: Dict[str, str]) -> List[str]:
    return [f for f in REQUIRED_MINIMUM if f not in column_map]


def infer_category(item_name: str) -> str:
    name_l = str(item_name).lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in name_l for kw in keywords):
            return category
    return FALLBACK_CATEGORY


@dataclass
class NormalizationResult:
    rows: List[dict]
    warnings: List[str] = field(default_factory=list)
    column_map: Dict[str, str] = field(default_factory=dict)
    fields_generated: List[str] = field(default_factory=list)


def normalize_dataframe(df: pd.DataFrame) -> NormalizationResult:
    column_map = detect_columns(df.columns)
    missing = missing_required_fields(column_map)
    if missing:
        raise ValueError(f"Missing required data: {', '.join(missing)}.")
    warnings, fields_generated = [], []
    work = pd.DataFrame()
    work['name'] = df[column_map['name']].astype(str).str.strip()
    work['price'] = pd.to_numeric(df[column_map['price']], errors='coerce')
    work['annual_qty'] = pd.to_numeric(df[column_map['annual_qty']], errors='coerce')
    work = work.dropna(subset=['price', 'annual_qty', 'name'])
    work = work[work['name'].str.len() > 0]
    if 'category' in column_map:
        work['category'] = df.loc[work.index, column_map['category']].astype(str).str.strip()
    else:
        work['category'] = work['name'].apply(infer_category)
        fields_generated.append('category')
    if 'ingredients' in column_map:
        work['ingredients'] = df.loc[work.index, column_map['ingredients']].fillna('').astype(str).str.strip()
    else:
        work['ingredients'] = ''
        fields_generated.append('ingredients')
    if 'theoretical_cost' in column_map:
        work['theoretical_cost'] = pd.to_numeric(df.loc[work.index, column_map['theoretical_cost']], errors='coerce')
    else:
        work['theoretical_cost'] = work.apply(
            lambda r: r['price'] * CATEGORY_FC_BENCHMARK.get(r['category'], DEFAULT_FC_BENCHMARK), axis=1)
        fields_generated.append('theoretical_cost')
    rows = [{'Menu Item': r['name'], 'Category': r['category'], 'Ingredients': r['ingredients'],
              'Total Qty Sold (annual)': r['annual_qty'], 'Price': r['price'],
              'Theoretical Cost': r['theoretical_cost']} for _, r in work.iterrows()]
    return NormalizationResult(rows=rows, warnings=warnings, column_map=column_map, fields_generated=fields_generated)


def looks_like_minimal_schema(headers) -> bool:
    column_map = detect_columns(headers)
    return not missing_required_fields(column_map)


RESTAURANT_LIST_ALIASES: Dict[str, List[str]] = {
    'restaurant_name': ['name', 'restaurant', 'business_name', 'biz_name', 'restaurant name', 'restaurant_title', 'location_name'],
    'zip_code': ['zip', 'zipcode', 'zip_or_postal_code', 'postal_code', 'zcta', 'zip code', 'postcode', 'zip_code_5', 'postal code'],
    'cuisines': ['cuisine', 'cuisine_type', 'food_type', 'categories', 'category', 'tags', 'cuisine_tags', 'style_tags'],
    'style': ['concept', 'restaurant_style', 'format'],
    'restaurant_type': ['service_type', 'segment', 'type', 'restaurant_class'],
    'price_range': ['price_tier', 'price_level', '$_rating', 'cost_rating'],
    'rating_value': ['rating', 'star_rating', 'avg_rating', 'review_score'],
    'review_count': ['num_reviews', 'reviews', 'review_total'],
    'city': ['town', 'municipality'],
    'state': ['state_or_province', 'st', 'province', 'state code'],
    'latitude': ['lat'],
    'longitude': ['lng', 'lon', 'long'],
}

REQUIRED_MINIMUM_RESTAURANT = ('restaurant_name', 'zip_code', 'cuisines')
_RESTAURANT_ALIAS_LOOKUP = _build_alias_lookup(RESTAURANT_LIST_ALIASES)


def detect_restaurant_columns(headers) -> Dict[str, str]:
    found = {}
    for h in headers:
        if h is None:
            continue
        norm = _normalize_header(h)
        canonical = _RESTAURANT_ALIAS_LOOKUP.get(norm)
        if canonical and canonical not in found:
            found[canonical] = h
    return found


@dataclass
class RestaurantNormalizationResult:
    df: pd.DataFrame
    warnings: List[str] = field(default_factory=list)
    column_map: Dict[str, str] = field(default_factory=dict)
    unmapped_columns: List[str] = field(default_factory=list)


def normalize_restaurant_dataframe(df: pd.DataFrame) -> RestaurantNormalizationResult:
    column_map = detect_restaurant_columns(df.columns)
    missing = [f for f in REQUIRED_MINIMUM_RESTAURANT if f not in column_map]
    if missing:
        raise ValueError(f"Missing required restaurant-list fields: {', '.join(missing)}.")
    rename_map = {orig: canon for canon, orig in column_map.items()}
    out = df.rename(columns=rename_map)
    unmapped = [c for c in out.columns if c not in RESTAURANT_LIST_ALIASES and c not in rename_map.values()]
    warnings = []
    out['zip_code'] = out['zip_code'].astype(str).str.extract(r'(\d{5})')[0]
    bad_zip = out['zip_code'].isna().sum()
    if bad_zip:
        warnings.append(f"{bad_zip} row(s) had an unparseable ZIP/ZCTA and were dropped.")
        out = out.dropna(subset=['zip_code'])
    return RestaurantNormalizationResult(df=out, warnings=warnings, column_map=column_map, unmapped_columns=unmapped)


COMPARABLE_RESTAURANT_ALIASES: Dict[str, List[str]] = {
    'restaurant_name': [
        'restaurant name', 'restaurant', 'name', 'location name', 'chain name', 'store name', 'business name',
        # MICROS-style raw POS export headers (same schema Menu Refresh's
        # sales_export path already recognizes) -- one row per menu item
        # per restaurant, BUSINESS_UNIT_NAME identifies which restaurant.
        'business unit name', 'business_unit_name',
    ],
    'price': [
        'price', 'avg price', 'average price', 'avg menu price', 'menu price', 'selling price', 'current price ($)', 'price ($)', 'unit price',
        'menu item price', 'menu_item_price',
    ],
    'annual_qty': [
        'quantity sold', 'qty sold', 'total qty sold', 'total quantity sold', 'annual qty', 'annual quantity sold', 'units sold', 'units',
        'store qty sold', 'store_qty_sold',
    ],
    # Item-level fields -- optional, only present when the comparable file
    # is a MICROS-style item-level export rather than a pre-aggregated
    # one-row-per-restaurant file. Used for item-to-item comparable
    # matching (see run_menu_creation.find_best_comparable_item), not for
    # the restaurant-level price/qty averaging path.
    'item_name': ['menu item name', 'menu_item_name', 'item name', 'item_name', 'recipe name', 'recipe_name'],
    'item_category': [
        'family group name', 'family_group_name', 'menu group 1', 'menu_group_1',
        'category', 'item category',
    ],
}
_COMPARABLE_ALIAS_LOOKUP = _build_alias_lookup(COMPARABLE_RESTAURANT_ALIASES)


def detect_comparable_restaurant_columns(headers) -> Dict[str, str]:
    found = {}
    for h in headers:
        if h is None:
            continue
        norm = _normalize_header(h)
        canonical = _COMPARABLE_ALIAS_LOOKUP.get(norm)
        if canonical and canonical not in found:
            found[canonical] = h
    return found


@dataclass
class ComparableRestaurantNormalizationResult:
    df: pd.DataFrame
    warnings: List[str] = field(default_factory=list)
    column_map: Dict[str, str] = field(default_factory=dict)


def normalize_comparable_restaurant_dataframe(df: pd.DataFrame) -> ComparableRestaurantNormalizationResult:
    column_map = detect_comparable_restaurant_columns(df.columns)
    if 'restaurant_name' not in column_map:
        raise ValueError("Missing required comparable-data field: restaurant_name.")
    warnings = []
    out = pd.DataFrame()
    out['restaurant_name'] = df[column_map['restaurant_name']].fillna('').astype(str).str.strip()
    if 'price' in column_map:
        out['price'] = pd.to_numeric(df[column_map['price']], errors='coerce')
    else:
        out['price'] = pd.NA
        warnings.append("No comparable price column found — comparable price values will be blank.")
    if 'annual_qty' in column_map:
        out['annual_qty'] = pd.to_numeric(df[column_map['annual_qty']], errors='coerce')
    else:
        out['annual_qty'] = pd.NA
        warnings.append("No comparable quantity-sold column found — comparable quantity values will be blank.")

    # Item-level fields (optional) -- present when this is a MICROS-style
    # per-item export. Used for item-to-item comparable matching, kept
    # separate from the restaurant-level price/qty aggregation above.
    if 'item_name' in column_map:
        out['item_name'] = df[column_map['item_name']].fillna('').astype(str).str.strip()
    else:
        out['item_name'] = ''
    if 'item_category' in column_map:
        out['item_category'] = df[column_map['item_category']].fillna('').astype(str).str.strip()
    else:
        out['item_category'] = ''

    out = out[out['restaurant_name'].str.len() > 0].reset_index(drop=True)
    if out.empty:
        raise ValueError("Comparable data file does not contain any usable restaurant names.")

    # MICROS-style raw sales exports have one row per menu item per
    # restaurant -- multiple rows will share the same restaurant_name, and
    # the caller (run_menu_creation._build_comparable_metrics_lookup) sums
    # their quantity-sold values into one per-restaurant total. That sum is
    # only a correct ANNUAL figure if this file's quantity column actually
    # covers a full year -- flagged here so that assumption isn't silent,
    # since revenue_forecast.compute_base_units_monthly divides it by 12.
    matched_via_business_unit = column_map.get('restaurant_name', '').strip().lower().replace('_', ' ') in (
        'business unit name',
    )
    n_restaurants = out['restaurant_name'].nunique()
    if matched_via_business_unit or (len(out) > n_restaurants and n_restaurants > 0):
        warnings.append(
            f"This comparable file has multiple rows per restaurant ({len(out)} rows across "
            f"{n_restaurants} restaurant(s)) -- quantities are being summed per restaurant and "
            f"treated as an ANNUAL total (Base Units divides this by 12). If this export actually "
            f"covers a different period (e.g. one month, year-to-date), the resulting monthly Base "
            f"Units will be off by that same factor -- verify the source export's time period."
        )
    if out['item_name'].str.len().gt(0).any():
        warnings.append(
            f"This comparable file includes item-level detail ({(out['item_name'].str.len() > 0).sum()} "
            f"named menu items) -- used for item-to-item comparable matching (best-match benchmark per "
            f"generated item), in addition to the restaurant-level price/qty averaging above."
        )

    return ComparableRestaurantNormalizationResult(df=out, warnings=warnings, column_map=column_map)