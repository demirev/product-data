"""Monthly keyword shares for three technology waves (mobile apps, crypto, AI) -> CSVs.

Text matched is the header plus the lede (first 1,000 characters of the body,
trailing partial word dropped), whitespace-collapsed. All terms are case-insensitive
except those wrapped in `(?-i:...)`.

Releases kept, in both numerator and denominator: scraped, dated, sender not
blacklisted, English (at least four of " the/and/of/to " in the text), and sender
not a market-research firm. A sender is market research if it has at least 20
releases and at least 25% of its headers read like a market-size report, or if its
slug is in ANALYST_FIRMS. Launch flags come from the latest `launch` run, as in
01-release-counts.

Outputs:
  monthly.csv   month, group (all|launch), series, n, senders, total (months with n = 0 omitted)
  terms.csv     month, group, series, term, n
  coverage.csv  month, scraped, market_research, non_english, kept
  senders.csv   senders with n >= 20 and report-header share >= 10%, and whether dropped
  samples.csv   up to 10 random matched headers per series and year

    python keywords.py [SAMPLE_SIZE OUT_DIR]
"""
import csv
import multiprocessing
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from pymongo import MongoClient

MOBILE = [
	r"\b(mobile|iphone|ipad|android|ios|smartphone|tablet) apps?\b",
	r"\b(mobile|smartphone) applications?\b",
	r"\bapps? (for|on) (the )?(iphone|ipad|ios|android|smartphones?|apple watch)\b",
	r"\bapp ?store\b",
	r"\bgoogle play\b", r"\bplay store\b", r"\bandroid market\b",
]
CRYPTO = [
	r"bitcoin", r"blockchain", r"crypto-?currenc", r"\bcrypto[- ]?assets?\b",
	r"ethereum", r"stablecoin", r"\bnfts?\b", r"non-fungible",
	r"\bweb3\b", r"\bdefi\b", r"initial coin offering",
	r"\bsmart contracts?\b", r"distributed ledger", r"\bon-?chain\b",
]
CRYPTO_BROAD = CRYPTO + [
	r"\bcrypto\b", r"\bweb 3(\.0)?\b",
	r"\bdigital[- ]assets? (exchange|trading|trader|custod|treasur|wallet|broker|mining|miner|fund|investment|space|industry|ecosystem|market|firm|compan|bank|infrastructure|platform|secur|lend|regulat|holding|issuer|issuance)",
	r"\btokeni[sz](ed|ation|ing|e)( of)? (real[- ]world |physical |traditional |financial )?(assets?|securities|funds?|stocks?|equit|bonds?|treasur|deposits?|real estate|capital markets|gold|money market)",
	r"\breal[- ]world assets?\b", r"\bdecentrali[sz]ed (finance|exchange|apps?|autonomous)",
	r"\bxrp\b", r"\bsolana\b(?! beach)", r"\bdogecoin\b", r"\blitecoin\b", r"\baltcoins?\b", r"\bmeme ?coins?\b",
]
AGENTIC = [r"agentic", r"\bai agents?\b"]
# Lookarounds on llm skip the law degree ("JD, LLM", "LLM in Taxation")
GENAI = [
	r"generative a\.?i\b", r"generative artificial intelligence", r"\bgen[- ]?ai\b",
	r"large language model", r"(?<!jd, )(?<!j\.d\., )(?<!jd )\bllms?\b(?!\s?,\s?(cpa|cfa)|\s(in|law)\b)",
	r"foundation models?\b", r"\bgpt-?[0-9]", r"chatgpt", r"\bopenai\b", r"\banthropic\b", r"(?<!co-)\bcopilot\b",
	r"diffusion models?\b", r"stable diffusion", r"\bdall[-·]e\b", r"midjourney",
	r"text-to-(image|video|3d)", r"\bai[- ]generated\b",
	r"retrieval[- ]augmented", r"prompt engineering",
]
GENAI_EXTRA = [
	r"(?<!cash[- ])(?<!value-)\bgenerative\b(?! (design|art|growth|grammar|medicine|nfts?|chemistry))",
	r"\bai (assistants?|copilots?|companions?)\b", r"\bdeepfakes?\b", r"\bdeepseek\b", r"\bperplexity\b",
	r"\bmistral ai\b", r"hugging ?face",
	r"\bgoogle'?s? gemini\b|\bgemini (ai|app|assistant|models?|pro|ultra|nano|flash|live|[0-9])\b",
	r"image generation", r"voice cloning", r"vector (database|search)", r"\bai hallucinations?\b|\bhallucinations? in ai\b",
]
AI = GENAI + AGENTIC + [
	r"artificial intelligence", r"machine learning", r"deep learning", r"neural networks?\b",
	r"computer vision", r"natural language processing", r"conversational ai", r"chatbots?\b",
	r"(?<![\w-])ai-(powered|driven|enabled|based|native|first|ready|assisted|enhanced|led|focused|optimized|integrated|centric|fueled|augmented|guided|infused|empowered|supported|accelerated)\b",
]
# Bare AI: not preceded by a word character, ".", "&" or "-"; may be followed by "-"
AI_BROAD = AI + GENAI_EXTRA + [
	r"(?-i:(?<![\w.&-])AI(?!\w|\.\w))", r"(?-i:(?<![\w.])A\.I\.(?![A-Z]\.))",
	r"\bcognitive computing\b", r"\bnlp\b",
]
SERIES = {
	"mobile": MOBILE, "crypto_broad": CRYPTO_BROAD, "crypto": CRYPTO,
	"ai_broad": AI_BROAD, "ai": AI, "genai_broad": GENAI + GENAI_EXTRA, "genai": GENAI, "agentic": AGENTIC,
}
# Each series' terms are a subset of its parent's, so it is searched only when the parent matched
PARENT = {"crypto": "crypto_broad", "ai": "ai_broad", "genai_broad": "ai_broad", "genai": "genai_broad", "agentic": "ai"}
COMBINED = {s: re.compile("|".join(terms), re.I) for s, terms in SERIES.items()}
TERMS = {s: [(t, re.compile(t, re.I)) for t in terms] for s, terms in SERIES.items()}

MARKET_REPORT_HEADER = r"market (size|share|report|research|analysis|forecast|outlook|trends|growth|to (reach|grow|witness|surpass|hit|be worth))|\bcagr\b|\bforecast (period|to 20)|market (is|will) (expected|projected|anticipated|estimated)"
ANALYST_FIRMS = re.compile(r"^/news/(frost-%26(amp%3b)?-sullivan|idtechex|dell%27oro-group|abi-research|parks-associates|strategy-analytics|omdia|qks-group|forrester|lux-research|mintel|counterpoint|gartner|juniper-research)")

def match(d):
	"""English flag and matched terms per series for one release; runs in the worker pool."""
	lede = d.pop("lede")
	lede = re.sub(r"\S*$", "", lede) if len(lede) > 1000 else lede
	text = re.sub(r"\s+", " ", (d.get("header") or "") + " " + lede).strip()
	english = len(re.findall(r" (the|and|of|to) ", text.lower())) >= 4
	hits = {}
	for s, rx in COMBINED.items():
		if english and (s not in PARENT or PARENT[s] in hits) and rx.search(text):
			hits[s] = [t for t, r in TERMS[s] if r.search(text)]
	return d, english, hits


# Fork before the Mongo client exists
pool = multiprocessing.get_context("fork").Pool(12)
db = MongoClient("mongodb://localhost:27017")["prn"]
sample = int(sys.argv[1]) if len(sys.argv) > 1 else None
out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent
blocked = db["sender_blacklist"].distinct("_id")
version = db["model_runs"].find_one({"task": "launch"}, sort=[("trained_at", -1)])["_id"]
launch = {p["article_id"] for p in db["predictions"].find({"model_version": version, "predicted_class": 1}, {"article_id": 1})}
eligible = {"status": "scraped", "published_at": {"$type": "date"}, "uploader_link": {"$nin": blocked}}

senders = list(db["articles"].aggregate([
	{"$match": eligible},
	{"$group": {
		"_id": "$uploader_link", "n": {"$sum": 1},
		"report": {"$sum": {"$cond": [{"$regexMatch": {"input": {"$ifNull": ["$header", ""]}, "regex": MARKET_REPORT_HEADER, "options": "i"}}, 1, 0]}},
	}},
	{"$match": {"n": {"$gte": 20}}},
], allowDiskUse=True))
for s in senders:
	s["share"] = s["report"] / s["n"]
	s["dropped"] = s["share"] >= 0.25 or bool(ANALYST_FIRMS.match(s["_id"] or ""))
market_research = {s["_id"] for s in senders if s["dropped"]}

pipeline = [{"$match": eligible}] + ([{"$sample": {"size": sample}}] if sample else []) + [
	{"$project": {"header": 1, "published_at": 1, "uploader_link": 1, "lede": {"$substrCP": [{"$ifNull": ["$body", ""]}, 0, 1001]}}},
]

n, terms, coverage = Counter(), Counter(), Counter()
who = defaultdict(set)
seen, samples = Counter(), defaultdict(list)
rng = random.Random(0)
for i, (d, english, hits) in enumerate(pool.imap(match, db["articles"].aggregate(pipeline, allowDiskUse=True), chunksize=200)):
	month = d["published_at"].strftime("%Y-%m")
	coverage[month, "scraped"] += 1
	if d.get("uploader_link") in market_research:
		coverage[month, "market_research"] += 1
		continue
	if not english:
		coverage[month, "non_english"] += 1
		continue
	coverage[month, "kept"] += 1
	groups = ("all", "launch") if d["_id"] in launch else ("all",)
	for g in groups:
		n[month, g, "_total"] += 1
	for s, hit in hits.items():
		for g in groups:
			n[month, g, s] += 1
			who[month, g, s].add(d.get("uploader_link"))
			for t in hit:
				terms[month, g, s, t] += 1
		key = (s, month[:4])
		seen[key] += 1
		row = [s, month, d["_id"] in launch, re.sub(r"\s+", " ", d.get("header") or "").strip(), "; ".join(hit), d["_id"]]
		if len(samples[key]) < 10:
			samples[key].append(row)
		elif (j := rng.randrange(seen[key])) < 10:
			samples[key][j] = row
	if i % 100000 == 0:
		print(i, month, flush=True)
pool.close()

out.mkdir(parents=True, exist_ok=True)
with open(out / "monthly.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["month", "group", "series", "n", "senders", "total"])
	for (m, g, s), k in sorted(n.items()):
		if s != "_total":
			w.writerow([m, g, s, k, len(who[m, g, s]), n[m, g, "_total"]])
with open(out / "terms.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["month", "group", "series", "term", "n"])
	w.writerows([*k, v] for k, v in sorted(terms.items()))
with open(out / "coverage.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["month", "scraped", "market_research", "non_english", "kept", "launch_kept", "launch_version"])
	for m in sorted({m for m, _ in coverage}):
		w.writerow([m, *(coverage[m, c] for c in ("scraped", "market_research", "non_english", "kept")), n[m, "launch", "_total"], version])
with open(out / "senders.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["uploader_link", "n", "report_headers", "share", "dropped"])
	w.writerows([s["_id"], s["n"], s["report"], round(s["share"], 3), s["dropped"]] for s in sorted(senders, key=lambda s: -s["share"]) if s["share"] >= 0.1 or s["dropped"])
with open(out / "samples.csv", "w", newline="") as f:
	w = csv.writer(f)
	w.writerow(["series", "month", "launch", "header", "terms", "id"])
	for k in sorted(samples):
		w.writerows(sorted(samples[k], key=lambda r: r[1]))
