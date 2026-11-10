"""Characteristics of the senders entering each technology wave early, at the peak and late -> xlsx.

A sender enters a wave in the month of its first matching release (`first_month` in the
03-tech-waves companies file). Waves are the headline series of 03-tech-waves, all
releases, measured by the centred 12-month rate per 1,000 releases. Two ways split a
wave into early / peak / late:

  level  peak: the contiguous run of months around the peak month whose rate is at least
         80% of the peak rate
  time   peak: the peak month +/- 6 months

Early is every month before the peak window, late every month after it. A level window
that runs into the first or last month with a centred rate is open on that side, and the
period beyond it is left empty. Entries in the current, partial month are dropped. Only
senders with `org_type` operating_company are kept.

Per sender, at its entry month:
  releases_prior, launches_prior  releases (launch-model positives) before the entry month
  years_since_first_release       entry month minus the sender's first release in the corpus
  pct_first_month_in_corpus       entry month is the sender's first month in the corpus
  years_since_founding            entry year minus `founded_year`
  public, origin, revenue, assets, employees  from `companies`, as of now
Releases are counted as in 03-tech-waves: scraped, dated, English. Revenue and assets
are USD only, in millions. Origin is `country`, grouped. Shares and medians are over
senders with a known value; `known_*` columns give that coverage in %.

Rows "All (mean)" average the technologies' values with equal weight; "All (pooled)"
computes each metric over every sender-technology entry.

Outputs:
  by_technology.xlsx  sheets level, time: technology, period, one column per metric;
                      sheet peaks: peak month, rate and window bounds per technology
  by_metric.xlsx      sheets level, time: metric, period, one column per technology

    python senders.py [COMPANIES_CSV]   (default: newest 03-tech-waves/companies_*.csv)
"""
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from pymongo import MongoClient

HERE = Path(__file__).parent
WAVES = HERE.parent / "03-tech-waves"
LABELS = {
	"mobile": "Mobile apps", "cloud": "Cloud", "saas": "SaaS", "big_data": "Big data", "iot": "Internet of things",
	"printing_3d": "3D printing", "wearables": "Wearables", "xr": "VR / AR", "metaverse": "Metaverse", "crypto": "Crypto",
	"genai": "Generative AI (excl. agents)", "agentic": "Agentic AI", "ai": "Any AI",
}
EUROPE = "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB CH NO IS LI".split()
ORIGIN = {"US": "US", "CA": "Canada", "CN": "China", "HK": "China", "MO": "China", "JP": "Japan", "IN": "India", "IL": "Israel", **dict.fromkeys(EUROPE, "Europe")}
ORIGINS = ["US", "Canada", "Europe", "China", "Japan", "India", "Israel", "Other"]
PERIODS = ["early", "peak", "late"]

path = Path(sys.argv[1]) if len(sys.argv) > 1 else max(WAVES.glob("companies_*.csv"))
version = path.stem.removeprefix("companies_")

# Peaks, smoothed as in 03-tech-waves/plot.R: data.table's centred window covers months i-5..i+6
cv = pd.read_csv(WAVES / "coverage.csv")
cv = cv[(cv.month >= "2011-01") & (cv.month < cv.month.max())].set_index("month")
last_month = cv.index.max()
m = pd.read_csv(WAVES / "monthly.csv").query("group == 'all'")
n = m.pivot(index="month", columns="series", values="n").reindex(cv.index).fillna(0)[list(LABELS)]
smooth = 1000 * n.rolling(12).sum().shift(-6).div(cv.kept.rolling(12).sum().shift(-6), axis=0)

peaks = []
for s in LABELS:
	v = smooth[s].to_numpy()
	i = lo = hi = int(np.nanargmax(v))
	while lo > 0 and v[lo - 1] >= 0.8 * v[i]:
		lo -= 1
	while hi < len(v) - 1 and v[hi + 1] >= 0.8 * v[i]:
		hi += 1
	valid = np.flatnonzero(~np.isnan(v))
	peak = pd.Period(smooth.index[i], "M")
	peaks.append({
		"series": s, "technology": LABELS[s], "peak_month": str(peak), "peak_rate": round(v[i], 1),
		"level_from": smooth.index[lo], "level_to": smooth.index[hi],
		"level_open_start": lo == valid[0], "level_open_end": hi == valid[-1],
		"time_from": str(peak - 6), "time_to": str(peak + 6),
	})
peaks = pd.DataFrame(peaks).set_index("series")

entries = pd.read_csv(path)
entries = entries[entries.series.isin(list(LABELS)) & (entries.first_month <= last_month)]
senders = entries.uploader_link.unique().tolist()

db = MongoClient("mongodb://localhost:27017")["prn"]
launch = {p["article_id"] for p in db["predictions"].find({"model_version": version, "predicted_class": 1}, {"article_id": 1})}
releases, launches = Counter(), Counter()
cursor = db["articles"].find(
	{"status": "scraped", "published_at": {"$type": "date"}, "uploader_link": {"$in": senders}},
	{"header": 1, "published_at": 1, "uploader_link": 1, "lede": {"$substrCP": [{"$ifNull": ["$body", ""]}, 0, 1001]}},
	batch_size=5000)
for i, d in enumerate(cursor):
	# English rule from 03-tech-waves/keywords.py
	lede = d["lede"]
	lede = re.sub(r"\S*$", "", lede) if len(lede) > 1000 else lede
	text = re.sub(r"\s+", " ", (d.get("header") or "") + " " + lede).strip()
	if len(re.findall(r" (the|and|of|to) ", text.lower())) >= 4:
		key = d["uploader_link"], d["published_at"].strftime("%Y-%m")
		releases[key] += 1
		launches[key] += d["_id"] in launch
	if i % 200000 == 0:
		print(i, flush=True)

sm = pd.DataFrame([(*k, v, launches[k]) for k, v in releases.items()], columns=["uploader_link", "month", "n", "n_launch"])
sm = sm.sort_values(["uploader_link", "month"])
g = sm.groupby("uploader_link")
sm["releases_prior"] = g.n.cumsum() - sm.n
sm["launches_prior"] = g.n_launch.cumsum() - sm.n_launch
sm["first_release"] = g.month.transform("min")

co = pd.DataFrame(db["companies"].find({"_id": {"$in": senders}}, {
	"org_type": 1, "founded_year": 1, "is_public": 1, "country": 1,
	"revenue": 1, "revenue_unit": 1, "assets": 1, "assets_unit": 1, "employees": 1,
})).rename(columns={"_id": "uploader_link"})
e = entries.merge(co, on="uploader_link", how="left")
n_dropped = (e.org_type != "operating_company").sum()
e = e[e.org_type == "operating_company"].merge(
	sm.drop(columns=["n", "n_launch"]), left_on=["uploader_link", "first_month"], right_on=["uploader_link", "month"], how="left")
print("entries without a sender-month:", e.releases_prior.isna().sum(), "of", len(e))
e["years_since_first_release"] = (
	e.first_month.str[:4].astype(float) * 12 + e.first_month.str[5:].astype(float)
	- e.first_release.str[:4].astype(float) * 12 - e.first_release.str[5:].astype(float)) / 12
e["years_since_founding"] = e.first_month.str[:4].astype(float) - e.founded_year
e["public"] = e.is_public.map({True: 100.0, False: 0.0})
e["origin"] = e.country.map(ORIGIN).fillna("Other").where(e.country.notna())
e["revenue_usd_m"] = e.revenue.where(e.revenue_unit == "USD") / 1e6
e["assets_usd_m"] = e.assets.where(e.assets_unit == "USD") / 1e6


def summarise(g):
	"""Metrics for one technology-period cell of entering senders."""
	return pd.Series({
		"senders": len(g),
		"releases_prior_median": g.releases_prior.median(), "releases_prior_mean": g.releases_prior.mean(),
		"launches_prior_median": g.launches_prior.median(), "launches_prior_mean": g.launches_prior.mean(),
		"years_since_first_release_median": g.years_since_first_release.median(), "years_since_first_release_mean": g.years_since_first_release.mean(),
		"pct_first_month_in_corpus": 100 * (g.years_since_first_release == 0).mean(),
		"years_since_founding_median": g.years_since_founding.median(),
		"pct_public": g.public.mean(),
		**{f"pct_{o.lower()}": 100 * (g.origin.dropna() == o).mean() for o in ORIGINS},
		"revenue_usd_m_median": g.revenue_usd_m.median(), "assets_usd_m_median": g.assets_usd_m.median(),
		"employees_median": g.employees.median(),
		**{f"known_{c}": 100 * g[c].notna().mean() for c in ["founded_year", "public", "origin", "revenue_usd_m", "assets_usd_m", "employees"]},
	})


long, wide = {}, {}
for definition in ["level", "time"]:
	p = peaks.loc[e.series]
	period = pd.Series(np.select(
		[e.first_month.to_numpy() < p[f"{definition}_from"].to_numpy(), e.first_month.to_numpy() <= p[f"{definition}_to"].to_numpy()],
		["early", "peak"], "late"), index=e.index)
	if definition == "level":
		period[(period == "early") & p.level_open_start.to_numpy()] = None
		period[(period == "late") & p.level_open_end.to_numpy()] = None
	e["period"] = period
	peaks[[f"{definition}_{q}" for q in PERIODS]] = e.groupby(["series", "period"]).size().unstack().reindex(index=peaks.index, columns=PERIODS).fillna(0).astype(int).to_numpy()

	grid = pd.MultiIndex.from_product([LABELS, PERIODS], names=["series", "period"])
	t = e.groupby(["series", "period"]).apply(summarise, include_groups=False).reindex(grid)
	t["senders"] = t.senders.fillna(0)
	t = t.reset_index()
	t.insert(0, "technology", t.series.map(LABELS))
	mean = t.groupby("period", sort=False).mean(numeric_only=True).reset_index().assign(technology="All (mean)")
	pooled = e.groupby("period").apply(summarise, include_groups=False).reindex(PERIODS).reset_index().assign(technology="All (pooled)")
	long[definition] = pd.concat([t.drop(columns="series"), mean, pooled])[["technology", "period", *pooled.columns.drop(["period", "technology"])]].round(1)
	wide[definition] = (long[definition].melt(id_vars=["technology", "period"], var_name="metric")
		.pivot(index=["metric", "period"], columns="technology", values="value")
		.reindex(index=pd.MultiIndex.from_product([long[definition].columns[2:], PERIODS]), columns=[*LABELS.values(), "All (mean)", "All (pooled)"])
		.rename_axis(["metric", "period"]).reset_index())

peaks["entries_dropped_not_operating"] = n_dropped
peaks["launch_version"] = version
for name, sheets in [("by_technology", {**long, "peaks": peaks}), ("by_metric", wide)]:
	with pd.ExcelWriter(HERE / f"{name}.xlsx") as xl:
		for sheet, df in sheets.items():
			df.to_excel(xl, sheet_name=sheet, index=False, freeze_panes=(1, 2))
			for col in xl.sheets[sheet].columns:
				xl.sheets[sheet].column_dimensions[col[0].column_letter].width = max(10, min(32, max(len(str(c.value or "")) for c in col) + 2))
print(peaks.drop(columns=["launch_version"]).to_string())
