"""Distances in the need space on the pilot, for the noise floor and planted pairs.

The distance between two releases is the symmetric Chamfer distance between their need
sets: each need's cosine distance (1 - cosine) to the closest need of the other release,
averaged over each side, then over the two sides. `first` uses only the first need.
Releases with no needs are left out.

  same release   one run against another on the same releases: luna twice (noise floor),
                 and across models
  pairs          within one run: sibling pairs, cross-era old vs new on one topic and on
                 different topics, all pairs of releases, each release's nearest other release
  retrieval      share of releases whose closest output in run B, among the non-sibling
                 releases, is its own
  cross-era      for each new (2020+) cross-era release, whether its closest old cross-era
                 release shares its topic; then the distance from tech_created and other
                 recent strata to the nearest pre-2020 release in the pilot
  disagreements  the sol and luna outputs furthest apart
  leak           how well a logistic regression (10-fold cross-validated AUC) tells AI from
                 non-AI launches of 2023 on, from the mean need vector, from the same without
                 needs that name AI, and from the `what_it_is` vector (luna); then, with the
                 need held fixed, the mean predicted P(AI) of old and new cross-era releases
  triplets       share of the human judgments in judgments.csv that agree with the space,
                 by how far apart the two candidates' distances to A are
  judges         pairwise agreement between the human, each judgments_<model>.csv from
                 judge_claude.py, and each embedding model in emb/

    python validate.py [MODEL] > validate.txt    # default sentence-transformers/all-mpnet-base-v2
"""
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from itertools import combinations, product
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score

HERE = Path(__file__).parent
EMB = (sys.argv[1] if len(sys.argv) > 1 else "sentence-transformers/all-mpnet-base-v2").replace("/", "__")
VERSION = hashlib.sha256(open(HERE / "codebook.md").read().split("\n---\n", 1)[1].strip().encode()).hexdigest()[:8]
RUNS = {"sol": f"gpt-6.1-sol_{VERSION}", "luna": f"gpt-6-luna_{VERSION}", "luna r2": f"gpt-6-luna_{VERSION}_r2"}

e = pd.read_parquet(HERE / "emb" / f"{EMB}.parquet")
vec = dict(zip(e.text, e.vec))
pilot = {r["id"]: r for r in csv.DictReader(open(HERE / "pilot.csv"))}
needs, what = {}, {}
for name, f in RUNS.items():
	rows = [r for l in open(HERE / "runs" / f"{f}.jsonl") if (r := json.loads(l))["id"] in pilot]
	needs[name] = {r["id"]: r["output"]["needs"] for r in rows if r["output"]["needs"]}
	what[name] = {r["id"]: r["output"]["what_it_is"] for r in rows if r["output"]["needs"]}
mat = {name: {i: np.stack([vec[n] for n in ns]) for i, ns in d.items()} for name, d in needs.items()}


def dist(a, b):
	d = 1 - a @ b.T
	return (d.min(1).mean() + d.min(0).mean()) / 2, d[0, 0]


def row(label, ds):
	q = np.quantile(np.array(ds), [0.1, 0.5, 0.9], axis=0).T
	print(f"{label:<34}{len(ds):>6}   " + "   ".join(f"{a:.3f} {b:.3f} {c:.3f}" for a, b, c in q))


print(f"prompt {VERSION}; embedding {EMB}; runs " + ", ".join(f"{k} {len(v)}" for k, v in needs.items()) + " releases with needs\n")
print(f"{'':<34}{'n':>6}   {'set: p10 median p90':<20}  {'first: p10 median p90'}")
print("same release")
for a, b in [("luna", "luna r2"), ("sol", "luna")]:
	row(f"  {a} vs {b}", [dist(mat[a][i], mat[b][i]) for i in sorted(mat[a].keys() & mat[b].keys())])

for m in ("sol", "luna"):
	print(f"\npairs of releases, {m}")
	ids = sorted(mat[m])
	groups = defaultdict(list)
	for i in ids:
		groups[pilot[i]["stratum"], pilot[i]["group"]].append(i)
	row("  sibling pairs", [dist(mat[m][a], mat[m][b]) for (s, _), v in groups.items() if s == "sibling" and len(v) == 2 for a, b in [v]])
	era = {g: ([i for i in v if pilot[i]["month"] < "2020"], [i for i in v if pilot[i]["month"] >= "2020"]) for (s, g), v in groups.items() if s == "cross_era"}
	row("  cross-era, same topic", [dist(mat[m][a], mat[m][b]) for old, new in era.values() for a, b in product(old, new)])
	row("  cross-era, different topic", [dist(mat[m][a], mat[m][b]) for g, h in product(era, era) if g != h for a, b in product(era[g][0], era[h][1])])
	all_pairs = {(a, b): dist(mat[m][a], mat[m][b]) for a, b in combinations(ids, 2)}
	row("  all pairs", list(all_pairs.values()))
	row("  nearest other release", [min((d for (a, b), d in all_pairs.items() if i in (a, b)), key=lambda d: d[0]) for i in ids])

print("\nretrieval: own output is the closest in run B (set distance), among non-sibling releases")
for a, b in [("luna", "luna r2"), ("sol", "luna"), ("luna", "sol")]:
	ids = sorted(i for i in mat[a].keys() & mat[b].keys() if pilot[i]["stratum"] != "sibling")
	cands = sorted(i for i in mat[b] if pilot[i]["stratum"] != "sibling")
	hits = [min(cands, key=lambda j: dist(mat[a][i], mat[b][j])[0]) == i for i in ids]
	print(f"  {a} -> {b:<10}{sum(hits):>4} of {len(ids)}  ({np.mean(hits):.0%})")

for m in ("sol", "luna"):
	ids = sorted(mat[m])
	old = [i for i in ids if pilot[i]["month"] < "2020"]
	xold = [i for i in old if pilot[i]["stratum"] == "cross_era"]
	new = [i for i in ids if pilot[i]["stratum"] == "cross_era" and pilot[i]["month"] >= "2020"]
	print(f"\ncross-era retrieval, {m}: + when a new release's closest old cross-era release is from its topic")
	print(f"  {'':<2}{'topic':<17}{'own':>6}{'best':>7}  new release; closest old cross-era release on a miss")
	hits = 0
	for i in new:
		d = {j: dist(mat[m][i], mat[m][j])[0] for j in old}
		best = min(xold, key=d.get)
		own = min((d[j] for j in xold if pilot[j]["group"] == pilot[i]["group"]), default=np.nan)
		hits += pilot[best]["group"] == pilot[i]["group"]
		print(f"  {'+' if pilot[best]['group'] == pilot[i]['group'] else '-':<2}{pilot[i]['group']:<17}{own:>6.3f}{d[best]:>7.3f}  {pilot[i]['header'][:70]}")
		if pilot[best]["group"] != pilot[i]["group"]:
			print(f"  {'':<32}{pilot[best]['group']}: {pilot[best]['header'][:60]}")
	print(f"  {hits} of {len(new)} hits")
	print(f"\ndistance to the nearest pre-2020 pilot release, {m}")
	for label, group in [("tech_created", [i for i in ids if pilot[i]["stratum"] == "tech_created"]), ("cross-era, new", new),
			("ai_2023+", [i for i in ids if pilot[i]["stratum"] == "ai_2023+"]), ("other_2023+", [i for i in ids if pilot[i]["stratum"] == "other_2023+"])]:
		row(f"  {label}", [min((dist(mat[m][i], mat[m][j]) for j in old), key=lambda d: d[0]) for i in group])
	for i in ids:
		if pilot[i]["stratum"] == "tech_created":
			print(f"    {min(dist(mat[m][i], mat[m][j])[0] for j in old):.3f}  {needs[m][i][0]}")

print("\nsol vs luna, furthest apart (set distance)")
far = sorted(((dist(mat["sol"][i], mat["luna"][i])[0], i) for i in mat["sol"].keys() & mat["luna"].keys()), reverse=True)[:8]
for d, i in far:
	print(f"\n  {d:.3f}  {pilot[i]['header'][:110]} ({pilot[i]['month']})")
	for m in ("sol", "luna"):
		print(f"    {m:<5}" + "\n         ".join(needs[m][i]))

print("\nleak: AI vs non-AI launches of 2023 on, luna, 10-fold AUC (mean, sd)")
ids = sorted(i for i in mat["luna"] if pilot[i]["stratum"] in ("ai_2023+", "leak_ai", "other_2023+", "leak_other"))
y = np.array([pilot[i]["ai"] == "True" for i in ids])
plain = {i: [n for n in needs["luna"][i] if not re.search(r"\b(AI|artificial intelligence|machine learning|LLMs?|generative|chatbots?)\b", n)] for i in ids}
cv = StratifiedKFold(10, shuffle=True, random_state=0)
for label, sub, X in [
		("needs", ids, [mat["luna"][i].mean(0) for i in ids]),
		("needs that do not name AI", [i for i in ids if plain[i]], [np.mean([vec[n] for n in plain[i]], 0) for i in ids if plain[i]]),
		("what_it_is", ids, [vec[what["luna"][i]] for i in ids])]:
	yy = np.array([pilot[i]["ai"] == "True" for i in sub])
	auc = cross_val_score(LogisticRegression(max_iter=5000), np.array(X), yy, cv=cv, scoring="roc_auc")
	print(f"  {label:<28}{len(sub):>5} releases ({yy.sum()} AI)   {auc.mean():.3f}  {auc.std():.3f}")

print("\nleak, same need: the models above fit on all 2023+ launches, mean P(AI) for old and new cross-era releases")
ce = sorted(i for i in mat["luna"] if pilot[i]["stratum"] == "cross_era")
new = np.array([pilot[i]["month"] >= "2020" for i in ce])
for label, X, Xce in [("needs", [mat["luna"][i].mean(0) for i in ids], [mat["luna"][i].mean(0) for i in ce]),
		("what_it_is", [vec[what["luna"][i]] for i in ids], [vec[what["luna"][i]] for i in ce])]:
	p = LogisticRegression(max_iter=5000).fit(np.array(X), y).predict_proba(np.array(Xce))[:, 1]
	by = pd.DataFrame({"topic": [pilot[i]["group"] for i in ce], "new": new, "p": p}).groupby(["topic", "new"]).p.mean().unstack()
	print(f"  {label:<14}old {p[~new].mean():.2f}  new {p[new].mean():.2f}   new above old in {(by[True] > by[False]).sum()} of {len(by)} topics")

if (HERE / "judgments.csv").exists():
	t = pd.read_csv(HERE / "triplets.csv").merge(pd.read_csv(HERE / "judgments.csv"), on="id")
	print(f"\ntriplets: {len(t)} judged, {(t.choice == 'none').sum()} can't tell")
	t = t[t.choice != "none"]
	t["d_ab"], t["d_ac"] = [1 - np.sum(np.stack(t.a.map(vec)) * np.stack(t[k].map(vec)), 1) for k in "bc"]
	t["agree"] = (t.choice == "b") == (t.d_ab < t.d_ac)
	t["gap"] = pd.qcut((t.d_ab - t.d_ac).abs(), 3, labels=["small gap", "medium gap", "large gap"])
	print(f"  {'all':<14}{len(t):>5}   {t.agree.mean():.0%} agree")
	for g, v in t.groupby("gap", observed=True):
		print(f"  {g:<14}{len(v):>5}   {v.agree.mean():.0%} agree   gap {(v.d_ab - v.d_ac).abs().min():.3f}-{(v.d_ab - v.d_ac).abs().max():.3f}")

judges = {"you": HERE / "judgments.csv"} | {f.stem.split("_", 1)[1]: f for f in sorted(HERE.glob("judgments_*.csv"))}
if len(judges) > 1 and all(f.exists() for f in judges.values()):
	t = pd.read_csv(HERE / "triplets.csv")
	picks = {}
	for f in sorted((HERE / "emb").glob("*.parquet")):
		ev = pd.read_parquet(f).set_index("text").vec
		if set(t.a) | set(t.b) | set(t.c) <= set(ev.index):
			va, vb, vc = (np.stack(ev.loc[t[k]]) for k in "abc")
			picks[f.stem.split("__")[-1]] = dict(zip(t.id, np.where((va * vb).sum(1) > (va * vc).sum(1), "b", "c")))
	picks |= {k: dict(zip(d.id, d.choice)) for k, f in judges.items() if len(d := pd.read_csv(f))}
	print("\njudges: agreement on triplets both answered, can't-tells left out")
	for a, b in combinations(picks, 2):
		common = [i for i in picks[a].keys() & picks[b].keys() if "none" not in (picks[a][i], picks[b][i])]
		print(f"  {a:<18} vs {b:<18}{len(common):>5}   {np.mean([picks[a][i] == picks[b][i] for i in common]):.0%}")
