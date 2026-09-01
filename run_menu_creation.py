import re

import pandas as pd

import market_data as md
import input_normalizer as inorm
from creation_engine import build_zcta_context, category_landscape, select_comparables, crowd_mix_split
from dish_library import select_dish, PRICE_BASELINE_BY_CATEGORY, TIER_PRICE_MULTIPLIER, DEFAULT_PRICE_BASELINE
from output_row_builder import build_output_row
import revenue_forecast as rf
import cuisine_affinity as caff

class MenuCreationError(Exception):
    pass


def _noop(message, fraction=None):
    pass


# ---------------------------------------------------------------------------
# Cross-item / cross-ZCTA concept registry. Menu items must not repeat the
# same underlying concept (protein+prep, see dish_library.is_duplicate_concept)
# within a single ZCTA's batch, AND -- per the brief's requirement that the
# "same menu concept is not repeatedly generated across ZCTAs" -- across
# every ZCTA processed in the same app session. This is intentionally
# process-local (not persisted to disk): a fresh app/process restart starts
# a clean registry, matching how the rest of this pipeline treats session
# state (comparable_usage, used_display_names) elsewhere in this file.
# Call reset_concept_registry() to explicitly start a new session (e.g. a
# "start over" action in the UI).
# ---------------------------------------------------------------------------
_SESSION_CONCEPT_KEYS = set()


def reset_concept_registry():
    _SESSION_CONCEPT_KEYS.clear()


def _norm_text(value) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value).strip().lower()).strip()


OUTPUT_COLUMNS = [
    "Recommended New Menu Item", "Recommended Category", "Recommended Ingredients",
    "Recommended Description", "Recommended Price Band", "Recommended Menu Price ($)",
    "Cuisine", "Primary Cuisine (ZCTA-wide)", "Secondary Cuisine (ZCTA-wide)",
    "Cuisine Affinity Score (this item's category)", "Why This Category Fits This ZCTA",
    "Uniqueness Validation Result",
    "Location", "Restaurant Type / Service Model", "ZIP Code",
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
    "Lunch Unit Share (%)", "Lunch Occasion Price ($)", "Lunch Price Multiplier", "Lunch Estimated Units (Monthly)",
    "Lunch Estimated Revenue (Monthly $)",
    "Dinner Unit Share (%)", "Dinner Occasion Price ($)", "Dinner Price Multiplier", "Dinner Estimated Units (Monthly)",
    "Dinner Estimated Revenue (Monthly $)",
    "Weekday Unit Share (%)", "Weekday Occasion Price ($)", "Weekday Price Multiplier", "Weekday Estimated Units (Monthly)",
    "Weekday Estimated Revenue (Monthly $)",
    "Weekend Unit Share (%)", "Weekend Occasion Price ($)", "Weekend Price Multiplier", "Weekend Estimated Units (Monthly)",
    "Weekend Estimated Revenue (Monthly $)",
    "Estimated Steady-State Monthly Revenue ($)",
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
            # NON-FATAL. Commuter flow only feeds the weekday/weekend
            # lift factor, the Flow section, and the Market Demand
            # Composite -- it is NOT needed for Customer Segmentation
            # (built entirely from economic_profile's income brackets,
            # already fetched successfully above) or for generating menu
            # items themselves. Aborting the whole run over a slow/hung
            # LODES connection was blocking segmentation and every other
            # section that doesn't actually depend on commuter data --
            # confirmed as a real, repeated failure mode (Tennessee's OD
            # file in particular has been consistently too slow to
            # finish within the timeout). Falls back to a neutral,
            # clearly-labeled zero commuter profile instead: weekday/
            # weekend lift becomes flat (no commuter-driven skew), Flow
            # section shows honestly as unavailable, everything else
            # proceeds on real data.
            issues.append(
                f"Commuter flow (LODES) data unavailable for ZCTA {zcta}: {e} Falling back to a neutral "
                f"commuter profile -- weekday/weekend crowd-mix lift and the Flow section will not "
                f"reflect real commuter patterns for this ZCTA, but Customer Segmentation, pricing, and "
                f"menu-item generation are unaffected (they don't depend on commuter data). Use manual "
                f"commuter-flow entry if you need real weekday/weekend lift for this ZCTA."
            )
            commuter_flow = {
                "daytime_workers": 0.0, "worker_inflow": 0.0, "resident_outflow": 0.0,
                "stay_local": 0.0, "pct_income_high": 0.0, "pct_income_low": 0.0,
                "pct_age_mid": 0.0, "pct_age_senior": 0.0, "pct_office_jobs": 0.0,
                "source": "Unavailable (LODES fetch failed) -- neutral zero fallback",
            }

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
    if not ctx.segmentation_available:
        issues.append(
            f"Customer segmentation (Value/Premium/Premium Edge) is unavailable for ZCTA {zcta}: Census "
            f"published a real Total Households figure ({ctx.total_households:,}) but the detailed income-"
            f"bracket breakdown (ACS Table B19001) came back empty for this geography -- a real suppression "
            f"pattern, not missing code. A neutral even-ish split is used internally to still generate menu "
            f"items and price tiers, but the report shows this section as unavailable rather than a "
            f"fabricated percentage."
        )

    progress("Analyzing local restaurant category landscape…", 0.60)
    landscape = category_landscape(zcta_restaurants)

    # CUISINE AFFINITY -- runs BEFORE any dish/category is picked. Answers
    # "what food is most appropriate for THIS ZCTA based on its actual
    # data?" first; category/dish selection below only operates inside
    # this ranking. Replaces the old "top 3 categories by competitor
    # count" selector, which is why every ZCTA used to converge on
    # whichever category (usually Burgers) had the most local competitors
    # regardless of who actually lives there.
    progress(f"Calculating cuisine affinity for ZCTA {zcta} (demographics + income + competition + trend)…", 0.62)
    cuisine_result = caff.compute_cuisine_affinity(
        zcta=zcta, family_pct=ctx.family_pct, premium_pct=ctx.premium_pct,
        premium_edge_pct=ctx.premium_edge_pct, median_age=ctx.median_age,
        population_growth_rate=ctx.population_growth_rate,
        landscape_df=landscape, total_restaurants=len(zcta_restaurants),
        ethnicity_composition=ethnicity_composition,
        income_by_ethnicity=income_by_ethnicity,
    )
    ctx.cuisine_affinity = cuisine_result  # attached for the report layer; doesn't change ctx's dataclass shape
    top_categories = cuisine_result.category_priority_list()[:3] or ["Fast Food"]
    avoided_categories = cuisine_result.avoided_categories()
    category_to_cuisine = cuisine_result.category_to_cuisine()
    category_priority_full = cuisine_result.category_priority_list() or ["Fast Food"]

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

        # Pick the next category from the cuisine-affinity ranking (not
        # raw competitor-count rotation). Skips categories flagged "avoid"
        # (weak evidence across every signal) and skips any category whose
        # concepts are already exhausted this session, moving to the next
        # affinity-ranked category instead of forcing a renamed repeat --
        # this is the concept-level UNIQUENESS VALIDATION step from the
        # brief, applied before generation rather than after.
        from dish_library import has_available_concept
        n_cats = len(category_priority_full)
        category = None
        avoided_but_used_note = ""
        for k in range(n_cats):
            candidate = category_priority_full[(i + k) % n_cats]
            if candidate in avoided_categories:
                continue
            if has_available_concept(candidate, _SESSION_CONCEPT_KEYS):
                category = candidate
                break
        if category is None:
            # Every non-avoided category is concept-exhausted this session
            # -- relax the avoid-list rather than the uniqueness rule
            # (better to use a weak-evidence category once than repeat a
            # concept that's already on the menu).
            for k in range(n_cats):
                candidate = category_priority_full[(i + k) % n_cats]
                if has_available_concept(candidate, _SESSION_CONCEPT_KEYS):
                    category = candidate
                    avoided_but_used_note = (
                        " Every stronger-evidence category was concept-exhausted this session, so a "
                        "weaker-evidence category was used rather than repeat an existing concept."
                    )
                    break
        if category is None:
            category = category_priority_full[i % n_cats]  # fully exhausted; last resort

        category_rank = int(landscape[landscape["category"] == category].index[0]) + 1
        item_cuisine = category_to_cuisine.get(category)
        cuisine_score = next((s for s in cuisine_result.ranked if s.cuisine == item_cuisine), None)

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
            used_concept_keys=_SESSION_CONCEPT_KEYS,
        )
        was_duplicate_rejected = dish.concept_key in _SESSION_CONCEPT_KEYS
        _SESSION_CONCEPT_KEYS.add(dish.concept_key)

        uniqueness_result = (
            "PASS -- new concept" if not was_duplicate_rejected else
            "FLAGGED -- every distinct concept for this category was already used this session; "
            "reused as a last resort (see item note)."
        ) + avoided_but_used_note

        row = build_output_row(
            ctx=ctx, dish=dish, tier=tier, category_rank=category_rank,
            comparables=comparables, comparable_details=comparable_details,
            comparable_total_found=total_found,
            weekday_pct=mix["weekday_pct"], weekend_pct=mix["weekend_pct"],
            top_categories=top_categories, used_names=used_display_names,
            best_item_match=best_item_match, income_by_ethnicity=income_by_ethnicity,
            cuisine_score=cuisine_score, primary_cuisine=cuisine_result.primary,
            secondary_cuisine=cuisine_result.secondary, uniqueness_result=uniqueness_result,
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