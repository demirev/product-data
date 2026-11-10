"""Needs of every eligible launch, one daily batch at a time -> data/needs/<prompt version>/<model>_r<pass>.jsonl.

Runs once a day from cron. The queue is every launch that `sample.py` counts as eligible,
shuffled with a fixed seed and frozen in queue.txt on the first run, so the releases done so
far are always a random sample. Pass 2 starts only when pass 1 has covered the whole queue.
A run stops when the tokens spent since midnight UTC, when the daily free quota resets,
would pass BUDGET, or the budget given as an argument.

Prompt, schema and release text are those of `extract.py`; the script exits if codebook.md
no longer hashes to PROMPT. A release that fails for good (missing from the database, a 400
error, output that is not JSON) goes to errors.jsonl and is not retried; more than MAX_ERRORS
of these in one run end it. Other API errors
skip the release until the next run; rate-limit and access errors end the run. After the
run on LAST, or once both passes are done, the script deletes its own crontab line.

Each line: id, model, prompt, at, usage, text, output ({product, users, what_it_is, needs})

    python extract_daily.py [BUDGET]
"""
import fcntl
import hashlib
import json
import random
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import openai
from openai import OpenAI
from pymongo import MongoClient

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MODEL, PROMPT, PASSES = "gpt-6-luna", "8a7e67b1", 2
BUDGET, LAST = 9_000_000, date(2027, 2, 11)
WORKERS, CHUNK, MAX_WORDS, MAX_ERRORS = 8, 64, 1200, 20
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

BUDGET = int(sys.argv[1]) if len(sys.argv) > 1 else BUDGET
OUT = ROOT / "data" / "needs" / PROMPT
OUT.mkdir(parents=True, exist_ok=True)
lock = open(OUT / "lock", "w")
try:
	fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
	sys.exit("an earlier run is still going")
prompt = open(HERE / "codebook.md").read().split("\n---\n", 1)[1].strip()
if hashlib.sha256(prompt.encode()).hexdigest()[:8] != PROMPT:
	sys.exit(f"codebook.md is no longer prompt version {PROMPT}")
start = datetime.now(timezone.utc)
print(start.isoformat(timespec="seconds"), "start", flush=True)

db = MongoClient("mongodb://localhost:27017")["prn"]
if not (OUT / "queue.txt").exists():
	# Eligibility as in sample.py.
	version = db["model_runs"].find_one({"task": "launch"}, sort=[("trained_at", -1)])["_id"]
	launch = {p["article_id"] for p in db["predictions"].find({"model_version": version, "predicted_class": 1}, {"article_id": 1})}
	ids = []
	for d in db["articles"].find(
			{"status": "scraped", "published_at": {"$type": "date"}, "uploader_link": {"$nin": db["sender_blacklist"].distinct("_id")}},
			{"header": 1, "lede": {"$substrCP": [{"$ifNull": ["$body", ""]}, 0, 1001]}}, batch_size=5000):
		if d["_id"] in launch:
			lede = re.sub(r"\S*$", "", d["lede"]) if len(d["lede"]) > 1000 else d["lede"]
			if len(re.findall(r" (the|and|of|to) ", (re.sub(r"\s+", " ", d.get("header") or "").strip() + " " + re.sub(r"\s+", " ", lede)).lower())) >= 4:
				ids.append(d["_id"])
	ids.sort()
	random.Random(0).shuffle(ids)
	(OUT / "queue.txt").write_text("\n".join(ids) + "\n")
	print(len(ids), "launches queued; launch model", version, flush=True)
queue = (OUT / "queue.txt").read_text().split()

since = start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
files = [OUT / f"{MODEL}_r{k}.jsonl" for k in range(1, PASSES + 1)]
done, used = [set() for _ in files], 0
for k, f in enumerate(files):
	for l in open(f) if f.exists() else []:
		r = json.loads(l)
		done[k].add(r["id"])
		used += r["usage"]["total_tokens"] if r["at"] >= since else 0
for l in open(OUT / "errors.jsonl") if (OUT / "errors.jsonl").exists() else []:
	r = json.loads(l)
	done[r["pass"] - 1].add(r["id"])

env = {k: v.strip().strip("'\"") for k, v in (l.strip().split("=", 1) for l in open(ROOT / ".env") if "=" in l and not l.startswith("#"))}
client = OpenAI(api_key=env["OPENAI_API_KEY"], max_retries=8, timeout=300)


def extract(d):
	body = re.split(r"\n\s*SOURCE ", d.get("body") or "")[0]
	about = [m.start() for m in re.finditer(r"\n\s*About [A-Z0-9]", body) if m.start() > len(body) / 3]
	body = body[:about[0]] if about else body
	text = (d.get("header") or "").strip() + "\n\n" + " ".join(body.split()[:MAX_WORDS])
	resp = client.responses.create(
		model=MODEL, instructions=prompt, input=text,
		text={"format": {"type": "json_schema", "name": "needs", "schema": SCHEMA, "strict": True}})
	return {"id": d["_id"], "model": MODEL, "prompt": PROMPT, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"usage": resp.usage.model_dump(), "text": text, "output": json.loads(resp.output_text)}


pool, stop, n_run, n_err, tokens_run = ThreadPoolExecutor(WORKERS), False, 0, 0, 0
for k, f in enumerate(files):
	todo = [i for i in queue if i not in done[k]]
	print(f"pass {k + 1}: {len(todo)} of {len(queue)} to do; {used} tokens used today", flush=True)
	with open(f, "a") as out, open(OUT / "errors.jsonl", "a") as err:
		for c in range(0, len(todo), CHUNK):
			# Keep a chunk's worth of headroom, at the mean cost per call so far.
			if stop or used + CHUNK * (tokens_run / n_run if n_run else 2100) > BUDGET:
				stop = True
				break
			chunk = todo[c:c + CHUNK]
			docs = {d["_id"]: d for d in db["articles"].find({"_id": {"$in": chunk}}, {"header": 1, "body": 1})}
			futures = [(i, pool.submit(extract, docs[i]) if i in docs else None) for i in chunk]
			for i, fut in futures:
				at = datetime.now(timezone.utc).isoformat(timespec="seconds")
				try:
					if fut is None:
						raise LookupError("not in the database")
					row = fut.result()
				except (LookupError, openai.BadRequestError, json.JSONDecodeError) as e:
					err.write(json.dumps({"id": i, "pass": k + 1, "at": at, "error": repr(e)}) + "\n")
					err.flush()
					done[k].add(i)
					n_err += 1
					stop = stop or n_err > MAX_ERRORS
				except (openai.RateLimitError, openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as e:
					print(at, i, "run ends:", repr(e), flush=True)
					stop = True
				except openai.APIError as e:
					print(at, i, "skipped:", repr(e), flush=True)
				else:
					out.write(json.dumps(row) + "\n")
					out.flush()
					done[k].add(i)
					n_run += 1
					tokens_run += row["usage"]["total_tokens"]
					used += row["usage"]["total_tokens"]
	if stop or len(done[k] & set(queue)) < len(queue):
		break
pool.shutdown()

left = [len(set(queue) - d) for d in done]
print(datetime.now(timezone.utc).isoformat(timespec="seconds"), f"end: {n_run} releases, {n_err} errors, {tokens_run} tokens this run; left in each pass {left}", flush=True)
if not left[-1] or date.today() >= LAST:
	tab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
	subprocess.run(["crontab", "-"], input="".join(l for l in tab.splitlines(True) if Path(__file__).name not in l), text=True, check=True)
	print("crontab line removed", flush=True)
