"""Compare two `launch` runs on labels neither trained on.

Two evaluation sets:

- Eval-split mode: the new run's held-out split (its eval_predictions.csv),
  restricted to articles labelled after the old run started, so the old run never
  saw them either. Old-run scores come from `predictions`, new-run scores from the
  CSV. A label edited after the old run carries a later `labeled_at` and so counts
  as unseen.

  Some labels were sampled from a served run's positives: the `fp-*` review
  batches (suspected false positives, many confirmed as launches) and explorer
  marks. That run called every one of them positive, so its recall and
  false-positive rate on them are 1 by construction; sections 1-3 are repeated
  without them.

- Hold-out mode: the articles of every launch review file marked hold-out in the
  labeler (`holdout_files`), which training never sees. Both runs' scores come
  from `predictions`; a run missing them is scored with
  `finetune.py --classify --holdout --model-version <run>`. Labels are the
  human-verified ones in `labels`, or with `--llm` the unverified LLM calls in the
  files' `decision` column.

Reports, each for both runs:
  1. AUC on the shared set, with a paired bootstrap CI on the difference
  2. share called positive (p >= 0.5) by four-way `launch_class`
  3. AUC on sub-problems: launches against each kind of negative
  4. eval-split mode: AUC, recall and false-positive rate by how the label was sampled
  5. hold-out mode: AUC, recall and false-positive rate by publication period,
     consecutive years merged until each period holds MIN_POS launches

    venv/bin/python py/classify/compare_runs.py [EVAL_CSV OLD_VERSION NEW_VERSION]
    venv/bin/python py/classify/compare_runs.py holdout [--llm] OLD_VERSION NEW_VERSION
"""
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from pymongo import MongoClient
from sklearn.metrics import roc_auc_score

HOLDOUT = len(sys.argv) > 1 and sys.argv[1] == "holdout"
LLM = "--llm" in sys.argv
args = [a for a in sys.argv[1:] if a not in ("holdout", "--llm")]
if HOLDOUT:
	OLD, NEW = args
else:
	csv = args[0] if len(args) > 0 else "data/eval_predictions_20261026.csv"
	OLD = args[1] if len(args) > 1 else "distilbert-finetuned-20260828161251"
	NEW = args[2] if len(args) > 2 else "distilbert-finetuned-20261026160427"
CLASSES = {0: "irrelevant", 1: "launch", 2: "product_info", 3: "feature_release"}
BOOT = 2000
MIN_POS = 15

db = MongoClient("mongodb://localhost:27017")["prn"]
# Run names end in their UTC start time; labeled_at is naive UTC.
cut = datetime.strptime(OLD[-14:], "%Y%m%d%H%M%S")
print(f"Old run: {OLD}\nNew run: {NEW}")

if HOLDOUT:
	files = list(db["holdout_files"].find({"task": "launch"}))
	ids = [i for f in files for i in f["ids"]]
	print(f"Hold-out files: {', '.join(f['file'] for f in files)}; {len(ids)} articles")
	# A run that started before a file was marked could have trained on its labels.
	for run in (OLD, NEW):
		start = datetime.strptime(run[-14:], "%Y%m%d%H%M%S")
		for f in files:
			n = db["labels"].count_documents({"_id": {"$in": f["ids"]}, "labeled_at": {"$lt": start}})
			if f["marked_at"] > start and n:
				print(f"WARNING: {run} started before {f['file']} was marked hold-out and may have trained on {n} of its labels")
	if LLM:
		labels = pd.concat(
			pd.read_csv(os.path.join("data/labels-for-review", f["file"]), usecols=["id", "decision"])
			for f in files
		).rename(columns={"id": "_id"})
		labels["launch_class"] = labels["decision"].str.strip().str.lower().map(
			{"irrelevant": 0, "relevant": 1, "product_info": 2, "feature_release": 3}
		)
		labels = labels.dropna(subset=["launch_class"])
	else:
		labels = pd.DataFrame(db["labels"].find({"_id": {"$in": ids}}, {"launch_class": 1}))
	print(f"Labels ({'unverified LLM calls' if LLM else 'human-verified'}): {len(labels)}")
	if labels.empty:
		sys.exit("No labels yet; review the hold-out files, or pass --llm")
	df = labels[["_id", "launch_class"]]
	for run, col in ((OLD, "p_old"), (NEW, "p_new")):
		scores = pd.DataFrame(
			{"_id": p["article_id"], col: p["class_probabilities"][1]}
			for p in db["predictions"].find(
				{"model_version": run, "article_id": {"$in": ids}},
				{"article_id": 1, "class_probabilities": 1},
			)
		)
		print(f"{run}: predictions on {len(scores)} hold-out articles")
		if scores.empty:
			sys.exit(f"Score it first: finetune.py --classify --holdout --task launch --model-version {run}")
		df = df.merge(scores, on="_id")
	dates = pd.DataFrame(db["articles"].find({"_id": {"$in": ids}}, {"published_date": 1}))
	# published_date is free text in a few layouts ("Apr 14, 2021, 08:00 ET", "01 Dec, 2023, ...").
	dates["year"] = dates["published_date"].astype(str).str.findall(r"(?:19|20)\d\d").str[-1].astype(float)
	df = df.merge(dates[["_id", "year"]], on="_id")
	df["y"] = (df["launch_class"] == 1).astype(int)
	print(f"Scored by both runs and labelled: {len(df)}, {df['y'].sum()} launches")
	populations = [("HOLD-OUT" + (" (UNVERIFIED LLM LABELS)" if LLM else ""), df)]
else:
	df = pd.read_csv(csv, usecols=["_id", "true_label", "p_relevant"]).rename(columns={"p_relevant": "p_new"})
	ids = df["_id"].tolist()
	labels = pd.DataFrame(db["labels"].find(
		{"_id": {"$in": ids}}, {"launch_class": 1, "source": 1, "review_file": 1, "labeled_at": 1}
	))
	old = pd.DataFrame(
		{"_id": p["article_id"], "p_old": p["class_probabilities"][1]}
		for p in db["predictions"].find(
			{"model_version": OLD, "article_id": {"$in": ids}},
			{"article_id": 1, "class_probabilities": 1},
		)
	)
	df = df.merge(labels, on="_id").merge(old, on="_id")
	df["y"] = (df["launch_class"] == 1).astype(int)
	mined = (df["source"] == "explorer") | df["review_file"].fillna("").str.startswith("fp-")
	df["mined"] = mined
	df["sampling"] = np.where(mined, df["review_file"].fillna("explorer").str[:4], df["source"].fillna("none"))

	mismatch = (df["y"] != (df["true_label"] == "relevant")).sum()
	print(f"New eval split: {len(ids)}; joined to labels and old predictions: {len(df)}; label changed since training: {mismatch}")
	df = df[df["labeled_at"] > cut].reset_index(drop=True)
	print(f"Labelled after {cut:%Y-%m-%d %H:%M} UTC (unseen by both): {len(df)}, {df['y'].sum()} launches")
	populations = [
		("ALL UNSEEN LABELS", df),
		("EXCLUDING LABELS MINED FROM SERVED-RUN POSITIVES", df[~df["mined"]].reset_index(drop=True)),
	]

rng = np.random.default_rng(0)
for title, pop in populations:
	y, p_old, p_new = pop["y"].to_numpy(), pop["p_old"].to_numpy(), pop["p_new"].to_numpy()
	boot = rng.integers(0, len(pop), size=(BOOT, len(pop)))
	print(f"\n=== {title}: n = {len(pop)}, {y.sum()} launches ===\n")

	print("1. AUC, launch vs everything else (paired bootstrap over articles)")
	auc_old, auc_new = roc_auc_score(y, p_old), roc_auc_score(y, p_new)
	diffs = np.array([roc_auc_score(y[b], p_new[b]) - roc_auc_score(y[b], p_old[b]) for b in boot])
	lo, hi = np.percentile(diffs, [2.5, 97.5])
	print(f"   old {auc_old:.3f}   new {auc_new:.3f}   new - old {auc_new - auc_old:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  P(new > old) {(diffs > 0).mean():.2f}\n")

	print("2. Share called positive (p >= 0.5) by four-way label")
	print(f"   {'class':<16}{'n':>5}{'old':>8}{'new':>8}")
	for c, name in CLASSES.items():
		m = pop["launch_class"] == c
		print(f"   {name:<16}{m.sum():>5}{(pop.loc[m, 'p_old'] >= 0.5).mean():>8.2f}{(pop.loc[m, 'p_new'] >= 0.5).mean():>8.2f}")
	print()

	print("3. AUC, launches against one kind of negative (paired bootstrap)")
	print(f"   {'negatives':<28}{'n neg':>6}{'old':>8}{'new':>8}{'new - old':>11}   95% CI")
	for name, negs in [
		("irrelevant", [0]),
		("product_info", [2]),
		("feature_release", [3]),
		("product_info + feature", [2, 3]),
	]:
		m = pop["launch_class"].isin([1] + negs).to_numpy()
		d = np.array([
			roc_auc_score(y[s], p_new[s]) - roc_auc_score(y[s], p_old[s])
			for s in (b[m[b]] for b in boot)
		])
		a_old, a_new = roc_auc_score(y[m], p_old[m]), roc_auc_score(y[m], p_new[m])
		lo, hi = np.percentile(d, [2.5, 97.5])
		print(f"   {name:<28}{(m & (y == 0)).sum():>6}{a_old:>8.3f}{a_new:>8.3f}{a_new - a_old:>+11.3f}   [{lo:+.3f}, {hi:+.3f}]")

if HOLDOUT:
	# Merge consecutive years until each period holds MIN_POS launches; a short tail joins the last period.
	periods, cur = [], []
	for year, g in df.dropna(subset=["year"]).groupby("year"):
		cur.append(int(year))
		if df.loc[df["year"].isin(cur), "y"].sum() >= MIN_POS:
			periods.append(cur)
			cur = []
	if cur:
		periods = periods[:-1] + [periods[-1] + cur] if periods else [cur]

	print(f"\n5. By publication period (years merged until each holds {MIN_POS} launches; paired bootstrap)")
	print(f"   {'period':<12}{'n':>5}{'pos':>5}{'AUC old':>9}{'AUC new':>9}{'new - old':>11}   {'95% CI':<18}{'TPR old':>8}{'TPR new':>8}{'FPR old':>8}{'FPR new':>8}")
	for years in periods:
		g = df[df["year"].isin(years)].reset_index(drop=True)
		y, p_old, p_new = g["y"].to_numpy(), g["p_old"].to_numpy(), g["p_new"].to_numpy()
		boot = rng.integers(0, len(g), size=(BOOT, len(g)))
		d = np.array([roc_auc_score(y[b], p_new[b]) - roc_auc_score(y[b], p_old[b]) for b in boot if 0 < y[b].sum() < len(b)])
		a_old, a_new = roc_auc_score(y, p_old), roc_auc_score(y, p_new)
		lo, hi = np.percentile(d, [2.5, 97.5])
		label = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
		print(
			f"   {label:<12}{len(g):>5}{y.sum():>5}{a_old:>9.3f}{a_new:>9.3f}{a_new - a_old:>+11.3f}   {f'[{lo:+.3f}, {hi:+.3f}]':<18}"
			f"{(p_old[y == 1] >= 0.5).mean():>8.2f}{(p_new[y == 1] >= 0.5).mean():>8.2f}"
			f"{(p_old[y == 0] >= 0.5).mean():>8.2f}{(p_new[y == 0] >= 0.5).mean():>8.2f}"
		)
	sys.exit()

print("\n4. By how the label was sampled (fp-N: review batch of suspected false positives)")
print(f"   {'sampling':<16}{'n':>5}{'pos':>5}{'AUC old':>9}{'AUC new':>9}{'TPR old':>9}{'TPR new':>9}{'FPR old':>9}{'FPR new':>9}")
for src, g in df.groupby("sampling"):
	both = g["y"].nunique() == 2
	neg = g["y"] == 0
	print(
		f"   {src:<16}{len(g):>5}{g['y'].sum():>5}"
		+ (f"{roc_auc_score(g['y'], g['p_old']):>9.3f}{roc_auc_score(g['y'], g['p_new']):>9.3f}" if both else f"{'-':>9}{'-':>9}")
		+ (f"{(g.loc[~neg, 'p_old'] >= 0.5).mean():>9.2f}{(g.loc[~neg, 'p_new'] >= 0.5).mean():>9.2f}" if (~neg).any() else f"{'-':>9}{'-':>9}")
		+ (f"{(g.loc[neg, 'p_old'] >= 0.5).mean():>9.2f}{(g.loc[neg, 'p_new'] >= 0.5).mean():>9.2f}" if neg.any() else f"{'-':>9}{'-':>9}")
	)
