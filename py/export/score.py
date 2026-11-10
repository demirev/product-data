"""Score exported article shards with a fine-tuned checkpoint.

Reads the `*.parquet` shards written by export_articles.py from IN_DIR and writes
one file of the same name to OUT_DIR, holding `article_id`, `predicted_class` and
`class_probabilities`. A shard whose output exists is skipped, so re-running the
same command resumes an interrupted run. `--part K/N` takes every Nth shard from
the Kth, for N concurrent processes.

`--gate DIR` scores only the articles that another model's outputs in DIR called
positive: the AI model over the launch model's positives.

Runs on CUDA in fp16 when a GPU is visible, otherwise on CPU with
OMP_NUM_THREADS threads. Needs only torch, transformers, pandas and pyarrow.

    python score.py MODEL_DIR IN_DIR OUT_DIR [--part K/N] [--batch-size B] [--gate DIR]
"""
import argparse
import glob
import os
import time

import numpy as np
import pandas as pd
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

parser = argparse.ArgumentParser(description="Score exported article shards")
parser.add_argument("model_dir")
parser.add_argument("in_dir")
parser.add_argument("out_dir")
parser.add_argument("--part", default="0/1", help="K/N: this process takes shards K, K+N, ...")
parser.add_argument("--batch-size", type=int, default=None, help="Default 256 on GPU, 16 on CPU")
parser.add_argument("--gate", default=None, help="Output dir of a model whose positives to score")
args = parser.parse_args()

device = "cuda" if torch.cuda.is_available() else "cpu"
batch_size = args.batch_size or (256 if device == "cuda" else 16)
tokenizer = AutoTokenizer.from_pretrained(args.model_dir)
model = AutoModelForSequenceClassification.from_pretrained(args.model_dir).to(device).eval()
if device == "cuda":
	model.half()
print(f"{args.model_dir} on {device}, batch {batch_size}, threads {torch.get_num_threads()}", flush=True)

k, n = map(int, args.part.split("/"))
os.makedirs(args.out_dir, exist_ok=True)
for path in sorted(glob.glob(os.path.join(args.in_dir, "*.parquet")))[k::n]:
	name = os.path.basename(path)
	out = os.path.join(args.out_dir, name)
	if os.path.exists(out):
		continue
	start = time.time()
	df = pd.read_parquet(path)
	if args.gate:
		gate = pd.read_parquet(os.path.join(args.gate, name), columns=["article_id", "predicted_class"])
		df = df[df["_id"].isin(gate.loc[gate["predicted_class"] == 1, "article_id"])]

	ids = tokenizer(df["text"].tolist(), truncation=True, max_length=512)["input_ids"]
	# Length-sorted batches pad less; results go back to row order through `idx`.
	order = np.argsort([len(x) for x in ids], kind="stable")
	probs = np.empty((len(ids), model.config.num_labels), dtype=np.float32)
	with torch.inference_mode():
		for i in range(0, len(order), batch_size):
			idx = order[i:i + batch_size]
			batch = tokenizer.pad({"input_ids": [ids[j] for j in idx]}, return_tensors="pt").to(device)
			probs[idx] = torch.softmax(model(**batch).logits.float(), dim=1).cpu().numpy()

	pd.DataFrame({
		"article_id": df["_id"].to_numpy(),
		"predicted_class": probs.argmax(axis=1),
		"class_probabilities": list(probs),
	}).to_parquet(out + ".tmp")
	os.replace(out + ".tmp", out)
	elapsed = time.time() - start
	print(f"{name}: {len(ids)} articles in {elapsed:.0f}s ({len(ids) / max(elapsed, 1e-9):.0f}/s)", flush=True)
