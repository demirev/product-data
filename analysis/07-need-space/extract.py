"""Needs of each pilot release's main product, from one OpenAI model -> runs/<model>_<prompt version>[_r<N>].jsonl.

The system prompt is codebook.md below its first `---` line; the prompt version is the first
8 hex digits of its SHA-256. The release text is the header plus the body, cut at the
"About ..." paragraph or the SOURCE line, whichever comes first, and capped at MAX_WORDS.
Releases already in the output file are skipped. `--rep N` (N >= 2) writes a repeat run
to its own file, for the run-to-run noise floor.

Each line: id, model, prompt, usage, text, output ({product, users, what_it_is, needs: [job, ...]})

    python extract.py MODEL [--rep N] [ID ...]    # an ID may be its numeric suffix; default is all of pilot.csv
"""
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

from openai import OpenAI
from pymongo import MongoClient

HERE = Path(__file__).parent
MAX_WORDS = 1200
SCHEMA = {
	"type": "object", "additionalProperties": False,
	"required": ["product", "users", "what_it_is", "needs"],
	"properties": {
		"product": {"type": "string"},
		"users": {"type": "string"},
		"what_it_is": {"type": "string"},
		"needs": {"type": "array", "items": {"type": "string"}},
	},
}

args = sys.argv[1:]
rep = ""
if "--rep" in args:
	k = args.index("--rep")
	rep = f"_r{args[k + 1]}"
	del args[k:k + 2]
model, picks = args[0], args[1:]
prompt = open(HERE / "codebook.md").read().split("\n---\n", 1)[1].strip()
version = hashlib.sha256(prompt.encode()).hexdigest()[:8]
out = HERE / "runs" / f"{model}_{version}{rep}.jsonl"
out.parent.mkdir(exist_ok=True)
done = {json.loads(l)["id"] for l in open(out)} if out.exists() else set()

pilot = [r["id"] for r in csv.DictReader(open(HERE / "pilot.csv"))]
ids = [i for i in pilot if any(i == a or i.endswith("-" + a) for a in picks)] if picks else pilot
ids = [i for i in ids if i not in done]

env = {k: v.strip().strip("'\"") for k, v in (l.strip().split("=", 1) for l in open(HERE.parents[1] / ".env") if "=" in l and not l.startswith("#"))}
client = OpenAI(api_key=env["OPENAI_API_KEY"])
db = MongoClient("mongodb://localhost:27017")["prn"]

for d in db["articles"].find({"_id": {"$in": ids}}, {"header": 1, "body": 1}):
	body = re.split(r"\n\s*SOURCE ", d.get("body") or "")[0]
	about = [m.start() for m in re.finditer(r"\n\s*About [A-Z0-9]", body) if m.start() > len(body) / 3]
	body = body[:about[0]] if about else body
	text = (d.get("header") or "").strip() + "\n\n" + " ".join(body.split()[:MAX_WORDS])
	resp = client.responses.create(
		model=model, instructions=prompt, input=text,
		text={"format": {"type": "json_schema", "name": "needs", "schema": SCHEMA, "strict": True}})
	row = {"id": d["_id"], "model": model, "prompt": version, "usage": resp.usage.model_dump(), "text": text, "output": json.loads(resp.output_text)}
	with open(out, "a") as f:
		f.write(json.dumps(row) + "\n")
	print(d["_id"], resp.usage.input_tokens, resp.usage.output_tokens, flush=True)
