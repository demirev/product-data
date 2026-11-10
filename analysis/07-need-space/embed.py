"""Embed every need statement and `what_it_is` sentence in the run files -> emb/<model>.parquet (columns: text, vec).

Each text is embedded on its own and bare, with no instruction or prefix. A
Qwen3-Embedding MODEL is last-token pooled; any other MODEL (a sentence-transformers
checkpoint such as all-mpnet-base-v2, the main model) is mean pooled. Vectors are
L2-normalised, so cosine similarity is a dot product. Texts already in the output file
are skipped.

Runs on CUDA in fp16 when a GPU is visible, otherwise on CPU. MODEL is a checkpoint on the
Hugging Face hub; its revision is printed.

    python embed.py MODEL [RUN ...]    # default: the runs of the current codebook version
"""
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer

HERE = Path(__file__).parent
BATCH = 64

model_id = sys.argv[1]
version = hashlib.sha256(open(HERE / "codebook.md").read().split("\n---\n", 1)[1].strip().encode()).hexdigest()[:8]
runs = [Path(p) for p in sys.argv[2:]] or sorted((HERE / "runs").glob(f"*_{version}*.jsonl"))
out = HERE / "emb" / (model_id.replace("/", "__") + ".parquet")
out.parent.mkdir(exist_ok=True)
old = pd.read_parquet(out) if out.exists() else pd.DataFrame({"text": [], "vec": []})

outputs = [json.loads(l)["output"] for p in runs for l in open(p)]
texts = sorted({t for o in outputs for t in o["needs"] + [o["what_it_is"]]} - set(old.text))
print(len(texts), "new texts from", len(runs), "run files", flush=True)
if not texts:
	sys.exit()

device = "cuda" if torch.cuda.is_available() else "cpu"
qwen = "Qwen3-Embedding" in model_id
tokenizer = AutoTokenizer.from_pretrained(model_id, padding_side="left" if qwen else "right")
model = AutoModel.from_pretrained(model_id, dtype=torch.float16 if device == "cuda" else torch.float32).to(device).eval()
print(model_id, "revision", model.config._commit_hash, "on", device, flush=True)

vecs = []
with torch.no_grad():
	for k in range(0, len(texts), BATCH):
		batch = tokenizer(texts[k:k + BATCH], padding=True, truncation=True, return_tensors="pt").to(device)
		h = model(**batch).last_hidden_state.float()
		m = batch["attention_mask"].unsqueeze(-1).float()
		# Left padding puts every text's last token in the final position.
		pooled = h[:, -1] if qwen else (h * m).sum(1) / m.sum(1)
		vecs += F.normalize(pooled, dim=1).cpu().tolist()
pd.concat([old, pd.DataFrame({"text": texts, "vec": vecs})]).to_parquet(out)
print(len(old) + len(texts), "texts in", out)
