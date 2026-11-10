"""Who launches AI products since ChatGPT, against who launches everything else -> xlsx.

Reads the newest firm_months_*.csv.gz from extract.py and company fields from Mongo.
The wave runs from WAVE_START (Dec 2022) to the last full month; periods are the whole
wave and each calendar year, with Dec 2022 counted in 2023 and the current year partial.
Groups are AI launches and non-AI launches (every other launch). Only senders with
`org_type` operating_company are kept.

Two units:
  launches  each launch is an observation, carrying its sender's characteristics in the
            launch month; a sender-month with k launches counts k times
  firms     each sender launching in the group and period is an observation, taken at
            its first such launch in the period
A sender launching both kinds of product appears in both groups.

Sender characteristics in month t:
  releases_prior, launches_prior  releases and launches before t (mean and median)
  years_since_first_release       t minus the sender's first release in the corpus
  years_since_founding            year of t minus `founded_year`
  pct_new_to_corpus               first release less than two years before t
  pct_wave_entrant                first release in the wave
  pct_first_launch                no launch before t
  pct_young                       founded at most five years before t
  pct_public, pct_<origin>        `is_public`; `country` grouped as in 05-wave-senders
  pct_tech                        modal PR Newswire category is business- or consumer-technology or telecommunications
  revenue_usd_m, assets_usd_m, employees  medians; revenue and assets USD only
Sender behaviour in the period:
  ai_share_of_own_launches        the sender's AI launches over all its launches in the period (mean)
  pct_repeat                      at least two launches of the group in the period
  pct_prev_year                   a launch of the group in the previous year (years from 2024)
Percentages and medians are over observations with a known value; `known_*` columns
give that coverage. Company fields are as of now.

Sheet market (per period and group): firms, launches, launches per firm, share of
launches from the top 1% and top 10% of firms, HHI (0-10,000).

Each metric is given for AI, non-AI and the ratio AI / non-AI (blank when non-AI is 0).

Outputs:
  by_period.xlsx  sheets launches, firms, market: period, group, one column per metric; sheet sample
  by_metric.xlsx  sheets launches, firms, market: metric, group, one column per period

    python tables.py [FIRM_MONTHS_CSV]
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pymongo import MongoClient

HERE = Path(__file__).parent
WAVE_START = "2022-12"
EUROPE = "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB CH NO IS LI".split()
ORIGIN = {"US": "US", "CA": "Canada", "CN": "China", "HK": "China", "MO": "China", "JP": "Japan", "IN": "India", "IL": "Israel", **dict.fromkeys(EUROPE, "Europe")}
ORIGINS = ["US", "Canada", "Europe", "China", "Japan", "India", "Israel", "Other"]
TECH = {"business-technology", "consumer-technology", "telecommunications"}
GROUPS = ["AI", "non-AI", "AI / non-AI"]

path = Path(sys.argv[1]) if len(sys.argv) > 1 else max(HERE.glob("firm_months_*.csv.gz"))
fm = pd.read_csv(path)
fm["mi"] = fm.month.str[:4].astype(int) * 12 + fm.month.str[5:].astype(int)
g = fm.groupby("uploader_link")
fm["releases_prior"] = g.releases.cumsum() - fm.releases
fm["launches_prior"] = g.launches.cumsum() - fm.launches
fm["first_mi"] = g.mi.transform("min")
fm["first_month"] = g.month.transform("min")
last_month = fm.month.max()
w = fm[(fm.month >= WAVE_START) & (fm.month < last_month)].copy()
w["year"] = w.month.str[:4].where(w.month != WAVE_START, str(int(WAVE_START[:4]) + 1))
periods = ["wave", *sorted(w.year.unique())]

db = MongoClient("mongodb://localhost:27017")["prn"]
co = pd.DataFrame(db["companies"].find({"_id": {"$in": w.uploader_link.unique().tolist()}}, {
	"org_type": 1, "founded_year": 1, "is_public": 1, "country": 1, "categories": 1,
	"revenue": 1, "revenue_unit": 1, "assets": 1, "assets_unit": 1, "employees": 1,
})).rename(columns={"_id": "uploader_link"})
co["tech"] = co.categories.map(lambda c: 100.0 * (max(c, key=c.get) in TECH) if isinstance(c, dict) and c else np.nan)
co["public"] = co.is_public.map({True: 100.0, False: 0.0})
co["origin"] = co.country.map(ORIGIN).fillna("Other").where(co.country.notna())
co["revenue_usd_m"] = co.revenue.where(co.revenue_unit == "USD") / 1e6
co["assets_usd_m"] = co.assets.where(co.assets_unit == "USD") / 1e6
w = w.merge(co, on="uploader_link", how="left")
sample = {
	"firm_months": path.name, "months": f"{WAVE_START} to {w.month.max()}",
	"senders": w.uploader_link.nunique(), "senders_not_operating_dropped": w[w.org_type != "operating_company"].uploader_link.nunique(),
}
w = w[w.org_type == "operating_company"]
sample["senders_kept"] = w.uploader_link.nunique()

w["years_since_first_release"] = (w.mi - w.first_mi) / 12
w["years_since_founding"] = w.month.str[:4].astype(int) - w.founded_year
w["new_to_corpus"] = 100.0 * (w.years_since_first_release < 2)
w["wave_entrant"] = 100.0 * (w.first_month >= WAVE_START)
w["first_launch"] = 100.0 * (w.launches_prior == 0)
w["young"] = (100.0 * (w.years_since_founding <= 5)).where(w.founded_year.notna())

# One row per sender-month, group and period, with n launches of the group
obs = pd.concat([w.assign(group="AI", n=w.ai_launches), w.assign(group="non-AI", n=w.launches - w.ai_launches)]).query("n > 0")
obs = pd.concat([obs.assign(period=obs.year), obs.assign(period="wave")], ignore_index=True)

own = pd.concat([w.assign(period=w.year), w.assign(period="wave")]).groupby(["period", "uploader_link"])[["launches", "ai_launches"]].sum()
fp = obs.groupby(["period", "group", "uploader_link"]).n.sum().rename("n_group").reset_index()
fp = fp.merge((100 * own.ai_launches / own.launches).rename("ai_share_of_own_launches").reset_index(), on=["period", "uploader_link"])
fp["repeat"] = 100.0 * (fp.n_group >= 2)
prev = fp[fp.period != "wave"][["period", "group", "uploader_link"]].assign(period=lambda x: (x.period.astype(int) + 1).astype(str), prev_year=100.0)
fp = fp.merge(prev, on=["period", "group", "uploader_link"], how="left")
fp["prev_year"] = fp.prev_year.fillna(0).where(~fp.period.isin(["wave", periods[1]]))
obs = obs.merge(fp, on=["period", "group", "uploader_link"])


def summarise(g):
	"""Metrics for one period-group cell."""
	return pd.Series({
		"n": len(g),
		"releases_prior_median": g.releases_prior.median(), "releases_prior_mean": g.releases_prior.mean(),
		"launches_prior_median": g.launches_prior.median(), "launches_prior_mean": g.launches_prior.mean(),
		"years_since_first_release_median": g.years_since_first_release.median(),
		"years_since_founding_median": g.years_since_founding.median(),
		**{f"pct_{c}": g[c].mean() for c in ["new_to_corpus", "wave_entrant", "first_launch", "young", "public", "tech"]},
		**{f"pct_{o.lower()}": 100 * (g.origin.dropna() == o).mean() for o in ORIGINS},
		"revenue_usd_m_median": g.revenue_usd_m.median(), "assets_usd_m_median": g.assets_usd_m.median(),
		"employees_median": g.employees.median(),
		"ai_share_of_own_launches_mean": g.ai_share_of_own_launches.mean(),
		"pct_repeat": g["repeat"].mean(), "pct_prev_year": g["prev_year"].mean(),
		**{f"known_{c}": 100 * g[c].notna().mean() for c in ["founded_year", "public", "tech", "origin", "revenue_usd_m", "assets_usd_m", "employees"]},
	})


def market(g):
	"""Concentration of launches across firms for one period-group cell."""
	n = g.n_group.sort_values(ascending=False).to_numpy()
	return pd.Series({
		"firms": len(n), "launches": n.sum(), "launches_per_firm": n.mean(),
		"top1pct_share": 100 * n[:math.ceil(0.01 * len(n))].sum() / n.sum(),
		"top10pct_share": 100 * n[:math.ceil(0.1 * len(n))].sum() / n.sum(),
		"hhi": 10000 * ((n / n.sum()) ** 2).sum(),
	})


obs = obs.reset_index(drop=True)
cells = {
	"launches": obs.loc[obs.index.repeat(obs.n)].groupby(["period", "group"]).apply(summarise, include_groups=False),
	"firms": obs.sort_values("month").groupby(["period", "group", "uploader_link"]).head(1).groupby(["period", "group"]).apply(summarise, include_groups=False),
	"market": fp.groupby(["period", "group"]).apply(market, include_groups=False),
}
long, wide = {}, {}
for name, t in cells.items():
	ratio = (t.xs("AI", level="group") / t.xs("non-AI", level="group")).replace([np.inf, -np.inf], np.nan).assign(group="AI / non-AI").set_index("group", append=True)
	t = pd.concat([t, ratio]).reindex(pd.MultiIndex.from_product([periods, GROUPS], names=["period", "group"]))
	long[name] = t.round(2).reset_index()
	wide[name] = (long[name].melt(id_vars=["period", "group"], var_name="metric").pivot(index=["metric", "group"], columns="period", values="value")
		.reindex(index=pd.MultiIndex.from_product([t.columns, GROUPS]), columns=periods).rename_axis(["metric", "group"]).reset_index())

for name, sheets in [("by_period", {**long, "sample": pd.DataFrame(sample.items(), columns=["item", "value"])}), ("by_metric", wide)]:
	with pd.ExcelWriter(HERE / f"{name}.xlsx") as xl:
		for sheet, df in sheets.items():
			df.to_excel(xl, sheet_name=sheet, index=False, freeze_panes=(1, 2))
			for col in xl.sheets[sheet].columns:
				xl.sheets[sheet].column_dimensions[col[0].column_letter].width = max(10, min(36, max(len(str(c.value or "")) for c in col) + 2))
print(long["firms"].set_index(["period", "group"]).T.to_string())
