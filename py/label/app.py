import os
import random
import sys
from datetime import datetime, timezone

import polars as pl
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db, ensure_indexes


LABELER = os.environ.get("LABELER_NAME", "anonymous")

DOMAINS = [
	"ai_ml", "software", "hardware_iot", "biotech_pharma", "healthcare",
	"fintech", "energy_cleantech", "automotive_mobility", "industrial",
	"consumer_goods", "media_entertainment", "other",
]
AUDIENCES = ["b2b", "b2c", "b2g", "mixed"]
MODALITIES = [
	"llm_text", "image_gen", "video_gen", "audio_speech",
	"multimodal", "agentic", "infrastructure", "other",
]

# Share of launch cards drawn from launch-keyword headers rather than at random.
# The random stream is what keeps an unbiased slice for eval / base-rate
# estimation and surfaces keyword-free positives, so it must stay non-zero.
KEYWORD_SAMPLE_PROB = 0.7

# Share of AI cards drawn from each `ai_vocab` tier once the seed pool drains:
# generative-AI vocabulary first (likely positives), then generic AI/ML wording
# (the classic-ML and AI-washing hard negatives), with the remainder unrestricted
# for an unbiased slice. See py/vocab.py for how the tiers are assigned.
AI_TIER_CUTOFFS = [(0.5, "genai"), (0.75, "generic")]

# Review rows at or above this LLM confidence are highlighted and can be accepted in bulk.
ACCEPT_CONFIDENCE = 0.9

DATA_DIR = os.path.join(
	os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"
)

app = FastAPI()
templates = Jinja2Templates(
	directory=os.path.join(os.path.dirname(__file__), "templates")
)

ensure_indexes()


ARTICLE_PROJECTION = {"_id": 1, "header": 1, "body": 1, "url": 1, "published_date": 1}


def sample_one(match):
	result = list(get_db()["articles"].aggregate(
		[{"$match": match}, {"$sample": {"size": 1}}, {"$project": ARTICLE_PROJECTION}]
	))
	return result[0] if result else None


def sample_launch_article():
	"""Oversample launch-keyword headers, with an unbiased random remainder."""
	match = {"status": "scraped", "_id": {"$nin": list(done_ids(TASKS["launch"]))}}

	if random.random() < KEYWORD_SAMPLE_PROB:
		article = sample_one({**match, "launch_vocab": True})
		if article:  # keyword pool can be exhausted while random examples remain
			article["sample_method"] = "keyword"
			return article

	article = sample_one(match)
	if article:
		article["sample_method"] = "random"
	return article


def sample_ai_article():
	"""Serve launch-labeled articles first, then a tiered draw on `ai_vocab`.

	The AI model runs downstream of the launch model, so articles already judged
	to contain a product are the closest match to what it sees at serve time —
	drain those first. That pool grows with every launch review batch; once it is
	empty, fall back to the vocabulary tiers.
	"""
	done = list(done_ids(TASKS["ai"]))
	match = {"status": "scraped", "_id": {"$nin": done}}

	# Every launch decision except a plain "irrelevant": a launch, a release
	# detailing an existing product and a feature release all describe a product.
	seed = [
		d["_id"] for d in get_db()["labels"].find(
			{"launch_class": {"$in": [1, 2, 3]}}, {"_id": 1}
		)
	]
	if seed:
		article = sample_one({**match, "_id": {"$in": seed, "$nin": done}})
		if article:
			article["sample_method"] = "launch_labeled"
			return article

	draw = random.random()
	tier = next((t for cutoff, t in AI_TIER_CUTOFFS if draw < cutoff), None)
	if tier:
		article = sample_one({**match, "ai_vocab": tier})
		if article:  # a tier can be exhausted while other articles remain
			article["sample_method"] = tier
			return article

	article = sample_one(match)
	if article:
		article["sample_method"] = "random"
	return article


# Each task is one question asked about an article, with its own label and skip
# collections, decision vocabulary, sampler and review-CSV drop folder. The two
# share every route, template and helper below.
#
# `field` is the single integer a decision is stored as: the index of the option
# that was clicked, recorded exactly as given. Both tasks deliberately offer more
# options than the model they feed has classes — the extra ones are distinctions
# worth having in the data, but still too thinly populated to separate — so
# training folds them into a coarser space. That fold lives in
# `TASKS[...]["collapse"]` in py/classify/finetune.py and nowhere else, never
# here: no decision is lost on the way in, so widening a head later is a config
# change rather than a relabelling pass.
TASKS = {
	"launch": {
		"slug": "launch",
		"title": "product launch",
		"labels": "labels",
		"skips": "label_skips",
		"field": "launch_class",
		"sample": sample_launch_article,
		"rubric": "_rubric_launch.html",
		# 2 and 3 both train as negatives for now: neither is a launch. But a
		# release detailing an already-launched product and one shipping an
		# incremental improvement to it are different things, and the second is
		# the boundary the model gets wrong most often, so they are kept apart.
		"decisions": [
			(1, "relevant"),
			(3, "feature release"),
			(0, "irrelevant"),
			(2, "irrelevant w/ product info"),
		],
		"csv_decisions": {
			"relevant": 1, "irrelevant": 0, "product_info": 2, "feature_release": 3,
		},
		"selects": [("audience", AUDIENCES), ("domain", DOMAINS)],
		"extra_actions": [],
		"stats": [
			("relevant", {"launch_class": 1}),
			("feature release", {"launch_class": 3}),
			("irrelevant", {"launch_class": 0}),
			("w/ product info", {"launch_class": 2}),
		],
		"review_dir": os.environ.get(
			"LABEL_REVIEW_DIR", os.path.join(DATA_DIR, "labels-for-review")
		),
		# The mirror of the AI task's launch dropdown: an optional AI call on
		# articles the AI labeler hasn't decided yet.
		"cross_task": "ai",
	},
	"ai": {
		"slug": "ai",
		"title": "AI product",
		"labels": "ai_labels",
		"skips": "ai_label_skips",
		"field": "ai_class",
		"sample": sample_ai_article,
		"rubric": "_rubric_ai.html",
		# 2 and 3 train as the *positive* class for now — any generative AI being
		# shipped counts — leaving "not AI" the model's only negative. Keep
		# drawing the four-way boundary anyway; the breakdown below is what says
		# when the rarer two have enough examples to be separated again.
		"decisions": [
			(1, "AI product"),
			(0, "not AI"),
			(2, "AI feature"),
			(3, "AI tooling"),
		],
		"csv_decisions": {"ai": 1, "not_ai": 0, "ai_feature": 2, "ai_tooling": 3},
		"selects": [("modality", MODALITIES)],
		# "no product" is a skip rather than a label: the question presupposes a
		# product, so those articles must stay out of the training set. Recording
		# the reason keeps them mineable instead of indistinguishable from an
		# ordinary "unsure" skip.
		"extra_actions": [("no product / N/A", "no_product")],
		"stats": [
			("ai", {"ai_class": 1}),
			("not ai", {"ai_class": 0}),
			("ai feature", {"ai_class": 2}),
			("ai tooling", {"ai_class": 3}),
		],
		"review_dir": os.environ.get(
			"AI_LABEL_REVIEW_DIR", os.path.join(DATA_DIR, "ai-labels-for-review")
		),
		# Broad-sampled AI cards are usually articles the launch labeler has never
		# seen. The labeler is reading the full text anyway, so offer the launch
		# call as an optional second control and harvest a real label rather than
		# inferring one — "AI product" does not imply "launch".
		"cross_task": "launch",
	},
}

templates.env.globals["tasks"] = TASKS


def get_task(slug):
	task = TASKS.get(slug)
	if task is None:
		raise HTTPException(status_code=404, detail=f"unknown task {slug!r}")
	return task


def done_ids(task, ids=None):
	"""Article ids already decided for this task — labeled or skipped."""
	db = get_db()
	query = {} if ids is None else {"_id": {"$in": ids}}
	found = {d["_id"] for d in db[task["labels"]].find(query, {"_id": 1})}
	found |= {d["_id"] for d in db[task["skips"]].find(query, {"_id": 1})}
	return found


def stats(task):
	db = get_db()
	return {
		"labeled": db[task["labels"]].count_documents({}),
		"skipped": db[task["skips"]].count_documents({}),
		"breakdown": [
			(name, db[task["labels"]].count_documents(query))
			for name, query in task["stats"]
		],
	}


def build_label_doc(task, decision, form):
	"""Core label doc shared by manual and review labelling."""
	doc = {task["field"]: decision}
	doc["labeler"] = LABELER
	doc["labeled_at"] = datetime.now(timezone.utc)

	# Metadata describes the thing being labeled, so it applies to every decision
	# except a plain "no": index 0 is the only option in either task with nothing
	# to describe — the launch task's hard negatives and feature releases, and the
	# AI task's feature and tooling classes, all still describe something.
	if decision != 0:
		metadata = {}
		for name in [n for n, _ in task["selects"]] + ["notes"]:
			value = (form.get(name) or "").strip()
			if value:
				metadata[name] = value
		if metadata:
			doc["metadata"] = metadata
	return doc


def write_cross_label(task, article_id, form):
	"""Record the optional second-task call offered on this task's card.

	Does nothing if the other task already labeled or skipped the article, so a
	side-label never overwrites a first-class one, and tags provenance: these come
	from a sample biased towards the *other* task's vocabulary, so they must be
	filterable out of base-rate estimates.
	"""
	decision = (form.get("cross_decision") or "").strip()
	cross = TASKS.get(task.get("cross_task"))
	if not decision or cross is None or done_ids(cross, [article_id]):
		return
	# No metadata: the form's notes and selects were filled in about *this* task.
	doc = build_label_doc(cross, int(decision), {})
	doc["source"] = f"{task['slug']}_side"
	doc["sample_method"] = form.get("sample_method", "")
	get_db()[cross["labels"]].update_one(
		{"_id": article_id}, {"$setOnInsert": doc}, upsert=True
	)


def decorate_cross(task, article):
	"""Flag whether the cross-task control should be shown for this article."""
	cross = TASKS.get(task.get("cross_task"))
	if article and cross is not None:
		article["cross_missing"] = not done_ids(cross, [article["_id"]])
		article["cross_task"] = cross
	return article


def render_card(request, task):
	return templates.TemplateResponse(request, "card.html", {
		"task": task,
		"article": decorate_cross(task, task["sample"]()),
		"stats": stats(task),
	})


def review_files(task):
	if not os.path.isdir(task["review_dir"]):
		return []
	return sorted(f for f in os.listdir(task["review_dir"]) if f.endswith(".csv"))


def read_review_csv(task, file):
	# basename guards against path traversal via the {file} path param.
	path = os.path.join(task["review_dir"], os.path.basename(file))
	if not os.path.isfile(path):
		return []
	# infer_schema_length=0 reads every column as a string; fill nulls for templates.
	df = pl.read_csv(path, infer_schema_length=0).fill_null("")
	return df.to_dicts()


def review_article(task, row):
	"""Assemble a display dict for one CSV row, joining text from Mongo if absent."""
	article = {
		"_id": row.get("id", ""),
		"header": row.get("header", ""),
		"body": row.get("body", ""),
		"url": row.get("url", ""),
		"published_date": row.get("published_date", ""),
	}
	if not article["body"]:
		doc = get_db()["articles"].find_one(
			{"_id": article["_id"]},
			{"header": 1, "body": 1, "url": 1, "published_date": 1},
		) or {}
		article["header"] = article["header"] or doc.get("header", "")
		article["body"] = doc.get("body", "")
		article["url"] = article["url"] or doc.get("url", "")
		article["published_date"] = article["published_date"] or doc.get("published_date", "")

	decision = row.get("decision", "").strip().lower()
	# A decision the task has no radio for (e.g. the AI task's "no_product") is
	# still shown and recorded — it just leaves the form unselected.
	article["provisional"] = task["csv_decisions"].get(decision)
	article["llm_label"] = decision
	article["llm_confidence"] = row.get("llm_confidence", "")
	article["llm_rationale"] = row.get("llm_rationale", "")
	for name in [n for n, _ in task["selects"]] + ["notes"]:
		article[name] = row.get(name, "")

	# A batch may also suggest the other task's call, e.g. `launch_decision` in an
	# AI batch, so both can be confirmed in one pass.
	cross = TASKS.get(task.get("cross_task"))
	if cross is not None:
		suggested = row.get(f"{cross['slug']}_decision", "").strip().lower()
		article["cross_provisional"] = cross["csv_decisions"].get(suggested)
	return article


def review_context(task, file, order="asc"):
	rows = read_review_csv(task, file)
	ids = [r["id"] for r in rows if r.get("id")]
	done = done_ids(task, ids)
	pending = [r for r in rows if r.get("id") and r["id"] not in done]

	confidence = {}
	for r in pending:
		try:
			confidence[r["id"]] = float(r.get("llm_confidence") or 0)
		except ValueError:
			pass
	# Rows with an unparseable confidence go last in either order.
	sign = -1 if order == "desc" else 1
	pending.sort(key=lambda r: (r["id"] not in confidence, sign * confidence.get(r["id"], 0)))

	# Confident rows whose decision the app can record, as a label or a reasoned skip.
	recordable = set(task["csv_decisions"]) | {reason for _, reason in task["extra_actions"]}
	confident = [
		r for r in pending
		if confidence.get(r["id"], 0) >= ACCEPT_CONFIDENCE
		and r.get("decision", "").strip().lower() in recordable
	]

	article = decorate_cross(task, review_article(task, pending[0]) if pending else None)
	if article:
		article["confident"] = pending[0] in confident

	labels = get_db()[task["labels"]]
	# Bulk-accepted rows carry no human_agreed, so they stay out of the rate.
	decided = labels.count_documents({"review_file": file, "human_agreed": {"$exists": True}})
	agreed = labels.count_documents({"review_file": file, "human_agreed": True})
	return {
		"task": task,
		"file": file,
		"order": order,
		"article": article,
		"confident": confident,
		"review_stats": {
			"total": len(ids),
			"reviewed": len(ids) - len(pending),
			"agreement": round(100 * agreed / decided) if decided else None,
			"confident": len(confident),
			"threshold": ACCEPT_CONFIDENCE,
		},
	}


def render_review_card(request, task, file, order):
	return templates.TemplateResponse(request, "review_card.html", review_context(task, file, order))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
	return sampling_page(request, "launch")


@app.get("/card/{task}", response_class=HTMLResponse)
def card(request: Request, task: str):
	return render_card(request, get_task(task))


@app.post("/label/{task}", response_class=HTMLResponse)
async def label(request: Request, task: str):
	task = get_task(task)
	form = await request.form()
	article_id = form["article_id"]
	doc = build_label_doc(task, int(form["decision"]), form)
	doc["source"] = "manual"
	doc["sample_method"] = form.get("sample_method", "")
	get_db()[task["labels"]].update_one({"_id": article_id}, {"$set": doc}, upsert=True)
	write_cross_label(task, article_id, form)
	return render_card(request, task)


@app.post("/skip/{task}", response_class=HTMLResponse)
async def skip(request: Request, task: str):
	task = get_task(task)
	form = await request.form()
	article_id = form["article_id"]
	doc = {
		"labeler": LABELER,
		"skipped_at": datetime.now(timezone.utc),
		"source": "manual",
		"sample_method": form.get("sample_method", ""),
	}
	reason = (form.get("reason") or "").strip()
	if reason:
		doc["reason"] = reason
	get_db()[task["skips"]].update_one({"_id": article_id}, {"$set": doc}, upsert=True)
	write_cross_label(task, article_id, form)
	return render_card(request, task)


@app.get("/review", response_class=HTMLResponse)
def review_root():
	return RedirectResponse("/review/launch")


@app.get("/review/{task}", response_class=HTMLResponse)
def review_index(request: Request, task: str):
	task = get_task(task)
	files = []
	for name in review_files(task):
		ids = [r["id"] for r in read_review_csv(task, name) if r.get("id")]
		files.append({
			"name": name,
			"total": len(ids),
			"reviewed": len(done_ids(task, ids)),
		})
	return templates.TemplateResponse(request, "review_index.html", {
		"task": task,
		"files": files,
		"labeler": LABELER,
	})


@app.get("/review/{task}/{file}", response_class=HTMLResponse)
def review_page(request: Request, task: str, file: str, order: str = "asc"):
	task = get_task(task)
	ctx = review_context(task, file, order)
	ctx["labeler"] = LABELER
	return templates.TemplateResponse(request, "review_page.html", ctx)


@app.post("/review/{task}/label", response_class=HTMLResponse)
async def review_label_post(request: Request, task: str):
	task = get_task(task)
	form = await request.form()
	article_id, file = form["article_id"], form["file"]
	decision = int(form["decision"])
	llm_label = form.get("llm_label", "")

	doc = build_label_doc(task, decision, form)
	doc["source"] = "llm_reviewed"
	doc["review_file"] = file
	doc["llm_label"] = llm_label
	# Agreement is on the core decision; metadata edits don't count as overwrites.
	doc["human_agreed"] = bool(llm_label) and task["csv_decisions"].get(llm_label) == decision
	get_db()[task["labels"]].update_one({"_id": article_id}, {"$set": doc}, upsert=True)
	write_cross_label(task, article_id, form)
	return render_review_card(request, task, file, form.get("order", "asc"))


@app.post("/review/{task}/skip", response_class=HTMLResponse)
async def review_skip_post(request: Request, task: str):
	task = get_task(task)
	form = await request.form()
	article_id, file = form["article_id"], form["file"]
	llm_label = form.get("llm_label", "")

	doc = {
		"labeler": LABELER,
		"skipped_at": datetime.now(timezone.utc),
		"source": "llm_reviewed",
		"review_file": file,
		"llm_label": llm_label,
	}
	reason = (form.get("reason") or "").strip()
	if reason:
		# A reasoned skip is a real decision, so it can agree with the LLM too —
		# e.g. both called it "no_product". An unsure skip stays unscored.
		doc["reason"] = reason
		doc["human_agreed"] = llm_label == reason
	get_db()[task["skips"]].update_one({"_id": article_id}, {"$set": doc}, upsert=True)
	write_cross_label(task, article_id, form)
	return render_review_card(request, task, file, form.get("order", "asc"))


@app.post("/review/{task}/accept", response_class=HTMLResponse)
async def review_accept_post(request: Request, task: str):
	"""Record every pending confident row in `file` as the LLM called it."""
	task = get_task(task)
	form = await request.form()
	file = form["file"]
	db = get_db()
	cross = TASKS.get(task.get("cross_task"))
	for row in review_context(task, file)["confident"]:
		llm_label = row["decision"].strip().lower()
		decision = task["csv_decisions"].get(llm_label)
		# No human_agreed: nobody read the article.
		common = {"source": "llm_accepted", "review_file": file, "llm_label": llm_label}
		if decision is None:
			doc = {"labeler": LABELER, "skipped_at": datetime.now(timezone.utc), "reason": llm_label, **common}
			db[task["skips"]].update_one({"_id": row["id"]}, {"$set": doc}, upsert=True)
		else:
			doc = {**build_label_doc(task, decision, row), **common}
			db[task["labels"]].update_one({"_id": row["id"]}, {"$set": doc}, upsert=True)
		suggested = cross and cross["csv_decisions"].get(row.get(f"{cross['slug']}_decision", "").strip().lower())
		if suggested is not None:
			write_cross_label(task, row["id"], {"cross_decision": str(suggested)})
	return render_review_card(request, task, file, form.get("order", "asc"))


# Declared last: this pattern would otherwise swallow /review and /card.
@app.get("/{task}", response_class=HTMLResponse)
def sampling_page(request: Request, task: str):
	task = get_task(task)
	return templates.TemplateResponse(request, "index.html", {
		"task": task,
		"tasks": TASKS,
		"article": decorate_cross(task, task["sample"]()),
		"stats": stats(task),
		"labeler": LABELER,
	})
