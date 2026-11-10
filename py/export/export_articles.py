"""Dump scraped articles to Parquet shards for scoring with score.py.

Each row is `_id` and the text the classifier reads, `clean_text(header + " " +
body)`, with whitespace collapsed and cut to MAX_CHARS. The tokenizer splits on
whitespace, so collapsing it leaves the tokens unchanged, and the model reads at
most 512 tokens (about 2,500 characters), so the cut stays past anything it sees.

    venv/bin/python py/export/export_articles.py OUT_DIR [--shard-size N] [--limit N]
"""
import argparse
import itertools
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db
from classify.finetune import clean_text

MAX_CHARS = 8000

parser = argparse.ArgumentParser(description="Export scraped articles to Parquet shards")
parser.add_argument("out_dir")
parser.add_argument("--shard-size", type=int, default=100_000)
parser.add_argument("--limit", type=int, default=0, help="Stop after N articles (0: all)")
args = parser.parse_args()

os.makedirs(args.out_dir, exist_ok=True)
cursor = get_db()["articles"].find(
	{"status": "scraped"}, {"header": 1, "body": 1}, batch_size=5000, limit=args.limit
)
total = 0
for shard, docs in enumerate(itertools.batched(cursor, args.shard_size)):
	table = pa.table({
		"_id": [d["_id"] for d in docs],
		"text": [
			" ".join(clean_text((d.get("header") or "") + " " + (d.get("body") or "")).split())[:MAX_CHARS]
			for d in docs
		],
	})
	pq.write_table(table, os.path.join(args.out_dir, f"shard-{shard:05d}.parquet"), compression="zstd")
	total += len(docs)
	print(f"shard {shard}: {len(docs)} articles ({total} total)", flush=True)
