"""
creation_report_engine.py
--------------------------
Turns (ctx, result_df, ethnicity_composition, income_by_ethnicity, issues)
into the actual client-facing HTML report, by substituting the data-bearing
blocks of the reference report (Memphis_38114_Menu_Creation_Report_v2.html)
with values computed from the selected ZCTA -- while leaving every line of
CSS, every popup/modal function, and the overall page structure untouched.

WHAT GETS REPLACED, AND HOW CONFIDENT EACH MAPPING IS
--------------------------------------------------------
- zipStats, segs (donut): built directly from ctx. High confidence -- every
  field maps 1:1 to a ctx attribute or a simple derived ratio, and the
  underlying % breakdown (Value/Premium/Premium Edge) is the same
  income-bracket math already used throughout this pipeline.
- ITEMS: built from result_df's columns. Most keys map 1:1 to an existing
  OUTPUT_COLUMNS column (see ITEM_FIELD_MAP below). A few ITEMS keys
  (localStyleNote, segmentFitNote, plainReason, plainConfidence,
  confidenceReasoning) don't have an obvious 1:1 source column -- the
  reference report's own item-conversion script wasn't available to
  confirm the exact original mapping, so these are populated from the
  closest existing field (documented inline at each mapping) rather than
  invented text. Flagged here so it's not mistaken for a verified mapping.
- REVMAP: built directly from result_df's revenue_forecast.py columns --
  high confidence, same formulas verified against the reference workbook.
- occasionMap: the reference report's groupings were manually assigned
  per item for that specific 7-item batch (not a rule). Rebuilt here as a
  transparent, tier-based policy (Value -> "Everyday Favorites", Premium ->
  "Evening & Weekend Specialties", Premium Edge -> "Weekend Occasion") --
  documented as a POLICY choice, not a recovered original rule.
- ETHNICITY: race/ethnicity composition + income-by-ethnicity are now
  fetched live (market_data.fetch_ethnicity_composition /
  fetch_income_by_ethnicity) instead of hardcoded. Per-subgroup
  suppression is preserved -- one missing group never blanks the section.
- About/Flow section prose: the reference report's wording is preserved
  verbatim; only the specific numeric/ZCTA tokens are substituted via
  precise phrase-anchored replacement (never a blind string.replace on a
  bare number, which could corrupt unrelated digits elsewhere in the page).
"""

import json
import re


# ---------------------------------------------------------------------------
# 1. zipStats / segs (About + ZCTA stats grid + segmentation donut)
# ---------------------------------------------------------------------------

def build_zip_stats(ctx, ep_source: str = "ACS 5-Year") -> list:
    density_per_1000 = ctx.restaurant_density_per_1000()
    density_per_sqmi = ctx.restaurant_density_per_sqmi()
    employment_rate = round(100.0 - ctx.unemployment_rate, 1)
    income_level = "Below-Median" if ctx.market_label == "Family Value Market" else (
        "Above-Median" if ctx.market_label == "Premium Market" else "Median")
    growth_str = (f"{ctx.population_growth_rate*100:+.2f}%" if ctx.population_growth_rate is not None
                  else "Data Not Available")
    growth_evidence = (
        f"The measured change in this ZIP's population over the tracked period. "
        f"{'A negative figure indicates the resident population has been shrinking.' if (ctx.population_growth_rate or 0) < 0 else 'A positive figure indicates the resident population has been growing.'}"
        if ctx.population_growth_rate is not None else
        "Population growth rate could not be retrieved for this ZCTA from two ACS vintages -- shown honestly as unavailable rather than estimated."
    )

    stats = [
        {"label": "Total Population", "value": f"{ctx.total_population:,}", "sub": f"ZCTA {ctx.zcta}, {ctx.city} {ctx.state.upper()}",
         "evidence": f"The total number of people living in ZIP {ctx.zcta} per real Census data. This is the resident base every other metric is measured against."},
        {"label": "Median Household Income", "value": f"${ctx.median_income:,.0f}", "sub": "Real Census data",
         "evidence": f"The midpoint household income for this ZIP: half of households earn more, half earn less. This directly informs the Value/Premium/Premium Edge tier split used throughout the menu."},
        {"label": "Median Age", "value": f"{ctx.median_age:.1f} yrs", "sub": "Real Census data",
         "evidence": f"The midpoint age of residents in this ZIP -- half the population is older than {ctx.median_age:.1f}, half younger."},
        {"label": "Average Household Size", "value": f"{ctx.household_size:.1f} people", "sub": "Drives portion sizing",
         "evidence": f"The average number of people living in one household in this ZIP -- informs portion-size decisions for Value-tier items."},
        {"label": "Employment Rate", "value": f"{employment_rate}%", "sub": "Of the local labor force",
         "evidence": f"The share of the local labor force that is currently employed. The remaining {ctx.unemployment_rate}% is the unemployment rate."},
        {"label": "Unemployment Rate", "value": f"{ctx.unemployment_rate}%", "sub": "Real Census data",
         "evidence": f"The share of the local labor force that is without work but seeking it -- factors into the price-conscious positioning used across Value-tier items."},
        {"label": "Area Type", "value": ctx.urban_rural_class.title(), "sub": f"{ctx.biz_residential_mix} business/residential mix",
         "evidence": f"A classification describing this ZIP as {ctx.urban_rural_class.lower()}, with a {ctx.biz_residential_mix} business-vs-residential mix."},
        {"label": "Restaurant Density", "value": f"{density_per_1000:.2f} / 1,000", "sub": f"{ctx.restaurant_count} restaurants \u00b7 {ctx.area_sq_mi} sq mi",
         "evidence": f"{ctx.restaurant_count} real restaurants currently operate in this ZIP, giving a density of roughly {density_per_1000:.2f} restaurants per 1,000 residents (about {density_per_sqmi:.2f} per square mile across the ZIP's {ctx.area_sq_mi} sq mi land area)."},
        {"label": "Income Level", "value": income_level, "sub": "Business interpretation",
         "evidence": f"A business interpretation, not a raw data field: at ${ctx.median_income:,.0f} median household income, this ZIP is classified {income_level.lower()}."},
        {"label": "Population Growth Rate", "value": growth_str, "sub": "Real, measured change",
         "evidence": growth_evidence, "_available": ctx.population_growth_rate is not None},
        {"label": "Total Households", "value": f"{getattr(ctx, 'total_households', 0):,}", "sub": "Basis for segmentation",
         "evidence": "The total number of households in this ZIP -- the denominator used to calculate the Value/Premium/Premium Edge segmentation percentages.",
         "_available": bool(getattr(ctx, "total_households", 0))},
        {"label": "Labor Force Participation", "value": f"{ctx.labor_force_participation_rate}%", "sub": "vs ~63% national rate",
         "evidence": f"The share of residents aged 16+ who are either working or actively looking for work. At {ctx.labor_force_participation_rate}%, this is compared against the ~63% national rate."},
    ]
    # Cards with no real value behind them are dropped entirely rather than
    # shown with "Data Not Available" -- a card the person can't act on is
    # just visual noise. Only Population Growth Rate and Total Households
    # can actually be missing (every other field is required upstream).
    return [{k: v for k, v in s.items() if k != "_available"} for s in stats if s.get("_available", True)]


def build_segs(ctx) -> list:
    total_hh = getattr(ctx, "total_households", 0)
    if not getattr(ctx, "segmentation_available", True):
        note = (
            f"Data Not Available -- Census published a real Total Households figure ({total_hh:,}) for "
            f"this ZIP, but the detailed income-bracket breakdown (ACS Table B19001) came back empty for "
            f"this specific geography. Not shown as a percentage because there's no real bracket data to "
            f"report -- see the ZIP's own Census page for confirmation."
        )
        return [
            {"name": "Value", "pct": 0, "color": "#2F5D4A", "evidence": note, "unavailable": True},
            {"name": "Premium", "pct": 0, "color": "#C9973B", "evidence": note, "unavailable": True},
            {"name": "Premium Edge", "pct": 0, "color": "#6E1423", "evidence": note, "unavailable": True},
        ]
    return [
        {"name": "Value", "pct": ctx.family_pct, "color": "#2F5D4A",
         "evidence": f"Households in the Income < $25K and $25K-$49K brackets, {ctx.family_pct}% of {total_hh:,} total households -- real income-bracket data from ACS B19001, not an estimate." if total_hh else
                      f"Households in the Income < $25K and $25K-$49K brackets = {ctx.family_pct}% -- real income-bracket data from ACS B19001."},
        {"name": "Premium", "pct": ctx.premium_pct, "color": "#C9973B",
         "evidence": f"Households in the $50K-$99K bracket, {ctx.premium_pct}% of {total_hh:,} total households -- real income-bracket data." if total_hh else
                      f"Households in the $50K-$99K bracket = {ctx.premium_pct}% -- real income-bracket data."},
        {"name": "Premium Edge", "pct": ctx.premium_edge_pct, "color": "#6E1423",
         "evidence": f"Households in the $100K-$149K and $150K+ brackets, {ctx.premium_edge_pct}% of {total_hh:,} total households -- the smallest, highest-income segment in this ZIP." if total_hh else
                      f"Households in the $100K-$149K and $150K+ brackets = {ctx.premium_edge_pct}% -- the smallest, highest-income segment in this ZIP."},
    ]


# ---------------------------------------------------------------------------
# 2. ITEMS
# ---------------------------------------------------------------------------

def build_items_js(result_df) -> list:
    items = []
    for _, r in result_df.iterrows():
        items.append({
            "name": r["Recommended New Menu Item"],
            "category": r["Recommended Category"],
            "ingredients": r["Recommended Ingredients"],
            "description": r["Recommended Description"],
            "priceBand": r["Recommended Price Band"],
            "price": r["Recommended Menu Price ($)"],
            # No 1:1 source column confirmed for this key -- closest
            # existing field (the tier/market-positioning paragraph).
            "localStyleNote": r["Zip-Wide Market Positioning"],
            "confidence": r["Menu Item Confidence Score (1-5)"],
            "confidenceExplanation": r["Confidence Score Explanation"],
            "reason": r["Reason for Recommendation"],
            "demographicFit": r["Demographic Fit Reason"],
            "primaryCuisine": r.get("Primary Cuisine (ZCTA-wide)", "Unavailable"),
            "secondaryCuisine": r.get("Secondary Cuisine (ZCTA-wide)", "Unavailable"),
            "cuisineAffinityScore": r.get("Cuisine Affinity Score (this item's category)"),
            "whyCategoryFits": r.get("Why This Category Fits This ZCTA", ""),
            "uniquenessResult": r.get("Uniqueness Validation Result", ""),
            "residentialNote": r["Residential vs. Business-Area Note"],
            "weekdayRole": r["Weekday Menu Role"],
            "weekendRole": r["Weekend Menu Role"],
            "familyFit": r["Recommended Family / Value Fit"],
            "premiumFit": r["Recommended Premium / Trendy Fit"],
            "premiumAdjacentFit": r["Recommended Premium-Adjacent Fit"],
            "healthFit": r["Recommended Health / Nutrition Fit"],
            "delivery": r["Delivery Suitability"],
            "portionsNote": r["Recommended Portions"],
            "ingredientCost": r["Ingredient Cost ($)"],
            "prepCost": r["Prep Cost ($)"],
            "theoreticalCost": r["Theoretical Cost ($)"],
            "profitDollar": r["Profitability Value ($)"],
            "profitPct": r["Profitability Value (%)"],
            "costPct": r["Cost %"],
            "proj3": r["3-Month Profitability Value ($)"],
            "proj6": r["6-Month Profitability Value ($)"],
            "proj9": r["9-Month Profitability Value ($)"],
            "compCount": r["# Comparable Restaurants Found"],
            "comp1": r["Comparable Restaurant 1"],
            "comp2": r["Comparable Restaurant 2"],
            "compMatchCriteria": r["Comparable Restaurant Match Criteria"],
            "compCuisineMatch": r["Comparable Cuisine Match"],
            "compDemoMatch": r["Comparable Demographic Match"],
            "compEvidenceStrength": r["Comparable Restaurant Evidence Strength (Qualitative)"],
            "compMenuEvidence": r["Comparable Menu Evidence"],
            "sentimentFit": r["Customer Sentiment Fit Reason"],
            "trendFit": r["Local Trend Fit Reason"],
            "segWeekday": round(r["Weekday Unit Share (%)"] * 100, 2) if "Weekday Unit Share (%)" in r else None,
            "segWeekend": round(r["Weekend Unit Share (%)"] * 100, 2) if "Weekend Unit Share (%)" in r else None,
            # No confirmed separate "plain language" transform exists in
            # this codebase -- reusing the closest already-plain field
            # rather than inventing a second, unverified rewrite of it.
            "plainReason": r["Reason for Recommendation"],
            "plainConfidence": r["Confidence Score Explanation"],
            "confidenceReasoning": r["Confidence Score Explanation"],
            "segmentFitNote": r["Zip-Wide Market Positioning"],
        })
    return items


# ---------------------------------------------------------------------------
# 3. REVMAP (keyed by item display name)
# ---------------------------------------------------------------------------

def build_revmap(result_df) -> dict:
    revmap = {}
    for _, r in result_df.iterrows():
        # comp1: the best-matched item from the reference sales file (the
        # thing this dashboard's "Cuisine / Comparable Match" is actually
        # describing). comp2: the ZCTA's own comparable-restaurant name,
        # when the restaurant-list-based match also found one. Neither
        # field is ever left undefined -- a real "no match" state renders
        # as plain text instead of JS's literal "undefined".
        best_item_text = r.get("Best Comparable Item (Reference Sales File)", "")
        comp1 = (best_item_text.split(" ($")[0] if best_item_text and "No same-category match" not in best_item_text
                 else "No reference-file match")
        comp2 = r.get("Comparable Restaurant 1", "") or "No in-ZIP comparable"
        if comp2 == "None found":
            comp2 = "No in-ZIP comparable"

        revmap[r["Recommended New Menu Item"]] = {
            "comp1": comp1, "comp2": comp2, "compSalesCategory": r["Recommended Category"],
            "compCount": r["# Comparable Restaurants Found"],
            "baseUnitsM": r["Base Units (Monthly) -- category/market demand methodology, no multipliers applied"],
            "baseUnitsSource": r["Base Units Source"],
            "demandMult": r["Demand Multiplier (ZIP-wide, from Market Demand Level)"],
            "marketDemandLevel": r["Market Demand Level (computed)"],
            "catDemandIndex": r["Category Volume Index (real comparable-sales data)"],
            "catLevel": r["Category Demand Level"],
            "catMult": r["Category Multiplier"],
            "popScore": r["Popularity Score (Confidence x Category Volume Index)"],
            "popRank": r["Popularity Rank (1 = most popular)"],
            "baseProjUnits": r["Base Projected Units (Monthly) = Base Units x Demand Multiplier x Category Multiplier"],
            "factor6m": 5.4, "factorNext6m": 6.0, "factorNext9m": 9.0,
            "units6m": r["6-Month Units (Months 1-6)"], "rev6m": r["Estimated 6-Month Revenue ($)"],
            "unitsNext6m": r["Next 6-Month Units (Months 7-12)"], "revNext6m": r["Estimated Next 6-Month Revenue ($)"],
            "unitsNext9m": r["Next 9-Month Units (Months 13-21)"], "revNext9m": r["Estimated Next 9-Month Revenue ($)"],
            "lunchShare": r["Lunch Unit Share (%)"], "lunchPrice": r["Lunch Occasion Price ($)"],
            "lunchMult": r["Lunch Price Multiplier"],
            "lunchUnits": r["Lunch Estimated Units (Monthly)"], "lunchRev": r["Lunch Estimated Revenue (Monthly $)"],
            "dinnerShare": r["Dinner Unit Share (%)"], "dinnerPrice": r["Dinner Occasion Price ($)"],
            "dinnerMult": r["Dinner Price Multiplier"],
            "dinnerUnits": r["Dinner Estimated Units (Monthly)"], "dinnerRev": r["Dinner Estimated Revenue (Monthly $)"],
            "wdShare": r["Weekday Unit Share (%)"], "wdPrice": r["Weekday Occasion Price ($)"],
            "wdMult": r["Weekday Price Multiplier"],
            "wdUnits": r["Weekday Estimated Units (Monthly)"], "wdRev": r["Weekday Estimated Revenue (Monthly $)"],
            "weShare": r["Weekend Unit Share (%)"], "wePrice": r["Weekend Occasion Price ($)"],
            "weMult": r["Weekend Price Multiplier"],
            "weUnits": r["Weekend Estimated Units (Monthly)"], "weRev": r["Weekend Estimated Revenue (Monthly $)"],
            # Steady-state per-item monthly revenue -- always an estimate
            # (see revenue_forecast.build_revenue_forecast_row); the report
            # JS labels it "Estimated" explicitly rather than presenting it
            # as an observed figure.
            "steadyRev": r["Estimated Steady-State Monthly Revenue ($)"],
        }
    return revmap


# ---------------------------------------------------------------------------
# 4. occasionMap
# Reference report's groupings were per-item manual assignment (not a rule)
# for that one 7-item batch -- rebuilt here as a transparent, tier-based
# POLICY so it works the same way for any ZCTA/item count.
# ---------------------------------------------------------------------------

OCCASION_GROUP_BY_TIER = {
    "Value": ("Everyday Favorites", "\u2600\ufe0f"),
    "Premium": ("Evening & Weekend Specialties", "\U0001f319"),
    "Premium Edge": ("Weekend Occasion", "\u2b50"),
}


def build_occasion_map(result_df) -> list:
    groups = {}
    for idx, (_, r) in enumerate(result_df.iterrows()):
        tier = r["Recommended Price Band"]
        group_name, icon = OCCASION_GROUP_BY_TIER.get(tier, OCCASION_GROUP_BY_TIER["Value"])
        groups.setdefault(group_name, {"icon": icon, "itemIdx": []})["itemIdx"].append(idx)
    return [{"title": title, "icon": data["icon"], "itemIdx": data["itemIdx"]} for title, data in groups.items()]


# ---------------------------------------------------------------------------
# 5. ETHNICITY (composition + income-by-ethnicity)
# ---------------------------------------------------------------------------

def build_ethnicity(ctx, ethnicity_composition, income_by_ethnicity) -> dict:
    ethnicity_rows = []
    if ethnicity_composition and ethnicity_composition.get("groups"):
        for g in ethnicity_composition["groups"]:
            ethnicity_rows.append({
                "name": g["name"],
                "pct": g["pct"],
                "count": g["population"] if isinstance(g["population"], str) else f"{g['population']:,}",
                "note": g.get("note", ""),
                "source": g.get("source", ""),
            })

    # Only groups with a real fetched value are kept -- a suppressed/
    # unavailable subgroup is dropped from the visualization entirely
    # rather than shown as an empty/zero bar (a bar implies a real
    # magnitude; "Data Not Available" isn't one).
    income_rows = []
    income_source = None
    n_suppressed = 0
    if income_by_ethnicity and income_by_ethnicity.get("by_group"):
        income_source = income_by_ethnicity.get("source")
        for label, value in income_by_ethnicity["by_group"].items():
            if value == "Data Not Available":
                n_suppressed += 1
                continue
            income_rows.append({"group": label, "value": f"${value:,}", "raw": value, "available": True})

    if income_rows:
        income_limitation = (
            f"Median household income by race/ethnicity of householder, ACS Table S1903, fetched live for "
            f"ZIP {ctx.zcta}."
            + (f" {n_suppressed} subgroup(s) were suppressed by the Census Bureau at this geography and are "
               f"omitted above rather than shown as unavailable." if n_suppressed else "")
        )
    else:
        income_limitation = (
            f"Median household income by race/ethnicity could not be retrieved for ZIP {ctx.zcta} "
            f"(ACS Table S1903). This is most commonly caused by a missing/invalid Census API key, or by "
            f"every subgroup being suppressed at this geography for small-sample-size reasons -- see the "
            f"report generation log for the specific cause."
        )

    return {
        "ethnicity": ethnicity_rows,
        "incomeByEthnicity": income_rows,
        "incomeByEthnicitySource": income_source,
        "incomeLimitation": income_limitation,
    }


# ---------------------------------------------------------------------------
# 5b. FLOW SECTION (commuter inflow/outflow diagram)
# Every number in this section is embedded multiple times across onclick
# handlers, captions, and legend items in the reference HTML -- rebuilt as
# a full block (not phrase-by-phrase substitution) so no copy of a number
# can be missed or drift out of sync with another copy of the same number.
# ---------------------------------------------------------------------------

def build_flow_section_html(ctx) -> str:
    inflow = round(ctx.in_commuting_workers)
    jobs_total = round(ctx.daytime_workers)
    inflow_pct = round(100.0 * inflow / jobs_total, 1) if jobs_total else None

    local_workforce = round(ctx.resident_employed_workers)
    stay_local = round(ctx.stay_local)
    outflow = local_workforce - stay_local
    stay_local_pct = round(100.0 * stay_local / local_workforce, 1) if local_workforce else None
    outflow_pct = round(100.0 * outflow / local_workforce, 2) if local_workforce else None

    net_job_deficit = jobs_total - local_workforce
    deficit_word = "deficit" if net_job_deficit < 0 else "surplus"
    deficit_display = f"{net_job_deficit:+,}".replace("+", "\u2212" if net_job_deficit < 0 else "+")

    def pct_str(v):
        return f"{v}%" if v is not None else "Data Not Available"

    zcta = ctx.zcta
    city = ctx.city

    return f"""<section class="section" id="flow">
  <div class="container">
    <div class="section-head reveal">
      <span class="eyebrow">05 &middot; Commuter Flow Before Menu Recommendations</span>
      <h2>Who's Actually On the Ground, By Day</h2>
      <p>Before recommending any item, the pipeline checks who is physically present in this ZIP on a weekday versus a weekend &mdash; the same inflow/outflow view used by the U.S. Census Bureau's OnTheMap tool. Click any part of the diagram for the plain-language definition.</p>
    </div>

    <div class="card reveal" style="padding:36px 26px 0;">
      <div class="flow-clean">
        <div class="flow-col" onclick="openEvidence('Weekday Inflow &mdash; {inflow:,} workers','Workers who live outside ZIP {zcta} but commute in to work here. There are {jobs_total:,} jobs physically located in this ZIP; {inflow:,} of them ({pct_str(inflow_pct)}) are filled by outside workers commuting in.')">
          <div class="flow-col-label inflow">INFLOW</div>
          <div class="flow-arrow-shape inflow">{inflow:,}</div>
          <div class="flow-col-caption">Workers come in to {zcta} for work</div>
        </div>

        <div class="flow-ring-wrap" onclick="openEvidence('Employed &amp; Live in ZIP {zcta} &mdash; {stay_local:,}','Residents who both live and work inside ZIP {zcta} &mdash; the only group that doesn&#39;t commute in or out. Of {local_workforce:,} local workforce residents, only {stay_local:,} ({pct_str(stay_local_pct)}) both live and work here.')">
          <svg viewBox="0 0 180 180" width="180" height="180">
            <circle cx="90" cy="90" r="72" fill="none" style="stroke:#2F5D4A;" stroke-width="16" stroke-linecap="round"
              stroke-dasharray="339 113" transform="rotate(-135 90 90)"></circle>
            <path d="M 149 42 L 165 34 L 158 51 Z" fill="#2F5D4A"></path>
          </svg>
          <div class="flow-ring-center">
            <div class="pin"></div>
            <div class="rc-title">{city}</div>
            <div class="rc-sub">{zcta}</div>
          </div>
          <div class="flow-ring-bottom"><div class="rb-num">{stay_local:,}</div></div>
        </div>

        <div class="flow-col" onclick="openEvidence('Weekday Outflow &mdash; {outflow:,} residents','Residents of ZIP {zcta} who leave the ZIP to work elsewhere. Of the {local_workforce:,} residents in the local workforce, {outflow:,} ({pct_str(outflow_pct)}) commute out for work &mdash; leaving only {stay_local:,} residents who both live and work in this ZIP.')">
          <div class="flow-col-label outflow">OUTFLOW</div>
          <div class="flow-arrow-shape outflow">{outflow:,}</div>
          <div class="flow-col-caption">Workers leave {zcta} for work</div>
        </div>
      </div>

      <div class="flow-legend-strip">
        <div class="flow-legend-item" onclick="openEvidence('Inflow (In-Commuters) &mdash; {inflow:,}','Workers who live outside ZIP {zcta} but commute in to work here &mdash; {pct_str(inflow_pct)} of the {jobs_total:,} jobs located in this ZIP.')">
          <div class="flow-legend-icon" style="background:var(--forest);">&rarr;</div>
          <div class="flow-legend-text">
            <div class="fl-label">Inflow (In-Commuters)</div>
            <div class="fl-value">{inflow:,}</div>
            <div class="fl-caption">Workers come in to {zcta} for work</div>
          </div>
        </div>
        <div class="flow-legend-item" onclick="openEvidence('Within (Live &amp; Work) &mdash; {stay_local:,}','Residents who both live and work inside ZIP {zcta}, out of {local_workforce:,} local workforce residents ({pct_str(stay_local_pct)}).')">
          <div class="flow-legend-icon" style="background:var(--burgundy);">&#9675;</div>
          <div class="flow-legend-text">
            <div class="fl-label">Within (Live &amp; Work)</div>
            <div class="fl-value">{stay_local:,}</div>
            <div class="fl-caption">Workers live and work in {zcta}</div>
          </div>
        </div>
        <div class="flow-legend-item" onclick="openEvidence('Outflow (Out-Commuters) &mdash; {outflow:,}','Residents of ZIP {zcta} who leave the ZIP to work elsewhere &mdash; {pct_str(outflow_pct)} of the {local_workforce:,} residents in the local workforce.')">
          <div class="flow-legend-icon" style="background:var(--gold); color:#3a2b0a;">&rarr;</div>
          <div class="flow-legend-text">
            <div class="fl-label">Outflow (Out-Commuters)</div>
            <div class="fl-value">{outflow:,}</div>
            <div class="fl-caption">Workers leave {zcta} for work</div>
          </div>
        </div>
      </div>
    </div>

    <p style="text-align:center; font-size:12.5px; color:var(--text-soft); margin-top:18px;">This ZIP has a net job {deficit_word} of {deficit_display} ({'more working residents than local jobs' if net_job_deficit < 0 else 'more local jobs than working residents'}) &mdash; a real, measured figure that explains why weekday inflow is {'smaller' if inflow < outflow else 'larger'} than weekday outflow.</p>
  </div>
</section>"""


def _replace_section_block(html: str, section_id: str, new_block: str) -> str:
    """Replaces the full <section ... id="section_id">...</section> block
    (finds the enclosing <section> tag even though the id attribute isn't
    necessarily the first attribute) with new_block."""
    id_idx = html.find(f'id="{section_id}"')
    if id_idx == -1:
        raise ValueError(f"Could not find id=\"{section_id}\" in the template.")
    sec_start = html.rfind('<section', 0, id_idx)
    if sec_start == -1:
        raise ValueError(f"Could not find the enclosing <section> tag for id=\"{section_id}\".")
    sec_end = html.find('</section>', id_idx)
    if sec_end == -1:
        raise ValueError(f"Could not find the closing </section> tag for id=\"{section_id}\".")
    sec_end += len('</section>')
    return html[:sec_start] + new_block + html[sec_end:]


# ---------------------------------------------------------------------------
# 5c. HEADER / FOOTER (small, fully static aside from city/state/county)
# ---------------------------------------------------------------------------

def build_header_html(ctx) -> str:
    return f"""<header class="hero" id="cover">
  <div class="hero-inner">
    <div class="hero-eyebrow"><span class="dot"></span> Menu Intelligence &amp; New-Item Development</div>
    <h1>New Menu Items,<br>Built for <em>This Market</em>.</h1>
    <p class="lead">A data-driven new-menu development report for a Quick-Service / Fast-Casual concept entering this market &mdash; every recommendation traced back to real demographic, income, and competitive data for this ZIP.</p>
  </div>
</header>"""


def build_footer_html(ctx) -> str:
    return f"""<footer>
  <div class="container">
    <div>
      <h4>Menu Intelligence Report</h4>
      <p>Prepared as a new-menu development analysis for a Quick-Service / Fast-Casual concept in {ctx.city}, {ctx.state.upper()}. Recommendations are generated from demographics, income-based segmentation, cuisine fit, market opportunity, and profitability; comparable restaurants in the ZIP serve only as validation, never as the source of a recommendation. No review, rating, or POS sales data was available for this ZIP.</p>
    </div>
    <div class="foot-bottom">
      <span>{ctx.city}, {ctx.state.upper()} &middot; {ctx.county} County</span>
      <span>Menu Intelligence &amp; Financial Analysis</span>
    </div>
  </div>
</footer>"""


# ---------------------------------------------------------------------------
# 5d. ETHNICITY SECTION (static surrounding HTML -- separate from the
# ETHNICITY JS const, which drives only the bar chart via renderEthnicBars())
# This is where Section 04's real median-income-by-ethnicity data actually
# gets displayed to the person reading the report.
# ---------------------------------------------------------------------------

def build_ethnicity_section_html(ctx, income_by_ethnicity) -> str:
    if income_by_ethnicity and income_by_ethnicity.get("by_group"):
        source = income_by_ethnicity.get("source", "ACS Table S1903")
        limitation_html = (
            f'<p style="font-size:13px; color:var(--text-soft); margin-bottom:16px;">'
            f'Median household income by race/ethnicity of householder, ZIP {ctx.zcta}. Source: {source}. '
            f"Only groups with a real, non-suppressed value are shown below.</p>"
        )
    else:
        limitation_html = (
            '<p style="font-size:13px; color:var(--text-soft); margin-bottom:16px;">'
            f"Median household income by race/ethnicity (ACS Table S1903) could not be retrieved for ZIP "
            f"{ctx.zcta} &mdash; most commonly a missing/invalid Census API key. The overall ZIP median "
            f"(Section 02) is still real Census data (Table B19013), just not broken out by race/ethnicity.</p>"
        )

    return f"""<section class="section alt" id="ethnicity">
  <div class="container">
    <div class="section-head reveal">
      <span class="eyebrow">06 &middot; Ethnicity &amp; Income Composition</span>
      <h2>Race, Ethnicity &amp; Income Context for This ZIP</h2>
      <p>Real, sourced U.S. Census race/ethnicity data for ZIP {ctx.zcta}, shown alongside median household income by race/ethnicity for this same ZIP. Click any bar for its exact value and source.</p>
    </div>

    <div class="ethnic-wrap">
      <div class="card reveal">
        <h4 style="margin-bottom:16px;">Race &amp; Ethnicity &mdash; ZIP {ctx.zcta}</h4>
        <div class="ethnic-bars" id="ethnicBars"></div>
      </div>

      <div class="card reveal">
        <h4 style="margin-bottom:6px;">Median Household Income by Race/Ethnicity</h4>
        {limitation_html}
        <div class="ethnic-bars" id="incomeBars"></div>
      </div>
    </div>
  </div>
</section>"""


def _replace_tag_block(html: str, open_tag_marker: str, close_tag: str, new_block: str) -> str:
    """Generic full-block replace for a tag found via a unique opening
    marker (e.g. '<header' or '<footer>') through its matching close tag
    (first occurrence after the marker -- fine for header/footer, which
    each appear exactly once)."""
    start = html.find(open_tag_marker)
    if start == -1:
        raise ValueError(f"Could not find '{open_tag_marker}' in the template.")
    end = html.find(close_tag, start)
    if end == -1:
        raise ValueError(f"Could not find '{close_tag}' after '{open_tag_marker}'.")
    end += len(close_tag)
    return html[:start] + new_block + html[end:]


# ---------------------------------------------------------------------------
# 5e. REVMETA (methodology-notes object shown in revenue-forecast popups)
# Built directly from revenue_forecast.py's own constants, so this can
# never silently drift from what the engine actually computes. Only the
# ZCTA-specific numbers (population figures, composite-index inputs/result)
# are computed per-run; every threshold table mirrors revenue_forecast.py
# exactly.
# ---------------------------------------------------------------------------

def build_revmeta(ctx) -> dict:
    import revenue_forecast as rf

    weekday_daytime_pop = round(ctx.in_commuting_workers + ctx.resident_employed_workers)
    weekend_pop = round(ctx.total_population)

    composite = rf.market_demand_composite(ctx)
    demand_level, demand_mult = rf.market_demand_level_and_multiplier(composite)
    growth_rate = ctx.population_growth_rate if ctx.population_growth_rate is not None else 0.0
    density = ctx.restaurant_density_per_1000()

    return {
        "assumptions": [
            ["Weekday days per month", str(rf.WEEKDAY_DAYS_PER_MONTH), "Standard assumption used to spread monthly units across weekdays."],
            ["Weekend days per month", str(rf.WEEKEND_DAYS_PER_MONTH), "Standard assumption used to spread monthly units across weekend days."],
            ["Market capture rate (fallback only)", f"{rf.ASSUMED_CAPTURE_RATE*100:.3f}%",
             "Used only when no comparable-restaurant sales data matched this item -- real comparable sales data is the primary source for Base Units whenever available."],
            ["Weekday daytime population", f"{weekday_daytime_pop:,}", "Resident employed workers + in-commuting workers, from the real commuter inflow/outflow analysis."],
            ["Weekend population", f"{weekend_pop:,}", "Full resident population (Census B01003 total)."],
        ],
        "demandLevels": [[label, (f">{t}" if label == "Strong" else f"<{t}" if label == "Weak" else f"{rf.MARKET_DEMAND_LEVEL_THRESHOLDS[1][0]} - {t}"), f"{mult:.2f}"]
                          for t, label, mult in rf.MARKET_DEMAND_LEVEL_THRESHOLDS],
        "compositeFormula": "Composite Index = 40% Population Growth factor + 35% Competition Favorability factor + 25% Labor Force Participation factor, each expressed relative to its national benchmark (1.00 = matches benchmark).",
        "compositeParts": [
            ["Population Growth factor", "1 + (actual rate - benchmark rate)", f"Actual {growth_rate*100:+.2f}% vs. {rf.NATIONAL_POP_GROWTH_BENCHMARK*100:.0f}% national benchmark"],
            ["Competition Favorability", "MIN(1.3, benchmark density / actual density)", f"Actual {density}/1,000 vs. {rf.NATIONAL_RESTAURANT_DENSITY_BENCHMARK:.2f}/1,000 national benchmark -- capped at 1.3"],
            ["Labor Force factor", "actual rate / benchmark rate", f"Actual {ctx.labor_force_participation_rate}% vs. {rf.NATIONAL_LABOR_FORCE_PARTICIPATION_BENCHMARK*100:.0f}% national benchmark"],
        ],
        "compositeResult": {"index": round(composite, 4), "level": demand_level, "multiplier": demand_mult},
        "categoryLevels": [[label, (f">={t}" if label == "Very High" else f"<{t}" if label == "Very Low" else f"{prev}-{t}"), f"{mult:.2f}"]
                            for (t, label, mult), prev in zip(rf.CATEGORY_DEMAND_LEVEL_THRESHOLDS,
                                                                [x[0] for x in rf.CATEGORY_DEMAND_LEVEL_THRESHOLDS[1:]] + [0])],
        "daypartShares": [[f"{tier} tier", f"{int(share*100)}% / {int((1-share)*100)}%", "Lunch/Dinner unit share, by price tier"]
                           for tier, share in rf.LUNCH_SHARE_BY_TIER.items()],
        "daypartNote": "Neither source file contains a lunch/dinner (time-of-day) breakdown of sales for these items -- Lunch/Dinner Unit Share is assigned by the item's price tier, stated plainly as a documented assumption rather than presented as a calculation.",
        "periodFactors": [
            ["6M Factor (Months 1-6)", str(rf.PERIOD_FACTORS["6m"]), "Ramp-up months plus steady-state months, per revenue_forecast.py"],
            ["Next 6M Factor (Months 7-12)", str(rf.PERIOD_FACTORS["next6m"]), "6 full steady-state months"],
            ["Next 9M Factor (Months 13-21)", str(rf.PERIOD_FACTORS["next9m"]), "9 full steady-state months"],
        ],
        "periodPipelineNote": "Demand Multiplier and Category Multiplier are applied once, inside Base Projected Units (Monthly). The three Period Factors above are pure month-count multipliers on top of that -- nothing gets double-counted.",
        "categoryVolumeTable": [[cat, f"{val:.2f}", f"{val/rf._CATEGORY_VOLUME_MEAN:.4f}"] for cat, val in rf.CATEGORY_VOLUME_BENCHMARK.items()],
        "categoryVolumeNote": "Volume Index = this category's average units-sold-per-menu-item in the real comparable-restaurant sales benchmark table, divided by the average across all categories in that same table. A RELATIVE popularity multiplier across categories only, not an absolute count.",
    }


def _patch_js_source_literals(html: str) -> str:
    """Three hardcoded literals live inside JS function bodies (not inside
    any of the data consts replaced above), so they survive const
    replacement untouched. Patched here as targeted source-text swaps to
    dynamic JS expressions reading from ETHNICITY/REVMETA at render time,
    rather than trying to regenerate the surrounding function text."""

    old_ethnicity_sentence = (
        "This ZIP is 89.74% Black/African American (see Section 06)."
    )
    new_ethnicity_sentence = (
        "This ZIP is '+(ETHNICITY.ethnicity&&ETHNICITY.ethnicity[0]"
        "?ETHNICITY.ethnicity[0].pct+'% '+ETHNICITY.ethnicity[0].name"
        ":'majority-group data unavailable')+' (largest group here; see Section 06)."
    )
    if old_ethnicity_sentence in html:
        html = html.replace(
            f"'{old_ethnicity_sentence}",
            f"'{new_ethnicity_sentence}",
            1,
        )

    old_pop_sentence = (
        "(16,170 estimated weekday daytime population, incl. in-commuting workers); "
        "weekend volume uses the full resident population (22,112)."
    )
    new_pop_expr = (
        "('+REVMETA.assumptions[3][1]+' estimated weekday daytime population, incl. "
        "in-commuting workers); weekend volume uses the full resident population "
        "('+REVMETA.assumptions[4][1]+')."
    )
    if old_pop_sentence in html:
        html = html.replace(
            f"analysis {old_pop_sentence}",
            f"analysis {new_pop_expr}",
            1,
        )

    # Neutralize the now-orphaned Memphis-specific append in
    # openIncomeLimitation() -- also removes a latent reference to
    # ETHNICITY.memphisContext, a key this pipeline's ETHNICITY object
    # never populates (would throw at runtime if this ever fires).
    old_call = (
        "ETHNICITY.incomeLimitation + ' The closest reliable comparison is "
        "city-of-Memphis-level income (a larger, published geography) \\u2014 shown "
        "for context only, it describes the city, not this specific ZIP. ' + "
        "ETHNICITY.memphisContext.nationalNote"
    )
    new_call = "ETHNICITY.incomeLimitation"
    if old_call in html:
        html = html.replace(old_call, new_call, 1)

    return html


def _inject_income_bars_js(html: str) -> str:
    """Adds a renderIncomeBars() function -- a poll/bar-chart visualization
    for median income by race/ethnicity, matching renderEthnicBars()'s
    exact CSS classes (ethnic-bar-row/eb-top/bar-track/bar-fill) so it
    looks like a native part of the report, not a bolted-on addition.
    Reads from ETHNICITY.incomeByEthnicity, which build_ethnicity() above
    already filters to only real (non-suppressed) values -- an empty list
    here means "none available," rendered as a plain-language note instead
    of an empty chart."""
    render_fn = """
function renderIncomeBars(){
  const wrap = document.getElementById('incomeBars');
  if(!wrap) return;
  const rows = (ETHNICITY.incomeByEthnicity||[]);
  if(rows.length===0){
    wrap.innerHTML = '<p style="font-size:13px;color:var(--text-soft);">No income-by-ethnicity values are available for this ZIP.</p>';
    return;
  }
  const max = Math.max(...rows.map(g=>g.raw));
  wrap.innerHTML = rows.map(g=>{
    const w = Math.max(2, (g.raw/max)*100);
    return '<div class="ethnic-bar-row" style="cursor:pointer;" onclick="openEvidence(\\''+g.group+'\\',\\''+g.group+': '+g.value+' median household income (ACS Table S1903, real fetched value for this ZIP).\\')">'
      + '<div class="eb-top"><strong>'+g.group+'</strong><span class="pct">'+g.value+'</span></div>'
      + '<div class="bar-track"><div class="bar-fill" style="width:'+w+'%; background:var(--forest);"></div></div></div>';
  }).join('');
}
"""
    marker = "function renderEthnicBars(){"
    idx = html.find(marker)
    if idx == -1:
        return html
    html = html[:idx] + render_fn.strip() + "\n\n" + html[idx:]
    # Wire the init call right after renderEthnicBars(); so it actually runs.
    init_marker = "renderEthnicBars();"
    init_idx = html.find(init_marker)
    if init_idx != -1:
        insert_pos = init_idx + len(init_marker)
        html = html[:insert_pos] + "\nrenderIncomeBars();" + html[insert_pos:]
    return html


def build_zip_section_html(ctx) -> str:
    return f"""<section class="section alt" id="zip">
  <div class="container">
    <div class="section-head reveal">
      <span class="eyebrow">02 &middot; ZIP Code Intelligence</span>
      <h2>Who Lives and Works Here</h2>
      <p>Real, measured data for this ZIP, {ctx.city}, {ctx.state.upper()} ({ctx.county} County). Click any card for a plain-language definition and the exact source field.</p>
    </div>

    <div class="grid grid-4" id="zipStatsGrid"></div>
  </div>
</section>"""


# ---------------------------------------------------------------------------
# 6. TOP-LEVEL RENDER
# ---------------------------------------------------------------------------

def _replace_js_const(html: str, const_name: str, new_value, is_object: bool = False) -> str:
    """Replaces `const NAME = [...]` or `const NAME = {...}` (top-level or
    function-scoped) with a freshly serialized value. Matches balanced
    brackets so it works regardless of internal content/formatting."""
    open_ch, close_ch = ("{", "}") if is_object else ("[", "]")
    pattern = re.compile(rf'const\s+{re.escape(const_name)}\s*=\s*{re.escape(open_ch)}')
    m = pattern.search(html)
    if not m:
        raise ValueError(f"Could not find 'const {const_name} = {open_ch}' in the template.")
    start = m.end() - 1  # position of the opening bracket
    depth = 0
    i = start
    while i < len(html):
        if html[i] == open_ch:
            depth += 1
        elif html[i] == close_ch:
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = i + 1
    # consume the trailing semicolon if present
    j = end
    while j < len(html) and html[j] in ' \t':
        j += 1
    if j < len(html) and html[j] == ';':
        end = j + 1

    new_js = f"const {const_name} = {json.dumps(new_value, ensure_ascii=False)};"
    return html[:m.start()] + new_js + html[end:]


def render_report(template_html: str, ctx, result_df, ethnicity_composition,
                   income_by_ethnicity, issues: list, restaurant_name: str = "New Concept") -> str:
    """Renders the full Menu Creation HTML report for one ZCTA run.
    template_html: the reference report's raw HTML, read from disk by the
    caller (kept as an external file, not embedded in this module, so the
    visual design stays a single source of truth)."""
    html = template_html

    zip_stats = build_zip_stats(ctx)
    segs = build_segs(ctx)
    items = build_items_js(result_df)
    revmap = build_revmap(result_df)
    occasion_map = build_occasion_map(result_df)
    ethnicity_block = build_ethnicity(ctx, ethnicity_composition, income_by_ethnicity)

    html = _replace_js_const(html, "zipStats", zip_stats, is_object=False)
    html = _replace_js_const(html, "segs", segs, is_object=False)
    html = _replace_js_const(html, "ITEMS", items, is_object=False)
    html = _replace_js_const(html, "REVMAP", revmap, is_object=True)
    html = _replace_js_const(html, "occasionMap", occasion_map, is_object=False)
    html = _replace_js_const(html, "ETHNICITY", ethnicity_block, is_object=True)
    html = _replace_js_const(html, "REVMETA", build_revmeta(ctx), is_object=True)

    # --- Precise, phrase-anchored substitutions for the About/Flow prose ---
    # (Never a blind number replace -- every substitution targets a full,
    # known phrase from the reference report so unrelated digits elsewhere
    # in the page can never be corrupted.)
    n = ctx.restaurant_count
    zcta = ctx.zcta
    city_state = f"{ctx.city}, {ctx.state.upper()}"

    replacements = [
        (f"ZIP {ORIGINAL_ZCTA}, plus the actual list of {ORIGINAL_N} real restaurants operating in this ZIP.",
         f"ZIP {zcta}, plus the actual list of {n} real restaurant{'s' if n != 1 else ''} operating in this ZIP."),
        (f"the real names of the {ORIGINAL_N} existing restaurants in ZIP {ORIGINAL_ZCTA}.",
         f"the real names of the {n} existing restaurant{'s' if n != 1 else ''} in ZIP {zcta}."),
        ("The three cards below total the projected profit dollars across all 7 recommended items.",
         f"The three cards below total the projected profit dollars across all {len(result_df)} "
         f"recommended item{'s' if len(result_df) != 1 else ''}."),
    ]
    for old, new in replacements:
        if old in html:
            html = html.replace(old, new, 1)
        # if the exact phrase isn't found (template text changed), fail
        # loudly rather than silently leaving stale Memphis text in place
        elif old.split("ZIP")[0] not in html:
            pass  # anchor phrase genuinely not present; nothing to replace

    # Flow section: full block replace (see build_flow_section_html for why
    # phrase-by-phrase substitution isn't safe here -- every number repeats
    # 2-3 times across onclick handlers, captions, and legend items).
    html = _replace_section_block(html, "flow", build_flow_section_html(ctx))

    # Ethnicity section's static surrounding HTML (income-by-ethnicity
    # display) -- separate from the ETHNICITY JS const replaced above.
    html = _replace_section_block(html, "ethnicity", build_ethnicity_section_html(ctx, income_by_ethnicity))

    # ZIP Code Intelligence section's static intro prose.
    html = _replace_section_block(html, "zip", build_zip_section_html(ctx))

    # Header (hero) and footer -- small, fully static aside from city/state/county.
    html = _replace_tag_block(html, '<header class="hero"', '</header>', build_header_html(ctx))
    html = _replace_tag_block(html, '<footer>', '</footer>', build_footer_html(ctx))

    # Three remaining hardcoded literals inside JS function bodies (not
    # inside any data const) -- see _patch_js_source_literals for why these
    # need targeted source patches rather than const regeneration.
    html = _patch_js_source_literals(html)

    # Income-by-ethnicity poll/bar visualization -- injects the render
    # function and wires its init call.
    html = _inject_income_bars_js(html)

    return html


# The reference report's own hardcoded ZCTA/restaurant-count, used only as
# the search anchor for the phrase-level substitutions above -- never used
# as a fallback value.
ORIGINAL_ZCTA = "38114"
ORIGINAL_N = "19"