"""Pilot sample for the need extraction -> pilot.csv.

Releases drawn: positives of the newest `launch` run, scraped, dated, sender not
blacklisted, English (the rule in 03-tech-waves/keywords.py). AI means also positive in
the newest `ai` run. Strata, in order of precedence (a release in two keeps the first):

  cross_era      hand-picked in cross_era.csv: one need met before 2020 without AI and from 2023 with AI
  tech_created   hand-picked in cross_era.csv: needs that exist only because of a technology
  sibling        pairs of launches by one sender with similar headers (word Jaccard 0.5-0.9), 2+ months apart
  wave_<family>  launch rows of 03-tech-waves/samples.csv for mobile, cloud, iot, crypto, wearables
  ai_2023+       AI launches from 2023 on
  other_<era>    non-AI launches in 2010-14, 2015-19, 2020-22 and 2023+
  leak_ai        further AI launches from 2023 on, for the technology-leak check
  leak_other     further non-AI launches from 2023 on, for the technology-leak check

`group` is the topic for cross_era and tech_created and the pair number for sibling.
`compare` marks rows outside the sibling pairs and leak strata for the cross-model comparison.

Columns: id, stratum, group, compare, month, uploader_link, ai, header

    python sample.py
"""
import csv
import random
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd
from pymongo import MongoClient

HERE = Path(__file__).parent
SIZES = {"ai_2023+": 40, "other_2010-14": 15, "other_2015-19": 15, "other_2020-22": 15, "other_2023+": 20}
WAVES = ["mobile", "cloud", "iot", "crypto", "wearables"]
PER_WAVE, SIBLING_PAIRS, COMPARE, LEAK = 6, 15, 30, 200
STOP = {"the", "and", "for", "with", "new", "its", "from", "into", "your", "that", "this", "first", "now", "available",
	"launch", "launches", "launched", "introduces", "introducing", "announces", "unveils", "debuts", "releases"}

rng = random.Random(0)
db = MongoClient("mongodb://localhost:27017")["prn"]
versions = {t: db["model_runs"].find_one({"task": t}, sort=[("trained_at", -1)])["_id"] for t in ("launch", "ai")}
launch = {p["article_id"] for p in db["predictions"].find({"model_version": versions["launch"], "predicted_class": 1}, {"article_id": 1})}
ai = {p["article_id"] for p in db["predictions"].find({"model_version": versions["ai"], "predicted_class": 1}, {"article_id": 1})} & launch

rows = {}
cursor = db["articles"].find(
	{"status": "scraped", "published_at": {"$type": "date"}, "uploader_link": {"$nin": db["sender_blacklist"].distinct("_id")}},
	{"header": 1, "published_at": 1, "uploader_link": 1, "lede": {"$substrCP": [{"$ifNull": ["$body", ""]}, 0, 1001]}},
	batch_size=5000)
for d in cursor:
	if d["_id"] not in launch:
		continue
	lede = re.sub(r"\S*$", "", d["lede"]) if len(d["lede"]) > 1000 else d["lede"]
	header = re.sub(r"\s+", " ", d.get("header") or "").strip()
	if len(re.findall(r" (the|and|of|to) ", (header + " " + re.sub(r"\s+", " ", lede)).lower())) >= 4:
		rows[d["_id"]] = {"month": d["published_at"].strftime("%Y-%m"), "uploader_link": d.get("uploader_link"), "ai": d["_id"] in ai, "header": header}
print(len(rows), "eligible launches,", sum(r["ai"] for r in rows.values()), "AI")

picked = {}
for r in csv.DictReader(open(HERE / "cross_era.csv")):
	if r["id"] not in rows:
		print("not an eligible launch, skipped:", r["id"])
	elif r["id"] not in picked:
		picked[r["id"]] = ("tech_created" if r["topic"] == "tech_created" else "cross_era", r["topic"])

by_sender = defaultdict(list)
for i, r in rows.items():
	if i not in picked:
		by_sender[r["uploader_link"]].append(i)
senders = sorted(s for s, ids in by_sender.items() if s and 2 <= len(ids) <= 400)
rng.shuffle(senders)
n_pairs = 0
for s in senders:
	ids = sorted(by_sender[s])
	words = [set(re.findall(r"[a-z0-9]{3,}", rows[i]["header"].lower())) - STOP for i in ids]
	months = [int(rows[i]["month"][:4]) * 12 + int(rows[i]["month"][5:]) for i in ids]
	cands = [(a, b) for a in range(len(ids)) for b in range(a + 1, len(ids))
		if len(words[a]) >= 3 and len(words[b]) >= 3 and abs(months[a] - months[b]) >= 2
		and 0.5 <= len(words[a] & words[b]) / len(words[a] | words[b]) < 0.9]
	if cands:
		n_pairs += 1
		for k in rng.choice(cands):
			picked[ids[k]] = ("sibling", f"s{n_pairs:02d}")
	if n_pairs == SIBLING_PAIRS:
		break

waves = pd.read_csv(Path(__file__).parents[1] / "03-tech-waves" / "samples.csv")
for fam in WAVES:
	ids = sorted(i for i in waves[(waves.series == fam) & waves.launch].id if i in rows and i not in picked)
	for i in rng.sample(ids, min(PER_WAVE, len(ids))):
		picked[i] = (f"wave_{fam}", "")

pools = defaultdict(list)
for i, r in sorted(rows.items()):
	y = int(r["month"][:4])
	era = "2010-14" if y < 2015 else "2015-19" if y < 2020 else "2020-22" if y < 2023 else "2023+"
	if i not in picked:
		pools[f"{'ai' if r['ai'] else 'other'}_{era}"].append(i)
for stratum, n in SIZES.items():
	for i in rng.sample(pools[stratum], n):
		picked[i] = (stratum, "")

compare = set(rng.sample(sorted(i for i, (s, _) in picked.items() if s != "sibling"), COMPARE))
for stratum, pool in [("leak_ai", "ai_2023+"), ("leak_other", "other_2023+")]:
	for i in rng.sample([i for i in pools[pool] if i not in picked], LEAK):
		picked[i] = (stratum, "")
with open(HERE / "pilot.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["id", "stratum", "group", "compare", "month", "uploader_link", "ai", "header"])
	for i, (s, g) in picked.items():
		r = rows[i]
		w.writerow([i, s, g, i in compare, r["month"], r["uploader_link"], r["ai"], r["header"]])
print(len(picked), "rows; versions", versions)
