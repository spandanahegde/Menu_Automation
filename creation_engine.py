"""
creation_engine.py
-------------------
Menu Creation's own logic ONLY -- everything that's genuinely specific to
turning a restaurant list + a ZCTA's economic/commuter profile into new
menu item recommendations. Fetching and column-mapping deliberately live
elsewhere (market_data.py, input_normalizer.py) and are NOT duplicated
here -- this module imports and consumes their outputs.

TIER LABEL: "Value" (not "Family") is used everywhere a tier string is
report-facing (Recommended Price Band, market_positioning_paragraph, etc).
ZctaContext.family_pct keeps its attribute name -- that's just a Python
identifier, not text shown to the person using the report.
"""

import math
from dataclasses import dataclass
from typing import Optional

from segment_calculator import calculate_customer_segments


@dataclass
class ZctaContext:
    zcta: str
    city: str
    state: str
    county: str
    median_income: float
    median_age: float
    household_size: float
    labor_force_participation_rate: float
    unemployment_rate: float
    total_population: int
    total_households: int
    restaurant_count: int
    area_sq_mi: float
    family_pct: float
    premium_pct: float
    premium_edge_pct: float
    market_label: str
    urban_rural_class: str
    biz_residential_mix: str
    has_anchor: bool
    anchor_note: str
    resident_employed_workers: float
    in_commuting_workers: float
    out_commute_rate_pct: float
    daytime_workers: float = 0.0
    stay_local: float = 0.0
    population_growth_rate: Optional[float] = None
    indulgent_pct: Optional[float] = None
    family_value_households: int = 0
    premium_households: int = 0
    premium_edge_households: int = 0
    customer_segments_available: bool = True
    segment_calculation_source: str = ""

    def restaurant_density_per_sqmi(self) -> float:
        return round(self.restaurant_count / self.area_sq_mi, 2) if self.area_sq_mi else 0.0

    def restaurant_density_per_1000(self) -> float:
        return round(1000.0 * self.restaurant_count / self.total_population, 2) if self.total_population else 0.0

    def location_str(self) -> str:
        return f"ZIP {self.zcta}, {self.city}, {self.state} ({self.county} County)"

    def weekday_lift_factor(self, base_pct: float, min_mult=0.90, max_mult=1.10) -> float:
        if self.resident_employed_workers == 0:
            ratio = 0.0
        else:
            ratio = self.in_commuting_workers / self.resident_employed_workers
        ratio = max(0.0, min(ratio, 1.0))
        mult = min_mult + (max_mult - min_mult) * ratio
        return round(base_pct * mult, 2)

    def market_positioning_paragraph(self, item_name: str, item_price: float, item_tier: str,
                                       weekday_tier_pct: float, weekend_tier_pct: float) -> str:
        tier_pct = {"Value": self.family_pct, "Premium": self.premium_pct,
                    "Premium Edge": self.premium_edge_pct}[item_tier]
        return (
            f"This ZIP code is classified a {self.market_label}. At ${item_price:.2f}, {item_name} sits in "
            f"the {item_tier} segment ({tier_pct}% of this ZIP's households by real income-bracket data). "
            f"This tier's real basis: households in the relevant income brackets make up {tier_pct}% of this "
            f"ZIP's households, per source-file income-bracket data (Value {self.family_pct}% / "
            f"Premium {self.premium_pct}% / Premium Edge {self.premium_edge_pct}%, from real ACS B19001 "
            f"income-bracket fields). By this ZIP's weekday/weekend crowd split, {item_tier} is an "
            f"estimated {weekday_tier_pct}% of weekday crowd and {weekend_tier_pct}% of weekend crowd -- "
            f"weekdays are lifted by in-commuting workers, weekends revert to the resident income mix."
        )


def classify_market(family_pct: float, premium_edge_pct: float) -> str:
    if family_pct >= 55:
        return "Family Value Market"
    if premium_edge_pct >= 20:
        return "Premium Market"
    return "Balanced Market"


def classify_urban_rural(pop_density_per_sqmi: float) -> str:
    if pop_density_per_sqmi >= 3000:
        return "URBAN"
    if pop_density_per_sqmi >= 800:
        return "SUBURBAN"
    return "RURAL"


def build_zcta_context(zcta: str, city: str, state: str, county: str,
                        economic_profile: dict, commuter_flow: dict,
                        restaurant_count: int, area_sq_mi: float,
                        biz_residential_mix: str, has_anchor: bool, anchor_note: str,
                        population_growth_rate: Optional[float] = None,
                        indulgent_pct: Optional[float] = None) -> ZctaContext:
    ep = economic_profile
    segments = calculate_customer_segments(ep)
    family_pct = segments["family_value_pct"] or 0.0
    premium_pct = segments["premium_pct"] or 0.0
    premium_edge_pct = segments["premium_edge_pct"] or 0.0

    total_population = ep.get("total_population")
    if total_population is None:
        total_population = 0

    pop_density = total_population / area_sq_mi if area_sq_mi else 0

    resident_employed_workers = commuter_flow["stay_local"] + commuter_flow["resident_outflow"]
    out_commute_rate = (
        round(100.0 * commuter_flow["resident_outflow"] / resident_employed_workers, 2)
        if resident_employed_workers else 0.0
    )

    return ZctaContext(
        zcta=zcta, city=city, state=state, county=county,
        median_income=ep["median_household_income"], median_age=ep["median_age"],
        household_size=ep["avg_household_size"],
        labor_force_participation_rate=ep["labor_force_participation_rate"],
        unemployment_rate=ep["unemployment_rate"],
        total_population=int(total_population),
        total_households=segments["total_households"] or int(ep.get("total_households", 0) or 0),
        restaurant_count=restaurant_count, area_sq_mi=area_sq_mi,
        family_pct=family_pct, premium_pct=premium_pct, premium_edge_pct=premium_edge_pct,
        market_label=classify_market(family_pct, premium_edge_pct),
        urban_rural_class=classify_urban_rural(pop_density),
        biz_residential_mix=biz_residential_mix, has_anchor=has_anchor, anchor_note=anchor_note,
        resident_employed_workers=resident_employed_workers,
        in_commuting_workers=commuter_flow["worker_inflow"],
        out_commute_rate_pct=out_commute_rate,
        daytime_workers=commuter_flow.get("daytime_workers", 0.0),
        stay_local=commuter_flow.get("stay_local", 0.0),
        population_growth_rate=population_growth_rate, indulgent_pct=indulgent_pct,
        family_value_households=segments["family_value_raw"],
        premium_households=segments["premium_raw"],
        premium_edge_households=segments["premium_edge_raw"],
        customer_segments_available=segments["available"],
        segment_calculation_source=segments["source"],
    )


def crowd_mix_split(tier_resident_pct: float, ctx: ZctaContext,
                     min_mult: float = 0.90, max_mult: float = 1.10) -> dict:
    weekend_pct = round(tier_resident_pct, 2)
    weekday_pct = ctx.weekday_lift_factor(tier_resident_pct, min_mult, max_mult)
    return {"weekday_pct": weekday_pct, "weekend_pct": weekend_pct}


CATEGORY_KEYWORDS = {
    "Wings": ["wings", "wing"], "Burgers": ["burger"], "Pizza": ["pizza", "pizzeria"],
    "Sandwiches": ["sandwich"], "BBQ": ["barbecue", "bbq", "barbeque"],
    "Seafood": ["seafood", "fish"], "Mexican": ["mexican", "taco", "burrito"],
    "Breakfast": ["breakfast", "brunch"], "Sushi/Asian": ["sushi", "japanese", "asian", "poke"],
    "Southern": ["southern"], "Italian": ["italian"], "Chicken": ["chicken", "fried chicken"],
    "Fast Food": ["fast food"], "Dessert": ["dessert", "ice cream", "bakery"],
}


def category_landscape(restaurant_df, cuisines_col: str = "cuisines"):
    import pandas as pd
    counts = {}
    text = restaurant_df[cuisines_col].fillna("").str.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        mask = text.apply(lambda t: any(kw in t for kw in kws))
        counts[cat] = int(mask.sum())
    out = pd.DataFrame({"category": list(counts.keys()), "count": list(counts.values())})
    return out.sort_values("count", ascending=False).reset_index(drop=True)


def select_comparables(restaurant_df, category: str, cuisines_col: str = "cuisines",
                        name_col: str = "restaurant_name", already_used: Optional[dict] = None,
                        max_reuse_share: float = 0.25, total_items_in_batch: int = 1):
    kws = CATEGORY_KEYWORDS.get(category, [category.lower()])
    text = restaurant_df[cuisines_col].fillna("").str.lower()
    mask = text.apply(lambda t: any(kw in t for kw in kws))
    matches = restaurant_df.loc[mask, name_col].dropna().unique().tolist()

    already_used = already_used or {}
    cap = math.ceil(max_reuse_share * total_items_in_batch)
    eligible = [m for m in matches if already_used.get(m, 0) < max(cap, 1)]
    chosen = eligible[:2] if eligible else matches[:2]
    return chosen, len(matches)


@dataclass
class ProfitabilityBlock:
    ingredient_cost: float
    prep_cost: float
    theoretical_cost: float
    menu_price: float
    profit_value: float
    profit_pct: float
    cost_pct: float
    profit_3mo: float
    profit_6mo: float
    profit_9mo: float


PERIOD_GROWTH_FACTOR = 1.9


def profitability_block(ingredient_cost: float, prep_cost: float, menu_price: float) -> ProfitabilityBlock:
    theoretical_cost = round(ingredient_cost + prep_cost, 2)
    profit_value = round(menu_price - theoretical_cost, 2)
    profit_pct = round(100.0 * profit_value / menu_price, 1) if menu_price else 0.0
    cost_pct = round(100.0 - profit_pct, 1)

    def projected(n: int) -> float:
        return round(profit_value * (PERIOD_GROWTH_FACTOR ** n), 2)

    return ProfitabilityBlock(
        ingredient_cost=ingredient_cost, prep_cost=prep_cost, theoretical_cost=theoretical_cost,
        menu_price=menu_price, profit_value=profit_value, profit_pct=profit_pct, cost_pct=cost_pct,
        profit_3mo=projected(1), profit_6mo=projected(2), profit_9mo=projected(3),
    )


def build_invention_prompt(ctx: ZctaContext, category: str, category_rank: int,
                            comparables: list, comparable_total_found: int,
                            price_tier: str, weekday_pct: float, weekend_pct: float,
                            existing_item_names_in_batch: list) -> str:
    return f"""Generate ONE new menu item recommendation. Follow this exact pipeline, in order:

FIXED CONTEXT (already computed -- do not alter or re-derive any number below):
- Location: {ctx.location_str()}
- Category to use: "{category}" (rank #{category_rank} by count in the local restaurant list)
- Price tier: {price_tier} (Value {ctx.family_pct}% / Premium {ctx.premium_pct}% / Premium Edge {ctx.premium_edge_pct}% of ZIP households)
- Weekday crowd share for this tier: {weekday_pct}%
- Weekend crowd share for this tier: {weekend_pct}%
- Real comparable restaurants in this category, this ZIP: {comparables if comparables else "NONE FOUND -- state this explicitly, do not invent one"}
- Total same-category restaurants found in ZIP: {comparable_total_found}
- Median household income: ${ctx.median_income:,.0f} | Unemployment: {ctx.unemployment_rate}%
- Avg household size: {ctx.household_size}
- Market label: {ctx.market_label}
- Urban/rural class: {ctx.urban_rural_class} | Biz/residential mix: {ctx.biz_residential_mix}
- Anchor present: {ctx.anchor_note if ctx.has_anchor else "None"}
- Indulgent-lean food profile: {f"{ctx.indulgent_pct}% (no formal review/rating data exists for this ZIP -- this is a judgment-call proxy)" if ctx.indulgent_pct is not None else "No data exists -- state this plainly"}
- Item names already used in this batch (new item must not duplicate): {existing_item_names_in_batch}

GENERATION STEPS (execute in order):
1. Pick 4-6 named, real, kitchen-standard, costable ingredients consistent with "{category}" and the {price_tier} price tier. No vague terms.
2. Derive the item name FROM those ingredients (format: [flavor/style descriptor present in your ingredient list] + [recognizable base item for the category]). Must not duplicate or closely mimic {comparables}.
3. Write a one-sentence description: [cooking method] + [named ingredients] + [flavor finish]. No unearned adjectives.
4. Write the Cuisine/rationale field: 2-3 sentences stating (a) what it is + category, (b) the category evidence path (rank #{category_rank} by count, OR explicit white-space justification if comparables list is empty), (c) that price tier came from the {price_tier} income-bracket %, not restaurant-count popularity.

Return ONLY this JSON object, no other text:
{{
  "item_name": "...", "category": "{category}", "ingredients": "comma-separated list",
  "description": "...", "price_band": "{price_tier}", "menu_price": 0.00,
  "cuisine_rationale": "...", "ingredient_cost_estimate": 0.00, "prep_cost_estimate": 0.00,
  "cost_basis_note": "one sentence on how the cost estimate was built",
  "demographic_fit_reason": "...", "customer_sentiment_fit_reason": "...",
  "health_nutrition_fit": "...", "delivery_suitability": "Low|Medium|High",
  "family_value_fit": "Weak|Moderate|Strong", "premium_trendy_fit": "Low|Moderate|Strong",
  "premium_adjacent_upgrade_path": "named ingredient swap + resulting tier",
  "portion_note": "...", "confidence_score": 1, "confidence_explanation": "...",
  "reason_for_recommendation": "Who it's for: ... What it does: ...",
  "weekday_menu_role": "...", "weekend_menu_role": "..."
}}"""


def validate_batch(rows: list, comparable_usage: dict, total_items: int) -> list:
    issues = []
    names_seen = set()
    premium_edge_count = 0
    for r in rows:
        if r["item_name"] in names_seen:
            issues.append(f"Duplicate item name: {r['item_name']}")
        names_seen.add(r["item_name"])
        if r.get("price_band") == "Premium Edge":
            premium_edge_count += 1

    if premium_edge_count > max(1, round(total_items / 6)):
        issues.append(f"Premium Edge tier used {premium_edge_count}x -- cap is ~1 per 6-8 items.")

    for name, count in comparable_usage.items():
        share = count / total_items if total_items else 0
        if share > 0.25:
            issues.append(f"Comparable restaurant '{name}' used in {share:.0%} of items -- exceeds 25% diversity cap.")
    return issues
