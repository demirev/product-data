"""Interactive map of where each technology wave's companies are, filled in over time -> map.html.

A company enters a wave in the month of its first release matching the wave's terms
(03-tech-waves). It is drawn at its city from geo.csv (geocode.py). Law firms,
research publishers and stock-commentary senders are left out, since they write about
other companies. Months run from 2010-01 to the last month in the companies file;
earlier entries are shown from 2010-01.

The contrast view compares a wave's companies per city with the companies there that
have a product release (releases file from 03-tech-waves) not matching the wave's
technology family: a series, its broad parent and its narrower children (PARENT in
keywords.py) form one family, so genAI is not compared with AI.

The page is template.html with the data inlined, so it opens from disk with no server.
deck.gl and the Natural Earth outlines load from public CDNs.

    python build.py ../03-tech-waves/companies_<launch version>.csv ../03-tech-waves/releases_<launch version>.csv
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
LABELS = {
	"mobile": "Mobile apps", "cloud": "Cloud", "saas": "SaaS", "big_data": "Big data", "iot": "Internet of things",
	"printing_3d": "3D printing", "wearables": "Wearables", "xr": "VR / AR", "metaverse": "Metaverse", "crypto": "Crypto",
	"genai": "Generative AI (excl. agents)", "agentic": "Agentic AI", "ai": "Any AI",
	"cloud_broad": "Cloud, broad terms", "iot_broad": "Internet of things, broad terms",
	"wearables_broad": "Wearables, broad terms", "crypto_broad": "Crypto, broad terms",
	"genai_broad": "Generative AI, broad terms", "ai_broad": "Any AI, broad terms",
}
DROPPED_ORG_TYPES = {"law_firm", "research_publisher", "stock_commentary"}
PARENT = {
	"cloud": "cloud_broad", "saas": "cloud", "iot": "iot_broad", "wearables": "wearables_broad",
	"crypto": "crypto_broad", "ai": "ai_broad", "genai_broad": "ai_broad", "genai": "genai_broad", "agentic": "ai"}
FAMILY = {}
for s in LABELS:
	FAMILY[s] = s
	while FAMILY[s] in PARENT:
		FAMILY[s] = PARENT[FAMILY[s]]

rows = list(csv.DictReader(open(sys.argv[1])))
releases = list(csv.DictReader(open(sys.argv[2])))
geo = {r["uploader_link"]: r for r in csv.DictReader(open(HERE / "geo.csv"))}
end = max(r["last_month"] for r in rows)
months = [f"{y}-{m:02d}" for y in range(2010, int(end[:4]) + 1) for m in range(1, 13) if f"{y}-{m:02d}" <= end]
month_idx = {m: i for i, m in enumerate(months)}

places, place_idx, firms, firm_idx = [], {}, [], {}
for link in dict.fromkeys(r["uploader_link"] for r in rows + releases):
	g = geo.get(link)
	if g and g["org_type"] not in DROPPED_ORG_TYPES and g["geonameid"] not in place_idx:
		place_idx[g["geonameid"]] = len(places)
		label = ", ".join(x for x in (g["place"], g["admin1"] if g["cc"] in ("US", "CA") else "", g["cc"]) if x)
		places.append([round(float(g["lon"]), 4), round(float(g["lat"]), 4), label])
series = {s: {"label": label, "family": FAMILY[s], "entries": [], "unplaced": 0, "dropped": 0} for s, label in LABELS.items()}
for r in rows:
	out = series[r["series"]]
	g = geo.get(r["uploader_link"])
	if g and g["org_type"] in DROPPED_ORG_TYPES:
		out["dropped"] += 1
		continue
	if not g:
		out["unplaced"] += 1
		continue
	if r["uploader_link"] not in firm_idx:
		firm_idx[r["uploader_link"]] = len(firms)
		firms.append([g["name"] or r["uploader_link"], place_idx[g["geonameid"]]])
	out["entries"].append([firm_idx[r["uploader_link"]], month_idx.get(r["first_month"], 0), int(r["n"])])
for s in series.values():
	s["entries"].sort(key=lambda e: e[1])

# Per family: companies per place with at least one product release outside the family
total, matched = Counter(), Counter()
for r in releases:
	g = geo.get(r["uploader_link"])
	if g and g["org_type"] not in DROPPED_ORG_TYPES:
		(total if r["family"] == "_total" else matched)[r["uploader_link"], r["family"]] += int(r["n"])
baseline = {fam: sorted(Counter(place_idx[geo[l]["geonameid"]] for l, _ in total if total[l, "_total"] > matched[l, fam]).items())
	for fam in set(FAMILY.values())}
data = {"source": Path(sys.argv[1]).name, "months": months, "places": places, "firms": firms, "series": series, "baseline": baseline}
html = (HERE / "template.html").read_text().replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
(HERE / "map.html").write_text(html)
print(f"{len(firms)} companies in {len(places)} places, {len(html) / 1e6:.1f} MB")
for s, v in series.items():
	print(f"{s:16} {len(v['entries']):6} placed {v['unplaced']:5} unplaced {v['dropped']:5} dropped")
print(Counter(geo[l]["method"] for l in firm_idx).most_common())
