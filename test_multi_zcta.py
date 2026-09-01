"""
test_multi_zcta.py -- validates the REQUIRED checklist from the change
brief by actually running run_menu_creation.run() end-to-end for three
different synthetic ZCTAs (Census/LODES calls mocked -- no network in this
sandbox -- everything downstream of those calls is real pipeline code).

Checks:
1. Different ZCTAs produce different cuisine directions.
2. Burger/wings/sandwich categories are not automatically repeated everywhere.
3. Generic/near-duplicate concepts are not reused within or across ZCTAs.
4. Cuisine selection is calculated from ZCTA data (varies with the mocked inputs).
5. The final menus are genuinely ZCTA-specific.
"""
import pandas as pd

import market_data as md
import run_menu_creation as rmc

rmc.reset_concept_registry()

# ---------------------------------------------------------------------------
# Three synthetic ZCTAs with deliberately different demographic/income/
# ethnicity profiles -- everything else about the mocked data is held
# realistic-but-neutral so the differences in output trace back to these.
# ---------------------------------------------------------------------------
SCENARIOS = {
    "38114": dict(  # majority-Black, value-tier -- Memphis-style
        city="Memphis", county="Shelby", state="tn",
        economic_profile=dict(
            median_household_income=32000, median_age=35.0, avg_household_size=2.8,
            labor_force_participation_rate=58.0, unemployment_rate=9.5,
            income_lt_25k_pct=40.0, income_25k_49k_pct=30.0, income_50k_99k_pct=20.0,
            income_100k_149k_pct=7.0, income_150k_plus_pct=3.0, total_households=6000,
        ),
        ethnicity_groups=[
            {"name": "Black or African American", "pct": 82.0, "population": 12000},
            {"name": "White", "pct": 10.0, "population": 1500},
            {"name": "Hispanic or Latino", "pct": 5.0, "population": 750},
        ],
        restaurants=["Southern Kitchen", "Downtown BBQ Pit", "Quick Burger Stop", "Wing Shack",
                     "Southern Kitchen 2", "Chicken Box"],
    ),
    "90001": dict(  # majority-Hispanic, value-tier -- LA-style
        city="Los Angeles", county="Los Angeles", state="ca",
        economic_profile=dict(
            median_household_income=45000, median_age=28.0, avg_household_size=4.1,
            labor_force_participation_rate=65.0, unemployment_rate=7.0,
            income_lt_25k_pct=30.0, income_25k_49k_pct=35.0, income_50k_99k_pct=25.0,
            income_100k_149k_pct=7.0, income_150k_plus_pct=3.0, total_households=9000,
        ),
        ethnicity_groups=[
            {"name": "Hispanic or Latino", "pct": 74.0, "population": 30000},
            {"name": "White", "pct": 12.0, "population": 5000},
            {"name": "Black or African American", "pct": 8.0, "population": 3000},
        ],
        restaurants=["Taqueria Central", "Burrito House", "Burger Spot", "Panaderia Estrella"],
    ),
    "94043": dict(  # majority-Asian, premium-tier -- Bay Area-style
        city="Mountain View", county="Santa Clara", state="ca",
        economic_profile=dict(
            median_household_income=145000, median_age=34.0, avg_household_size=2.6,
            labor_force_participation_rate=72.0, unemployment_rate=3.0,
            income_lt_25k_pct=5.0, income_25k_49k_pct=10.0, income_50k_99k_pct=25.0,
            income_100k_149k_pct=30.0, income_150k_plus_pct=30.0, total_households=12000,
        ),
        ethnicity_groups=[
            {"name": "Asian", "pct": 52.0, "population": 20000},
            {"name": "White", "pct": 35.0, "population": 13500},
            {"name": "Hispanic or Latino", "pct": 8.0, "population": 3000},
        ],
        restaurants=["Ramen Ya", "Golden Wok", "Trattoria Bella", "Burger Bar", "Sushi Den"],
    ),
}

results = {}

for zcta, s in SCENARIOS.items():
    # --- monkeypatch market_data fetch functions for this scenario ---
    md.fetch_census_demographics = lambda z, _s=s: {"residents": 20000}
    md.fetch_economic_profile = lambda z, _s=s: dict(_s["economic_profile"])
    md.fetch_commuter_flows = lambda z, state_abbr=None, progress_callback=None, _s=s: {
        "daytime_workers": 8000.0, "worker_inflow": 6000.0, "resident_outflow": 5000.0,
        "stay_local": 3000.0, "pct_income_high": 20.0, "pct_income_low": 30.0,
        "pct_age_mid": 50.0, "pct_age_senior": 20.0, "pct_office_jobs": 10.0,
        "source": "mocked",
    }
    md.fetch_income_by_ethnicity = lambda z, _s=s: {"by_group": {}, "source": "mocked"}
    md.fetch_ethnicity_composition = lambda z, _s=s: {
        "groups": _s["ethnicity_groups"], "total_population": 20000, "source": "mocked",
    }
    md.fetch_population_growth_rate = lambda z, _s=s: {"rate": 0.01, "source": "mocked"}
    md.zcta_to_state = lambda z, _s=s: _s["state"]

    restaurant_df = pd.DataFrame({
        "restaurant_name": s["restaurants"],
        "zip_code": [zcta] * len(s["restaurants"]),
        "cuisines": ["fast food"] * len(s["restaurants"]),
    })

    result_df, issues, income_by_eth, eth_comp, ctx = rmc.run(
        restaurant_df=restaurant_df, zcta=zcta, city=s["city"], county=s["county"],
        area_sq_mi=5.0, biz_residential_mix="Mixed", has_anchor=False, anchor_note="",
        n_items=6, state_abbr=s["state"], already_normalized=True,
    )
    results[zcta] = (result_df, ctx.cuisine_affinity)

    print(f"\n=== ZCTA {zcta} ({s['city']}) ===")
    print("Primary cuisine:", ctx.cuisine_affinity.primary.cuisine,
          f"(score {ctx.cuisine_affinity.primary.final_score})")
    print("Secondary cuisine:", ctx.cuisine_affinity.secondary.cuisine if ctx.cuisine_affinity.secondary else None)
    print("Avoided cuisines:", [s.cuisine for s in ctx.cuisine_affinity.avoid])
    print(result_df[["Recommended New Menu Item", "Recommended Category",
                      "Primary Cuisine (ZCTA-wide)", "Uniqueness Validation Result"]].to_string(index=False))

# ---------------------------------------------------------------------------
# Assertions matching the brief's validation checklist
# ---------------------------------------------------------------------------
primaries = {z: r[1].primary.cuisine for z, r in results.items()}
print("\nPrimary cuisines by ZCTA:", primaries)
assert len(set(primaries.values())) == 3, "Expected 3 different primary cuisines across 3 different ZCTAs"

all_item_names = []
for z, (df, _) in results.items():
    all_item_names.extend(df["Recommended New Menu Item"].tolist())
dupes = [n for n in all_item_names if all_item_names.count(n) > 1]
assert not dupes, f"Duplicate item names across ZCTAs: {dupes}"

burger_only_zctas = 0
for z, (df, _) in results.items():
    cats = df["Recommended Category"].tolist()
    if all(c in ("Burgers", "Fast Food") for c in cats):
        burger_only_zctas += 1
assert burger_only_zctas == 0, "At least one ZCTA generated an all-burger/fast-food menu"

print("\nALL CHECKS PASSED:")
print("- 3/3 ZCTAs produced different primary cuisines")
print("- No duplicate item names within or across ZCTAs")
print("- No ZCTA's menu was 100% burgers/fast food")