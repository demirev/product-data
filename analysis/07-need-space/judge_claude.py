"""A Claude model answers the triplets of triplets.csv, as a second judge -> judgments_<model>.csv (id, choice, time).

The model gets the instruction the human sees in judge.py and the triplet with B and C as
options 1 and 2, and answers b, c or none (can't tell). Triplets already in the output
file are skipped.

    python judge_claude.py [MODEL]    # default claude-sonnet-5-5
"""
import csv
import json
import sys
import time
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-5-5"
OUT = HERE / f"judgments_{MODEL}.csv"
SYSTEM = ("You compare customer needs, each written as a job someone is trying to get done. Given a need A and "
	"two others, say which of the two is closer in need to A. Judge the job to be done, not the wording: would a "
	"solution to one go a long way toward the other? Answer b for option 1, c for option 2, or none if you cannot tell.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["choice"],
	"properties": {"choice": {"type": "string", "enum": ["b", "c", "none"]}}}

env = {k: v.strip().strip("'\"") for k, v in (l.strip().split("=", 1) for l in open(HERE.parents[1] / ".env") if "=" in l and not l.startswith("#"))}
client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
done = {r["id"] for r in csv.DictReader(open(OUT))} if OUT.exists() else set()
if not OUT.exists():
	OUT.write_text("id,choice,time\n")

for t in csv.DictReader(open(HERE / "triplets.csv")):
	if t["id"] in done:
		continue
	resp = client.messages.create(
		model=MODEL, max_tokens=1000, system=SYSTEM,
		messages=[{"role": "user", "content": f"A: {t['a']}\n\nOption 1: {t['b']}\nOption 2: {t['c']}"}],
		output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
	if resp.stop_reason == "refusal":
		print(t["id"], "refused", flush=True)
		continue
	choice = json.loads(next(b.text for b in resp.content if b.type == "text"))["choice"]
	with open(OUT, "a") as f:
		f.write(f"{t['id']},{choice},{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
	print(t["id"], choice, resp.usage.input_tokens, resp.usage.output_tokens, flush=True)
