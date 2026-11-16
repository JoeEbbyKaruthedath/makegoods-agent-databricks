"""Synthetic sales org for the make-good lifecycle: sellers, their sales managers, who owns which client, and the
client contacts that make-good offers are emailed to.

Reads the files gen_raw.py wrote (stations, advertisers, contracts) and adds three feeds next to them, without
touching the existing ones (so the hero spots and the evaluation set stay the same):

  data/raw/sellers/sellers.csv                     seller_id, seller_name, seller_email, role, region, markets, manager_id
  data/raw/seller_book/seller_book.csv             advertiser_id, station_id, market, seller_id (local account ownership)
  data/raw/advertiser_contacts/advertiser_contacts.csv   advertiser_id, contact_name, contact_email, contact_title

The 40 markets are split into 4 sales regions. Each region has one sales manager and 3 sellers; every
advertiser buying in a region is owned by one of that region's sellers. All addresses are on example.com: the
app's mail policy sends demo mail only to the addresses you allow (see agent_server/mail/policy.py).

  python gen/gen_sellers.py
"""
import hashlib
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent / "data"
RAW = ROOT / "raw"

REGION_MARKETS = {
    "East": ["Baltimore", "Pittsburgh", "Syracuse", "Portland ME", "Richmond", "Raleigh-Durham", "Charleston WV",
             "Columbus OH", "Dayton", "Cincinnati"],
    "Southeast": ["Birmingham", "Nashville", "Macon", "Savannah", "Tallahassee", "Columbus GA", "Mobile-Pensacola",
                  "Greenville-Spartanburg", "New Orleans", "Baton Rouge", "Lafayette LA"],
    "Central": ["Milwaukee", "Flint-Saginaw", "Des Moines", "Peoria", "Champaign-Springfield", "Wichita", "Tulsa",
                "Oklahoma City", "Little Rock", "San Antonio", "Corpus Christi", "Beaumont-Port Arthur", "Amarillo"],
    "West": ["Seattle-Tacoma", "Yakima", "Eugene", "Medford", "Las Vegas", "Salt Lake City"],
}
REGIONS = list(REGION_MARKETS)
MANAGERS = ["Renee Castillo", "Marcus Bell", "Priya Natarajan", "Tom Okafor"]
SELLERS = [["Dana Whitfield", "Luis Romero", "Hannah Cho"], ["Jordan Price", "Aisha Grant", "Ben Kowalski"],
           ["Megan Duarte", "Sam Lindqvist", "Carla Jimenez"], ["Evan Brooks", "Nina Petrov", "Omar Haddad"]]
CONTACT_FIRST = ["Alex", "Morgan", "Taylor", "Jamie", "Casey", "Riley", "Drew", "Quinn", "Avery", "Parker", "Reese", "Skyler"]
CONTACT_LAST = ["Monroe", "Avila", "Fischer", "Kim", "Okonkwo", "Russo", "Novak", "Shah", "Bennett", "Larsen", "Ortiz", "Park"]
TITLES = ["Media Buyer", "Marketing Director", "Account Supervisor", "Broadcast Buyer"]


def h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def slug(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def email(name: str, domain: str) -> str:
    first, last = name.lower().split(" ", 1)
    return f"{first}.{last.replace(' ', '')}@{domain}"


def main():
    stations = pd.read_csv(RAW / "stations" / "stations.csv")
    advertisers = pd.read_csv(RAW / "advertisers" / "advertisers.csv")
    contracts = pd.concat([pd.read_json(f, lines=True) for f in sorted((RAW / "contracts").glob("*.json"))])

    # regions by geography; a market not listed falls to a region by hash
    markets = stations.sort_values("dma_rank").market.tolist()
    listed = {m: r for r, ms in REGION_MARKETS.items() for m in ms}
    region_of = {m: listed.get(m, REGIONS[h(m) % len(REGIONS)]) for m in markets}

    people = []
    for r, (region, manager) in enumerate(zip(REGIONS, MANAGERS)):
        reg_markets = [m for m in markets if region_of[m] == region]
        mid = f"MGR-{r + 1:02d}"
        people.append({"seller_id": mid, "seller_name": manager, "seller_email": email(manager, "sales.example.com"),
                       "role": "manager", "region": region, "markets": json.dumps(reg_markets), "manager_id": None})
        for k, name in enumerate(SELLERS[r]):
            people.append({"seller_id": f"SLR-{r * 3 + k + 1:02d}", "seller_name": name,
                           "seller_email": email(name, "sales.example.com"), "role": "seller", "region": region,
                           "markets": json.dumps(reg_markets), "manager_id": mid})
    sellers = pd.DataFrame(people)

    # local account ownership: each advertiser buying in a region belongs to one of its sellers
    st = stations.set_index("station_id")
    book = contracts[["advertiser_id", "station_id"]].drop_duplicates().copy()
    book["market"] = book.station_id.map(st.market)
    book["region"] = book.market.map(region_of)
    reps = sellers[sellers.role == "seller"].groupby("region").seller_id.apply(list).to_dict()
    book["seller_id"] = [reps[reg][h(adv + reg) % len(reps[reg])] for adv, reg in zip(book.advertiser_id, book.region)]
    book = book.drop(columns="region").sort_values(["advertiser_id", "station_id"])

    contacts = advertisers[["advertiser_id", "advertiser_name"]].copy()
    contacts["contact_name"] = [f"{CONTACT_FIRST[h(a) % len(CONTACT_FIRST)]} {CONTACT_LAST[h(a + 'l') % len(CONTACT_LAST)]}"
                                for a in contacts.advertiser_id]
    contacts["contact_email"] = [email(n, f"{slug(adv)[:24]}.example.com")
                                 for n, adv in zip(contacts.contact_name, contacts.advertiser_name)]
    contacts["contact_title"] = [TITLES[h(a + "t") % len(TITLES)] for a in contacts.advertiser_id]
    contacts = contacts.drop(columns="advertiser_name")

    for name, df in (("sellers", sellers), ("seller_book", book), ("advertiser_contacts", contacts)):
        out = RAW / name
        out.mkdir(parents=True, exist_ok=True)
        df.to_csv(out / f"{name}.csv", index=False)
        print(f"{name}: {len(df):,} rows -> {out}")
    hero = book[book.advertiser_id == "ADV-0151"].merge(sellers, on="seller_id")
    for _, r in hero.iterrows():
        print(f"hero account: Ironwood Motors on {r.station_id} ({r.market}) is owned by {r.seller_name} ({r.seller_id}), "
              f"manager {r.manager_id}")


if __name__ == "__main__":
    main()
