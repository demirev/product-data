"""Load score.py outputs into `predictions` under one model version.

Writes the fields `classify_press_releases` writes, keyed on (article_id,
model_version), so the classify service treats these articles as done and
scores only what was scraped after the export. Re-running is safe.

    venv/bin/python py/export/import_predictions.py OUT_DIR MODEL_VERSION
"""
import argparse
import glob
import os
import sys
from datetime import datetime, timezone

import pandas as pd
from pymongo import UpdateOne

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db, ensure_indexes

CHUNK = 5000

parser = argparse.ArgumentParser(description="Import score.py outputs into predictions")
parser.add_argument("out_dir")
parser.add_argument("model_version", help="Run name, e.g. distilbert-finetuned-20261026160427")
args = parser.parse_args()

db = get_db()
if db["model_runs"].find_one({"_id": args.model_version}) is None:
	sys.exit(f"{args.model_version} is not in model_runs; the version must name a trained run.")
ensure_indexes()

total = 0
for path in sorted(glob.glob(os.path.join(args.out_dir, "*.parquet"))):
	df = pd.read_parquet(path)
	now = datetime.now(timezone.utc)
	for i in range(0, len(df), CHUNK):
		chunk = df.iloc[i:i + CHUNK]
		db["predictions"].bulk_write([
			UpdateOne(
				{"article_id": a, "model_version": args.model_version},
				{"$set": {
					"predicted_class": int(c),
					"class_probabilities": [float(x) for x in p],
					"predicted_at": now,
				}},
				upsert=True,
			)
			for a, c, p in zip(chunk["article_id"], chunk["predicted_class"], chunk["class_probabilities"])
		], ordered=False)
	total += len(df)
	print(f"{os.path.basename(path)}: {len(df)} ({total} total)", flush=True)
