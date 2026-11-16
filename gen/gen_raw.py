"""
Make-Goods Agent — synthetic source-system extracts (Phase 0.1).

Simulates what a TV station group's traffic, audience, measurement and order systems would export,
landed as files for Auto Loader:

  raw/
    stations/            stations.csv
    advertisers/         advertisers.csv
    contracts/           contracts_batch_*.json   -> bronze_contracts
    as_run_logs/         as_run_<date>.csv        -> bronze_ad_schedule   (planted DQ issues)
    inventory_avails/    avails_<isoweek>.csv     -> bronze_inventory_avails
    audience_segments/   segments.json            -> bronze_audience_segments (taxonomy export)
    delivery_pacing/     pacing_<date>.csv        -> bronze_delivery_pacing  (hourly grain)
    make_good_offers/    offers_<isoweek>.json    -> bronze_make_good_offers (history)
    reference/           make_good_policy.md
  staging/day2/          Oct 29-30 batch held back for the live incremental-load demo

Story, as of Fri 2026-10-30 (the weekend before the Nov 3 midterms):
  * College-football Saturdays / NFL Sundays overrun into Prime Access, Prime and Late Fringe
    on FOX/ABC/CBS affiliates -> Sports Overrun preemptions every weekend.
  * Hurricane week (Oct 5-9) in 4 Gulf markets -> Breaking News preemptions.
  * Political windows from Oct 15 in 6 battleground markets -> Political Priority bumps.
  * KTVR has a bad master-control week (Oct 12-18) -> Technical "Missed" spots.
  * The inherited rules engine only looks 3 days ahead. When it finds nothing it offers a
    weaker daypart, advertisers reject it, and the station group refunds -- even though commensurate
    inventory usually existed later in the 14-day window. That is the yield leak.
  * gpt_assist offers burned ~20-40K tokens each: the cost baseline for the new agent.
  * Hero for the live demo: Ironwood Motors (new advertiser, loves sports adjacency) had two
    Prime spots on a FOX affiliate preempted by college football / NFL overruns on Oct 24-25.
"""
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent / "data"
OUT, STAGE = ROOT / "raw", ROOT / "staging" / "day2"
shutil.rmtree(ROOT, ignore_errors=True)

rng = np.random.default_rng(7)

START, DAY1_END, AS_OF = date(2026, 9, 28), date(2026, 10, 28), date(2026, 10, 30)
AVAIL_END = AS_OF + timedelta(days=21)


def mk(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def d64(d):
    return np.datetime64(d, "D")


# ---------------------------------------------------------------- dayparts & programming
DAYPARTS = ["Early Morning", "Daytime", "Early Fringe", "Prime Access", "Prime", "Late Fringe"]
# value order from make_good_policy.md (low -> high)
DP_RANK = {"Daytime": 1, "Early Morning": 2, "Early Fringe": 3, "Late Fringe": 4, "Prime Access": 5, "Prime": 6}
WEAKER = {"Prime": "Prime Access", "Prime Access": "Late Fringe", "Late Fringe": "Early Fringe",
          "Early Fringe": "Early Morning", "Early Morning": "Daytime"}
DP_RATE = {"Early Morning": 140, "Daytime": 110, "Early Fringe": 320, "Prime Access": 560, "Prime": 1450, "Late Fringe": 420}
DP_RATING = {"Early Morning": 1.8, "Daytime": 1.4, "Early Fringe": 3.1, "Prime Access": 4.2, "Prime": 6.5, "Late Fringe": 2.9}
DP_HOURS = {"Early Morning": (5, 9), "Daytime": (9, 16), "Early Fringe": (16, 19), "Prime Access": (19, 20), "Prime": (20, 23), "Late Fringe": (23, 25)}
DP_WEIGHT = np.array([0.10, 0.18, 0.20, 0.14, 0.26, 0.12])
# open avails per station-day per daypart: Prime / Prime Access / Late Fringe are scarce
DP_AVAILS = {"Early Morning": 5, "Daytime": 8, "Early Fringe": 3, "Prime Access": 1.2, "Prime": 0.6, "Late Fringe": 1.2}

PROGRAMS = {  # daypart -> [(program, genre, adjacency_tags)]
    "Early Morning": [("Good Morning Local", "News", "news|local|weather"), ("Morning Network Show", "Talk/Lifestyle", "lifestyle|family")],
    "Daytime": [("Daytime Court", "Syndicated", "court|talk"), ("Local Midday News", "News", "news|local|weather"),
                ("Daytime Talk", "Talk/Lifestyle", "talk|lifestyle|family")],
    "Early Fringe": [("Local News at 5", "News", "news|local|weather"), ("Game Show Hour", "Entertainment", "game_show|family")],
    "Prime Access": [("Entertainment Tonight", "Entertainment", "entertainment|celebrity"), ("Local News at 7", "News", "news|local")],
    "Prime": [("Network Prime Drama", "Entertainment", "entertainment|drama"),
              ("Network Prime Comedy", "Entertainment", "entertainment|comedy|family")],
    "Late Fringe": [("Late Local News", "News", "news|local|weather"), ("Late Night Talk", "Entertainment", "late_night|comedy")],
}
SPORTS_PROG = {5: ("College Football Primetime", "Sports", "sports|live"), 6: ("NFL Overtime", "Sports", "sports|live")}
SPORTS_NETS = {5: ["FOX", "ABC"], 6: ["FOX", "CBS"]}
SPORTS_DP = ["Prime Access", "Prime", "Late Fringe"]
GENRES = ["News", "Sports", "Entertainment", "Syndicated", "Talk/Lifestyle"]
TAGS = ["news", "local", "weather", "sports", "live", "entertainment", "drama", "comedy", "family",
        "late_night", "lifestyle", "court", "talk", "game_show", "celebrity"]


def pick_programs(dp_arr, dow_arr, net_arr, sports_p):
    n = len(dp_arr)
    prog, genre, tags = (np.empty(n, dtype=object) for _ in range(3))
    for d, opts in PROGRAMS.items():
        m = dp_arr == d
        k = rng.integers(0, len(opts), m.sum())
        prog[m] = [opts[i][0] for i in k]
        genre[m] = [opts[i][1] for i in k]
        tags[m] = [opts[i][2] for i in k]
    for dow, nets in SPORTS_NETS.items():
        m = (dow_arr == dow) & np.isin(net_arr, nets) & np.isin(dp_arr, SPORTS_DP) & (rng.random(n) < sports_p)
        p, g, t = SPORTS_PROG[dow]
        prog[m], genre[m], tags[m] = p, g, t
    return prog, genre, tags


# ---------------------------------------------------------------- audience taxonomy
SEGMENTS = [  # id, code, name, gender, age_min, age_max, universe factor vs TV households
    ("SEG-001", "A18-49", "Adults 18-49", "A", 18, 49, 1.05),
    ("SEG-002", "A25-54", "Adults 25-54", "A", 25, 54, 1.00),
    ("SEG-003", "A35-64", "Adults 35-64", "A", 35, 64, 0.95),
    ("SEG-004", "W25-54", "Women 25-54", "W", 25, 54, 0.52),
    ("SEG-005", "M25-54", "Men 25-54", "M", 25, 54, 0.48),
    ("SEG-006", "HH", "Households", "H", None, None, 1.00),
]
SEG_CODES = [s[1] for s in SEGMENTS]
SEG_FACTOR = {s[1]: s[6] for s in SEGMENTS}
BUYABLE = SEG_CODES[:5]
# segment rating = household rating x daypart index x genre index
DP_INDEX = {
    "A18-49": dict(zip(DAYPARTS, [0.80, 0.70, 0.85, 0.95, 1.10, 1.15])),
    "A25-54": dict(zip(DAYPARTS, [0.95, 0.85, 1.00, 1.00, 1.05, 1.05])),
    "A35-64": dict(zip(DAYPARTS, [1.10, 1.00, 1.10, 1.05, 1.00, 0.95])),
    "W25-54": dict(zip(DAYPARTS, [1.00, 1.20, 1.05, 1.05, 1.00, 0.90])),
    "M25-54": dict(zip(DAYPARTS, [0.90, 0.70, 0.95, 0.95, 1.05, 1.10])),
    "HH": dict(zip(DAYPARTS, [1.0] * 6)),
}
GENRE_INDEX = {
    "Sports":         {"A18-49": 1.15, "A25-54": 1.10, "A35-64": 1.05, "W25-54": 0.75, "M25-54": 1.40, "HH": 1.0},
    "News":           {"A18-49": 0.85, "A25-54": 1.00, "A35-64": 1.20, "W25-54": 1.00, "M25-54": 0.95, "HH": 1.0},
    "Entertainment":  {"A18-49": 1.05, "A25-54": 1.00, "A35-64": 0.95, "W25-54": 1.15, "M25-54": 0.90, "HH": 1.0},
    "Syndicated":     {"A18-49": 0.90, "A25-54": 0.95, "A35-64": 1.05, "W25-54": 1.15, "M25-54": 0.85, "HH": 1.0},
    "Talk/Lifestyle": {"A18-49": 0.85, "A25-54": 0.95, "A35-64": 1.05, "W25-54": 1.25, "M25-54": 0.75, "HH": 1.0},
}
ALIASES = {  # published in the taxonomy export (exact-match aliases)
    "A18-49": ["Adults 18-49", "P18-49", "Persons 18-49", "A 18-49"],
    "A25-54": ["Adults 25-54", "P25-54", "Persons 25-54", "A 25-54"],
    "A35-64": ["Adults 35-64", "P35-64", "Persons 35-64", "A 35-64"],
    "W25-54": ["Women 25-54", "F25-54", "Females 25-54", "W 25-54"],
    "M25-54": ["Men 25-54", "Males 25-54", "M 25-54"],
    "HH": ["Households", "Homes", "TV Households"],
}
MESSY = {  # what sales actually typed into orders -- only resolvable by normalization
    "A18-49": ["adults 18 to 49", "18-49 Adults", "ADULTS18-49"],
    "A25-54": ["Adults 25 to 54", "25-54 adults", "adults 25–54"],
    "A35-64": ["Adults 35 to 64", "35-64 Adults", "a35 - 64"],
    "W25-54": ["women 25 to 54", "Female 25-54", "W25 - 54"],
    "M25-54": ["men 25 to 54", "Male 25-54", "M25 - 54"],
}
GARBAGE = ["TBD", "Fall promo demo", "see agency RFP", "Primary target"]


def audience_text(code):
    u = rng.random()
    if u < 0.70:
        return code
    if u < 0.90:
        return str(rng.choice(ALIASES[code]))
    if u < 0.97:
        return str(rng.choice(MESSY[code]))
    return str(rng.choice(GARBAGE))


# ---------------------------------------------------------------- stations (40)
markets = [
    ("Seattle-Tacoma", 13), ("Baltimore", 26), ("Pittsburgh", 27), ("Nashville", 29), ("Columbus OH", 32),
    ("Salt Lake City", 30), ("San Antonio", 31), ("Milwaukee", 36), ("Cincinnati", 37), ("Oklahoma City", 39),
    ("Raleigh-Durham", 24), ("Las Vegas", 40), ("Birmingham", 44), ("Greenville-Spartanburg", 35), ("Richmond", 54),
    ("Tulsa", 60), ("Mobile-Pensacola", 58), ("New Orleans", 50), ("Little Rock", 57), ("Dayton", 64),
    ("Wichita", 67), ("Des Moines", 68), ("Charleston WV", 65), ("Flint-Saginaw", 73), ("Portland ME", 75),
    ("Syracuse", 87), ("Baton Rouge", 89), ("Champaign-Springfield", 90), ("Savannah", 91), ("Tallahassee", 106),
    ("Corpus Christi", 129), ("Peoria", 117), ("Columbus GA", 125), ("Lafayette LA", 121), ("Yakima", 122),
    ("Eugene", 119), ("Amarillo", 131), ("Macon", 120), ("Beaumont-Port Arthur", 142), ("Medford", 136),
]
networks = rng.choice(["FOX", "ABC", "CBS", "NBC", "CW"], size=40, p=[0.30, 0.28, 0.18, 0.12, 0.12])
letters = list("ABCDEFGHIJKLMNOPRSTUVWXYZ")
calls, seen = [], set()
for i in range(40):
    while True:
        c = ("W" if i % 2 == 0 else "K") + "".join(rng.choice(letters, 3))
        if c not in seen:
            seen.add(c)
            calls.append(c)
            break
calls[19] = "KTVR"  # the station with the master-control problem
stations = pd.DataFrame({
    "station_id": [f"STN-{i+1:03d}" for i in range(40)],
    "call_letters": calls,
    "market": [m for m, _ in markets],
    "dma_rank": [r for _, r in markets],
    "network_affiliation": networks,
    "timezone": rng.choice(["ET", "CT", "MT", "PT"], 40, p=[0.45, 0.35, 0.08, 0.12]),
})
GULF = {"Mobile-Pensacola", "New Orleans", "Baton Rouge", "Lafayette LA"}
POLITICAL = {"Pittsburgh", "Milwaukee", "Las Vegas", "Raleigh-Durham", "Flint-Saginaw", "Savannah"}
market_mult = np.round(np.clip(2.4 - np.log10(stations.dma_rank) * 0.9, 0.55, 1.9), 2).values
tv_hh_k = np.round(7500 * stations.dma_rank.values.astype(float) ** -0.65).astype(int)  # TV households (000)
has_acr = rng.random(40) < 0.65  # stations with set-top-box / ACR measurement (hourly pacing)
stations.to_csv(mk(OUT / "stations") / "stations.csv", index=False)

hero_s = int(stations.index[(stations.network_affiliation == "FOX") & stations.dma_rank.between(20, 70)
                            & ~stations.market.isin(GULF | POLITICAL) & (stations.call_letters != "KTVR")][0])

# ---------------------------------------------------------------- advertisers (150 + hero)
cats = ["Automotive", "Legal Services", "Healthcare", "Retail", "QSR / Restaurants", "Telecom",
        "Insurance", "Home Services", "Political", "Financial Services", "Entertainment", "CPG"]
cat_p = np.array([0.18, 0.10, 0.09, 0.12, 0.10, 0.06, 0.07, 0.08, 0.06, 0.05, 0.04, 0.05])
prefix = ["Summit", "Riverbend", "Lakeside", "Patriot", "Northstar", "Heritage", "Liberty", "Crescent", "Pioneer",
          "Keystone", "Evergreen", "Golden", "Coastal", "Valley", "Metro", "Prairie", "Harbor", "Ironwood", "Redstone", "Bluewater"]
suffix = {"Automotive": "Auto Group", "Legal Services": "Injury Law", "Healthcare": "Health", "Retail": "Furniture",
          "QSR / Restaurants": "Burgers", "Telecom": "Wireless", "Insurance": "Insurance", "Home Services": "Roofing",
          "Political": "for Senate PAC", "Financial Services": "Credit Union", "Entertainment": "Casino", "CPG": "Foods"}
CAT_SEG = {"Automotive": ["M25-54", "A25-54"], "Legal Services": ["A35-64", "A25-54"], "Healthcare": ["A35-64", "W25-54"],
           "Retail": ["W25-54", "A25-54"], "QSR / Restaurants": ["A18-49", "A25-54"], "Telecom": ["A18-49", "A25-54"],
           "Insurance": ["A35-64", "A25-54"], "Home Services": ["A35-64", "W25-54"], "Political": ["A35-64", "A25-54"],
           "Financial Services": ["A35-64", "A25-54"], "Entertainment": ["A18-49", "M25-54"], "CPG": ["W25-54", "A25-54"]}
CAT_PREFS = {"Legal Services": {"court": 0.6, "news": 0.3}, "Healthcare": {"news": 0.4, "late_night": -0.5},
             "Insurance": {"news": 0.4, "late_night": -0.5}, "Entertainment": {"family": -0.8, "sports": 0.4},
             "Automotive": {"sports": 0.5}, "QSR / Restaurants": {"sports": 0.3, "family": 0.3}, "Political": {"news": 0.5}}
N_ADV = 150
adv_cat = rng.choice(cats, N_ADV, p=cat_p / cat_p.sum())
adv_names = [f"{rng.choice(prefix)} {suffix[c]}" + (f" {i}" if i % 7 == 0 else "") for i, c in enumerate(adv_cat)]
advertisers = pd.DataFrame({
    "advertiser_id": [f"ADV-{i+1:04d}" for i in range(N_ADV)],
    "advertiser_name": adv_names,
    "category": adv_cat,
    "agency": rng.choice(["Direct", "Horizon Media", "GroupM", "Publicis", "Local Agency"], N_ADV, p=[0.35, 0.15, 0.2, 0.1, 0.2]),
    "tier": rng.choice(["Local", "Regional", "National"], N_ADV, p=[0.55, 0.25, 0.20]),
})
adv_primary = [str(rng.choice(CAT_SEG[c])) for c in adv_cat]

# hidden "true" advertiser preferences: drive human make-good choices and acceptance in history
true_prefs = []
for c in adv_cat:
    p = {str(t): float(np.round(rng.choice([-1, 1]) * rng.uniform(0.2, 0.5), 2))
         for t in rng.choice(TAGS, rng.integers(0, 3), replace=False)}
    p.update(CAT_PREFS.get(c, {}))
    true_prefs.append(p)
for c in ["Automotive", "QSR / Restaurants", "Entertainment"]:  # three sports-lovers with history
    true_prefs[int(np.where(adv_cat == c)[0][0])].update({"sports": 0.9, "live": 0.4})

HERO_ADV = N_ADV  # index 150 -> ADV-0151, brand-new advertiser (no make-good history)
advertisers.loc[HERO_ADV] = ["ADV-0151", "Ironwood Motors", "Automotive", "Direct", "Regional"]
adv_cat = np.append(adv_cat, "Automotive")
adv_primary.append("A25-54")
true_prefs.append({"sports": 0.9, "live": 0.5, "news": -0.3})
advertisers.to_csv(mk(OUT / "advertisers") / "advertisers.csv", index=False)

# ---------------------------------------------------------------- contracts (~2,000 + hero)
N_ORD = 2000
o_station = rng.integers(0, 40, N_ORD)
o_adv = rng.integers(0, N_ADV, N_ORD)
o_dp = rng.choice(DAYPARTS, N_ORD, p=DP_WEIGHT)
fstart = np.array([START + timedelta(days=int(x)) for x in rng.integers(-10, 30, N_ORD)])
flen = rng.integers(14, 56, N_ORD)
o_seg = np.where(rng.random(N_ORD) < 0.8, np.array(adv_primary[:N_ADV])[o_adv], rng.choice(BUYABLE, N_ORD))
o_len = rng.choice([15, 30, 60], N_ORD, p=[0.2, 0.72, 0.08])
o_spw = rng.integers(3, 18, N_ORD)
o_rate = np.round(np.array([DP_RATE[d] for d in o_dp]) * market_mult[o_station] * rng.lognormal(0, 0.25, N_ORD), 0).clip(40)
o_guar = np.round([DP_RATING[d] * DP_INDEX[s][d] * rng.uniform(0.85, 1.0) for d, s in zip(o_dp, o_seg)], 1)
o_window = rng.choice([14, 7, 21], N_ORD, p=[0.80, 0.15, 0.05])
o_cat = adv_cat[o_adv]
o_elig = np.where(o_cat == "Political", rng.random(N_ORD) > 0.40, rng.random(N_ORD) > 0.05)
o_preempt = rng.random(N_ORD) < 0.25

# hero order: Ironwood Motors, Prime on a FOX affiliate, flight Oct 19 - Dec 13
HERO_O = N_ORD
o_station = np.append(o_station, hero_s)
o_adv = np.append(o_adv, HERO_ADV)
o_dp = np.append(o_dp, "Prime")
fstart = np.append(fstart, date(2026, 10, 19))
flen = np.append(flen, 55)
o_seg = np.append(o_seg, "A25-54")
o_len = np.append(o_len, 30)
o_spw = np.append(o_spw, 10)
o_rate = np.append(o_rate, round(DP_RATE["Prime"] * market_mult[hero_s]))
o_guar = np.append(o_guar, 6.1)
o_window = np.append(o_window, 14)
o_cat = np.append(o_cat, "Automotive")
o_elig = np.append(o_elig, True)
o_preempt = np.append(o_preempt, False)
fend = np.array([s + timedelta(days=int(n)) for s, n in zip(fstart, flen)])
N_ALL = N_ORD + 1

universe_k = np.array([[int(tv_hh_k[s] * SEG_FACTOR[g]) for g in SEG_CODES] for s in range(40)])
SEG_POS = {g: i for i, g in enumerate(SEG_CODES)}
o_univ = universe_k[o_station, [SEG_POS[g] for g in o_seg]]
o_planned = np.round(o_spw * (flen + 1) / 7).astype(int)
o_seg_text = [audience_text(g) for g in o_seg]
o_seg_text[HERO_O] = "Adults 25-54"
contracts = pd.DataFrame({
    "order_id": [f"ORD-{i+1:05d}" for i in range(N_ALL)],
    "advertiser_id": advertisers.advertiser_id.values[o_adv],
    "station_id": stations.station_id.values[o_station],
    "campaign_name": [f"{advertisers.advertiser_name.values[a]} - Fall 2026" for a in o_adv],
    "daypart": o_dp,
    "spot_length_sec": o_len,
    "unit_rate_usd": o_rate,
    "spots_per_week": o_spw,
    "flight_start": [d.isoformat() for d in fstart],
    "flight_end": [d.isoformat() for d in fend],
    "audience_segment_text": o_seg_text,
    "guaranteed_rating": o_guar,
    "contracted_impressions": np.round(o_planned * o_guar / 100 * o_univ * 1000).astype(int),
    "makegood_window_days": o_window,
    "makegood_eligible": o_elig,
    "competitive_category": o_cat,
    "preemptible": o_preempt,
})
contracts.loc[HERO_O, "campaign_name"] = "Ironwood Motors - Holiday Sales Event"
# eval ground truth only (never read by the pipeline): ad ops labeled the intended segment of each order
pd.DataFrame({"order_id": contracts.order_id, "audience_segment_text": contracts.audience_segment_text,
              "labeled_segment_code": o_seg}).to_csv(mk(ROOT / "eval_labels") / "contract_audience_labels.csv", index=False)
mk(OUT / "contracts")
for i, part in enumerate([contracts.iloc[: N_ALL // 2], contracts.iloc[N_ALL // 2 :]]):
    part.to_json(OUT / "contracts" / f"contracts_batch_{i+1}.json", orient="records", lines=True)

# ---------------------------------------------------------------- as-run spots
a_start = np.array([max(s, START) for s in fstart])
a_end = np.array([min(e, AS_OF) for e in fend])
active_days = np.array([(e - s).days + 1 for s, e in zip(a_start, a_end)]).clip(0)
n_spots = np.round(o_spw * active_days / 7).astype(int)
idx = np.repeat(np.arange(N_ALL), n_spots)
offs = (rng.random(len(idx)) * active_days[idx]).astype(int)
air_date = pd.to_datetime(a_start[idx]) + pd.to_timedelta(offs, unit="D")
ad = air_date.values.astype("datetime64[D]")
dp = o_dp[idx]
st_i = o_station[idx]
seg = o_seg[idx]
mkt = stations.market.values[st_i]
net = stations.network_affiliation.values[st_i]
dow = air_date.dayofweek.values  # 5=Sat 6=Sun
prog, genre, ptags = pick_programs(dp, dow, net, sports_p=0.25)

reason = np.full(len(idx), None, dtype=object)
u = rng.random(len(idx))
sports = ((((dow == 5) & np.isin(net, ["FOX", "ABC"])) | ((dow == 6) & np.isin(net, ["FOX", "CBS"])))
          & np.isin(dp, SPORTS_DP) & (genre != "Sports"))
hurricane = np.isin(mkt, list(GULF)) & (ad >= d64("2026-10-05")) & (ad <= d64("2026-10-09"))
political = np.isin(mkt, list(POLITICAL)) & (ad >= d64("2026-10-15"))
ktvr = (np.array(calls)[st_i] == "KTVR") & (ad >= d64("2026-10-12")) & (ad <= d64("2026-10-18"))
c1 = np.where(sports, 0.22, 0)
c2 = c1 + np.where(hurricane, 0.30, 0)
c3 = c2 + np.where(political, 0.08, 0)
c4 = c3 + np.where(ktvr, 0.18, 0.004)
c5 = c4 + 0.004
c6 = c5 + 0.003
reason[u < c1] = "Sports Overrun"
reason[(u >= c1) & (u < c2)] = "Breaking News"
reason[(u >= c2) & (u < c3)] = "Political Priority"
reason[(u >= c3) & (u < c4)] = "Technical Failure"
reason[(u >= c4) & (u < c5)] = "Network Override"
reason[(u >= c5) & (u < c6)] = "Traffic Error"

hero_rows = np.where(idx == HERO_O)[0]
reason[hero_rows] = None
for row, ts in zip(hero_rows[:2], ["2026-10-24", "2026-10-25"]):  # Sat college football, Sun NFL
    air_date = air_date.where(np.arange(len(air_date)) != row, pd.Timestamp(ts))
    reason[row] = "Sports Overrun"
    prog[row], genre[row], ptags[row] = PROGRAMS["Prime"][0]
ad = air_date.values.astype("datetime64[D]")

status = np.where(reason == None, "Aired",  # noqa: E711
                  np.where(np.isin(reason, ["Technical Failure", "Traffic Error"]), "Missed", "Preempted"))
h0 = np.array([DP_HOURS[d][0] for d in dp])
h1 = np.array([DP_HOURS[d][1] for d in dp])
mins = ((h0 + rng.random(len(idx)) * (h1 - h0)) * 60).astype(int)
mins[hero_rows[:2]] = [20 * 60 + 40, 21 * 60 + 10]
sched = air_date + pd.to_timedelta(mins, unit="m")
seg_idx = np.array([DP_INDEX[g][d] * GENRE_INDEX[gn][g] for g, d, gn in zip(seg, dp, genre)])
rating_act = np.round(np.array([DP_RATING[d] for d in dp]) * seg_idx * rng.lognormal(0, 0.15, len(idx)), 2)
rating_act = np.where(status == "Aired", rating_act, np.nan)
asrun = pd.DataFrame({
    "spot_id": [f"SPT-{i+1:07d}" for i in range(len(idx))],
    "order_id": contracts.order_id.values[idx],
    "station_id": stations.station_id.values[st_i],
    "scheduled_ts": sched.strftime("%Y-%m-%d %H:%M:%S"),
    "daypart": dp,
    "program_name": prog,
    "program_genre": genre,
    "spot_length_sec": o_len[idx],
    "unit_rate_usd": o_rate[idx],
    "status": status,
    "preemption_reason": reason,
    "rating_actual": rating_act,
})
truth = asrun.copy()  # clean copy used to derive offers + pacing (source systems agree on these)
truth["o_i"], truth["st_i"], truth["seg_c"], truth["air_d"] = idx, st_i, seg, ad
HERO_SPOTS = truth.spot_id.values[hero_rows[:2]].tolist()

# ---- deliberate bad data for SDP expectations (never on the hero order)
n = len(asrun)
pool = np.setdiff1d(np.arange(n), hero_rows)
asrun.loc[rng.choice(pool, int(n * 0.003), replace=False), "spot_id"] = None
neg = rng.choice(pool, int(n * 0.002), replace=False)
asrun.loc[neg, "unit_rate_usd"] = -asrun.loc[neg, "unit_rate_usd"]
asrun.loc[rng.choice(pool, int(n * 0.002), replace=False), "station_id"] = "STN-999"
asrun["_d"] = ad
asrun = pd.concat([asrun, asrun.loc[pool].sample(frac=0.01, random_state=3)], ignore_index=True)  # ~1% dup rows
mk(OUT / "as_run_logs")
mk(STAGE / "as_run_logs")
for d, g in asrun.groupby("_d"):
    d = pd.Timestamp(d).date()
    folder = OUT / "as_run_logs" if d <= DAY1_END else STAGE / "as_run_logs"
    g.drop(columns="_d").to_csv(folder / f"as_run_{d.isoformat()}.csv", index=False)

# ---------------------------------------------------------------- inventory avails (open replacement slots)
days = pd.date_range(START, AVAIL_END)
S, D, P = np.meshgrid(np.arange(40), np.arange(len(days)), np.arange(6), indexing="ij")
cnt = rng.poisson(np.array([DP_AVAILS[d] for d in DAYPARTS])[P.ravel()])
s_idx, d_idx, p_idx = (np.repeat(x.ravel(), cnt) for x in (S, D, P))
av_dp = np.array(DAYPARTS)[p_idx]
av_dt = days[d_idx]
av_prog, av_genre, av_tags = pick_programs(av_dp, av_dt.dayofweek.values, networks[s_idx], sports_p=0.5)
sport_boost = np.where(av_genre == "Sports", 1.15, 1.0)
blocked_cats = [c for c in cats if c != "Political"]
av = pd.DataFrame({
    "station_id": stations.station_id.values[s_idx],
    "air_date": av_dt.strftime("%Y-%m-%d"),
    "daypart": av_dp,
    "program_name": av_prog,
    "program_genre": av_genre,
    "adjacency_tags": av_tags,
    "spot_length_sec": rng.choice([15, 30, 60], len(s_idx), p=[0.2, 0.72, 0.08]),
    "est_rating_hh": np.round(np.array([DP_RATING[d] for d in av_dp]) * sport_boost * rng.lognormal(0, 0.15, len(s_idx)), 2),
    "rate_card_usd": np.round(np.array([DP_RATE[d] for d in av_dp]) * market_mult[s_idx] * sport_boost
                              * rng.uniform(0.9, 1.1, len(s_idx)), 0),
    "competitive_category_blocked": ["|".join(rng.choice(blocked_cats, k, replace=False)) if k else None
                                     for k in rng.choice([0, 1, 2], len(s_idx), p=[0.45, 0.40, 0.15])],
    "avail_status": rng.choice(["Open", "Held"], len(s_idx), p=[0.85, 0.15]),
    "_hero": False,
})
# hero station: guaranteed Prime inventory right after the as-of date, sports and entertainment
hero_av = [("2026-10-31", SPORTS_PROG[5], 6.6), ("2026-11-02", PROGRAMS["Prime"][0], 7.4),
           ("2026-11-04", PROGRAMS["Prime"][0], 7.1), ("2026-11-05", PROGRAMS["Prime"][1], 6.9),
           ("2026-11-07", SPORTS_PROG[5], 6.4)]
av = pd.concat([av, pd.DataFrame([{
    "station_id": stations.station_id[hero_s], "air_date": d, "daypart": "Prime", "program_name": p[0],
    "program_genre": p[1], "adjacency_tags": p[2], "spot_length_sec": 30, "est_rating_hh": r,
    "rate_card_usd": round(DP_RATE["Prime"] * market_mult[hero_s] * (1.15 if p[1] == "Sports" else 1.0)),
    "competitive_category_blocked": None, "avail_status": "Open", "_hero": True} for d, p, r in hero_av])], ignore_index=True)
av = av.sort_values(["air_date", "station_id", "daypart"], kind="stable").reset_index(drop=True)
av.insert(0, "avail_id", [f"AVL-{i+1:06d}" for i in range(len(av))])
av_cols = [c for c in av.columns if not c.startswith("_")]
mk(OUT / "inventory_avails")
av["_wk"] = pd.to_datetime(av.air_date).dt.strftime("%G-W%V")
for wk, g in av.groupby("_wk"):
    g[av_cols].to_csv(OUT / "inventory_avails" / f"avails_{wk}.csv", index=False)

# segment rating of every avail, per segment (what the commensurate rules compare)
for g in SEG_CODES:
    av[f"_r_{g}"] = av.est_rating_hh.values * np.array(
        [DP_INDEX[g][d] * GENRE_INDEX[gn][g] for d, gn in zip(av.daypart, av.program_genre)])
av["_date"] = pd.to_datetime(av.air_date).values.astype("datetime64[D]")
av["_rank"] = av.daypart.map(DP_RANK).values
AV = {k: g for k, g in av.groupby(["station_id", "spot_length_sec"])}

# ---------------------------------------------------------------- make-good offer history (inherited system's output)
pre = truth[truth.status != "Aired"].copy()
pre = pre[~pre.spot_id.isin(HERO_SPOTS)]
pre["offer_lag"] = rng.integers(1, 5, len(pre))
pre["offer_d"] = pre.air_d + pre.offer_lag.values.astype("timedelta64[D]")
pre = pre[(pre.offer_d <= d64(AS_OF)) & o_elig[pre.o_i.values] & (rng.random(len(pre)) < 0.82)]
pre = pre.sort_values(["offer_d", "scheduled_ts"])
pre["by"] = rng.choice(["rules_engine", "gpt_assist", "manual"], len(pre), p=[0.50, 0.25, 0.25])

W = dict(ie=0.30, genre=0.20, pref=0.35, yld=0.10, timely=0.10, comp=0.40)  # hidden human utility
booked, offers = set(), []
diag = {"cands": [], "human_top3_by_agent": [], "human_top1_by_agent": []}
for sp in pre.itertuples():
    air_d, offer_d = np.datetime64(sp.air_d, "D"), np.datetime64(sp.offer_d, "D")
    o = sp.o_i
    A = AV.get((sp.station_id, o_len[o]))
    if A is None:
        continue
    win_end = air_d + np.timedelta64(int(o_window[o]), "D")
    flight_end = d64(fend[o])
    avail_ok = (A["_date"].values > offer_d) & ~A["_hero"].values & ~A.avail_id.isin(booked).values
    in_window = avail_ok & (A["_date"].values > air_d) & (A["_date"].values <= win_end) & (A["_date"].values <= flight_end)
    seg_r = A[f"_r_{sp.seg_c}"].values
    comm = (in_window & (A["_rank"].values >= DP_RANK[sp.daypart]) & (A.avail_status.values == "Open")
            & (seg_r >= 0.9 * o_guar[o]))
    C = A[comm]
    ie = seg_r[comm] / o_guar[o]
    genre_m = (C.program_genre.values == sp.program_genre).astype(float)
    tp = true_prefs[o_adv[o]]
    pref = np.clip([sum(tp.get(t, 0) for t in tags.split("|")) for tags in C.adjacency_tags], -1, 1)
    yld = np.clip(1 - np.abs(C.rate_card_usd.values / o_rate[o] - 1), 0, 1)
    timely = 1 - (C["_date"].values - air_d).astype(int) / o_window[o]
    comp = np.array([bool(b) and o_cat[o] in b.split("|") for b in C.competitive_category_blocked.fillna("")], dtype=float)
    U = (W["ie"] * np.minimum(ie, 1.3) / 1.3 + W["genre"] * genre_m + W["pref"] * pref + W["yld"] * yld
         + W["timely"] * timely - W["comp"] * comp + rng.gumbel(0, 0.12, len(C)))
    S0 = 0.35 * np.minimum(ie, 1.2) / 1.2 + 0.15 * genre_m + 0.15 * yld + 0.10 * timely - 0.30 * comp
    order_U = np.argsort(-U)

    def downgrade(days_ahead):
        w = WEAKER.get(sp.daypart)
        m = avail_ok & (A.daypart.values == w) & (A.avail_status.values == "Open") & (A["_date"].values <= offer_d + np.timedelta64(days_ahead, "D"))
        return A[m].iloc[0] if m.any() else None

    pick, kind, tokens = None, None, (None, None)
    if sp.by == "rules_engine":  # 3-day lookahead, earliest avail; else downgrade
        near = C[C["_date"].values <= offer_d + np.timedelta64(3, "D")]
        if len(near):
            pick, kind = near.iloc[0], "commensurate"
        else:
            pick, kind = downgrade(3), "downgrade"
    elif sp.by == "gpt_assist":
        tokens = (int(rng.lognormal(np.log(26000), 0.25)), int(rng.lognormal(np.log(1400), 0.3)))
        r = rng.random()
        bad = A[avail_ok & (A["_date"].values <= win_end) & ((A.avail_status.values == "Held") | (A["_rank"].values < DP_RANK[sp.daypart]))]
        if r < 0.15 and len(bad):
            pick, kind = bad.iloc[rng.integers(len(bad))], "invalid"
        elif r < 0.30 or not len(C):
            pick, kind = downgrade(7), "downgrade"
        else:
            pick, kind = C.iloc[rng.integers(len(C))], "commensurate"
    else:  # manual: an ad-ops expert picks the best fit
        if len(C):
            k = order_U[0] if (rng.random() < 0.9 or len(C) == 1) else order_U[1]
            pick, kind = C.iloc[k], "commensurate"
            diag["cands"].append(len(C))
            diag["human_top3_by_agent"].append(k in np.argsort(-S0)[:3])
            diag["human_top1_by_agent"].append(k == np.argmax(S0))
        else:
            pick, kind = downgrade(7), "downgrade"
    if pick is None:
        continue

    if kind == "commensurate":
        rank = int(np.where(order_U == np.where(C.avail_id.values == pick.avail_id)[0][0])[0][0])
        p_acc = 0.93 if rank == 0 else 0.85 if rank < 3 else 0.65
    else:
        p_acc = 0.40 if kind == "downgrade" else 0.15
    offered_d = pd.Timestamp(str(offer_d))
    if rng.random() < p_acc:
        status_ = "Accepted"
        booked.add(pick.avail_id)
    elif (d64(AS_OF) - offer_d).astype(int) <= 4 and rng.random() < 0.6:
        status_ = "Pending"
    else:
        status_ = "Rejected" if rng.random() < 0.75 else "Expired"
    offers.append({
        "offer_id": None,
        "spot_id": sp.spot_id,
        "order_id": sp.order_id,
        "station_id": sp.station_id,
        "original_daypart": sp.daypart,
        "offered_avail_id": pick.avail_id,
        "offered_daypart": pick.daypart,
        "offered_air_date": pick.air_date,
        "offered_program": pick.program_name,
        "offered_ts": (offered_d + timedelta(minutes=int(rng.integers(8 * 60, 18 * 60)))).strftime("%Y-%m-%d %H:%M:%S"),
        "offer_status": status_,
        "decision_ts": None if status_ == "Pending" else
        (offered_d + timedelta(days=int(rng.integers(1, 3)), minutes=int(rng.integers(0, 600)))).strftime("%Y-%m-%d %H:%M:%S"),
        "refund_issued": status_ in ("Rejected", "Expired"),
        "refund_amount_usd": float(o_rate[o]) if status_ in ("Rejected", "Expired") else 0.0,
        "matched_by": sp.by,
        "reviewer": f"adops-{rng.integers(1, 9):02d}" if sp.by == "manual" else None,
        "gpt_prompt_tokens": tokens[0],
        "gpt_completion_tokens": tokens[1],
        "_kind": kind,
        "_d": offer_d,
    })
offers = pd.DataFrame(offers)
offers["offer_id"] = [f"MGO-{i+1:06d}" for i in range(len(offers))]
offers[["gpt_prompt_tokens", "gpt_completion_tokens"]] = offers[["gpt_prompt_tokens", "gpt_completion_tokens"]].astype("Int64")
mk(OUT / "make_good_offers")
mk(STAGE / "make_good_offers")
offers["_wk"] = pd.to_datetime(offers._d).dt.strftime("%G-W%V")
off_cols = [c for c in offers.columns if not c.startswith("_")]
day1 = offers._d <= d64(DAY1_END)
for wk, g in offers[day1].groupby("_wk"):
    g[off_cols].to_json(OUT / "make_good_offers" / f"offers_{wk}.json", orient="records", lines=True)
offers[~day1][off_cols].to_json(STAGE / "make_good_offers" / "offers_day2.json", orient="records", lines=True)

# ---------------------------------------------------------------- delivery pacing (hourly grain, mixed latency)
aired = truth[truth.status == "Aired"]
ev = pd.to_datetime(aired.scheduled_ts).dt.floor("h")
imps = np.round(aired.rating_actual.values / 100 * universe_k[aired.st_i.values, [SEG_POS[g] for g in aired.seg_c]] * 1000)
acr = has_acr[aired.st_i.values]
first_rep = np.where(acr, (ev + pd.to_timedelta(rng.integers(60, 180, len(ev)), unit="m")).values,
                     (ev.dt.normalize() + np.timedelta64(30, "h") + pd.to_timedelta(rng.integers(0, 180, len(ev)), unit="m")).values)
base = pd.DataFrame({
    "order_id": aired.order_id.values, "station_id": aired.station_id.values, "spot_id": aired.spot_id.values,
    "event_hour": ev.dt.strftime("%Y-%m-%d %H:00:00").values, "audience_segment_code": aired.seg_c.values,
})
prelim = base.assign(measurement_stage=np.where(acr, "preliminary", "overnight"), impressions=imps.astype(int),
                     reported_ts=pd.to_datetime(first_rep), source_system=np.where(acr, "ACR", "Nielsen"))
final = base.assign(measurement_stage="final_c3", impressions=np.round(imps * rng.lognormal(0.03, 0.04, len(imps))).astype(int),
                    reported_ts=(ev.dt.normalize() + np.timedelta64(82, "h")
                                 + pd.to_timedelta(rng.integers(0, 240, len(ev)), unit="m")).values,
                    source_system="Nielsen")
pacing = pd.concat([prelim, final], ignore_index=True)
pacing = pacing[pacing.reported_ts < pd.Timestamp(AS_OF + timedelta(days=1))]
pacing.loc[rng.choice(pacing.index, int(len(pacing) * 0.001), replace=False), "order_id"] = None
pacing = pd.concat([pacing, pacing.sample(frac=0.003, random_state=5)], ignore_index=True)
pacing["_rd"] = pacing.reported_ts.dt.date
pacing["reported_ts"] = pacing.reported_ts.dt.strftime("%Y-%m-%d %H:%M:%S")
mk(OUT / "delivery_pacing")
mk(STAGE / "delivery_pacing")
for d, g in pacing.groupby("_rd"):
    folder = OUT / "delivery_pacing" if d <= DAY1_END else STAGE / "delivery_pacing"
    g.drop(columns="_rd").to_csv(folder / f"pacing_{d.isoformat()}.csv", index=False)

# ---------------------------------------------------------------- audience taxonomy export (one record per segment)
with open(mk(OUT / "audience_segments") / "segments.json", "w") as f:
    for sid, code, name, gender, amin, amax, factor in SEGMENTS:
        f.write(json.dumps({
            "audience_segment_id": sid, "segment_code": code, "segment_name": name, "gender": gender,
            "age_min": amin, "age_max": amax,
            "aliases": [code] + ALIASES[code],
            "daypart_index": [{"daypart": d, "index": v} for d, v in DP_INDEX[code].items()],
            "genre_index": [{"genre": gn, "index": GENRE_INDEX[gn][code]} for gn in GENRES],
            "universe_estimates": [{"market": stations.market[s], "dma_rank": int(stations.dma_rank[s]),
                                    "universe_000": int(universe_k[s, SEG_POS[code]])} for s in range(40)],
        }) + "\n")

# ---------------------------------------------------------------- reference: commensurate policy
with open(mk(OUT / "reference") / "make_good_policy.md", "w") as f:
    f.write("""# Make-Good Policy (commensurate replacement rules)

A preempted or missed spot on a make-good-eligible contract must receive a **commensurate**
make-good, otherwise the advertiser is refunded.

## Must-match (all required)
1. **Same station** (same DMA) as the original spot.
2. **Same or better daypart.** Value order (low -> high):
   Daytime < Early Morning < Early Fringe < Late Fringe < Prime Access < Prime.
3. **Audience delivery:** the avail's rating *in the contract's audience segment* must be
   >= 90% of the contract's guaranteed_rating. Segment rating = household rating x daypart
   index x genre index (from the audience taxonomy).
4. **Same spot length** (15 / 30 / 60 sec).
5. **Timing:** airs after the preempted spot, within the contract's make-good window
   (makegood_window_days, default 14) and no later than flight_end.
6. **Avail status is Open** (not Held, not already booked for another make-good).

## Soft-match (ranking, not eligibility)
- Program genre matches the preempted spot's program genre.
- Adjacency fits the advertiser's preferences (learned from accept / reject history).
- Competitive separation: avoid pods that already carry a competitor in the same category.
- Yield: avoid giving away much more valuable inventory than the spot was sold for.
- Timeliness: sooner is better.

Refund exposure = unit_rate_usd of any preempted/missed spot whose offer is Rejected or Expired,
or that has no commensurate inventory.
""")

# ---------------------------------------------------------------- summary
print(f"stations={len(stations)} advertisers={len(advertisers)} contracts={len(contracts)}")
print(f"as_run rows={len(asrun)} avails={len(av)} offers={len(offers)} pacing rows={len(pacing)}")
print("status mix:", truth.status.value_counts().to_dict())
print("reason mix:", truth.preemption_reason.value_counts().to_dict())
print("offers by matched_by x kind:\n", pd.crosstab(offers.matched_by, offers._kind))
print("accept rate by kind:", offers.groupby("_kind").offer_status.apply(lambda s: round((s == "Accepted").mean(), 2)).to_dict())
print("refund $ by original daypart:", offers.groupby("original_daypart").refund_amount_usd.sum().round().sort_values(ascending=False).to_dict())
ev_cases = offers[(offers.matched_by == "manual") & (offers.offer_status == "Accepted") & (offers._kind == "commensurate")
                  & (offers._d >= d64("2026-10-15"))]
print(f"eval-eligible manual accepts (>= Oct 15): {len(ev_cases)}")
print(f"manual: median candidates={np.median(diag['cands']):.0f}, human pick in agent top-1/top-3 (no prefs)="
      f"{np.mean(diag['human_top1_by_agent']):.2f}/{np.mean(diag['human_top3_by_agent']):.2f}")
print(f"gpt_assist median prompt tokens={offers.gpt_prompt_tokens.median():.0f}")
print(f"hero station={stations.call_letters[hero_s]} ({stations.market[hero_s]}, {networks[hero_s]}) hero spots={HERO_SPOTS}")
