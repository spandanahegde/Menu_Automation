"""
cuisine_affinity.py
--------------------
Calculates a ZCTA-specific CUISINE AFFINITY / CUISINE DEMAND score BEFORE
any menu item is generated. This module answers "what food is most
appropriate for THIS ZCTA based on its actual data?" -- the menu/dish
generation layer (dish_library.select_dish, run_menu_creation) then only
operates inside the categories this module ranks highest. It is never the
other way around (pick a dish, then justify it).

WHY THIS EXISTS
Previously (run_menu_creation.py), category selection was:
    top_categories = category_landscape(zcta_restaurants).head(3)
i.e. "whichever categories the most competitor restaurants in this ZIP's
own restaurant list happen to carry" -- a single competition-count signal.
That's how the same category (usually "Burgers") ended up selected for
almost every ZCTA regardless of who actually lives there, and combined
with dish_library's 2-templates-per-category rotation, produced the
"Fire-Grilled Burger / Char-Grilled Burger / Loaded Burger" repetition
problem this module exists to fix.

WHAT THIS MODULE DOES NOT DO
It does NOT map an ethnicity directly to a cuisine ("this ZCTA is X% one
group, therefore serve Y cuisine"). Ethnicity composition is one weighted
input (demographic_fit) among five, combined with income positioning,
local competitive evidence, competition/saturation, and a trend/context
signal built from population growth + age + commuter mix. A cuisine with
a real ethnicity-affinity signal but no other supporting evidence will
NOT out-score a cuisine with broad, income/competition-backed evidence --
and vice versa. The weights below are the "existing application's logic"
this build already uses elsewhere (income-bracket tiering, category
landscape, indulgent-lean scoring) recombined into one scoring layer,
not new invented demographic rules.

DATA PRIORITY (matches the project's existing convention throughout
market_data.py / menu_intelligence_ingest.py): real ZCTA data first,
computed/derived signals second, neutral defaults only when a whole data
source is unavailable (never invented).
"""

import math
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Cuisine taxonomy. Each entry maps to the dish_library.py / creation_engine
# CATEGORY_KEYWORDS category (or categories, tried in order) that actually
# has templates/pricing/upgrade data -- this module decides WHICH cuisine
# direction fits the ZCTA; dish_library still owns HOW the dish is built.
#
# ethnicity_signals: partial, non-exclusive evidence weights (0-1) against
#   market_data.fetch_ethnicity_composition() group labels. A weight here
#   means "this group's local population share is ONE supporting signal
#   for this cuisine having local demand," not "this group eats this
#   cuisine." Groups not listed contribute 0 to that cuisine's
#   demographic_fit -- they simply don't count for or against it.
# income_position: "value" | "mixed" | "premium" -- typical price
#   position for this cuisine bucket, scored against the ZCTA's own real
#   income-bracket tiering (ctx.family_pct/premium_pct/premium_edge_pct),
#   not used to pick cuisine on its own.
# trend_lean: 0-1, how much a younger/growing-population trend signal
#   should favor this cuisine, vs. an older/stable-population signal
#   favoring traditional/value cuisines.
# generic_fallback: True for the small set of categories (Burgers, Wings,
#   Sandwiches, Fast Food) explicitly called out in the brief as NOT
#   allowed to auto-appear everywhere. These can still win on merit, but
#   only when their computed score actually clears the others -- they get
#   no default/free ranking boost.
# ---------------------------------------------------------------------------
CUISINE_PROFILES = {
    "Southern / Soul Food": {
        "categories": ["Southern", "Chicken"],
        "ethnicity_signals": {"Black or African American": 0.55, "White": 0.15},
        "income_position": "value",
        "trend_lean": 0.25,
        "generic_fallback": False,
    },
    "Mexican / Latin American": {
        "categories": ["Mexican"],
        "ethnicity_signals": {"Hispanic or Latino": 0.65},
        "income_position": "value",
        "trend_lean": 0.45,
        "generic_fallback": False,
    },
    "Asian / Pan-Asian": {
        "categories": ["Sushi/Asian"],
        "ethnicity_signals": {"Asian": 0.6},
        "income_position": "mixed",
        "trend_lean": 0.6,
        "generic_fallback": False,
    },
    "Italian": {
        "categories": ["Italian", "Pizza"],
        "ethnicity_signals": {"White": 0.15},
        "income_position": "premium",
        "trend_lean": 0.35,
        "generic_fallback": False,
    },
    "BBQ / Smokehouse": {
        "categories": ["BBQ"],
        "ethnicity_signals": {"Black or African American": 0.2, "White": 0.2},
        "income_position": "mixed",
        "trend_lean": 0.4,
        "generic_fallback": False,
    },
    "Seafood / Coastal": {
        "categories": ["Seafood"],
        "ethnicity_signals": {},
        "income_position": "premium",
        "trend_lean": 0.3,
        "generic_fallback": False,
    },
    "Pizza": {
        "categories": ["Pizza"],
        "ethnicity_signals": {"White": 0.1},
        "income_position": "value",
        "trend_lean": 0.2,
        "generic_fallback": False,
    },
    "Breakfast / All-Day": {
        "categories": ["Breakfast"],
        "ethnicity_signals": {},
        "income_position": "mixed",
        "trend_lean": 0.3,
        "generic_fallback": False,
    },
    "Dessert / Bakery": {
        "categories": ["Dessert"],
        "ethnicity_signals": {},
        "income_position": "mixed",
        "trend_lean": 0.5,
        "generic_fallback": False,
    },
    # -- generic-fallback bucket: explicitly the categories the brief says
    # must NOT auto-appear everywhere. No ethnicity/trend boost of their
    # own; they only rank up via real local competitive evidence + income
    # fit, same formula as every other cuisine, no default advantage.
    "American Classic / Burgers": {
        "categories": ["Burgers", "Fast Food"],
        "ethnicity_signals": {},
        "income_position": "value",
        "trend_lean": 0.1,
        "generic_fallback": True,
    },
    "Wings / Bar Food": {
        "categories": ["Wings"],
        "ethnicity_signals": {},
        "income_position": "value",
        "trend_lean": 0.15,
        "generic_fallback": True,
    },
    "Sandwiches / Deli": {
        "categories": ["Sandwiches"],
        "ethnicity_signals": {},
        "income_position": "value",
        "trend_lean": 0.1,
        "generic_fallback": True,
    },
}

# Component weights -- sum to 1.0. Kept as named constants (not magic
# numbers inline) so the weighting scheme is auditable/tunable in one
# place, matching how PERIOD_GROWTH_FACTOR / TIER_PRICE_MULTIPLIER are
# defined as named constants elsewhere in this codebase.
WEIGHT_DEMOGRAPHIC_FIT = 0.30
WEIGHT_INCOME_FIT = 0.20
WEIGHT_LOCAL_DEMAND = 0.20
WEIGHT_COMPETITION_FIT = 0.15
WEIGHT_TREND_FIT = 0.15

AVOID_SCORE_THRESHOLD = 0.16   # below this final score -> flagged "avoid"
SATURATION_SHARE = 0.40        # a category held by >40% of local restaurants is saturated


# Maps market_data.fetch_ethnicity_composition() group labels (population
# share, Tables B02001/B03003) to market_data.fetch_income_by_ethnicity()
# group labels (median household income, Table S1903) -- two different ACS
# tables with slightly different label text for the same group, fetched
# separately by this project. Needed so a cuisine's ethnicity_signals can
# be paired with that SAME group's real median income in this ZCTA, not
# just its population share.
ETHNICITY_TO_INCOME_LABEL = {
    "White": "White alone",
    "Black or African American": "Black or African American alone",
    "American Indian / Alaska Native": "American Indian and Alaska Native alone",
    "Asian": "Asian alone",
    "Native Hawaiian / Other Pacific Islander": "Native Hawaiian and Other Pacific Islander alone",
    "Some Other Race": "Some other race alone",
    "Two or More Races": "Two or more races",
    "Hispanic or Latino": "Hispanic or Latino (of any race)",
}


@dataclass
class CuisineScore:
    cuisine: str
    categories: list
    demographic_fit: float
    income_fit: float
    local_demand: float
    competition_fit: float
    trend_fit: float
    final_score: float
    evidence_count: int
    competitor_share: float
    reasoning: str
    ethnicity_income_basis: list = field(default_factory=list)


@dataclass
class CuisineAffinityResult:
    zcta: str
    ranked: list                 # list[CuisineScore], descending by final_score
    primary: Optional[CuisineScore]
    secondary: Optional[CuisineScore]
    supporting: list             # list[CuisineScore]
    avoid: list                  # list[CuisineScore] -- weak-evidence cuisines
    data_completeness_note: str

    def category_priority_list(self) -> list:
        """Flattened, de-duplicated list of dish_library categories in
        affinity-rank order -- what run_menu_creation should actually
        cycle through instead of raw category_landscape counts."""
        cats = []
        for score in self.ranked:
            for c in score.categories:
                if c not in cats:
                    cats.append(c)
        return cats

    def category_to_cuisine(self) -> dict:
        return {c: s.cuisine for s in self.ranked for c in s.categories}

    def avoided_categories(self) -> set:
        return {c for s in self.avoid for c in s.categories}


def _demographic_fit(ethnicity_signals: dict, ethnicity_groups: Optional[list]) -> float:
    if not ethnicity_signals or not ethnicity_groups:
        return 0.0
    pct_by_name = {
        g["name"]: g["pct"] for g in ethnicity_groups
        if isinstance(g.get("pct"), (int, float))
    }
    score = 0.0
    for group_name, weight in ethnicity_signals.items():
        pct = pct_by_name.get(group_name)
        if pct is None:
            continue
        score += (pct / 100.0) * weight
    return min(score, 1.0)


def _ethnicity_income_basis(ethnicity_signals: dict, ethnicity_groups: Optional[list],
                             income_by_ethnicity: Optional[dict]) -> list:
    """For every ethnicity group this cuisine has a weighted signal for,
    pair its population share (Tables B02001/B03003) with that SAME
    group's real median household income in this ZCTA (Table S1903), when
    available. Returns a list of dicts -- the structured basis for
    exactly how ethnicity + income-by-ethnicity are influencing this
    cuisine's score in THIS ZCTA, not a generic statement."""
    if not ethnicity_signals or not ethnicity_groups:
        return []
    pct_by_name = {g["name"]: g["pct"] for g in ethnicity_groups if isinstance(g.get("pct"), (int, float))}
    by_group_income = (income_by_ethnicity or {}).get("by_group", {})

    basis = []
    for group_name, weight in ethnicity_signals.items():
        pct = pct_by_name.get(group_name)
        if pct is None:
            continue
        income_label = ETHNICITY_TO_INCOME_LABEL.get(group_name)
        income_value = by_group_income.get(income_label) if income_label else None
        income_available = isinstance(income_value, (int, float))
        basis.append({
            "group": group_name,
            "population_pct": pct,
            "cuisine_signal_weight": weight,
            "demographic_fit_contribution": round((pct / 100.0) * weight, 4),
            "median_income_for_group": income_value if income_available else None,
            "median_income_available": income_available,
            "median_income_source": "ACS S1903 (Median Income by Race/Ethnicity of Householder)" if income_by_ethnicity else None,
        })
    return sorted(basis, key=lambda b: -b["demographic_fit_contribution"])


def _income_fit(income_position: str, family_pct: float, premium_pct: float, premium_edge_pct: float,
                 ethnicity_income_basis: Optional[list] = None) -> float:
    if income_position == "value":
        base = family_pct / 100.0
    elif income_position == "premium":
        base = min(1.0, (premium_pct * 0.5 + premium_edge_pct * 1.5) / 100.0)
    else:
        # "mixed" -- rewards a balanced ZCTA rather than any single tier
        base = max(0.0, 1.0 - abs(family_pct - premium_pct) / 100.0)

    # Median Income by Ethnicity is a required signal in its own right
    # (see module docstring), not just population share -- when the real
    # median income is available for the group(s) driving this cuisine's
    # demographic signal, it REFINES the ZCTA-wide income-bracket read
    # with that specific group's actual purchasing power, weighted 50/50
    # against the ZCTA-wide bracket rather than replacing it outright.
    available = [b for b in (ethnicity_income_basis or []) if b["median_income_available"]]
    if available:
        # weight each group's income signal by its demographic contribution
        total_w = sum(b["cuisine_signal_weight"] for b in available)
        weighted_income = sum(b["median_income_for_group"] * b["cuisine_signal_weight"] for b in available) / total_w
        if income_position == "value":
            group_component = max(0.0, min(1.0, (60000 - weighted_income) / 45000))
        elif income_position == "premium":
            group_component = max(0.0, min(1.0, (weighted_income - 55000) / 90000))
        else:
            group_component = max(0.0, min(1.0, 1.0 - abs(weighted_income - 65000) / 65000))
        return round(0.5 * base + 0.5 * group_component, 4)
    return round(base, 4)


def _local_demand_and_competition(categories: list, landscape_lookup: dict, total_restaurants: int):
    evidence_count = sum(landscape_lookup.get(c, 0) for c in categories)
    if total_restaurants <= 0:
        return 0.0, 0.5, evidence_count, 0.0  # no restaurant list -> neutral competition_fit
    share = evidence_count / total_restaurants
    # local_demand: some real local presence is evidence of proven demand,
    # saturating fast so 1-2 competitors already reads as "validated" --
    # matches this project's existing use of category presence as
    # local-market evidence (see dish_library.py module docstring).
    local_demand = min(1.0, evidence_count / 3.0)
    # competition_fit: inverse of saturation. A little competition is
    # healthy evidence (already folded into local_demand above); a LOT of
    # competition in one category is whitespace running out, and the
    # brief explicitly wants oversaturated categories penalized.
    if share >= SATURATION_SHARE:
        competition_fit = max(0.0, 1.0 - (share - SATURATION_SHARE) / (1.0 - SATURATION_SHARE))
    else:
        competition_fit = 1.0
    return round(local_demand, 4), round(competition_fit, 4), evidence_count, round(share, 4)


def _trend_fit(trend_lean: float, median_age: Optional[float], population_growth_rate: Optional[float]) -> float:
    # Younger median age and positive population growth both read as a
    # more trend-forward local customer base; older/flat-or-declining
    # reads as more traditional. Both are optional real inputs -- when
    # absent, trend_fit falls back to a flat neutral 0.5 x trend_lean
    # rather than fabricating a direction.
    age_component = 0.5
    if median_age is not None:
        # 25 -> 1.0 (very trend-forward), 55+ -> 0.0
        age_component = max(0.0, min(1.0, (55.0 - median_age) / 30.0))
    growth_component = 0.5
    if population_growth_rate is not None:
        growth_component = max(0.0, min(1.0, 0.5 + population_growth_rate * 10))
    return round(trend_lean * (0.6 * age_component + 0.4 * growth_component), 4)


def _reasoning(cuisine: str, cs_kwargs: dict, ethnicity_groups, family_pct, premium_pct, premium_edge_pct,
               ethnicity_income_basis: list) -> str:
    parts = []
    # ETHNICITY + INCOME-BY-ETHNICITY BASIS -- stated explicitly and
    # quantitatively for THIS ZCTA, not as a generic disclaimer. Shows
    # exactly which group(s), what population share, what weight this
    # cuisine assigns that group, and -- separately -- that group's real
    # median household income here, if the Census data has it.
    if ethnicity_income_basis:
        basis_bits = []
        for b in ethnicity_income_basis:
            income_bit = (
                f"median household income ${b['median_income_for_group']:,.0f} ({b['median_income_source']})"
                if b["median_income_available"]
                else "median income by ethnicity not available for this group at this geography (ACS suppressed/small-sample)"
            )
            basis_bits.append(
                f"{b['group']} is {b['population_pct']:.1f}% of this ZCTA's population (this cuisine "
                f"weights that group at {b['cuisine_signal_weight']:.2f}, contributing "
                f"{b['demographic_fit_contribution']:.3f} of the {cs_kwargs['demographic_fit']:.3f} "
                f"demographic_fit score); {income_bit}"
            )
        parts.append("Ethnicity + income-by-ethnicity basis: " + "; and ".join(basis_bits))
    elif cs_kwargs["demographic_fit"] == 0:
        parts.append("no ethnicity signal contributed to this cuisine's score in this ZCTA")

    if cs_kwargs["income_fit"] >= 0.5:
        parts.append(
            f"this ZCTA's overall income-bracket mix (Value {family_pct}% / Premium {premium_pct}% / "
            f"Premium Edge {premium_edge_pct}% of households) also fits this cuisine's typical price position"
        )
    if cs_kwargs["evidence_count"] > 0:
        parts.append(f"{cs_kwargs['evidence_count']} restaurant(s) already in this ZCTA's own list carry it")
    elif cs_kwargs["local_demand"] == 0:
        parts.append("no restaurants in this ZCTA's own list currently carry it (white-space, not disqualifying)")
    if cs_kwargs["competitor_share"] >= SATURATION_SHARE:
        parts.append(f"but {cs_kwargs['competitor_share']*100:.0f}% of local restaurants already compete in it (saturated)")
    if cs_kwargs["trend_fit"] > 0.2:
        parts.append("local age/growth profile favors trend-forward categories")
    if not parts:
        parts.append("weak evidence across every signal available for this ZCTA")
    return ". ".join(p[0].upper() + p[1:] for p in parts) + "."


def compute_cuisine_affinity(
    zcta: str,
    family_pct: float, premium_pct: float, premium_edge_pct: float,
    median_age: Optional[float],
    population_growth_rate: Optional[float],
    landscape_df,
    total_restaurants: int,
    ethnicity_composition: Optional[dict] = None,
    income_by_ethnicity: Optional[dict] = None,
) -> CuisineAffinityResult:
    """
    Runs BEFORE any menu item/dish is generated. landscape_df: the
    creation_engine.category_landscape() DataFrame (category, count).
    ethnicity_composition: market_data.fetch_ethnicity_composition() result
    dict, or None if that fetch failed (data-completeness is then noted,
    not silently substituted).c
    income_by_ethnicity: market_data.fetch_income_by_ethnicity() result
    dict, or None if that fetch failed/wasn't available. Used to refine
    income_fit with each cuisine's specific ethnicity group's real median
    income in this ZCTA (Table S1903) -- Median Income by Ethnicity is a
    required signal in its own right, not a stand-in for population share.

    Pipeline this function embodies:
      Ethnicity + Median Income by Ethnicity + Income bracket + Age/Growth
        + Local competition -> per-cuisine component scores
        -> weighted final_score -> ranked cuisines
        -> primary/secondary/supporting/avoid
    """
    landscape_lookup = dict(zip(landscape_df["category"], landscape_df["count"])) if landscape_df is not None else {}
    ethnicity_groups = ethnicity_composition.get("groups") if ethnicity_composition else None

    scored = []
    for cuisine, profile in CUISINE_PROFILES.items():
        demographic_fit = _demographic_fit(profile["ethnicity_signals"], ethnicity_groups)
        eth_income_basis = _ethnicity_income_basis(profile["ethnicity_signals"], ethnicity_groups, income_by_ethnicity)
        income_fit = _income_fit(profile["income_position"], family_pct, premium_pct, premium_edge_pct,
                                  ethnicity_income_basis=eth_income_basis)
        local_demand, competition_fit, evidence_count, competitor_share = _local_demand_and_competition(
            profile["categories"], landscape_lookup, total_restaurants)
        trend_fit = _trend_fit(profile["trend_lean"], median_age, population_growth_rate)

        final_score = round(
            WEIGHT_DEMOGRAPHIC_FIT * demographic_fit
            + WEIGHT_INCOME_FIT * income_fit
            + WEIGHT_LOCAL_DEMAND * local_demand
            + WEIGHT_COMPETITION_FIT * competition_fit
            + WEIGHT_TREND_FIT * trend_fit,
            4,
        )
        # Generic-fallback categories (Burgers/Wings/Sandwiches/Fast Food)
        # get no boost -- but per the brief they must be actively
        # de-prioritized against equally-plausible non-fallback cuisines
        # so they don't win ties by default. Small, explicit tie-break
        # penalty only, never enough to override real evidence.
        if profile["generic_fallback"]:
            final_score = round(final_score - 0.02, 4)

        kwargs = dict(
            demographic_fit=demographic_fit, income_fit=income_fit, local_demand=local_demand,
            competition_fit=competition_fit, trend_fit=trend_fit, evidence_count=evidence_count,
            competitor_share=competitor_share,
        )
        reasoning = _reasoning(cuisine, kwargs, ethnicity_groups, family_pct, premium_pct, premium_edge_pct,
                                eth_income_basis)

        scored.append(CuisineScore(
            cuisine=cuisine, categories=profile["categories"], final_score=final_score,
            reasoning=reasoning, ethnicity_income_basis=eth_income_basis, **kwargs,
        ))

    ranked = sorted(scored, key=lambda s: -s.final_score)
    primary = ranked[0] if ranked else None
    secondary = ranked[1] if len(ranked) > 1 else None
    supporting = ranked[2:4]
    avoid = [s for s in ranked if s.final_score < AVOID_SCORE_THRESHOLD]

    completeness_notes = []
    if ethnicity_groups is None:
        completeness_notes.append(
            "Ethnicity composition data was unavailable for this ZCTA -- demographic_fit scored 0 for "
            "every cuisine (Priority 4: neutral, not invented) rather than assuming a distribution."
        )
    if income_by_ethnicity is None:
        completeness_notes.append(
            "Median income by ethnicity (ACS S1903) was unavailable for this ZCTA -- income_fit used only "
            "the ZCTA-wide income-bracket mix, not a per-group refinement."
        )
    if not completeness_notes:
        completeness_notes.append("All primary ZCTA data sources used for this scoring were available.")

    return CuisineAffinityResult(
        zcta=zcta, ranked=ranked, primary=primary, secondary=secondary,
        supporting=supporting, avoid=avoid,
        data_completeness_note=" ".join(completeness_notes),
    )


if __name__ == "__main__":
    import pandas as pd

    landscape_hispanic = pd.DataFrame({"category": ["Burgers", "Mexican", "Fast Food"], "count": [2, 1, 3]})
    result_a = compute_cuisine_affinity(
        zcta="90001", family_pct=62.0, premium_pct=30.0, premium_edge_pct=8.0,
        median_age=29.5, population_growth_rate=0.02, landscape_df=landscape_hispanic,
        total_restaurants=20,
        ethnicity_composition={"groups": [
            {"name": "Hispanic or Latino", "pct": 68.4, "population": 30000},
            {"name": "White", "pct": 18.0, "population": 8000},
            {"name": "Black or African American", "pct": 6.0, "population": 2600},
        ]},
        income_by_ethnicity={"by_group": {
            "Hispanic or Latino (of any race)": 48000,
            "White alone": 61000,
            "Black or African American alone": 44000,
        }},
    )
    print("ZCTA 90001 (Hispanic-majority, value-tier) primary:", result_a.primary.cuisine, result_a.primary.final_score)
    print("  Reasoning:", result_a.primary.reasoning)

    landscape_asian = pd.DataFrame({"category": ["Sushi/Asian", "Italian", "Burgers"], "count": [3, 2, 1]})
    result_b = compute_cuisine_affinity(
        zcta="94043", family_pct=15.0, premium_pct=35.0, premium_edge_pct=50.0,
        median_age=34.0, population_growth_rate=0.03, landscape_df=landscape_asian,
        total_restaurants=25,
        ethnicity_composition={"groups": [
            {"name": "Asian", "pct": 55.0, "population": 20000},
            {"name": "White", "pct": 30.0, "population": 11000},
        ]},
        income_by_ethnicity={"by_group": {
            "Asian alone": 142000,
            "White alone": 118000,
        }},
    )
    print("ZCTA 94043 (Asian-majority, premium-tier) primary:", result_b.primary.cuisine, result_b.primary.final_score)
    print("  Reasoning:", result_b.primary.reasoning)

    landscape_south = pd.DataFrame({"category": ["Southern", "BBQ", "Burgers"], "count": [1, 1, 2]})
    result_c = compute_cuisine_affinity(
        zcta="38114", family_pct=70.0, premium_pct=25.0, premium_edge_pct=5.0,
        median_age=38.0, population_growth_rate=-0.01, landscape_df=landscape_south,
        total_restaurants=15,
        ethnicity_composition={"groups": [
            {"name": "Black or African American", "pct": 82.0, "population": 12000},
            {"name": "White", "pct": 12.0, "population": 1800},
        ]},
        income_by_ethnicity={"by_group": {
            "Black or African American alone": 31000,
            "White alone": 52000,
        }},
    )
    print("ZCTA 38114 (Black-majority, value-tier) primary:", result_c.primary.cuisine, result_c.primary.final_score)
    print("  Reasoning:", result_c.primary.reasoning)

    # No income-by-ethnicity data available -- should degrade gracefully,
    # never invent a value, and say so in the reasoning/completeness note.
    result_d = compute_cuisine_affinity(
        zcta="38114", family_pct=70.0, premium_pct=25.0, premium_edge_pct=5.0,
        median_age=38.0, population_growth_rate=-0.01, landscape_df=landscape_south,
        total_restaurants=15,
        ethnicity_composition={"groups": [
            {"name": "Black or African American", "pct": 82.0, "population": 12000},
        ]},
        income_by_ethnicity=None,
    )
    assert "not available" in result_d.primary.reasoning or "Median income by ethnicity" in result_d.data_completeness_note
    print("\nNo income-by-ethnicity data -> degrades gracefully: PASS")

    assert result_a.primary.cuisine != result_b.primary.cuisine != result_c.primary.cuisine
    print("Different ZCTAs produced different primary cuisines: PASS")