import csv
import gzip
import io
import json
import os
import pickle
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

ACS_YEAR = 2023  # most recent 5-year ACS release as of writing; bump as new releases ship
LODES_VERSION = "LODES8"
LODES_YEAR = 2021  # match whatever year your reference numbers came from
LODES_STREAM_TIMEOUT_SECONDS = 180  # per OD file (main/aux) -- see fetch_commuter_flows
LODES_JOB_TYPE = "JT00"  # all jobs
LODES_BASE_URL = f"https://lehd.ces.census.gov/data/lodes/{LODES_VERSION}"

TIGERWEB_ZCTA_POP_QUERY_URLS = (
    ("TIGERweb 2020 ZCTAs",
     "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1/query"),
)


class MarketDataError(Exception):
    pass


def _load_local_env():
    """Load KEY=VALUE pairs from a local .env file without overriding env vars."""
    env_path = Path(__file__).resolve().with_name(".env")
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value
    except OSError:
        return


_load_local_env()


def _get_census_api_key():
    return os.environ.get('CENSUS_API_KEY', '').strip()


def _short_snippet(text, limit=240):
    text = (text or "").strip().replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _make_request(url, accept="application/json"):
    return urllib.request.Request(url, headers={
        "User-Agent": "menu-intelligence-app/1.0",
        "Accept": accept,
    })


def _request_json(url, params, timeout=60, label="Request"):
    qs = urllib.parse.urlencode(params)
    full_url = f"{url}?{qs}"
    request = _make_request(full_url, accept="application/json, application/geo+json;q=0.9, */*;q=0.1")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            text = resp.read().decode('utf-8-sig', errors='replace')
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise MarketDataError(
                    f"{label} returned non-JSON data (content-type="
                    f"{resp.headers.get('Content-Type','')!r}, body={_short_snippet(text)!r})."
                ) from e
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode('utf-8', errors='replace')
        except Exception:
            pass
        raise MarketDataError(f"{label} HTTP error ({e.code}) for {url}: {_short_snippet(body) or e.reason}") from e
    except urllib.error.URLError as e:
        raise MarketDataError(f"{label} request failed ({url}): {e.reason}") from e


# ----------------------------------------------------------------------
# Census ACS5 — resident population. Solid, documented, simple endpoint.
# ----------------------------------------------------------------------
ACS_VARS = {'population': 'B01003_001E'}


def _validate_zcta(zcta):
    zcta = str(zcta).strip()
    if not zcta.isdigit() or len(zcta) != 5:
        raise MarketDataError(f"ZCTA must be a 5-digit ZIP code, got {zcta!r}.")
    return zcta


def fetch_census_demographics(zcta):
    """Returns {'residents': float, 'residents_source': str}. Tries a
    couple of recent ACS 5-year vintages before falling back to TIGERweb —
    the very latest ACS5 vintage sometimes isn't published for ZCTAs yet
    even after the year rolls over, so a single hardcoded year can fail
    for a reason that has nothing to do with the ZCTA itself."""
    zcta = _validate_zcta(zcta)
    acs_errors = []
    for year in (ACS_YEAR, ACS_YEAR - 1, ACS_YEAR - 2):
        url = f"https://api.census.gov/data/{year}/acs/acs5"
        params = {'get': ACS_VARS['population'], 'for': f'zip code tabulation area:{zcta}'}
        if _get_census_api_key():
            params['key'] = _get_census_api_key()
        try:
            data = _request_json(url, params, timeout=20, label=f"Census ACS5 {year}")
            if not data or len(data) < 2:
                raise MarketDataError(f"Census ACS5 {year} returned no rows for ZCTA {zcta}.")
            row = dict(zip(data[0], data[1]))
            return {'residents': float(row[ACS_VARS['population']]), 'residents_source': f'ACS {year} B01003_001E'}
        except MarketDataError as e:
            acs_errors.append(str(e))
            continue

    try:
        pop = _fetch_tigerweb_zcta_population(zcta)
        return {'residents': pop, 'residents_source': 'TIGERweb 2020 POP100 fallback'}
    except MarketDataError as fallback_error:
        raise MarketDataError(
            f"Census ACS lookup failed for years {ACS_YEAR}/{ACS_YEAR-1}/{ACS_YEAR-2} "
            f"({'; '.join(acs_errors)}) and the TIGERweb fallback also failed "
            f"({fallback_error})."
            ) from fallback_error


# ----------------------------------------------------------------------
# Census ACS5 -- economic profile (median income, age, household size,
# labor force, unemployment, income brackets). Same retry-year pattern,
# same _request_json/_validate_zcta helpers as fetch_census_demographics.
# ----------------------------------------------------------------------

ACS_VARS_ADDITIONS = {
    'median_household_income': 'B19013_001E',
    'median_age': 'B01002_001E',
    'avg_household_size': 'B25010_001E',
    'labor_force_total': 'B23025_002E',
    'pop_16_plus': 'B23025_001E',
    'employed': 'B23025_004E',
    'unemployed': 'B23025_005E',
}

# Household income bracket variables (B19001), rolled up to the same
# 5-bucket convention already used in the ground-truth 38114 workbook:
#   income_lt_25k_pct    = brackets 002-005  (<$25,000)
#   income_25k_49k_pct   = brackets 006-010  ($25,000-$49,999)
#   income_50k_99k_pct   = brackets 011-013  ($50,000-$99,999)
#   income_100k_149k_pct = brackets 014-015  ($100,000-$149,999)
#   income_150k_plus_pct = brackets 016-017  ($150,000+)
INCOME_BRACKET_VARS = {
    'income_lt_25k_pct':     ['B19001_002E', 'B19001_003E', 'B19001_004E', 'B19001_005E'],
    'income_25k_49k_pct':    ['B19001_006E', 'B19001_007E', 'B19001_008E', 'B19001_009E', 'B19001_010E'],
    'income_50k_99k_pct':    ['B19001_011E', 'B19001_012E', 'B19001_013E'],
    'income_100k_149k_pct':  ['B19001_014E', 'B19001_015E'],
    'income_150k_plus_pct':  ['B19001_016E', 'B19001_017E'],
}
INCOME_TOTAL_VAR = 'B19001_001E'

_ALL_ECONOMIC_VARS = (
    list(ACS_VARS_ADDITIONS.values())
    + [v for group in INCOME_BRACKET_VARS.values() for v in group]
    + [INCOME_TOTAL_VAR]
)


def fetch_economic_profile(zcta):
    """
    Returns the ACS5 fields Menu Creation's ZCTA context block needs,
    beyond what fetch_census_demographics() already covers:

      median_household_income, median_age, avg_household_size,
      labor_force_participation_rate, unemployment_rate,
      total_households,
      income_lt_25k_count, income_25k_49k_count, income_50k_99k_count,
      income_100k_149k_count, income_150k_plus_count,
      income_lt_25k_pct, income_25k_49k_pct, income_50k_99k_pct,
      income_100k_149k_pct, income_150k_plus_pct, source

    Same retry-year fallback as fetch_census_demographics (the very
    latest ACS5 vintage sometimes isn't published for ZCTAs yet), same
    _validate_zcta / _request_json / _get_census_api_key plumbing --
    no new HTTP pattern introduced.
    """
    zcta = _validate_zcta(zcta)
    errors = []
    for year in (ACS_YEAR, ACS_YEAR - 1, ACS_YEAR - 2):
        url = f"https://api.census.gov/data/{year}/acs/acs5"
        params = {'get': ",".join(_ALL_ECONOMIC_VARS), 'for': f'zip code tabulation area:{zcta}'}
        if _get_census_api_key():
            params['key'] = _get_census_api_key()
        try:
            data = _request_json(url, params, timeout=20, label=f"Census ACS5 economic profile {year}")
            if not data or len(data) < 2:
                raise MarketDataError(f"Census ACS5 {year} returned no rows for ZCTA {zcta}.")
            row = dict(zip(data[0], data[1]))

            def f(varname):
                try:
                    return float(row.get(varname) or 0)
                except (TypeError, ValueError):
                    return 0.0

            total_hh = f(INCOME_TOTAL_VAR)
            raw_brackets = {
                name.replace("_pct", "_count"): int(round(sum(f(v) for v in varlist)))
                for name, varlist in INCOME_BRACKET_VARS.items()
            }
            pct_brackets = {
                name: round(100.0 * raw_brackets[name.replace("_pct", "_count")] / total_hh, 2)
                for name in INCOME_BRACKET_VARS
            } if total_hh else {name: 0.0 for name in INCOME_BRACKET_VARS}

            labor_force_total = f(ACS_VARS_ADDITIONS['labor_force_total'])
            pop_16_plus = f(ACS_VARS_ADDITIONS['pop_16_plus']) or 1.0
            employed = f(ACS_VARS_ADDITIONS['employed'])
            unemployed = f(ACS_VARS_ADDITIONS['unemployed'])
            labor_denom = employed + unemployed

            return {
                'median_household_income': f(ACS_VARS_ADDITIONS['median_household_income']),
                'median_age': f(ACS_VARS_ADDITIONS['median_age']),
                'avg_household_size': f(ACS_VARS_ADDITIONS['avg_household_size']),
                'labor_force_participation_rate': round(100.0 * labor_force_total / pop_16_plus, 1),
                'unemployment_rate': round(100.0 * unemployed / labor_denom, 1) if labor_denom else 0.0,
                'total_households': int(round(total_hh)) if total_hh else 0,
                **raw_brackets,
                **pct_brackets,
                'source': f'ACS {year} B19013/B01002/B25010/B23025/B19001',
            }
        except MarketDataError as e:
            errors.append(str(e))
            continue

    raise MarketDataError(
        f"Census ACS economic-profile lookup failed for years "
        f"{ACS_YEAR}/{ACS_YEAR-1}/{ACS_YEAR-2} for ZCTA {zcta}: {'; '.join(errors)}"
    )


def fetch_population_growth_rate(zcta):
    """Population growth rate: (current ACS5 total population - population
    5 years earlier) / population 5 years earlier, both from the same
    B01003_001E variable. Two real ACS vintages, not a single-point
    estimate. Returns {'rate': float_or_None, 'source': str}; rate is None
    (never 0.0) if either vintage's fetch fails, so a real "Data Not
    Available" state is never silently reported as "no change"."""
    zcta = _validate_zcta(zcta)
    try:
        current = fetch_census_demographics(zcta)
    except MarketDataError as e:
        return {'rate': None, 'source': f'Unavailable: {e}'}

    earlier_pop = None
    earlier_year = None
    for year in (ACS_YEAR - 5, ACS_YEAR - 6, ACS_YEAR - 4):
        url = f"https://api.census.gov/data/{year}/acs/acs5"
        params = {'get': ACS_VARS['population'], 'for': f'zip code tabulation area:{zcta}'}
        if _get_census_api_key():
            params['key'] = _get_census_api_key()
        try:
            data = _request_json(url, params, timeout=20, label=f"Census ACS5 {year} (population, prior vintage)")
            if data and len(data) >= 2:
                row = dict(zip(data[0], data[1]))
                earlier_pop = float(row[ACS_VARS['population']])
                earlier_year = year
                break
        except MarketDataError:
            continue

    if earlier_pop is None or not earlier_pop:
        return {'rate': None, 'source': f'Unavailable: no prior ACS5 vintage (~{ACS_YEAR-5}) returned data for ZCTA {zcta}.'}

    rate = round((current['residents'] - earlier_pop) / earlier_pop, 4)
    return {
        'rate': rate,
        'source': f"ACS {ACS_YEAR} vs ACS {earlier_year}, B01003_001E ({earlier_pop:,.0f} -> {current['residents']:,.0f})",
    }


# ----------------------------------------------------------------------
# Census ACS5 Subject Tables -- median household income by race/ethnicity
# of householder (Table S1903). Folded in from acs_income_by_race.py,
# using this module's existing _validate_zcta/_request_json/MarketDataError
# plumbing instead of that script's standalone urllib calls, so it behaves
# like every other fetch_* function here (same retry-year pattern, same
# .env-sourced API key, same error type).
#
# api.census.gov hard-requires a registered key on every subject-table
# call (confirmed by direct testing -- see acs_income_by_race.py's
# docstring) -- unlike the detail-table calls above, which have
# historically tolerated no key at low volume. This function raises
# immediately with a clear message if no key is configured, rather than
# letting the request fail with a generic "Missing Key" HTTP body.
# ----------------------------------------------------------------------

ACS_SUBJECT_YEAR = 2023  # most recent 5-year ACS subject-table vintage as of writing

# S1903 variable codes -> plain-language group labels. C03 = "Median
# household income (dollars)" column of table S1903.
S1903_VARS = {
    "S1903_C03_001E": "Overall (all races/ethnicities)",
    "S1903_C03_002E": "White alone",
    "S1903_C03_003E": "Black or African American alone",
    "S1903_C03_004E": "American Indian and Alaska Native alone",
    "S1903_C03_005E": "Asian alone",
    "S1903_C03_006E": "Native Hawaiian and Other Pacific Islander alone",
    "S1903_C03_007E": "Some other race alone",
    "S1903_C03_008E": "Two or more races",
    "S1903_C03_009E": "Hispanic or Latino (of any race)",
    "S1903_C03_010E": "White alone, not Hispanic or Latino",
}

# Census uses specific sentinel codes for "not available" instead of null:
#   -666666666  -> estimate could not be computed (too few sample cases)
#   null/None   -> not applicable / suppressed
# Any of these are treated as Data Not Available for that subgroup only --
# never for the whole table.
NOT_AVAILABLE_SENTINELS = {None, "", "null", "-666666666", "-999999999", "N", "(X)"}


def fetch_income_by_ethnicity(zcta):
    """Median household income by race/ethnicity of householder (ACS
    Table S1903) for one ZCTA.

    Returns {'by_group': {label: int_or_'Data Not Available'}, 'source': str}.
    Per-subgroup granularity: if the Census API returns a suppressed/null
    value for one group (common at ZCTA geography for small subgroups),
    only that subgroup is marked 'Data Not Available' -- every other
    subgroup that DID come back is still reported. Raises MarketDataError
    only on a genuine request/auth failure (including no API key
    configured), never because one subgroup was suppressed.
    """
    zcta = _validate_zcta(zcta)

    if not _get_census_api_key():
        raise MarketDataError(
            "Table S1903 (income by race/ethnicity) requires a registered Census API key on "
            "every request -- api.census.gov returns 'Missing Key' otherwise. Set "
            "CENSUS_API_KEY in the environment or .env file. A free key is available at "
            "https://api.census.gov/data/key_signup.html (arrives by email, no wait)."
        )

    var_codes = list(S1903_VARS.keys())
    errors = []
    for year in (ACS_SUBJECT_YEAR, ACS_SUBJECT_YEAR - 1, ACS_SUBJECT_YEAR - 2):
        url = f"https://api.census.gov/data/{year}/acs/acs5/subject"
        params = {
            'get': "NAME," + ",".join(var_codes),
            'for': f'zip code tabulation area:{zcta}',
            'key': _get_census_api_key(),
        }
        try:
            data = _request_json(url, params, timeout=30, label=f"Census ACS5 S1903 {year}")
            if not data or len(data) < 2:
                raise MarketDataError(f"Census ACS5 S1903 {year} returned no rows for ZCTA {zcta}.")
            row_map = dict(zip(data[0], data[1]))

            by_group = {}
            for code, label in S1903_VARS.items():
                value = row_map.get(code)
                if value in NOT_AVAILABLE_SENTINELS:
                    by_group[label] = "Data Not Available"
                else:
                    try:
                        by_group[label] = int(float(value))
                    except (TypeError, ValueError):
                        by_group[label] = "Data Not Available"

            return {
                'by_group': by_group,
                'zcta_name': row_map.get('NAME', zcta),
                'source': f'ACS {year} 5-Year, Table S1903 (Median Income by Race/Ethnicity of Householder)',
            }
        except MarketDataError as e:
            errors.append(str(e))
            continue

    raise MarketDataError(
        f"Census ACS S1903 lookup failed for years "
        f"{ACS_SUBJECT_YEAR}/{ACS_SUBJECT_YEAR-1}/{ACS_SUBJECT_YEAR-2} for ZCTA {zcta}: "
        f"{'; '.join(errors)}"
    )


# ----------------------------------------------------------------------
# Census ACS5 detail tables -- race/ethnicity population composition
# (Table B02001 for race alone, Table B03003 for Hispanic-or-Latino
# ethnicity, which is asked and tabulated separately from race by the
# Census Bureau and can overlap any race category). This is the
# population-share counterpart to fetch_income_by_ethnicity() above --
# income and population composition are two different ACS tables and are
# fetched separately, same as the reference workbook's ETHNICITY object
# keeps them as two distinct components.
# ----------------------------------------------------------------------

B02001_RACE_VARS = {
    'B02001_002E': 'White',
    'B02001_003E': 'Black or African American',
    'B02001_004E': 'American Indian / Alaska Native',
    'B02001_005E': 'Asian',
    'B02001_006E': 'Native Hawaiian / Other Pacific Islander',
    'B02001_007E': 'Some Other Race',
    'B02001_008E': 'Two or More Races',
}
B02001_TOTAL_VAR = 'B02001_001E'

B03003_HISPANIC_VAR = 'B03003_003E'   # Hispanic or Latino (any race)
B03003_TOTAL_VAR = 'B03003_001E'


def fetch_ethnicity_composition(zcta):
    """Race/ethnicity population composition for one ZCTA (ACS Tables
    B02001 -- race alone -- and B03003 -- Hispanic-or-Latino ethnicity,
    which overlaps race and is reported separately per Census convention).

    Returns:
        {
          'groups': [{'name': str, 'population': int, 'pct': float, 'source': str}, ...],
          'total_population': int,
          'source': str,
        }
    Groups are sorted descending by population share, matching the
    reference report's bar-chart ordering. Hispanic/Latino is appended
    with note='Ethnicity, overlaps race categories' via the caller (see
    creation_engine/report layer) since it is not mutually exclusive with
    the race rows above -- the Census Bureau tabulates it as a separate
    question, not a race category.

    If a subgroup count is null/suppressed, its pct is 'Data Not Available'
    rather than 0 -- a suppressed subgroup should never silently read as
    "no one here," which is a different (false) claim than "not published."
    """
    zcta = _validate_zcta(zcta)
    race_vars = list(B02001_RACE_VARS.keys())
    all_vars = race_vars + [B02001_TOTAL_VAR, B03003_HISPANIC_VAR, B03003_TOTAL_VAR]

    errors = []
    for year in (ACS_YEAR, ACS_YEAR - 1, ACS_YEAR - 2):
        url = f"https://api.census.gov/data/{year}/acs/acs5"
        params = {'get': ",".join(all_vars), 'for': f'zip code tabulation area:{zcta}'}
        if _get_census_api_key():
            params['key'] = _get_census_api_key()
        try:
            data = _request_json(url, params, timeout=20, label=f"Census ACS5 race/ethnicity {year}")
            if not data or len(data) < 2:
                raise MarketDataError(f"Census ACS5 {year} returned no rows for ZCTA {zcta}.")
            row = dict(zip(data[0], data[1]))

            def raw(varname):
                return row.get(varname)

            def as_int_or_none(varname):
                v = raw(varname)
                if v in NOT_AVAILABLE_SENTINELS:
                    return None
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return None

            total_pop = as_int_or_none(B02001_TOTAL_VAR)
            groups = []
            for code, label in B02001_RACE_VARS.items():
                count = as_int_or_none(code)
                if count is None or not total_pop:
                    groups.append({'name': label, 'population': 'Data Not Available',
                                    'pct': 'Data Not Available', 'source': f'ACS {year} {code}'})
                else:
                    groups.append({'name': label, 'population': count,
                                    'pct': round(100.0 * count / total_pop, 2),
                                    'source': f'ACS {year} {code} / {B02001_TOTAL_VAR}'})

            hisp_total = as_int_or_none(B03003_TOTAL_VAR)
            hisp_count = as_int_or_none(B03003_HISPANIC_VAR)
            if hisp_count is None or not hisp_total:
                groups.append({'name': 'Hispanic or Latino', 'population': 'Data Not Available',
                                'pct': 'Data Not Available', 'source': f'ACS {year} {B03003_HISPANIC_VAR}',
                                'note': 'Ethnicity, overlaps race categories'})
            else:
                groups.append({'name': 'Hispanic or Latino', 'population': hisp_count,
                                'pct': round(100.0 * hisp_count / hisp_total, 2),
                                'source': f'ACS {year} {B03003_HISPANIC_VAR} / {B03003_TOTAL_VAR}',
                                'note': 'Ethnicity, overlaps race categories'})

            groups_sorted = sorted(
                groups,
                key=lambda g: g['pct'] if isinstance(g['pct'], (int, float)) else -1,
                reverse=True,
            )

            return {
                'groups': groups_sorted,
                'total_population': total_pop if total_pop is not None else 'Data Not Available',
                'source': f'ACS {year} 5-Year, Tables B02001 (race) + B03003 (Hispanic/Latino origin)',
            }
        except MarketDataError as e:
            errors.append(str(e))
            continue

    raise MarketDataError(
        f"Census ACS race/ethnicity composition lookup failed for years "
        f"{ACS_YEAR}/{ACS_YEAR-1}/{ACS_YEAR-2} for ZCTA {zcta}: {'; '.join(errors)}"
    )


# ----------------------------------------------------------------------
# LODES / ZCTA helpers.
# ----------------------------------------------------------------------
ZCTA_BLOCK_RELATIONSHIP_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_tabblock20_natl.txt"
)

_STATE_FIPS_TO_ABBR = {
    "01": "al",
    "02": "ak",
    "04": "az",
    "05": "ar",
    "06": "ca",
    "08": "co",
    "09": "ct",
    "10": "de",
    "11": "dc",
    "12": "fl",
    "13": "ga",
    "15": "hi",
    "16": "id",
    "17": "il",
    "18": "in",
    "19": "ia",
    "20": "ks",
    "21": "ky",
    "22": "la",
    "23": "me",
    "24": "md",
    "25": "ma",
    "26": "mi",
    "27": "mn",
    "28": "ms",
    "29": "mo",
    "30": "mt",
    "31": "ne",
    "32": "nv",
    "33": "nh",
    "34": "nj",
    "35": "nm",
    "36": "ny",
    "37": "nc",
    "38": "nd",
    "39": "oh",
    "40": "ok",
    "41": "or",
    "42": "pa",
    "44": "ri",
    "45": "sc",
    "46": "sd",
    "47": "tn",
    "48": "tx",
    "49": "ut",
    "50": "vt",
    "51": "va",
    "53": "wa",
    "54": "wv",
    "55": "wi",
    "56": "wy",
    "60": "as",
    "66": "gu",
    "69": "mp",
    "72": "pr",
    "78": "vi",
}


def _cache_dir():
    cache_dir = Path(__file__).resolve().with_name(".census_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _candidate_uszips_paths():
    candidates = []
    env_value = (
        os.environ.get("USZIPS_CSV_PATH")
        or os.environ.get("SIMPLEMAPS_USZIPS_CSV")
        or os.environ.get("SIMPLEMAPS_USZIPS_PATH")
    )
    if env_value:
        env_path = Path(env_value).expanduser()
        candidates.append(env_path / "uszips.csv" if env_path.is_dir() else env_path)

    project_root = Path(__file__).resolve().parent
    candidates.extend([
        project_root / "uszips.csv",
        project_root / "simplemaps_uszips_basicv1.94" / "uszips.csv",
        Path.home() / "Downloads" / "simplemaps_uszips_basicv1.94" / "uszips.csv",
        Path.home() / "Downloads" / "uszips.csv",
    ])

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


@lru_cache(maxsize=1)
def _load_uszips_df():
    for candidate in _candidate_uszips_paths():
        if not candidate.is_file():
            continue
        try:
            df = pd.read_csv(candidate, usecols=["zip", "state_id"], dtype=str, low_memory=False)
        except Exception as exc:
            raise MarketDataError(f"Failed to read ZIP-to-state lookup file at {candidate}: {exc}") from exc
        df["zip"] = df["zip"].fillna("").astype(str).str.zfill(5)
        df["state_id"] = df["state_id"].fillna("").astype(str).str.strip().str.lower()
        return df
    raise MarketDataError(
        "Could not locate simplemaps uszips.csv. Set USZIPS_CSV_PATH or place "
        "simplemaps_uszips_basicv1.94/uszips.csv in Downloads."
    )


def _state_abbr_from_fips(fips):
    return _STATE_FIPS_TO_ABBR.get(str(fips).strip().zfill(2))


def _infer_state_from_blocks(blocks):
    counts = Counter()
    for block in blocks:
        block = str(block).strip()
        if len(block) < 2:
            continue
        state = _state_abbr_from_fips(block[:2])
        if state:
            counts[state] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


@lru_cache(maxsize=256)
def _load_zcta_blocks(zcta):
    zcta = _validate_zcta(zcta)
    cache_path = _cache_dir() / f"zcta_blocks_rel2020_{zcta}.pkl"
    if cache_path.is_file():
        try:
            with cache_path.open("rb") as handle:
                cached = pickle.load(handle)
            if cached:
                return frozenset(str(block).strip().zfill(15) for block in cached if str(block).strip())
        except Exception:
            pass

    request = _make_request(ZCTA_BLOCK_RELATIONSHIP_URL, accept="text/plain, */*;q=0.1")
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            text_stream = io.TextIOWrapper(resp, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text_stream, delimiter="|")
            field_lookup = {name.strip().upper(): name for name in (reader.fieldnames or []) if name}
            zcta_field = field_lookup.get("GEOID_ZCTA5_20")
            block_field = field_lookup.get("GEOID_TABBLOCK_20")
            if not zcta_field or not block_field:
                raise MarketDataError(
                    "Unexpected Census relationship file header; expected GEOID_ZCTA5_20 and GEOID_TABBLOCK_20."
                )

            blocks = set()
            for row in reader:
                if str(row.get(zcta_field, "")).strip().zfill(5) != zcta:
                    continue
                block = str(row.get(block_field, "")).strip()
                if len(block) == 15 and block.isdigit():
                    blocks.add(block)

        if not blocks:
            raise MarketDataError(f"No Census tabulation blocks were found for ZCTA {zcta}.")

        try:
            with cache_path.open("wb") as handle:
                pickle.dump(frozenset(blocks), handle, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass
        return frozenset(blocks)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise MarketDataError(
            f"ZCTA-to-block relationship file HTTP error ({e.code}) for ZCTA {zcta}: "
            f"{_short_snippet(body) or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise MarketDataError(
            f"ZCTA-to-block relationship file request failed for ZCTA {zcta}: {e.reason}"
        ) from e


def _fetch_tigerweb_zcta_population(zcta):
    zcta = _validate_zcta(zcta)
    errors = []

    for label, url in TIGERWEB_ZCTA_POP_QUERY_URLS:
        params = {
            "where": f"ZCTA5='{zcta}'",
            "outFields": "POP100",
            "returnGeometry": "false",
            "f": "json",
        }
        try:
            data = _request_json(url, params, timeout=30, label=label)
            features = data.get("features") if isinstance(data, dict) else None
            if features:
                attrs = (features[0] or {}).get("attributes", {}) or {}
                pop = attrs.get("POP100")
                if pop not in (None, ""):
                    return float(pop)
            errors.append(f"{label} returned no POP100 value for ZCTA {zcta}.")
        except MarketDataError as e:
            errors.append(str(e))

    # If TIGERweb is unavailable, fall back to the 2020 decennial Census API.
    try:
        url = "https://api.census.gov/data/2020/dec/pl"
        params = {"get": "P1_001N", "for": f"zip code tabulation area:{zcta}"}
        if _get_census_api_key():
            params["key"] = _get_census_api_key()
        data = _request_json(url, params, timeout=20, label="Census 2020 decennial population")
        if not data or len(data) < 2:
            raise MarketDataError(f"Census 2020 decennial population returned no rows for ZCTA {zcta}.")
        row = dict(zip(data[0], data[1]))
        pop = row.get("P1_001N")
        if pop in (None, ""):
            raise MarketDataError(f"Census 2020 decennial population returned no population for ZCTA {zcta}.")
        return float(pop)
    except MarketDataError as e:
        errors.append(str(e))

    raise MarketDataError(f"Population lookup failed for ZCTA {zcta}: {'; '.join(errors)}")


def zcta_to_state(zcta):
    zcta = _validate_zcta(zcta)
    try:
        df = _load_uszips_df()
        matches = df[df["zip"] == zcta]
        if not matches.empty:
            states = matches["state_id"].dropna().astype(str).str.strip().str.lower()
            states = states[states != ""]
            if not states.empty:
                return states.value_counts().idxmax()
    except MarketDataError:
        pass

    try:
        return _infer_state_from_blocks(_load_zcta_blocks(zcta))
    except MarketDataError:
        return None


def _row_float(row, field_name):
    try:
        return float(row.get(field_name) or 0)
    except (TypeError, ValueError):
        return 0.0


def _lodes_od_url(state_abbr, part):
    state_abbr = str(state_abbr).strip().lower()
    return f"{LODES_BASE_URL}/{state_abbr}/od/{state_abbr}_od_{part}_{LODES_JOB_TYPE}_{LODES_YEAR}.csv.gz"


def fetch_commuter_flows(zcta, state_abbr=None, progress_callback=None):
    zcta = _validate_zcta(zcta)
    progress_callback = progress_callback or (lambda *args, **kwargs: None)

    blocks = _load_zcta_blocks(zcta)
    state_abbr = (state_abbr or zcta_to_state(zcta) or "").strip().lower()
    if not state_abbr:
        state_abbr = _infer_state_from_blocks(blocks) or ""
    if not state_abbr or len(state_abbr) != 2 or not state_abbr.isalpha():
        raise MarketDataError(
            f"Couldn't determine a two-letter state abbreviation for ZCTA {zcta}."
        )

    cache_path = _cache_dir() / f"commuter_{state_abbr}_{zcta}_{LODES_VERSION}_{LODES_YEAR}.pkl"
    if cache_path.is_file():
        try:
            with cache_path.open("rb") as handle:
                cached = pickle.load(handle)
            if isinstance(cached, dict):
                progress_callback(f"Loaded cached commuter flow data for ZCTA {zcta}.", 1.0)
                return cached
        except Exception:
            pass

    progress_callback(f"Fetching Census block crosswalk for ZCTA {zcta}...", 0.15)
    blocks = set(blocks)
    if not blocks:
        raise MarketDataError(f"No Census tabulation blocks were found for ZCTA {zcta}.")

    flow_totals = {
        "daytime_workers": 0.0,
        "worker_inflow": 0.0,
        "resident_outflow": 0.0,
        "stay_local": 0.0,
        "inflow_sa01": 0.0,
        "inflow_sa02": 0.0,
        "inflow_sa03": 0.0,
        "inflow_se01": 0.0,
        "inflow_se03": 0.0,
    }

    for idx, part in enumerate(("main", "aux"), start=1):
        url = _lodes_od_url(state_abbr, part)
        progress_callback(
            f"Streaming LODES {LODES_YEAR} {part} OD file for {state_abbr.upper()}...",
            0.15 + (idx * 0.35),
        )
        request = _make_request(url, accept="application/gzip, application/octet-stream, */*")
        start_time = time.monotonic()
        rows_seen = 0
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                with gzip.GzipFile(fileobj=resp) as gz:
                    text_stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace", newline="")
                    reader = csv.DictReader(text_stream)
                    for row in reader:
                        rows_seen += 1
                        # Wall-clock timeout on the STREAM/PARSE loop itself,
                        # not just the initial connection -- urlopen's own
                        # timeout only covers opening the connection. A
                        # connection that opens fine but then stalls or
                        # trickles data slowly mid-stream can otherwise
                        # block here indefinitely with no exception ever
                        # raised, which is exactly what produced a silent,
                        # unbounded hang for at least one real state (no
                        # error, no completion, just stuck).
                        if rows_seen % 250_000 == 0:
                            elapsed = time.monotonic() - start_time
                            progress_callback(
                                f"Streaming LODES {LODES_YEAR} {part} OD file for {state_abbr.upper()}... "
                                f"({rows_seen:,} rows processed, {elapsed:.0f}s elapsed)",
                                0.15 + (idx * 0.35),
                            )
                            if elapsed > LODES_STREAM_TIMEOUT_SECONDS:
                                raise MarketDataError(
                                    f"LODES {part} OD file for state {state_abbr.upper()} (ZCTA {zcta}) "
                                    f"took longer than {LODES_STREAM_TIMEOUT_SECONDS}s to stream/parse "
                                    f"({rows_seen:,} rows processed before giving up) -- treating this as "
                                    f"a hung/too-slow connection rather than waiting indefinitely. Try "
                                    f"again (LODES servers can be intermittently slow), or use manual "
                                    f"commuter-flow entry instead."
                                )
                        w_geocode = str(row.get("w_geocode", "")).strip()
                        h_geocode = str(row.get("h_geocode", "")).strip()
                        if len(w_geocode) != 15 or not w_geocode.isdigit():
                            continue
                        if len(h_geocode) != 15 or not h_geocode.isdigit():
                            continue

                        work_in = w_geocode in blocks
                        home_in = h_geocode in blocks
                        if not work_in and not home_in:
                            continue

                        jobs = _row_float(row, "S000")
                        if work_in:
                            flow_totals["daytime_workers"] += jobs
                            if home_in:
                                flow_totals["stay_local"] += jobs
                            else:
                                flow_totals["worker_inflow"] += jobs
                                flow_totals["inflow_sa01"] += _row_float(row, "SA01")
                                flow_totals["inflow_sa02"] += _row_float(row, "SA02")
                                flow_totals["inflow_sa03"] += _row_float(row, "SA03")
                                flow_totals["inflow_se01"] += _row_float(row, "SE01")
                                flow_totals["inflow_se03"] += _row_float(row, "SE03")
                        elif home_in:
                            flow_totals["resident_outflow"] += jobs
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise MarketDataError(
                f"LODES {part} OD file HTTP error ({e.code}) for state {state_abbr.upper()} "
                f"and ZCTA {zcta}: {_short_snippet(body) or e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise MarketDataError(
                f"LODES {part} OD file request failed for state {state_abbr.upper()} "
                f"and ZCTA {zcta}: {e.reason}"
            ) from e

    worker_inflow = flow_totals["worker_inflow"]
    if worker_inflow:
        pct_income_high = round(100.0 * flow_totals["inflow_se03"] / worker_inflow, 2)
        pct_income_low = round(100.0 * flow_totals["inflow_se01"] / worker_inflow, 2)
        pct_age_mid = round(100.0 * flow_totals["inflow_sa02"] / worker_inflow, 2)
        pct_age_senior = round(100.0 * flow_totals["inflow_sa03"] / worker_inflow, 2)
    else:
        pct_income_high = pct_income_low = pct_age_mid = pct_age_senior = 0.0

    result = {
        "daytime_workers": float(flow_totals["daytime_workers"]),
        "worker_inflow": float(flow_totals["worker_inflow"]),
        "resident_outflow": float(flow_totals["resident_outflow"]),
        "stay_local": float(flow_totals["stay_local"]),
        "pct_income_high": pct_income_high,
        "pct_income_low": pct_income_low,
        "pct_age_mid": pct_age_mid,
        "pct_age_senior": pct_age_senior,
        "pct_office_jobs": 0.0,
        "source": (
            f"LODES {LODES_YEAR} OD main+aux ({state_abbr.upper()}) + "
            "2020 Census block relationship file"
        ),
    }

    try:
        with cache_path.open("wb") as handle:
            pickle.dump(result, handle, protocol=pickle.HIGHEST_PROTOCOL)
    except OSError:
        pass

    progress_callback(f"Finished commuter flow fetch for ZCTA {zcta}.", 1.0)
    return result
