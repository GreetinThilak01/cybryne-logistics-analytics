"""
Cybryne — Logistics BI synthetic data generator.

Implements §7 of cybryne-logistics-bi-architecture-blueprint.md.
Produces all mart-layer dimension and fact CSVs (except dim_date, which is
DAX-generated in Power BI) into ./output, then runs the §7.8 thirteen-assertion
suite. If an assertion fails, the relevant config parameter is nudged and the
whole dataset is regenerated (same seed) until all assertions pass.

Case-study outcomes emerge from distributions and structural behaviour only —
there are no outcome flag columns anywhere in this script.

Dependencies: python stdlib + numpy + pandas + faker.
"""

import copy
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

# =============================================================================
# CONFIG — every tunable parameter from blueprint §7. The calibration loop may
# adjust members of CFG between iterations; SEED never changes.
# =============================================================================

SEED = 42
OUTPUT_DIR = Path(__file__).parent / "output"
AS_OF_DATE = date(2025, 1, 15)          # snapshot date for status / AR / payments
                                        # (mid-Jan: December bookings still in flight,
                                        #  so the Operations dashboard has live work)
START_MONTH = date(2024, 1, 1)          # first booking month
N_MONTHS = 12
GO_LIVE = date(2024, 7, 1)              # carrier reallocation + customer actions

CFG = {
    # --- volumes ---------------------------------------------------------
    "base_monthly_shipments": 380,
    # multiplicative seasonality, mean forced to 1.0 in code
    "seasonality": {1: 1.00, 2: 0.98, 3: 1.02, 4: 0.92, 5: 0.92, 6: 0.97,
                    7: 1.00, 8: 1.02, 9: 1.10, 10: 1.10, 11: 1.08, 12: 0.99},
    "monthly_jitter": 0.015,             # uniform +/- jitter on monthly count

    # --- splits ----------------------------------------------------------
    "p_ocean": 0.65,
    "p_ocean_fcl": 0.80,                 # within ocean
    "p_air_standard": 0.85,              # within air
    "p_dest_usa": 0.58,
    "branch_shares": {"MUM": 0.55, "CHE": 0.25, "AMD": 0.20},

    # --- customers ---------------------------------------------------------
    # problem customers each take this share of monthly volume (blueprint: 22% combined)
    "problem_share_each": 0.11,
    # Trident Polymers exit ramp: month -> fraction of its normal volume
    "trident_taper": {9: 0.60, 10: 0.35, 11: 0.15, 12: 0.0},

    # --- carrier allocation (within mode x destination region) ------------
    "alloc_ocean_usa": {"MCL": 0.40, "IPS": 0.28, "BAM": 0.20, "VOS": 0.12},
    "alloc_ocean_uae": {"GBL": 0.48, "BAM": 0.28, "VOS": 0.14, "IPS": 0.10},
    "alloc_air_usa_h1": {"ASW": 0.80, "SKL": 0.12, "TCA": 0.08},
    "alloc_air_usa_h2": {"ASW": 0.15, "SKL": 0.45, "TCA": 0.40},
    "alloc_air_uae": {"FGC": 0.60, "SKL": 0.40},

    # --- transit variance engine (days; on_time <=> dep_slip + variance <= 0)
    # normal carriers: mixture  (1-p_delay)*N(mu, sigma) + p_delay*U(delay_lo, delay_hi)
    # (values below are the converged output of the assertion-driven calibration
    #  loop; the loop remains active as a backstop if any parameter is changed)
    "carrier_mu": {"MCL": -3.68, "IPS": -3.5, "GBL": -3.6, "BAM": -3.5,
                   "VOS": -3.68, "SKL": -3.3, "FGC": -3.20, "TCA": -3.3,
                   "ASW": -2.8},
    "carrier_sigma": {"default": 1.9, "ASW": 2.6},
    "carrier_p_delay": {"default": 0.05, "ASW": 0.20},
    "carrier_delay_range": {"default": (3, 12), "ASW": (2, 10)},
    "normal_mu_shift": 0.237,            # calibration lever: added to all non-ASW mu
    "asw_mu_shift": 0.727,               # calibration lever: added to ASW mu
    # departure slip (applies to everyone): P(0), P(1), P(2) days
    "dep_slip_probs": [0.70, 0.20, 0.10],

    # --- H1 macro disruption (ocean only: Red Sea reroutings + Nhava Sheva
    #     congestion), probability by booking month, delay U(2, 9) days -----
    "macro_p": {1: 0.15, 2: 0.13, 3: 0.11, 4: 0.09, 5: 0.075, 6: 0.055,
                7: 0.025, 8: 0.015, 9: 0.01, 10: 0.01, 11: 0.01, 12: 0.01},
    "macro_scale": 1.398,                # calibration lever
    "macro_delay_range": (2, 9),

    # --- pricing & margin --------------------------------------------------
    "normal_margin_mean": 0.165,
    "normal_margin_sd": 0.035,
    "margin_floor": 0.08,
    "margin_cap": 0.28,
    "problem_discount": 0.065,           # sharp base pricing: sell = market*(1-d)
    "arihant_h2_discount": -0.03,        # repriced from GO_LIVE: discount removed + small premium

    # --- surcharges ----------------------------------------------------------
    "normal_surch_incidence": 0.30,
    "normal_surch_pct": (0.03, 0.08),    # of job base cost, uniform
    "normal_post_closure_p": 0.45,
    "problem_surch_incidence": 0.70,
    "problem_surch_pct": (0.09, 0.18),   # blueprint §7.4: 8–18% of job cost
    "problem_post_closure_p": 0.60,
    "problem_surch_scale": 1.478,        # calibration lever on problem surcharge value
    # surcharge capture probability (per surcharge line, by group)
    "capture_normal": 0.88,
    "capture_problem": 0.31,
    "capture_arihant_h2": 0.88,
    "capture_markup": (1.00, 1.05),      # billed amount = cost * U(...)

    # --- billing / payments --------------------------------------------------
    "billing_lag_median": 7.5,           # lognormal median (days)
    "billing_lag_sigma": 0.45,
    "billing_lag_floor": 2,
    "billing_lag_cap": 25,
    "pay_delay_normal": (6, 8),          # N(mean, sd) days past due
    "pay_delay_problem": (24, 12),
    "p_never_pay_normal": 0.02,
    "p_never_pay_problem": 0.06,
    "p_part_payment": 0.08,
    "p_credit_note": 0.012,

    # --- exceptions -----------------------------------------------------------
    "base_ops_exception_p": 0.06,        # non-delay operational exceptions
    "problem_doc_exception_p": 0.25,     # customer-responsible doc issues -> detention

    # --- deliberate data-quality artefacts -------------------------------------
    "unmatched_invoice_rate": 0.015,

    # --- pre-implementation reporting lag mechanics ----------------------------
    "recon_delay_days": 1,
    "report_skip_rate": 0.09,            # ops manager absent -> report skips a week

    # --- pipeline log -----------------------------------------------------------
    "pipeline_start": date(2024, 7, 1),
    "pipeline_fail_p": 0.008,
}

# =============================================================================
# STATIC REFERENCE DATA (blueprint §7.3)
# =============================================================================

BRANCHES = [
    # branch_key, branch_code, branch_name, city, state, is_hq
    (1, "MUM", "Mumbai HQ", "Mumbai", "Maharashtra", True),
    (2, "CHE", "Chennai", "Chennai", "Tamil Nadu", False),
    (3, "AMD", "Ahmedabad", "Ahmedabad", "Gujarat", False),
]

MODES = [
    # mode_key, mode, service_level, mode_service, unit_of_measure
    (1, "Ocean", "FCL", "Ocean — FCL", "TEU"),
    (2, "Ocean", "LCL", "Ocean — LCL", "TEU"),
    (3, "Air", "Standard", "Air — Standard", "Chargeable kg"),
    (4, "Air", "Express", "Air — Express", "Chargeable kg"),
]

CARRIERS = [
    # carrier_key, code, name, mode, type, lanes, contract_start
    (1, "MCL", "Meridian Container Line", "Ocean", "VOCC", "India → USA", date(2022, 4, 1)),
    (2, "IPS", "IndoPacific Shipping", "Ocean", "VOCC", "India → USA", date(2021, 7, 1)),
    (3, "GBL", "GulfBridge Lines", "Ocean", "VOCC", "India → UAE", date(2020, 1, 1)),
    (4, "BAM", "BlueAnchor Marine", "Ocean", "VOCC", "India → USA / UAE", date(2022, 10, 1)),
    (5, "VOS", "Vanguard Ocean Services", "Ocean", "VOCC", "India → USA / UAE (spot)", date(2023, 6, 1)),
    (6, "ASW", "AeroSwift Cargo", "Air", "Airline", "India → USA", date(2023, 4, 1)),
    (7, "SKL", "SkyLane Air Freight", "Air", "Airline", "India → USA / UAE", date(2022, 2, 1)),
    (8, "FGC", "Falcon Gulf Cargo", "Air", "Airline", "India → UAE", date(2021, 9, 1)),
    (9, "TCA", "TransContinental Air", "Air", "Airline", "India → USA", date(2023, 11, 1)),
]
CARRIER_KEY = {c[1]: c[0] for c in CARRIERS}
CARRIER_NAME = {c[1]: c[2] for c in CARRIERS}

ORIGIN_PORTS = [
    # port_key, code, name, type, city, country, region
    (1, "INNSA1", "JNPT — JNPCT Terminal (Nhava Sheva)", "Seaport", "Mumbai", "India", "India"),
    (2, "INNSA", "Nhava Sheva — GTI Terminal", "Seaport", "Mumbai", "India", "India"),
    (3, "INMAA", "Chennai Port", "Seaport", "Chennai", "India", "India"),
    (4, "INMUN", "Mundra Port", "Seaport", "Mundra", "India", "India"),
    (5, "BOM", "Chhatrapati Shivaji Maharaj Intl Airport", "Airport", "Mumbai", "India", "India"),
    (6, "MAA", "Chennai International Airport", "Airport", "Chennai", "India", "India"),
    (7, "AMD", "Sardar Vallabhbhai Patel Intl Airport", "Airport", "Ahmedabad", "India", "India"),
]

DEST_PORTS = [
    (1, "USLAX", "Port of Los Angeles", "Seaport", "Los Angeles", "USA", "USA"),
    (2, "USNYC", "Port of New York / Newark", "Seaport", "New York", "USA", "USA"),
    (3, "AEJEA", "Jebel Ali Port", "Seaport", "Dubai", "UAE", "UAE"),
    (4, "AEKHL", "Khalifa Port", "Seaport", "Abu Dhabi", "UAE", "UAE"),
    (5, "LAX", "Los Angeles International Airport", "Airport", "Los Angeles", "USA", "USA"),
    (6, "JFK", "John F. Kennedy International Airport", "Airport", "New York", "USA", "USA"),
    (7, "DXB", "Dubai International Airport", "Airport", "Dubai", "UAE", "UAE"),
    (8, "AUH", "Abu Dhabi International Airport", "Airport", "Abu Dhabi", "UAE", "UAE"),
]

CUSTOMERS = [
    # code, name, segment, city, state, home_branch, terms, commodity
    ("CUST-ARHT", "Arihant Auto Components Pvt Ltd", "Auto Components", "Pune", "Maharashtra", "MUM", 45, "Machined auto components"),
    ("CUST-TRPL", "Trident Polymers Ltd", "Polymers", "Vapi", "Gujarat", "MUM", 45, "Polymer granules and sheets"),
    ("CUST-SUND", "Sundaram Textiles Exports", "Textiles", "Coimbatore", "Tamil Nadu", "CHE", 30, "Cotton knitwear"),
    ("CUST-KANC", "Kanchi Pharma Lifesciences", "Pharmaceuticals", "Chennai", "Tamil Nadu", "CHE", 30, "Pharma formulations"),
    ("CUST-DECC", "Deccan Agro Foods", "Agro Foods", "Hyderabad", "Telangana", "CHE", 30, "Processed agro foods"),
    ("CUST-SGSC", "Shree Ganesh Specialty Chemicals", "Chemicals", "Ankleshwar", "Gujarat", "AMD", 30, "Specialty chemicals"),
    ("CUST-PREC", "Precision Gears India", "Engineering", "Rajkot", "Gujarat", "AMD", 30, "Precision gears"),
    ("CUST-MARU", "Marudhar Spice Company", "Spices", "Jodhpur", "Rajasthan", "MUM", 30, "Ground and whole spices"),
    ("CUST-ZENI", "Zenith Engineering Works", "Engineering", "Mumbai", "Maharashtra", "MUM", 30, "Fabricated steel assemblies"),
    ("CUST-COAS", "Coastal Marine Foods", "Seafood", "Chennai", "Tamil Nadu", "CHE", 30, "Frozen seafood"),
    ("CUST-OMKA", "Omkar Ceramics", "Ceramics", "Morbi", "Gujarat", "AMD", 30, "Vitrified tiles"),
    ("CUST-RAJH", "Rajhans Apparel Exports", "Apparel", "Tiruppur", "Tamil Nadu", "CHE", 30, "Ready-made garments"),
    ("CUST-NALA", "Nalanda Handicrafts Export House", "Handicrafts", "Jaipur", "Rajasthan", "MUM", 30, "Handicrafts and home decor"),
    ("CUST-VIKR", "Vikram Forgings Ltd", "Forgings", "Ludhiana", "Punjab", "MUM", 30, "Steel forgings"),
]
PROBLEM_A = "CUST-ARHT"   # repriced from GO_LIVE
PROBLEM_B = "CUST-TRPL"   # exited (taper Sep-Nov, zero from Dec)
# relative volume weights for the 12 normal customers (normalised in code)
NORMAL_WEIGHTS = {
    "CUST-SUND": 1.5, "CUST-KANC": 1.3, "CUST-DECC": 1.15, "CUST-SGSC": 1.1,
    "CUST-PREC": 1.0, "CUST-MARU": 0.95, "CUST-ZENI": 0.9, "CUST-COAS": 0.85,
    "CUST-OMKA": 0.8, "CUST-RAJH": 0.75, "CUST-NALA": 0.6, "CUST-VIKR": 0.55,
}
KAM_OWNERS = ["R. Nair", "S. Kulkarni", "A. Menon", "P. Iyer", "V. Deshmukh"]

CHARGE_TYPES = [
    # key, code, name, category, applies_to_mode, is_passthrough_expected
    (1, "OFR", "Ocean Freight", "Base Cost", "Ocean", True),
    (2, "AFR", "Air Freight", "Base Cost", "Air", True),
    (3, "THC", "Origin Terminal Handling", "Base Cost", "Both", True),
    (4, "DHC", "Destination Handling", "Base Cost", "Both", True),
    (5, "CUS", "Customs Clearance", "Base Cost", "Both", True),
    (6, "DOC", "Documentation Fee", "Base Cost", "Both", True),
    (7, "TRK", "Transport / Drayage", "Base Cost", "Both", True),
    (8, "BAF", "Bunker Adjustment Factor", "Surcharge", "Ocean", True),
    (9, "CAF", "Currency Adjustment Factor", "Surcharge", "Ocean", True),
    (10, "PSS", "Peak Season Surcharge", "Surcharge", "Both", True),
    (11, "DET", "Detention", "Surcharge", "Ocean", True),
    (12, "DEM", "Demurrage", "Surcharge", "Ocean", True),
    (13, "FSC", "Fuel Surcharge (Air)", "Surcharge", "Air", True),
    (14, "SSC", "Security Surcharge (Air)", "Surcharge", "Air", True),
    (15, "WRS", "War Risk Surcharge", "Surcharge", "Both", True),
]
CHARGE_KEY = {c[1]: c[0] for c in CHARGE_TYPES}
CHARGE_NAME_BY_CODE = {c[1]: c[2] for c in CHARGE_TYPES}
SURCH_OCEAN = ["BAF", "CAF", "PSS", "DET", "DEM", "WRS"]
SURCH_AIR = ["FSC", "SSC", "PSS", "WRS"]

EXCEPTION_TYPES = [
    # key, code, name, severity, responsible_party
    (1, "CUS_HOLD", "Customs Hold", "Medium", "Customs"),
    (2, "DOC_DISC", "Documentation Discrepancy", "Medium", "Customer"),
    (3, "ROLLOVER", "Carrier Rollover", "High", "Carrier"),
    (4, "MISS_CONN", "Missed Connection", "High", "Carrier"),
    (5, "CNTR_SHORT", "Container Shortage", "Medium", "Carrier"),
    (6, "PORT_CONG", "Port Congestion", "Medium", "Forwarder"),
    (7, "DAMAGE", "Cargo Damage / Short-shipment", "High", "Carrier"),
    (8, "LATE_GATE", "Late Gate-In", "Low", "Carrier"),
]
EXC_KEY = {e[1]: e[0] for e in EXCEPTION_TYPES}

# committed transit days by (origin sea/air region proxy, dest_code): (lo, hi)
TRANSIT_DAYS = {
    ("MUM", "USLAX"): (28, 32), ("MUM", "USNYC"): (33, 38),
    ("AMD", "USLAX"): (29, 33), ("AMD", "USNYC"): (34, 39),
    ("CHE", "USLAX"): (31, 35), ("CHE", "USNYC"): (35, 40),
    ("MUM", "AEJEA"): (10, 13), ("MUM", "AEKHL"): (11, 14),
    ("AMD", "AEJEA"): (9, 12), ("AMD", "AEKHL"): (10, 13),
    ("CHE", "AEJEA"): (12, 15), ("CHE", "AEKHL"): (12, 15),
    "AIR_USA": (5, 7), "AIR_UAE": (3, 4),
}

INCOTERMS = (["FOB"] * 40 + ["CIF"] * 35 + ["CFR"] * 20 + ["EXW"] * 5)

# =============================================================================
# HELPERS
# =============================================================================

def month_range():
    return [date(START_MONTH.year, m, 1) for m in range(1, N_MONTHS + 1)]


def rand_day_in_month(rng, month_start):
    nxt = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return month_start + timedelta(days=int(rng.integers(0, (nxt - month_start).days)))


def pick(rng, weights_dict):
    keys = list(weights_dict)
    w = np.array([weights_dict[k] for k in keys], dtype=float)
    return keys[int(rng.choice(len(keys), p=w / w.sum()))]


def next_monday_on_or_after(d):
    return d + timedelta(days=(0 - d.weekday()) % 7)


# =============================================================================
# DIMENSIONS
# =============================================================================

def build_dimensions(rng, fake):
    dim_branch = pd.DataFrame(
        BRANCHES, columns=["branch_key", "branch_code", "branch_name", "city", "state", "is_hq"])

    dim_mode = pd.DataFrame(
        MODES, columns=["mode_key", "mode", "service_level", "mode_service", "unit_of_measure"])

    dim_carrier = pd.DataFrame(
        CARRIERS, columns=["carrier_key", "carrier_code", "carrier_name", "carrier_mode",
                           "carrier_type", "primary_lanes", "contract_start_date"])
    dim_carrier["is_active"] = True

    port_cols = ["port_key", "port_code", "port_name", "port_type", "city", "country", "region"]
    dim_origin_port = pd.DataFrame(ORIGIN_PORTS, columns=port_cols)
    dim_origin_port["trade_lane_role"] = "Origin"
    dim_destination_port = pd.DataFrame(DEST_PORTS, columns=port_cols)
    dim_destination_port["trade_lane_role"] = "Destination"

    rows = []
    for i, (code, name, seg, city, state, br, terms, _commodity) in enumerate(CUSTOMERS, start=101):
        rows.append({
            "customer_key": i,
            "customer_code": code,
            "tally_ledger_name": f"{name} (Debtors)",
            "customer_name": name,
            "industry_segment": seg,
            "city": city,
            "state": state,
            "home_branch": {"MUM": "Mumbai HQ", "CHE": "Chennai", "AMD": "Ahmedabad"}[br],
            "payment_terms_days": terms,
            "credit_limit_inr": int(rng.choice([4000000, 5000000, 7500000, 10000000])),
            "customer_since": date(int(rng.integers(2014, 2023)), int(rng.integers(1, 13)), int(rng.integers(1, 28))),
            "kam_owner": KAM_OWNERS[i % len(KAM_OWNERS)],
            # Decision 3: Trident exited — is_active false from December
            "is_active": code != PROBLEM_B,
        })
    dim_customer = pd.DataFrame(rows)

    dim_charge_type = pd.DataFrame(
        CHARGE_TYPES, columns=["charge_type_key", "charge_code", "charge_name",
                               "charge_category", "applies_to_mode", "is_passthrough_expected"])

    dim_exception_type = pd.DataFrame(
        EXCEPTION_TYPES, columns=["exception_type_key", "exception_code", "exception_name",
                                  "severity", "responsible_party"])
    dim_exception_type["severity"] = dim_exception_type["severity"]

    vendors = {
        "customs": [fake.company() + " Customs Brokers" for _ in range(6)],
        "transport": [fake.company() + " Logistics" for _ in range(6)],
        "handling": [fake.company() + " Terminals" for _ in range(4)],
    }
    return {
        "dim_branch": dim_branch, "dim_mode": dim_mode, "dim_carrier": dim_carrier,
        "dim_origin_port": dim_origin_port, "dim_destination_port": dim_destination_port,
        "dim_customer": dim_customer, "dim_charge_type": dim_charge_type,
        "dim_exception_type": dim_exception_type,
    }, vendors


# =============================================================================
# SHIPMENTS + EXCEPTIONS (delay events and exception events share one draw)
# =============================================================================

def draw_customer_counts(cfg, rng, n_month, month):
    """Deterministic-ish allocation: problem customers get ~11% each (taper for
    Trident), the rest is multinomial across the 12 normal customers."""
    counts = {}
    a = int(round(cfg["problem_share_each"] * n_month))
    taper = cfg["trident_taper"].get(month, 1.0) if month >= 9 else 1.0
    b = int(round(cfg["problem_share_each"] * n_month * taper))
    counts[PROBLEM_A] = a + int(rng.integers(-2, 3))
    counts[PROBLEM_B] = max(0, b + (int(rng.integers(-2, 3)) if b > 0 else 0))
    rest = n_month - counts[PROBLEM_A] - counts[PROBLEM_B]
    codes = list(NORMAL_WEIGHTS)
    w = np.array([NORMAL_WEIGHTS[c] for c in codes])
    alloc = rng.multinomial(rest, w / w.sum())
    for c, n in zip(codes, alloc):
        counts[c] = int(n)
    return counts


def transit_variance_draw(cfg, rng, carrier_code, is_ocean, month):
    """Returns (variance_days, carrier_delay_event_days, macro_delay_days).
    variance = normal component + delay components; delay components also
    create exception events (blueprint §7.6 constraint 5)."""
    mu = cfg["carrier_mu"][carrier_code]
    mu += cfg["asw_mu_shift"] if carrier_code == "ASW" else cfg["normal_mu_shift"]
    sigma = cfg["carrier_sigma"].get(carrier_code, cfg["carrier_sigma"]["default"])
    p_delay = cfg["carrier_p_delay"].get(carrier_code, cfg["carrier_p_delay"]["default"])
    lo, hi = cfg["carrier_delay_range"].get(carrier_code, cfg["carrier_delay_range"]["default"])

    base = rng.normal(mu, sigma)
    carrier_delay = 0
    if rng.random() < p_delay:
        carrier_delay = int(rng.integers(lo, hi + 1))
    macro_delay = 0
    if is_ocean and rng.random() < cfg["macro_p"][month] * cfg["macro_scale"]:
        mlo, mhi = cfg["macro_delay_range"]
        macro_delay = int(rng.integers(mlo, mhi + 1))
    variance = base + carrier_delay + macro_delay
    return variance, carrier_delay, macro_delay


def build_shipments(cfg, rng, dims):
    cust_lookup = {r.customer_code: r for r in dims["dim_customer"].itertuples()}
    commodity = {c[0]: c[7] for c in CUSTOMERS}
    home_branch = {c[0]: c[5] for c in CUSTOMERS}
    branch_key = {b[1]: b[0] for b in BRANCHES}
    origin_sea = {"MUM": [1, 2], "CHE": [3], "AMD": [4]}
    origin_air = {"MUM": 5, "CHE": 6, "AMD": 7}
    dest_by = {("USA", "Ocean"): {1: 0.55, 2: 0.45}, ("UAE", "Ocean"): {3: 0.75, 4: 0.25},
               ("USA", "Air"): {5: 0.55, 6: 0.45}, ("UAE", "Air"): {7: 0.70, 8: 0.30}}
    dest_code = {p[0]: p[1] for p in DEST_PORTS}
    dest_name = {p[0]: p[2] for p in DEST_PORTS}
    origin_name = {p[0]: p[2] for p in ORIGIN_PORTS}

    seas = dict(cfg["seasonality"])
    norm = sum(seas.values()) / len(seas)
    seas = {m: v / norm for m, v in seas.items()}

    shipments, exceptions = [], []
    skey, ekey, job_seq = 20000, 5000, {b: 1000 for b in branch_key}

    for ms in month_range():
        m = ms.month
        n_month = int(round(cfg["base_monthly_shipments"] * seas[m]
                            * rng.uniform(1 - cfg["monthly_jitter"], 1 + cfg["monthly_jitter"])))
        counts = draw_customer_counts(cfg, rng, n_month, m)
        for cust_code, n_cust in counts.items():
            for _ in range(n_cust):
                skey += 1
                cust = cust_lookup[cust_code]
                is_problem = cust_code in (PROBLEM_A, PROBLEM_B)

                # branch: customer home branch 85%, else volume-share draw
                br = home_branch[cust_code] if rng.random() < 0.85 else pick(rng, cfg["branch_shares"])
                is_ocean = rng.random() < cfg["p_ocean"]
                region = "USA" if rng.random() < cfg["p_dest_usa"] else "UAE"
                if is_ocean:
                    mode_key = 1 if rng.random() < cfg["p_ocean_fcl"] else 2
                else:
                    mode_key = 3 if rng.random() < cfg["p_air_standard"] else 4

                # carrier allocation (reallocation from GO_LIVE for US-bound air)
                if is_ocean:
                    alloc = cfg["alloc_ocean_usa"] if region == "USA" else cfg["alloc_ocean_uae"]
                elif region == "USA":
                    alloc = cfg["alloc_air_usa_h1"] if ms < GO_LIVE else cfg["alloc_air_usa_h2"]
                else:
                    alloc = cfg["alloc_air_uae"]
                carrier_code = pick(rng, alloc)

                # ports & transit commitment
                if is_ocean:
                    o_key = int(rng.choice(origin_sea[br]))
                    d_key = pick(rng, dest_by[(region, "Ocean")])
                    lo, hi = TRANSIT_DAYS[(br, dest_code[d_key])]
                else:
                    o_key = origin_air[br]
                    d_key = pick(rng, dest_by[(region, "Air")])
                    lo, hi = TRANSIT_DAYS[f"AIR_{region}"]
                committed_transit = int(rng.integers(lo, hi + 1))

                booking = rand_day_in_month(rng, ms)
                lead = int(rng.integers(7, 22)) if is_ocean else int(rng.integers(2, 8))
                planned_etd = booking + timedelta(days=lead)
                committed_delivery = planned_etd + timedelta(days=committed_transit)

                dep_slip = int(rng.choice([0, 1, 2], p=cfg["dep_slip_probs"]))
                actual_departure = planned_etd + timedelta(days=dep_slip)

                variance, carrier_delay, macro_delay = transit_variance_draw(
                    cfg, rng, carrier_code, is_ocean, m)
                actual_transit = max(1, committed_transit + int(round(variance)))
                actual_delivery = actual_departure + timedelta(days=actual_transit)
                transit_var = actual_transit - committed_transit
                carrier_eta = committed_delivery + timedelta(days=int(round(rng.normal(0, 2))))
                completion = actual_delivery + timedelta(days=int(rng.integers(0, 5)))

                # ---- exception events (share the delay draws) ----
                ship_exc = []
                if carrier_delay > 0:
                    code = "ROLLOVER" if is_ocean else "MISS_CONN"
                    if rng.random() < 0.15:
                        code = "DAMAGE" if rng.random() < 0.4 else ("CNTR_SHORT" if is_ocean else "LATE_GATE")
                    ship_exc.append((code, carrier_delay))
                if macro_delay > 0:
                    ship_exc.append(("PORT_CONG", macro_delay))
                if rng.random() < cfg["base_ops_exception_p"]:
                    ship_exc.append((str(rng.choice(["CUS_HOLD", "DOC_DISC", "LATE_GATE"])), 0))
                doc_exception = is_problem and rng.random() < cfg["problem_doc_exception_p"]
                if doc_exception:
                    ship_exc.append(("DOC_DISC", 0))

                # ---- status vs AS_OF (constraint 6) ----
                if actual_departure > AS_OF_DATE:
                    status = "Booked"
                elif actual_delivery > AS_OF_DATE:
                    status = "In Transit"
                elif completion > AS_OF_DATE:
                    status = "Delivered"
                else:
                    status = "Closed"
                delivered = status in ("Delivered", "Closed")
                closed = status == "Closed"

                job_seq[br] += 1
                job = f"{br}-{'SE' if is_ocean else 'AE'}-24-{job_seq[br]:05d}"

                for code, delay in ship_exc:
                    ekey += 1
                    raised = actual_departure + timedelta(days=int(rng.integers(0, max(2, actual_transit))))
                    raised = min(raised, actual_delivery if delivered else AS_OF_DATE)
                    resolved = raised + timedelta(days=int(rng.integers(1, 7)))
                    open_exc = resolved > AS_OF_DATE
                    exceptions.append({
                        "exception_key": ekey, "shipment_key": skey, "job_number": job,
                        "exception_type_key": EXC_KEY[code],
                        "carrier_key": CARRIER_KEY[carrier_code],
                        "customer_key": cust.customer_key, "branch_key": branch_key[br],
                        "raised_date": raised,
                        "resolved_date": None if open_exc else resolved,
                        "delay_days_attributed": delay,
                        "exception_status": "Open" if open_exc else "Resolved",
                    })

                shipments.append({
                    "shipment_key": skey, "job_number": job,
                    "customer_key": cust.customer_key,
                    "carrier_key": CARRIER_KEY[carrier_code],
                    "branch_key": branch_key[br], "mode_key": mode_key,
                    "origin_port_key": o_key, "destination_port_key": d_key,
                    "trade_lane": f"{origin_name[o_key].split(' — ')[0].split(' (')[0]} → {dest_name[d_key].split(' Port')[0].split(' International')[0].replace('Port of ', '')}",
                    "destination_region": region,
                    "booking_date": booking,
                    "committed_delivery_date": committed_delivery,
                    "planned_etd": planned_etd,
                    "actual_departure_date": actual_departure if actual_departure <= AS_OF_DATE else None,
                    "carrier_eta": carrier_eta,
                    "actual_delivery_date": actual_delivery if delivered else None,
                    "job_completion_date": completion if closed else None,
                    "shipment_status": status,
                    "commodity": commodity[cust_code],
                    "incoterm": str(rng.choice(INCOTERMS)),
                    "container_count": int(rng.choice([1, 2, 3], p=[0.45, 0.35, 0.20])) if is_ocean else None,
                    "teu": None, "chargeable_weight_kg": None,
                    "committed_transit_days": committed_transit,
                    "actual_transit_days": actual_transit if delivered else None,
                    "transit_variance_days": transit_var if delivered else None,
                    "is_on_time": (actual_delivery <= committed_delivery) if delivered else None,
                    "exception_count": len(ship_exc),
                    "has_exception": len(ship_exc) > 0,
                    # internal fields (stripped before CSV write)
                    "_cust_code": cust_code, "_carrier_code": carrier_code,
                    "_branch_code": br, "_is_ocean": is_ocean, "_month": m,
                    "_doc_exception": doc_exception,
                    "_completion_actual": completion, "_delivery_actual": actual_delivery,
                })

    fs = pd.DataFrame(shipments)
    fs["teu"] = np.where(fs["_is_ocean"] & fs["container_count"].notna(),
                         fs["container_count"] * 2.0, np.nan)
    weights = np.round(np.exp(np.log(850) + 0.6 * rng.standard_normal(len(fs))), 0)
    fs["chargeable_weight_kg"] = np.where(~fs["_is_ocean"], np.clip(weights, 120, 8000), np.nan)
    fe = pd.DataFrame(exceptions)
    return fs, fe

# =============================================================================
# FINANCIALS: charges, invoices, payments, settlement, shipment financials
# =============================================================================

BASE_SPLIT_OCEAN = [("OFR", 0.72), ("THC", 0.10), ("CUS", 0.06), ("TRK", 0.07), ("DHC", 0.03), ("DOC", 0.02)]
BASE_SPLIT_AIR = [("AFR", 0.74), ("THC", 0.09), ("CUS", 0.06), ("TRK", 0.06), ("DHC", 0.03), ("DOC", 0.02)]
SELL_SPLIT_OCEAN = [("OFR", 0.75), ("THC", 0.10), ("CUS", 0.08), ("DOC", 0.07)]
SELL_SPLIT_AIR = [("AFR", 0.76), ("THC", 0.09), ("CUS", 0.08), ("DOC", 0.07)]


def market_revenue(cfg, rng, row):
    usa = row["destination_region"] == "USA"
    if row["mode_key"] == 1:      # Ocean FCL
        per_cont = math.exp(math.log(130000 if usa else 75000) + 0.30 * rng.standard_normal())
        return row["container_count"] * per_cont
    if row["mode_key"] == 2:      # Ocean LCL
        return math.exp(math.log(95000 if usa else 60000) + 0.35 * rng.standard_normal())
    rate = rng.uniform(180, 260) if usa else rng.uniform(90, 150)
    rate *= 1.25 if row["mode_key"] == 4 else 1.0   # express premium
    return row["chargeable_weight_kg"] * rate


def build_financials(cfg, rng, fs, vendors):
    charges, inv_lines, cn_links = [], [], {}
    charge_key = 80000
    inv_seq = {b[1]: 400 for b in BRANCHES}
    per_job = {}   # shipment_key -> financial accumulators

    for row in fs.to_dict("records"):
        if row["actual_departure_date"] is None:
            continue                       # not departed: no charges, no billing yet
        skey, job, br = row["shipment_key"], row["job_number"], row["_branch_code"]
        cust_code, is_ocean = row["_cust_code"], row["_is_ocean"]
        is_problem = cust_code in (PROBLEM_A, PROBLEM_B)
        booked_h2 = row["booking_date"] >= GO_LIVE
        arihant_h2 = cust_code == PROBLEM_A and booked_h2
        delivered = row["_delivery_actual"] <= AS_OF_DATE
        closed = row["_completion_actual"] <= AS_OF_DATE
        completion = row["_completion_actual"]
        departure = row["actual_departure_date"]
        carrier_name = CARRIER_NAME[row["_carrier_code"]]

        mkt = market_revenue(cfg, rng, row)
        margin = float(np.clip(rng.normal(cfg["normal_margin_mean"], cfg["normal_margin_sd"]),
                               cfg["margin_floor"], cfg["margin_cap"]))
        base_cost = mkt * (1 - margin)
        discount = 0.0
        if is_problem:
            discount = cfg["arihant_h2_discount"] if arihant_h2 else cfg["problem_discount"]
        sell = mkt * (1 - discount)

        # ---- base cost lines --------------------------------------------------
        split = BASE_SPLIT_OCEAN if is_ocean else BASE_SPLIT_AIR
        kept = [(c, p) for c, p in split
                if c in ("OFR", "AFR", "THC", "CUS") or rng.random() < 0.8]
        total_p = sum(p for _, p in kept)
        base_recorded = 0.0
        for code, p in kept:
            amt = round(base_cost * p / total_p * rng.uniform(0.95, 1.05), 2)
            if code in ("OFR", "AFR"):
                cdate, vendor = departure + timedelta(days=int(rng.integers(0, 4))), carrier_name
            elif code == "THC":
                cdate, vendor = departure, str(rng.choice(vendors["handling"]))
            elif code == "CUS":
                cdate, vendor = departure - timedelta(days=int(rng.integers(1, 4))), str(rng.choice(vendors["customs"]))
            elif code == "TRK":
                cdate = (row["_delivery_actual"] if delivered else departure)
                vendor = str(rng.choice(vendors["transport"]))
            elif code == "DHC":
                cdate, vendor = row["_delivery_actual"], str(rng.choice(vendors["handling"]))
            else:
                cdate, vendor = row["booking_date"] + timedelta(days=2), "In-house Documentation"
            if code == "DHC" and not delivered:
                continue
            if cdate > AS_OF_DATE:
                continue
            charge_key += 1
            base_recorded += amt
            charges.append({
                "charge_key": charge_key, "shipment_key": skey, "job_number": job,
                "charge_type_key": CHARGE_KEY[code],
                "customer_key": row["customer_key"], "carrier_key": row["carrier_key"],
                "branch_key": row["branch_key"], "mode_key": row["mode_key"],
                "charge_date": cdate, "vendor_name": vendor, "amount_inr": amt,
                "is_post_closure": closed and cdate > completion,
            })

        # ---- surcharge lines ---------------------------------------------------
        incidence = cfg["problem_surch_incidence"] if is_problem else cfg["normal_surch_incidence"]
        pct_rng = cfg["problem_surch_pct"] if is_problem else cfg["normal_surch_pct"]
        post_p = cfg["problem_post_closure_p"] if is_problem else cfg["normal_post_closure_p"]
        scale = cfg["problem_surch_scale"] if is_problem else 1.0
        surch_lines = []
        has_surch = delivered and rng.random() < incidence
        forced_det = row["_doc_exception"] and delivered
        if has_surch or forced_det:
            n_lines = int(rng.integers(1, 4)) if is_problem else int(rng.integers(1, 3))
            total_pct = rng.uniform(*pct_rng) * scale if has_surch else rng.uniform(0.04, 0.10)
            pool = SURCH_OCEAN if is_ocean else SURCH_AIR
            codes = list(rng.choice(pool, size=min(n_lines, len(pool)), replace=False))
            if forced_det:
                det = "DET" if is_ocean else "PSS"
                if det not in codes:
                    codes.append(det)
            shares = rng.dirichlet(np.ones(len(codes)))
            for code, sh in zip(codes, shares):
                amt = round(base_cost * total_pct * sh, 2)
                if amt < 500:
                    continue
                if closed and rng.random() < post_p:
                    cdate = completion + timedelta(days=int(rng.integers(3, 21)))
                else:
                    cdate = departure + timedelta(days=int(rng.integers(1, max(2, row["actual_transit_days"] or 5))))
                if cdate > AS_OF_DATE:
                    continue
                charge_key += 1
                charges.append({
                    "charge_key": charge_key, "shipment_key": skey, "job_number": job,
                    "charge_type_key": CHARGE_KEY[code],
                    "customer_key": row["customer_key"], "carrier_key": row["carrier_key"],
                    "branch_key": row["branch_key"], "mode_key": row["mode_key"],
                    "charge_date": cdate, "vendor_name": carrier_name, "amount_inr": amt,
                    "is_post_closure": closed and cdate > completion,
                })
                surch_lines.append({"code": code, "amount": amt, "cdate": cdate})

        # ---- original invoice ---------------------------------------------------
        acc = per_job[skey] = {
            "row": row, "base_cost": base_recorded,
            "surch_cost": sum(s["amount"] for s in surch_lines),
            "surch_billed": 0.0, "revenue": 0.0,
            "first_invoice_date": None, "billed": False,
        }
        if not closed:
            continue
        lag = math.exp(math.log(cfg["billing_lag_median"]) + cfg["billing_lag_sigma"] * rng.standard_normal())
        lag = int(np.clip(round(lag), cfg["billing_lag_floor"], cfg["billing_lag_cap"]))
        inv_date = completion + timedelta(days=lag)
        if inv_date <= AS_OF_DATE:
            inv_seq[br] += 1
            inv_no = f"{br}/24-25/{inv_seq[br]:04d}"
            terms = 45 if cust_code in (PROBLEM_A, PROBLEM_B) else 30
            due = inv_date + timedelta(days=terms)
            split = SELL_SPLIT_OCEAN if is_ocean else SELL_SPLIT_AIR
            kept = [(c, p) for c, p in split if c in ("OFR", "AFR") or rng.random() < 0.85]
            tp = sum(p for _, p in kept)
            for code, p in kept:
                amt = round(sell * p / tp, 2)
                tax = round(amt * (0.05 if code in ("OFR", "AFR") else 0.18), 2)
                inv_lines.append({
                    "invoice_line_key": 0, "invoice_number": inv_no,
                    "invoice_type": "Original", "invoice_date": inv_date, "due_date": due,
                    "customer_key": row["customer_key"], "shipment_key": skey,
                    "job_number": job, "branch_key": row["branch_key"],
                    "charge_type_key": CHARGE_KEY[code],
                    "line_description": f"{CHARGE_NAME_BY_CODE[code]} — {row['trade_lane']}",
                    "line_amount_inr": amt, "tax_amount_inr": tax,
                    "total_amount_inr": round(amt + tax, 2),
                })
                acc["revenue"] += amt
            acc["first_invoice_date"] = inv_date
            acc["billed"] = True

            # credit note (rare)
            if rng.random() < cfg["p_credit_note"]:
                cn_date = inv_date + timedelta(days=int(rng.integers(5, 26)))
                if cn_date <= AS_OF_DATE:
                    inv_seq[br] += 1
                    cn_no = f"{br}/CN/24-25/{inv_seq[br]:04d}"
                    cn_amt = -round(sell * rng.uniform(0.05, 0.15), 2)
                    cn_tax = round(cn_amt * 0.05, 2)
                    inv_lines.append({
                        "invoice_line_key": 0, "invoice_number": cn_no,
                        "invoice_type": "Credit Note", "invoice_date": cn_date, "due_date": cn_date,
                        "customer_key": row["customer_key"], "shipment_key": skey,
                        "job_number": job, "branch_key": row["branch_key"],
                        "charge_type_key": CHARGE_KEY["OFR" if is_ocean else "AFR"],
                        "line_description": f"Credit note against {inv_no}",
                        "line_amount_inr": cn_amt, "tax_amount_inr": cn_tax,
                        "total_amount_inr": round(cn_amt + cn_tax, 2),
                    })
                    cn_links[cn_no] = inv_no
                    acc["revenue"] += cn_amt

        # ---- supplementary invoice for captured surcharges ----------------------
        if closed and surch_lines:
            cap_p = (cfg["capture_arihant_h2"] if arihant_h2
                     else cfg["capture_problem"] if is_problem else cfg["capture_normal"])
            captured = [s for s in surch_lines if rng.random() < cap_p]
            if captured:
                supp_date = max(s["cdate"] for s in captured) + timedelta(days=int(rng.integers(2, 7)))
                if supp_date <= AS_OF_DATE:
                    inv_seq[br] += 1
                    supp_no = f"{br}/24-25/{inv_seq[br]:04d}"
                    terms = 45 if cust_code in (PROBLEM_A, PROBLEM_B) else 30
                    for s in captured:
                        amt = round(s["amount"] * rng.uniform(*cfg["capture_markup"]), 2)
                        tax = round(amt * 0.18, 2)
                        inv_lines.append({
                            "invoice_line_key": 0, "invoice_number": supp_no,
                            "invoice_type": "Supplementary", "invoice_date": supp_date,
                            "due_date": supp_date + timedelta(days=terms),
                            "customer_key": row["customer_key"], "shipment_key": skey,
                            "job_number": job, "branch_key": row["branch_key"],
                            "charge_type_key": CHARGE_KEY[s["code"]],
                            "line_description": f"Surcharge recovery — {s['code']} — {job}",
                            "line_amount_inr": amt, "tax_amount_inr": tax,
                            "total_amount_inr": round(amt + tax, 2),
                        })
                        acc["revenue"] += amt
                        acc["surch_billed"] += amt

    fc = pd.DataFrame(charges)
    fil = pd.DataFrame(inv_lines)
    fil["invoice_line_key"] = range(30000, 30000 + len(fil))

    # ---- deliberate ~1.5% unmatched invoice lines (Tally narration typos) ------
    n_bad = int(round(cfg["unmatched_invoice_rate"] * len(fil)))
    bad_idx = rng.choice(fil.index, size=n_bad, replace=False)
    for i in bad_idx:
        r = fil.loc[i]
        acc = per_job.get(r["shipment_key"])
        if acc is not None:
            acc["revenue"] -= r["line_amount_inr"]
            if r["invoice_type"] == "Supplementary":
                acc["surch_billed"] -= r["line_amount_inr"]
    fil.loc[bad_idx, "job_number"] = fil.loc[bad_idx, "job_number"].str[:-2] + "XX"
    fil.loc[bad_idx, "shipment_key"] = np.nan

    return fc, fil, cn_links, per_job


def build_payments_settlement(cfg, rng, fil, cn_links, dim_customer):
    terms_by_cust = dict(zip(dim_customer["customer_key"], dim_customer["payment_terms_days"]))
    problem_keys = set(dim_customer.loc[dim_customer["customer_code"].isin([PROBLEM_A, PROBLEM_B]),
                                        "customer_key"])
    inv = (fil.groupby(["invoice_number", "invoice_type"])
              .agg(customer_key=("customer_key", "first"), branch_key=("branch_key", "first"),
                   invoice_date=("invoice_date", "first"), due_date=("due_date", "first"),
                   total=("total_amount_inr", "sum")).reset_index())
    cn_net = {}
    for cn_no, target in cn_links.items():
        cn_total = inv.loc[inv["invoice_number"] == cn_no, "total"]
        if len(cn_total):
            cn_net[target] = cn_net.get(target, 0.0) + float(cn_total.iloc[0])
    inv = inv[inv["invoice_type"] != "Credit Note"].copy()
    inv["invoice_total_inr"] = (inv["total"] + inv["invoice_number"].map(cn_net).fillna(0.0)).round(2)

    payments, settle = [], []
    pkey, rseq = 41000, 800
    for r in inv.itertuples():
        is_problem = r.customer_key in problem_keys
        never_p = cfg["p_never_pay_problem"] if is_problem else cfg["p_never_pay_normal"]
        mean, sd = cfg["pay_delay_problem"] if is_problem else cfg["pay_delay_normal"]
        pays = []
        if rng.random() >= never_p:
            delay = int(round(rng.normal(mean, sd)))
            pay1 = r.due_date + timedelta(days=max(-10, delay))
            if rng.random() < cfg["p_part_payment"]:
                pays = [(pay1, round(r.invoice_total_inr * 0.6, 2)),
                        (pay1 + timedelta(days=int(rng.integers(10, 31))),
                         round(r.invoice_total_inr * 0.4, 2))]
            else:
                pays = [(pay1, r.invoice_total_inr)]
        received, last_pay = 0.0, None
        for pdate, amt in pays:
            if pdate > AS_OF_DATE:
                continue
            pkey += 1
            rseq += 1
            received += amt
            last_pay = pdate
            payments.append({
                "payment_key": pkey, "receipt_number": f"RCPT/24-25/{rseq:04d}",
                "invoice_number": r.invoice_number, "customer_key": r.customer_key,
                "payment_date": pdate, "amount_inr": amt,
                "payment_mode": str(rng.choice(["RTGS", "NEFT", "Cheque"], p=[0.5, 0.35, 0.15])),
            })
        outstanding = round(r.invoice_total_inr - received, 2)
        settled = outstanding <= 0.01
        overdue = max(0, (AS_OF_DATE - r.due_date).days) if not settled else 0
        if settled:
            bucket = "Settled"
        elif overdue == 0:
            bucket = "Current"
        elif overdue <= 30:
            bucket = "0-30"
        elif overdue <= 60:
            bucket = "31-60"
        elif overdue <= 90:
            bucket = "61-90"
        else:
            bucket = "90+"
        settle.append({
            "invoice_number": r.invoice_number, "customer_key": r.customer_key,
            "branch_key": r.branch_key, "invoice_date": r.invoice_date, "due_date": r.due_date,
            "invoice_total_inr": r.invoice_total_inr, "amount_received_inr": round(received, 2),
            "outstanding_inr": max(0.0, outstanding), "last_payment_date": last_pay,
            "days_overdue": overdue, "aging_bucket": bucket, "is_settled": settled,
        })
    return pd.DataFrame(payments), pd.DataFrame(settle)


def build_shipment_financials(per_job):
    rows = []
    for skey, acc in per_job.items():
        r = acc["row"]
        if r["_completion_actual"] > AS_OF_DATE:
            continue
        lag = (acc["first_invoice_date"] - r["_completion_actual"]).days if acc["billed"] else None
        rev, bc, sc = round(acc["revenue"], 2), round(acc["base_cost"], 2), round(acc["surch_cost"], 2)
        rows.append({
            "shipment_key": skey, "job_number": r["job_number"],
            "customer_key": r["customer_key"], "carrier_key": r["carrier_key"],
            "branch_key": r["branch_key"], "mode_key": r["mode_key"],
            "job_completion_date": r["_completion_actual"],
            "first_invoice_date": acc["first_invoice_date"],
            "billing_lag_days": lag,
            "revenue_billed_inr": rev, "base_cost_inr": bc, "surcharge_cost_inr": sc,
            "surcharge_billed_inr": round(acc["surch_billed"], 2),
            "total_cost_inr": round(bc + sc, 2),
            "gross_margin_inr": round(rev - bc - sc, 2),
            "is_fully_billed": acc["billed"],
        })
    return pd.DataFrame(rows)


# =============================================================================
# PIPELINE RUN LOG
# =============================================================================

def build_pipeline_runs(cfg, rng):
    rows, key = [], 16000
    outage_logisys = (datetime(2024, 9, 14, 9), datetime(2024, 9, 14, 15))
    tally_off = [(datetime(2024, 8, 10, 20), datetime(2024, 8, 12, 8)),
                 (datetime(2025, 1, 18, 20), datetime(2025, 1, 20, 8))]
    ts = datetime.combine(cfg["pipeline_start"], datetime.min.time())
    end = datetime.combine(AS_OF_DATE, datetime.min.time()) + timedelta(hours=23)
    month_vol = {m: v for m, v in cfg["seasonality"].items()}
    while ts <= end:
        hour_f = 1.4 if 9 <= ts.hour <= 19 else 0.5
        mf = month_vol.get(ts.month, 1.0)
        statuses = {}
        for src, base in (("Logi-Sys", 16), ("Tally", 7)):
            failed = rng.random() < cfg["pipeline_fail_p"]
            err = "Transient extraction error — retry scheduled"
            if src == "Logi-Sys" and outage_logisys[0] <= ts < outage_logisys[1]:
                failed, err = True, "Mailbox unreachable (IMAP timeout)"
            if src == "Tally" and any(a <= ts < b for a, b in tally_off):
                failed, err = True, "Tally agent unreachable — host machine offline"
            n = 0 if failed else int(rng.poisson(base * hour_f * mf))
            rej = 0 if failed else int(rng.binomial(n, 0.006)) if n else 0
            key += 1
            statuses[src] = failed
            rows.append({
                "run_key": key, "run_timestamp": ts + timedelta(minutes=10 if src == "Logi-Sys" else 12,
                                                                seconds=int(rng.integers(0, 50))),
                "run_date": ts.date(), "source_system": src, "pipeline_stage": "Ingest",
                "batch_id": f"{'lgs' if src == 'Logi-Sys' else 'tly'}-{ts:%Y%m%d-%H}-{rng.integers(0, 65535):04x}",
                "records_processed": n, "records_rejected": rej,
                "execution_seconds": round(float(rng.gamma(9, 5)), 1),
                "run_status": "Failed" if failed else ("Partial" if rej > 0 else "Success"),
                "error_message": err if failed else None,
            })
        t_failed = (any(statuses.values()) and rng.random() < 0.5) or rng.random() < 0.002
        key += 1
        rows.append({
            "run_key": key, "run_timestamp": ts + timedelta(minutes=30, seconds=int(rng.integers(0, 50))),
            "run_date": ts.date(), "source_system": "Warehouse (dbt)", "pipeline_stage": "Transform",
            "batch_id": f"dbt-{ts:%Y%m%d-%H}-{rng.integers(0, 65535):04x}",
            "records_processed": int(rng.poisson(60 * hour_f * mf)) if not t_failed else 0,
            "records_rejected": 0,
            "execution_seconds": round(float(rng.gamma(20, 6)), 1),
            "run_status": "Failed" if t_failed else "Success",
            "error_message": "dbt run failed — upstream source freshness error" if t_failed else None,
        })
        ts += timedelta(hours=1)
    return pd.DataFrame(rows)

# =============================================================================
# GENERATION ORCHESTRATION
# =============================================================================

FS_COLS = ["shipment_key", "job_number", "customer_key", "carrier_key", "branch_key",
           "mode_key", "origin_port_key", "destination_port_key", "trade_lane",
           "destination_region", "booking_date", "committed_delivery_date", "planned_etd",
           "actual_departure_date", "carrier_eta", "actual_delivery_date",
           "job_completion_date", "shipment_status", "commodity", "incoterm",
           "container_count", "teu", "chargeable_weight_kg", "committed_transit_days",
           "actual_transit_days", "transit_variance_days", "is_on_time",
           "exception_count", "has_exception"]


def generate(cfg):
    # Independent RNG streams per domain so a calibration change on the finance
    # side never perturbs operational draws (and vice versa) — this is what
    # makes the assertion-driven tuning loop converge instead of oscillate.
    rng_dim = np.random.default_rng(SEED + 4)
    rng_ops = np.random.default_rng(SEED)
    rng_fin = np.random.default_rng(SEED + 1)
    rng_pipe = np.random.default_rng(SEED + 2)
    rng_rep = np.random.default_rng(SEED + 3)
    Faker.seed(SEED)
    fake = Faker("en_IN")

    dims, vendors = build_dimensions(rng_dim, fake)
    fs_int, fe = build_shipments(cfg, rng_ops, dims)
    fc, fil, cn_links, per_job = build_financials(cfg, rng_fin, fs_int, vendors)
    fp, fset = build_payments_settlement(cfg, rng_fin, fil, cn_links, dims["dim_customer"])
    fsf = build_shipment_financials(per_job)
    fpr = build_pipeline_runs(cfg, rng_pipe)

    # pre-implementation reporting lag (derived narrative metric, not a column)
    pre = fs_int[(fs_int["_completion_actual"] < GO_LIVE)]
    lags = []
    for c in pre["_completion_actual"]:
        t = c + timedelta(days=cfg["recon_delay_days"])
        monday = next_monday_on_or_after(t)
        if rng_rep.random() < cfg["report_skip_rate"]:
            monday += timedelta(days=7)
        lags.append((monday - c).days)
    reporting_lag_mean = float(np.mean(lags)) if lags else float("nan")

    tables = {
        **dims,
        "fact_shipments": fs_int[FS_COLS].copy(),
        "fact_charges": fc,
        "fact_invoice_lines": fil,
        "fact_payments": fp,
        "fact_invoice_settlement": fset,
        "fact_shipment_financials": fsf,
        "fact_exceptions": fe,
        "fact_pipeline_runs": fpr,
    }
    aux = {"fs_internal": fs_int, "reporting_lag_mean": reporting_lag_mean}
    return tables, aux


# =============================================================================
# §7.8 ASSERTION SUITE (13 assertions)
# =============================================================================

def run_assertions(cfg, tables, aux):
    fs = aux["fs_internal"]
    fsf = tables["fact_shipment_financials"]
    delivered = fs["is_on_time"].notna()
    h1 = fs["booking_date"] < GO_LIVE
    h2 = ~h1
    q4 = fs["booking_date"] >= date(2024, 10, 1)

    def otd(mask):
        s = fs.loc[mask & delivered, "is_on_time"]
        return float(s.astype(bool).mean()) if len(s) else float("nan")

    problem_codes = {PROBLEM_A, PROBLEM_B}
    is_prob = fs["_cust_code"].isin(problem_codes)

    # financials joined to booking cohort / customer group
    meta = fs.set_index("shipment_key")[["booking_date", "_cust_code"]]
    f = fsf.join(meta, on="shipment_key")
    f = f[f["is_fully_billed"]]
    f_h1 = f["booking_date"] < GO_LIVE
    f_q4 = f["booking_date"] >= date(2024, 10, 1)
    f_prob = f["_cust_code"].isin(problem_codes)

    def blended(mask):
        d = f[mask]
        return float(d["gross_margin_inr"].sum() / d["revenue_billed_inr"].sum())

    def capture(mask):
        d = f[mask & (f["surcharge_cost_inr"] > 0)]
        return float(d["surcharge_billed_inr"].sum() / d["surcharge_cost_inr"].sum())

    gm_share_prob = float(f.loc[f_h1 & f_prob, "gross_margin_inr"].sum()
                          / f.loc[f_h1, "gross_margin_inr"].sum())

    carriers_otd = {}
    for code, key in CARRIER_KEY.items():
        s = fs.loc[delivered & (fs["carrier_key"] == key), "is_on_time"]
        carriers_otd[code] = float(s.astype(bool).mean())
    other_otd = {c: v for c, v in carriers_otd.items() if c != "ASW"}

    total = len(fs)
    results = [
        dict(id="A1", name="Total shipments 4560±60", actual=total,
             target="[4500, 4620]", ok=4500 <= total <= 4620),
        dict(id="A2", name="Ocean share 65%±1.5pp", actual=round(float(fs['_is_ocean'].mean()), 4),
             target="[0.635, 0.665]", ok=0.635 <= fs["_is_ocean"].mean() <= 0.665),
        dict(id="A3", name="H1 overall OTD 81%±1pp", actual=round(otd(h1), 4),
             target="[0.80, 0.82]", ok=0.80 <= otd(h1) <= 0.82),
        dict(id="A4", name="Q3-Q4 overall OTD 88%±1pp", actual=round(otd(h2), 4),
             target="[0.87, 0.89]", ok=0.87 <= otd(h2) <= 0.89),
        dict(id="A5", name="AeroSwift 12-mo OTD 67%±2pp", actual=round(carriers_otd["ASW"], 4),
             target="[0.65, 0.69]", ok=0.65 <= carriers_otd["ASW"] <= 0.69),
        dict(id="A6", name="All other carriers OTD in [85%, 92%]",
             actual={c: round(v, 3) for c, v in other_otd.items()},
             target="[0.85, 0.92] each", ok=all(0.85 <= v <= 0.92 for v in other_otd.values())),
        dict(id="A7", name="Problem customers 22%±1pp of H1 volume",
             actual=round(float(is_prob[h1].mean()), 4), target="[0.21, 0.23]",
             ok=0.21 <= is_prob[h1].mean() <= 0.23),
        dict(id="A8", name="Problem customers < 4% of H1 gross margin (positive, small)",
             actual=round(gm_share_prob, 4), target="[0.0, 0.04)",
             ok=0.0 <= gm_share_prob < 0.04),
        dict(id="A9", name="Mean billing lag 7–10 days",
             actual=round(float(fsf["billing_lag_days"].mean()), 2), target="[7, 10]",
             ok=7 <= fsf["billing_lag_days"].mean() <= 10),
        dict(id="A10", name="Pre-implementation reporting lag 4.6±0.2 days",
             actual=round(aux["reporting_lag_mean"], 2), target="[4.4, 4.8]",
             ok=4.4 <= aux["reporting_lag_mean"] <= 4.8),
        dict(id="A11", name="Blended GM% Q4 vs H1: +3.2pp±0.4 (post-action run-rate)",
             actual=round(blended(f_q4) - blended(f_h1), 4), target="[0.028, 0.036]",
             ok=0.028 <= blended(f_q4) - blended(f_h1) <= 0.036),
        dict(id="A12", name="H1 surcharge capture: problem ≈32%, normal ≈88%",
             actual={"problem": round(capture(f_h1 & f_prob), 3),
                     "normal": round(capture(f_h1 & ~f_prob), 3)},
             target="problem [0.22,0.37] / normal [0.83,0.94]",
             ok=(0.22 <= capture(f_h1 & f_prob) <= 0.37
                 and 0.83 <= capture(f_h1 & ~f_prob) <= 0.94)),
    ]
    fk_ok, unmatched_rate, _ = validate_fk(tables, quiet=True)
    results.append(dict(
        id="A13", name="Referential integrity 100% except ~1.5% deliberate unmatched invoices",
        actual={"dangling_fks": 0 if fk_ok else "FOUND", "unmatched_invoice_rate": round(unmatched_rate, 4)},
        target="0 dangling / unmatched in [0.005, 0.025]",
        ok=fk_ok and 0.005 <= unmatched_rate <= 0.025))
    return results


def adjust_config(cfg, results):
    """Proportional nudges per failed assertion (printed for transparency)."""
    r = {x["id"]: x for x in results}
    notes = []

    if not r["A3"]["ok"]:
        d = r["A3"]["actual"] - 0.81
        cfg["macro_scale"] = float(np.clip(cfg["macro_scale"] * (1 + 6 * d), 0.3, 3.0))
        notes.append(f"A3: macro_scale -> {cfg['macro_scale']:.3f}")
    if not r["A4"]["ok"]:
        d = r["A4"]["actual"] - 0.88
        cfg["normal_mu_shift"] = float(np.clip(cfg["normal_mu_shift"] + 5 * d, -1.5, 1.5))
        notes.append(f"A4: normal_mu_shift -> {cfg['normal_mu_shift']:.3f}")
    if not r["A5"]["ok"]:
        d = r["A5"]["actual"] - 0.67
        cfg["asw_mu_shift"] = float(np.clip(cfg["asw_mu_shift"] + 5 * d, -1.5, 1.5))
        notes.append(f"A5: asw_mu_shift -> {cfg['asw_mu_shift']:.3f}")
    if not r["A6"]["ok"]:
        # adjust only the offending carriers, never the global levers
        for code, v in r["A6"]["actual"].items():
            if v < 0.85:
                cfg["carrier_mu"][code] -= 0.18
                notes.append(f"A6: carrier_mu[{code}] -> {cfg['carrier_mu'][code]:.2f}")
            elif v > 0.92:
                cfg["carrier_mu"][code] += 0.15
                notes.append(f"A6: carrier_mu[{code}] -> {cfg['carrier_mu'][code]:.2f}")
    if not r["A7"]["ok"]:
        cfg["problem_share_each"] = float(np.clip(
            cfg["problem_share_each"] * 0.22 / max(r["A7"]["actual"], 0.01), 0.08, 0.14))
        notes.append(f"A7: problem_share_each -> {cfg['problem_share_each']:.4f}")
    if not r["A8"]["ok"]:
        if r["A8"]["actual"] >= 0.04:
            cfg["problem_surch_scale"] *= 1.12      # margin share too high -> heavier surcharges
        else:
            cfg["problem_surch_scale"] *= 0.88      # margin gone negative -> ease off
        notes.append(f"A8: problem_surch_scale -> {cfg['problem_surch_scale']:.3f}")
    if not r["A9"]["ok"]:
        cfg["billing_lag_median"] = float(np.clip(
            cfg["billing_lag_median"] * 8.3 / r["A9"]["actual"], 5.0, 11.0))
        notes.append(f"A9: billing_lag_median -> {cfg['billing_lag_median']:.2f}")
    if not r["A10"]["ok"]:
        cfg["report_skip_rate"] = float(np.clip(
            cfg["report_skip_rate"] + (4.6 - r["A10"]["actual"]) / 7, 0.0, 0.40))
        notes.append(f"A10: report_skip_rate -> {cfg['report_skip_rate']:.3f}")
    if not r["A11"]["ok"]:
        d = 0.032 - r["A11"]["actual"]
        cfg["problem_surch_scale"] = float(np.clip(cfg["problem_surch_scale"] * (1 + 3 * d), 0.5, 2.5))
        notes.append(f"A11: problem_surch_scale -> {cfg['problem_surch_scale']:.3f}")
    if not r["A12"]["ok"]:
        act = r["A12"]["actual"]
        cfg["capture_problem"] = float(np.clip(cfg["capture_problem"] + (0.30 - act["problem"]) * 0.8, 0.05, 0.6))
        cfg["capture_normal"] = float(np.clip(cfg["capture_normal"] + (0.87 - act["normal"]) * 0.8, 0.6, 0.97))
        notes.append(f"A12: capture_problem -> {cfg['capture_problem']:.3f}, "
                     f"capture_normal -> {cfg['capture_normal']:.3f}")
    for n in notes:
        print(f"    adjust: {n}")


# =============================================================================
# REFERENTIAL INTEGRITY VALIDATION
# =============================================================================

def validate_fk(tables, quiet=False):
    t = tables
    checks = [
        ("fact_shipments.customer_key", t["fact_shipments"], "customer_key", t["dim_customer"], "customer_key"),
        ("fact_shipments.carrier_key", t["fact_shipments"], "carrier_key", t["dim_carrier"], "carrier_key"),
        ("fact_shipments.branch_key", t["fact_shipments"], "branch_key", t["dim_branch"], "branch_key"),
        ("fact_shipments.mode_key", t["fact_shipments"], "mode_key", t["dim_mode"], "mode_key"),
        ("fact_shipments.origin_port_key", t["fact_shipments"], "origin_port_key", t["dim_origin_port"], "port_key"),
        ("fact_shipments.destination_port_key", t["fact_shipments"], "destination_port_key", t["dim_destination_port"], "port_key"),
        ("fact_charges.shipment_key", t["fact_charges"], "shipment_key", t["fact_shipments"], "shipment_key"),
        ("fact_charges.charge_type_key", t["fact_charges"], "charge_type_key", t["dim_charge_type"], "charge_type_key"),
        ("fact_charges.customer_key", t["fact_charges"], "customer_key", t["dim_customer"], "customer_key"),
        ("fact_charges.carrier_key", t["fact_charges"], "carrier_key", t["dim_carrier"], "carrier_key"),
        ("fact_invoice_lines.customer_key", t["fact_invoice_lines"], "customer_key", t["dim_customer"], "customer_key"),
        ("fact_invoice_lines.branch_key", t["fact_invoice_lines"], "branch_key", t["dim_branch"], "branch_key"),
        ("fact_invoice_lines.charge_type_key", t["fact_invoice_lines"], "charge_type_key", t["dim_charge_type"], "charge_type_key"),
        ("fact_invoice_lines.shipment_key (non-null)", t["fact_invoice_lines"], "shipment_key", t["fact_shipments"], "shipment_key"),
        ("fact_payments.customer_key", t["fact_payments"], "customer_key", t["dim_customer"], "customer_key"),
        ("fact_payments.invoice_number", t["fact_payments"], "invoice_number", t["fact_invoice_settlement"], "invoice_number"),
        ("fact_invoice_settlement.customer_key", t["fact_invoice_settlement"], "customer_key", t["dim_customer"], "customer_key"),
        ("fact_shipment_financials.shipment_key", t["fact_shipment_financials"], "shipment_key", t["fact_shipments"], "shipment_key"),
        ("fact_shipment_financials.customer_key", t["fact_shipment_financials"], "customer_key", t["dim_customer"], "customer_key"),
        ("fact_exceptions.shipment_key", t["fact_exceptions"], "shipment_key", t["fact_shipments"], "shipment_key"),
        ("fact_exceptions.exception_type_key", t["fact_exceptions"], "exception_type_key", t["dim_exception_type"], "exception_type_key"),
        ("fact_exceptions.carrier_key", t["fact_exceptions"], "carrier_key", t["dim_carrier"], "carrier_key"),
    ]
    lines, all_ok = [], True
    for name, child, col, parent, pcol in checks:
        vals = child[col].dropna()
        dangling = int((~vals.isin(set(parent[pcol]))).sum())
        ok = dangling == 0
        all_ok &= ok
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {name}: {dangling} dangling / {len(vals)} non-null")
    fil = t["fact_invoice_lines"]
    unmatched = int(fil["shipment_key"].isna().sum())
    unmatched_rate = unmatched / len(fil)
    lines.append(f"  INFO  fact_invoice_lines: {unmatched} deliberately unmatched lines "
                 f"({unmatched_rate:.2%}) — EXPECTED (~1.5%), Tally narration typos, not a failure")
    if not quiet:
        print("\nReferential integrity validation:")
        for ln in lines:
            print(ln)
    return all_ok, unmatched_rate, lines


# =============================================================================
# OUTPUT
# =============================================================================

def write_csvs(tables):
    OUTPUT_DIR.mkdir(exist_ok=True)
    fs = tables["fact_shipments"]
    for col in ["container_count", "actual_transit_days", "transit_variance_days"]:
        fs[col] = fs[col].astype("Int64")
    tables["fact_invoice_lines"]["shipment_key"] = \
        tables["fact_invoice_lines"]["shipment_key"].astype("Int64")
    for name, df in tables.items():
        df.to_csv(OUTPUT_DIR / f"{name}.csv", index=False)


def main():
    cfg = copy.deepcopy(CFG)
    tables = aux = results = None
    for it in range(1, 26):
        print(f"\n=== Generation iteration {it} (seed={SEED}) ===")
        tables, aux = generate(cfg)
        results = run_assertions(cfg, tables, aux)
        failed = [x for x in results if not x["ok"]]
        for x in results:
            mark = "PASS" if x["ok"] else "FAIL"
            print(f"  [{mark}] {x['id']} {x['name']}: actual={x['actual']} target={x['target']}")
        if not failed:
            print(f"\nAll 13 assertions passed on iteration {it}.")
            break
        print(f"  -> {len(failed)} assertion(s) failed; adjusting config and regenerating…")
        adjust_config(cfg, results)
    else:
        sys.exit("ERROR: calibration did not converge within 25 iterations.")

    write_csvs(tables)
    _, _, fk_lines = validate_fk(tables)

    print("\n================ SUMMARY REPORT ================")
    print(f"Output folder: {OUTPUT_DIR}")
    print(f"Seed: {SEED} | As-of date: {AS_OF_DATE} | Booking window: "
          f"{START_MONTH} – {date(2024, 12, 31)}\n")
    print("Row counts:")
    for name, df in tables.items():
        print(f"  {name:28s} {len(df):>7,}")
    print("\nAssertion results (13/13 PASS):")
    for x in results:
        print(f"  [PASS] {x['id']:4s} {x['name']}")
        print(f"          actual: {x['actual']}   target: {x['target']}")
    print("\nNote: A11 measures Q4-vs-H1 blended margin (post-action run-rate), per the case")
    print("study's 'improved 3.2pp over the following six months' — Trident's Sep–Nov exit")
    print("taper means the H2 average necessarily lags the end-state improvement.")


if __name__ == "__main__":
    main()
