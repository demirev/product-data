"""Triplets for the human check of the need space -> triplets.csv.

Each triplet is an anchor need A and two needs B and C from other releases; a person
judges which of B and C is closer in need to A (judge.py), and the space agrees when that
one has the smaller cosine distance. B and C sit at two ranks of A's neighbour list drawn
log-uniformly from 1 to RANKS, so triplets range from easy (one near, one far) to hard
(both near). Needs of A's own release and near-copies of A (distance below 0.05) are left
out. B and C are in random order.

Columns: id, a, b, c, d_ab, d_ac (cosine distances in EMB, the space the triplets were drawn in)

    python triplets.py [RUN ...]    # default: the luna run of the current codebook version
"""
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
EMB = "sentence-transformers__all-mpnet-base-v2"
N, RANKS = 300, 200

version = hashlib.sha256(open(HERE / "codebook.md").read().split("\n---\n", 1)[1].strip().encode()).hexdigest()[:8]
runs = [Path(p) for p in sys.argv[1:]] or [HERE / "runs" / f"gpt-6-luna_{version}.jsonl"]
releases = defaultdict(set)
for p in runs:
	for l in open(p):
		r = json.loads(l)
		for n in r["output"]["needs"]:
			releases[n].add(r["id"])
texts = sorted(releases)
e = pd.read_parquet(HERE / "emb" / f"{EMB}.parquet").set_index("text")
V = np.stack(e.loc[texts, "vec"])

rng = np.random.default_rng(0)
rows = []
for k, a in enumerate(rng.choice(len(texts), N, replace=False)):
	d = 1 - V @ V[a]
	ok = np.array([d[j] >= 0.05 and not releases[t] & releases[texts[a]] for j, t in enumerate(texts)])
	order = np.flatnonzero(ok)[np.argsort(d[ok])]
	r1, r2 = np.exp(rng.uniform(0, np.log(min(RANKS, len(order))), 2)).astype(int) - 1
	b, c = order[r1], order[r2 + (r2 == r1)]
	if rng.random() < 0.5:
		b, c = c, b
	rows.append([f"t{k:03d}", texts[a], texts[b], texts[c], round(float(d[b]), 4), round(float(d[c]), 4)])

with open(HERE / "triplets.csv", "w", newline="") as f:
	csv.writer(f).writerows([["id", "a", "b", "c", "d_ab", "d_ac"]] + rows)
print(len(rows), "triplets from", len(texts), "needs in", ", ".join(p.name for p in runs))
