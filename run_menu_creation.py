import re

import pandas as pd

import market_data as md
import input_normalizer as inorm
from creation_engine import build_zcta_context, category_landscape, select_comparables, crowd_mix_split
from dish_library import select_dish, PRICE_BASELINE_BY_CATEGORY, TIER_PRICE_MULTIPLIER, DEFAULT_PRICE_BASELINE
from output_row_builder import build_output_row
import revenue_forecast as rf


class MenuCreationError(Exception):
    pass


def _noop(message, fraction=None):
    pass


def _norm_text(value) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value).strip().lower()).strip()


OUTPUT_COLUMNS = [
    "Recommended New Menu Item", "Recommended Category", "Recommended Ingredients",
    "Recommended Description", "Recommended Price Band", "Recommended Menu Price ($)",
    "Cuisine", "Location", "Restaurant Type / Service Model", "ZIP Code",
    "Median Income ($)", "Median Age", "Household Size", "Resident Labor Force Note",
    "Unemployment Rate (%)", "Total Population", "Local Restaurant Density",
    "Zip-Wide Market Positioning", "Comparable Restaurant 1", "Comparable Restaurant 2",
    "Comparable Restaurant 1 Price ($)", "Comparable Restaurant 1 Qty Sold (Annual)",
    "Comparable Restaurant 2 Price ($)", "Comparable Restaurant 2 Qty Sold (Annual)",
    "Comparable Restaurant Match Criteria", "Best Comparable Item (Reference Sales File)",
    "# Comparable Restaurants Found",
    "Comparable Cuisine Match", "Comparable Demographic Match", "Comparable Price Tier Match",
    "Comparable Category Presence (Qualitative)", "Comparable Restaurant Evidence Strength (Qualitative)",
    "Comparable Menu Evidence", "Demographic Fit Reason", "Local Trend Fit Reason",
    "Customer Sentiment Fit Reason", "Recommended Health / Nutrition Fit", "Delivery Suitability",
    "Recommended Family / Value Fit", "Recommended Premium / Trendy Fit",
    "Recommended Premium-Adjacent Fit", "Ingredient Cost ($)", "Prep Cost ($)",
    "Theoretical Cost ($)", "Profitability Value ($)", "Profitability Value (%)", "Cost %",
    "3-Month Profitability Value ($)", "6-Month Profitability Value ($)",
    "9-Month Profitability Value ($)", "Recommended Portions", "Menu Item Confidence Score (1-5)",
    "Confidence Score Explanation", "Reason for Recommendation", "Residential vs. Business-Area Note",
    "Weekday Menu Role", "Weekend Menu Role",
    # Section 11-15: popularity/volume, demand chain, revenue forecast, occasion splits.
    "Comparable Sales Category (for Volume Index)", "Category Volume Index (real comparable-sales data)",
    "Category Demand Level", "Category Multiplier",
    "Popularity Score (Confidence x Category Volume Index)", "Popularity Rank (1 = most popular)",
    "Market Demand Composite Index (computed)", "Market Demand Level (computed)",
    "Demand Multiplier (ZIP-wide, from Market Demand Level)",
    "Base Units (Monthly) -- category/market demand methodology, no multipliers applied",
    "Base Units Source",
    "Base Projected Units (Monthly) = Base Units x Demand Multiplier x Category Multiplier",
    "Lunch Unit Share (%)", "Lunch Occasion Price ($)", "Lunch Estimated Units (Monthly)",
    "Lunch Estimated Revenue (Monthly $)",
    "Dinner Unit Share (%)", "Dinner Occasion Price ($)", "Dinner Estimated Units (Monthly)",
    "Dinner Estimated Revenue (Monthly $)",
    "Weekday Unit Share (%)", "Weekday Occasion Price ($)", "Weekday Estimated Units (Monthly)",
    "Weekday Estimated Revenue (Monthly $)",
    "Weekend Unit Share (%)", "Weekend Occasion Price ($)", "Weekend Estimated Units (Monthly)",
    "Weekend Estimated Revenue (Monthly $)",
    "6-Month Units (Months 1-6)", "Estimated 6-Month Revenue ($)",
    "Next 6-Month Units (Months 7-12)", "Estimated Next 6-Month Revenue ($)",
    "Next 9-Month Units (Months 13-21)", "Estimated Next 9-Month Revenue ($)",
]

# Tier label used throughout this pipeline and in every report-facing
# column. Matches the reference workbook's own tier string ("Value"), NOT
# "Family" -- an earlier pass of this codebase used "Family" here, which
# silently mismatched every "Value"-labeled column/row in the actual
# reference output (Recommended Price Band, Recommended Value-Tier Fit,
# Item Occasion Role, etc.). ctx.family_pct (creation_engine.py) keeps its
# name since it's just a Python attribute, not report-facing text.
TIER_LABELS = ("Value", "Premium", "Premium Edge")


def tier_item_counts(total_items: int, family_pct: float, premium_pct: float, premium_edge_pct: float) -> dict:
    raw = {"Value": total_items * family_pct / 100, "Premium": total_items * premium_pct / 100,
           "Premium Edge": total_items * premium_edge_pct / 100}
    counts = {k: max(0, round(v)) for k, v in raw.items()}
    counts["Premium Edge"] = min(counts["Premium Edge"], 1)
    diff = total_items - sum(counts.values())
    counts["Value"] += diff
    return counts


def detect_zctas(restaurant_df: pd.DataFrame) -> pd.Series:
    return restaurant_df["zip_code"].value_counts()


def _build_comparable_metrics_lookup(comparable_df: pd.DataFrame):
    try:
        norm = inorm.normalize_comparable_restaurant_dataframe(comparable_df)
    except ValueError as e:
        raise MenuCreationError(str(e))

    lookup = {}
    for _, row in norm.df.iterrows():
        name = str(row["restaurant_name"]).strip()
        key = _norm_text(name)
        if not key:
            continue

        entry = lookup.setdefault(key, {
            "restaurant_name": name,
            "price_values": [],
            "qty_values": [],
        })
        if not entry["restaurant_name"]:
            entry["restaurant_name"] = name

        price = row.get("price")
        if pd.notna(price):
            entry["price_values"].append(float(price))

        qty = row.get("annual_qty")
        if pd.notna(qty):
            entry["qty_values"].append(float(qty))

    metrics_lookup = {}
    has_any_metric = False
    for key, entry in lookup.items():
        price = round(sum(entry["price_values"]) / len(entry["price_values"]), 2) if entry["price_values"] else None
        qty = round(sum(entry["qty_values"]), 2) if entry["qty_values"] else None
        if price is not None or qty is not None:
            has_any_metric = True
        metrics_lookup[key] = {
            "restaurant_name": entry["restaurant_name"],
            "price": price,
            "annual_qty": qty,
        }

    return metrics_lookup, norm.warnings, has_any_metric, norm.df


def run(restaurant_df: pd.DataFrame, zcta: str, city: str, county: str,
        area_sq_mi: float, biz_residential_mix: str, has_anchor: bool, anchor_note: str,
        n_items: int = 7, state_abbr: str = None, progress_callback=None,
        comparable_metrics_df: pd.DataFrame = None,
        already_normalized: bool = True,
        commuter_flow_override: dict = None):
    """
    restaurant_df: uploaded restaurant list.
    commuter_flow_override: optional -- a dict with the same keys
                           market_data.fetch_commuter_flows() returns
                           (daytime_workers, worker_inflow, resident_outflow,
                           stay_local, pct_income_high, pct_income_low,
                           pct_age_mid, pct_age_senior). When supplied, the
                           live LODES fetch is skipped entirely -- the same
                           manual-entry escape hatch Menu Refresh already
                           has, since Menu Creation had none until now and
                           a hung/slow LODES fetch had no way around it.
    comparable_metrics_df: optional file with restaurant_name + price +
                           quantity-sold columns for the named comparables.
                           This is also the PRIMARY source for each item's
                           Base Units (Monthly) revenue-forecast input (see
                           revenue_forecast.compute_base_units_monthly) --
                           real sales data for a named comparable restaurant
                           outranks any modeled estimate.
    Returns (result_df, issues).
    """
    progress = progress_callback or _noop
    zcta = str(zcta).zfill(5)
    issues = []

    if not already_normalized:
        try:
            norm = inorm.normalize_restaurant_dataframe(restaurant_df)
        except ValueError as e:
            raise MenuCreationError(str(e))
        restaurant_df = norm.df

    zcta_restaurants = restaurant_df[restaurant_df["zip_code"].astype(str).str.zfill(5) == zcta].reset_index(drop=True)
    if zcta_restaurants.empty:
        raise MenuCreationError(f"No restaurants found for ZCTA {zcta} in the uploaded restaurant file.")

    comparable_metrics_lookup = {}
    comparable_metrics_warning_seen = False
    comparable_items_df = None
    if comparable_metrics_df is not None:
        progress("Normalizing comparable-restaurant metrics file…", 0.20)
        comparable_metrics_lookup, comparable_warnings, has_any_metric, comparable_items_df = (
            _build_comparable_metrics_lookup(comparable_metrics_df))
        issues.extend(comparable_warnings)
        if not has_any_metric:
            raise MenuCreationError(
                "The comparable-data file has restaurant names, but no usable price or quantity-sold values. "
                "Upload a file with at least one of those columns populated."
            )
        comparable_metrics_warning_seen = True

    state_abbr = state_abbr or md.zcta_to_state(zcta)
    if not state_abbr:
        raise MenuCreationError(f"Couldn't determine a state for ZCTA {zcta} -- pass state_abbr explicitly.")

    progress(f"Fetching Census population for ZCTA {zcta}…", 0.15)
    try:
        pop = md.fetch_census_demographics(zcta)
    except md.MarketDataError as e:
        raise MenuCreationError(f"Population fetch failed: {e}")

    progress(f"Fetching Census economic profile for ZCTA {zcta}…", 0.30)
    try:
        economic_profile = md.fetch_economic_profile(zcta)
    except md.MarketDataError as e:
        raise MenuCreationError(f"Economic profile fetch failed: {e}")
    economic_profile["total_population"] = pop["residents"]

    progress(f"Fetching commuter flow data (LODES) for ZCTA {zcta}…", 0.45)
    if commuter_flow_override is not None:
        progress("Using manually-entered commuter flow data (auto-fetch skipped).", 0.45)
        commuter_flow = commuter_flow_override
    else:
        try:
            commuter_flow = md.fetch_commuter_flows(zcta, state_abbr, progress_callback=progress)
        except md.MarketDataError as e:
            raise MenuCreationError(
                f"Commuter flow fetch failed: {e} If this keeps happening for this ZCTA/state, use "
                f"manual commuter-flow entry instead of waiting on the auto-fetch."
            )

    # Income-by-ethnicity (ACS S1903) and race/ethnicity population
    # composition (ACS B02001/B03003) -- market-context data only, never
    # used to drive item selection (see creation_engine / dish_library:
    # category/tier/price selection never reads these). Failures here are
    # non-fatal to the whole run -- these are supplementary report sections,
    # not inputs the recommendation engine depends on -- but are surfaced
    # as issues so the report can render "Data Not Available" honestly
    # rather than silently omitting the section.
    progress(f"Fetching income-by-ethnicity data (ACS S1903) for ZCTA {zcta}…", 0.48)
    try:
        income_by_ethnicity = md.fetch_income_by_ethnicity(zcta)
    except md.MarketDataError as e:
        issues.append(f"Income-by-ethnicity data unavailable: {e}")
        income_by_ethnicity = None

    progress(f"Fetching race/ethnicity composition (ACS B02001/B03003) for ZCTA {zcta}…", 0.50)
    try:
        ethnicity_composition = md.fetch_ethnicity_composition(zcta)
    except md.MarketDataError as e:
        issues.append(f"Ethnicity composition data unavailable: {e}")
        ethnicity_composition = None

    progress(f"Fetching population growth rate for ZCTA {zcta}…", 0.52)
    pop_growth = md.fetch_population_growth_rate(zcta)
    if pop_growth["rate"] is None:
        issues.append(f"Population growth rate unavailable: {pop_growth['source']}")

    ctx = build_zcta_context(
        zcta=zcta, city=city, state=state_abbr, county=county,
        economic_profile=economic_profile, commuter_flow=commuter_flow,
        restaurant_count=len(zcta_restaurants), area_sq_mi=area_sq_mi,
        biz_residential_mix=biz_residential_mix, has_anchor=has_anchor, anchor_note=anchor_note,
        population_growth_rate=pop_growth["rate"],
    )

    progress("Analyzing local restaurant category landscape…", 0.60)
    landscape = category_landscape(zcta_restaurants)
    top_categories = landscape[landscape["count"] > 0].head(3)["category"].tolist() or ["Fast Food"]

    counts = tier_item_counts(n_items, ctx.family_pct, ctx.premium_pct, ctx.premium_edge_pct)
    tier_plan = []
    for tier in TIER_LABELS:
        tier_plan.extend([tier] * counts.get(tier, 0))
    while len(tier_plan) < n_items:
        tier_plan.append("Value")
    tier_plan = tier_plan[:n_items]

    comparable_usage, rows_out, used_display_names = {}, [], set()
    category_template_index = {}  # rotates dish_library templates per category
    matched_comparable_metric = False
    pop_scores = []

    for i, tier in enumerate(tier_plan):
        progress(f"Building item {i+1} of {n_items} ({tier} tier)…", 0.65 + 0.3 * (i / max(n_items, 1)))
        category = top_categories[i % len(top_categories)]
        category_rank = int(landscape[landscape["category"] == category].index[0]) + 1

        comparables, total_found = select_comparables(
            zcta_restaurants, category, already_used=comparable_usage, total_items_in_batch=n_items,
        )
        for c in comparables:
            comparable_usage[c] = comparable_usage.get(c, 0) + 1

        comparable_details = []
        for c in comparables:
            metrics = comparable_metrics_lookup.get(_norm_text(c)) if comparable_metrics_lookup else None
            if metrics is not None and (metrics.get("price") is not None or metrics.get("annual_qty") is not None):
                matched_comparable_metric = True
            comparable_details.append({
                "name": c,
                "price": metrics["price"] if metrics else None,
                "annual_qty": metrics["annual_qty"] if metrics else None,
            })

        tier_pct_map = {"Value": ctx.family_pct, "Premium": ctx.premium_pct, "Premium Edge": ctx.premium_edge_pct}
        mix = crowd_mix_split(tier_pct_map[tier], ctx)

        t_idx = category_template_index.get(category, 0)
        category_template_index[category] = t_idx + 1

        # Item-to-item comparable match, computed BEFORE dish selection so
        # it can actually influence which template/ingredients get chosen
        # -- not just explain a pre-picked dish afterward. Uses the same
        # price-estimation formula dish_library.select_dish() uses
        # internally (category baseline x tier multiplier), since the
        # final dish object doesn't exist yet at this point.
        estimated_price = (PRICE_BASELINE_BY_CATEGORY.get(category, DEFAULT_PRICE_BASELINE)
                            * TIER_PRICE_MULTIPLIER.get(tier, 1.0))
        best_item_match = rf.find_best_comparable_item(
            target_category=category, target_price=estimated_price,
            comparable_items_df=comparable_items_df,
        )

        dish = select_dish(
            category=category, tier=tier, template_index=t_idx,
            evidence_count=total_found, household_size=ctx.household_size,
            lunch_share=rf.LUNCH_SHARE_BY_TIER.get(tier, 0.55),
            best_comparable_item=best_item_match,
        )

        row = build_output_row(
            ctx=ctx, dish=dish, tier=tier, category_rank=category_rank,
            comparables=comparables, comparable_details=comparable_details,
            comparable_total_found=total_found,
            weekday_pct=mix["weekday_pct"], weekend_pct=mix["weekend_pct"],
            top_categories=top_categories, used_names=used_display_names,
            best_item_match=best_item_match, income_by_ethnicity=income_by_ethnicity,
        )
        used_display_names.add(row["Recommended New Menu Item"])

        forecast_row = rf.build_revenue_forecast_row(
            category=dish.category, tier=tier, price=dish.price,
            confidence_score=row["Menu Item Confidence Score (1-5)"],
            comparable_details=comparable_details, ctx=ctx,
            best_item_match=best_item_match,
        )
        row.update(forecast_row)
        pop_scores.append(forecast_row["Popularity Score (Confidence x Category Volume Index)"])

        rows_out.append(row)

    ranks = rf.popularity_ranks(pop_scores)
    for row, rank in zip(rows_out, ranks):
        row["Popularity Rank (1 = most popular)"] = rank

    if comparable_metrics_warning_seen and not matched_comparable_metric:
        issues.append(
            "The comparable-data file was loaded, but none of the selected comparable restaurant names matched "
            "usable price or quantity-sold rows. Comparable price/qty columns and Base Units (Monthly) fell back "
            "to the labeled reasoned-estimate model for every item."
        )

    result_df = pd.DataFrame(rows_out, columns=OUTPUT_COLUMNS)
    progress("Done.", 1.0)
    return result_df, issues, income_by_ethnicity, ethnicity_composition, ctx