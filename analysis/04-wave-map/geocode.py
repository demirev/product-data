"""City-level coordinates, from GeoNames, for the senders in 03-tech-waves companies and releases files.

Each company is placed by its registry location (`country` / `region` / `hq_city` on
`companies`) and, failing that, by its modal dateline (`hq`). The dateline also wins
when it places the company in a different country, or a different US state, than the
registry ("NEW YORK" against a Dover, Del. agent address):
GLEIF often holds a registered agent's address (Wilmington, Del.), and SEC name
matches can pick a namesake filer. A city name is matched
against GeoNames names, ASCII names and alternate names, accent- and case-folded,
within the country (and US state) when one is known. The most populous match wins,
with a place's own name counting ten times its population over an alternate name,
so Waterloo, Ont. is not Austin (once called Waterloo).
Outside the US the registry region is ignored, since sources code it differently.
A dateline region is read as a US state, a country, or a first-level division of
any country. A dateline naming several cities is tried city by city, keeping the
region ("DALLAS and PORTSMOUTH, N.H.") or the first city's region ("PALO ALTO,
Calif. and BERLIN"). A dateline whose region is unknown or absent is matched worldwide.

Needs cities500.txt, admin1CodesASCII.txt and countryInfo.txt from
https://download.geonames.org/export/dump/ in data/geonames/.

Outputs:
  geo.csv     uploader_link, name, org_type, lat, lon, geonameid, place, admin1, cc, method
  misses.csv  city, region, companies: unplaced dateline places, most frequent first

    python geocode.py ../03-tech-waves/companies_<launch version>.csv ../03-tech-waves/releases_<launch version>.csv
"""
import csv
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "py"))
from company import us_state  # noqa: E402

HERE = Path(__file__).parent
GEONAMES = HERE.parents[1] / "data" / "geonames"
# Seat of government outranks a bigger namesake at equal population
FEATURE_RANK = {"PPLC": 4, "PPLA": 3, "PPLA2": 2, "PPL": 1}
# Region spellings in datelines that are neither GeoNames country names nor admin1 names
COUNTRY_ALIASES = {
	"england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB", "uk": "GB", "u k": "GB",
	"great britain": "GB", "britain": "GB", "uae": "AE", "u a e": "AE", "south korea": "KR", "korea": "KR",
	"russia": "RU", "taiwan": "TW", "vietnam": "VN", "czech republic": "CZ", "holland": "NL", "usa": "US",
	"u s a": "US", "u s": "US", "us": "US", "hong kong sar": "HK", "macau": "MO", "the netherlands": "NL",
	"turkiye": "TR", "ivory coast": "CI", "republic of korea": "KR", "prc": "CN", "p r china": "CN",
}
# Areas that datelines name in place of a city
CITY_ALIASES = {
	"silicon valley": "san jose", "san francisco bay area": "san francisco", "bay area": "san francisco",
	"orange county": "irvine", "research triangle park": "durham", "research triangle": "durham",
	"tampa bay": "tampa", "silicon slopes": "lehi", "grand cayman": "george town", "napa valley": "napa",
	# Datelines that lost their leading "SAN"
	"francisco": "san francisco", "diego": "san diego", "mateo": "san mateo",
}
REGION_ALIASES = {
	**{k: ("CA", v) for k, v in {"ont": "08", "on": "08", "alta": "01", "ab": "01", "que": "10", "qc": "10", "man": "03",
		"mb": "03", "sask": "11", "sk": "11", "ns": "07", "nb": "04", "nfld": "05", "nl": "05", "pei": "09", "pe": "09",
		"bc": "02", "b c": "02", "yt": "12", "nwt": "13"}.items()},
	**{k: ("AU", v) for k, v in {"act": "01", "nsw": "02", "qld": "04", "vic": "07"}.items()},
}


def fold(s):
	s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
	s = re.sub(r"[^a-z0-9]+", " ", s).strip()
	return re.sub(r"^(st|ste|ft|mt) ", lambda m: {"st": "saint ", "ste": "sainte ", "ft": "fort ", "mt": "mount "}[m[1]], s)


cities = defaultdict(list)
with open(GEONAMES / "cities500.txt", encoding="utf-8") as f:
	for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
		own = {fold(row[1]), fold(row[2])}
		for key in (own | {fold(n) for n in row[3].split(",")}) - {""}:
			pop = int(row[14] or 0) * (10 if key in own else 1)
			cities[key].append((pop, FEATURE_RANK.get(row[7], 0), row[0], row[1], float(row[4]), float(row[5]), row[8], row[10]))

countries = dict(COUNTRY_ALIASES)
with open(GEONAMES / "countryInfo.txt", encoding="utf-8") as f:
	for row in csv.reader((l for l in f if not l.startswith("#")), delimiter="\t"):
		countries.update({fold(row[4]): row[0], fold(row[0]): row[0], fold(row[1]): row[0]})
admin1 = defaultdict(set)
with open(GEONAMES / "admin1CodesASCII.txt", encoding="utf-8") as f:
	for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
		cc, code = row[0].split(".")
		admin1[fold(row[1])].add((cc, code))
		admin1[fold(row[2])].add((cc, code))
admin1.update({k: {v} for k, v in REGION_ALIASES.items()})


def lookup(city, cc=None, adm=None):
	"""Most populous GeoNames place for `city`, within country `cc` and division `adm` when given."""
	hits = [r for r in cities.get(CITY_ALIASES.get(fold(city), fold(city)), []) if (cc is None or r[6] == cc) and (adm is None or r[7] == adm)]
	return max(hits, default=None)


firms = {row["uploader_link"] for path in sys.argv[1:] for row in csv.DictReader(open(path))}
db = MongoClient("mongodb://localhost:27017")["prn"]
out, misses, methods = [], Counter(), Counter()
for c in db["companies"].find({"_id": {"$in": list(firms)}}, {"name": 1, "org_type": 1, "hq": 1, "country": 1, "region": 1, "hq_city": 1}):
	hq = c.get("hq") or {}
	region = re.split(r"\s+and\s+", (hq.get("region") or "").strip())[0]
	key = fold(region)
	if us_state(region) in ("PR", "VI", "GU"):
		cc, adm, method = us_state(region), None, "dateline_country"
	elif us_state(region):
		cc, adm, method = "US", us_state(region), "dateline_state"
	elif key in countries:
		cc, adm, method = countries[key], None, "dateline_country"
	elif len(admin1.get(key, ())) == 1:
		(cc, adm), method = next(iter(admin1[key])), "dateline_admin1"
	else:
		cc, adm, method = None, None, "dateline_city_only"
	parts = re.split(r"\s+(?:and|&)\s+|\s*[,/;]\s*", hq.get("city") or "")
	hit = next(filter(None, (lookup(part, cc, adm) for part in parts)), None)
	# "TBILISI, Georgia": a state name that is also a country
	if not hit and cc == "US" and key in countries:
		cc, adm, method = countries[key], None, "dateline_country"
		hit = next(filter(None, (lookup(part, cc) for part in parts)), None)
	reg = c.get("hq_city") and c.get("country") and lookup(c["hq_city"], c["country"], c.get("region") if c["country"] == "US" else None)
	if reg and not (hit and (reg[6] != hit[6] or (hit[6] == "US" and reg[7] != hit[7]))):
		hit, method = reg, "registry"
	elif hit and reg:
		method += "_over_registry"
	if hit:
		methods[method] += 1
		out.append([c["_id"], c.get("name"), c.get("org_type"), hit[4], hit[5], hit[2], hit[3], hit[7], hit[6], method])
	else:
		misses[hq.get("city") or "", region] += 1

with open(HERE / "geo.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["uploader_link", "name", "org_type", "lat", "lon", "geonameid", "place", "admin1", "cc", "method"])
	w.writerows(sorted(out))
with open(HERE / "misses.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["city", "region", "companies"])
	w.writerows([*k, v] for k, v in misses.most_common())
print(f"{len(firms)} companies, {len(out)} placed ({len(out) / len(firms):.1%}), {sum(misses.values())} not")
print(methods.most_common())
