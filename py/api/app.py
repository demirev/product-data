"""The Product Release Explorer's JSON API, and the container that serves the
SPA next to it.

Read-only by default: the only collections this process writes are the `fe_*`
read model it owns (see `readmodel.py`). With `EXPLORER_LABELER` set it also
takes launch labels from the release cards and writes them to `labels`, and
edits `sender_blacklist`, the senders the read model leaves out. Nothing
here loads a model — the predictions were written by `py/classify/finetune.py`
long before a request arrives — which is why the image can leave torch and
transformers out.
"""
import asyncio
import csv
import io
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pymongo import DESCENDING

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db

from . import queries, readmodel
from .queries import BadQuery


# Built by `npm run build`; absent in development, where Vite serves the SPA on
# 5173 and proxies /api here.
DIST = os.path.join(
	os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fe", "dist"
)
REFRESH_SECONDS = 600

# Who is labelling from the release cards. Unset, the cards carry no labelling
# control and `POST /api/labels` is a 404.
LABELER = os.environ.get("EXPLORER_LABELER")
# The launch labeler's three negative decisions; see TASKS["launch"] in
# py/label/app.py.
FALSE_POSITIVE_CLASSES = (0, 2, 3)


@asynccontextmanager
async def lifespan(app):
	"""Refresh on startup and every ten minutes after, in a thread so a rebuild
	never blocks a request. Startup deliberately does not await the first one:
	the service has to come up and serve pages while the read model is still
	empty, or a first deploy would look broken for as long as the build takes."""
	async def loop():
		while True:
			try:
				print("readmodel:", await asyncio.to_thread(readmodel.refresh), flush=True)
			except Exception as error:  # a failed refresh must not kill the loop
				print(f"readmodel: refresh failed: {error!r}", flush=True)
			await asyncio.sleep(REFRESH_SECONDS)

	task = asyncio.create_task(loop())
	yield
	task.cancel()


app = FastAPI(title="Product Release Explorer", lifespan=lifespan)


@app.exception_handler(BadQuery)
async def bad_query(request, error):
	return JSONResponse({"detail": str(error)}, status_code=400)


def served_models(meta):
	"""The two runs behind everything on screen, flattened for the client.

	`classes` is read off the run rather than hardcoded: the heads are binary
	today, but index 1 is the positive class either way, and a widened head
	would still name its classes here.
	"""
	models = {}
	for task in readmodel.TASKS:
		run = meta.get(task)
		if not run:
			models[task] = None
			continue
		metrics = run.get("metrics") or {}
		models[task] = {
			"version": run["_id"],
			"trained_at": queries.serializable(run.get("trained_at")),
			"classes": run.get("classes"),
			"positive_class": (run.get("classes") or [None, None])[1],
			"n_train": run.get("n_train"),
			"n_eval": run.get("n_eval"),
			"metrics": {k: metrics.get(k) for k in (
				"eval_accuracy", "eval_roc_auc",
				"eval_false_positive_rate", "eval_false_negative_rate",
			)},
		}
	return models


@app.get("/api/overview")
def get_overview(request: Request):
	db = get_db()
	meta = queries.meta(db)
	return {
		**queries.overview(db, meta),
		"models": served_models(meta),
		"built_at": queries.serializable(meta.get("built_at")),
	}


@app.get("/api/filters")
def get_filters(request: Request):
	meta = queries.meta(get_db())
	facets = meta.get("facets") or {}
	return {
		**queries.serializable(facets),
		"models": served_models(meta),
		"labeling": bool(LABELER),
		"n_releases": meta.get("n_releases", 0),
		"n_companies": meta.get("n_companies", 0),
		"built_at": queries.serializable(meta.get("built_at")),
	}


@app.get("/api/releases")
def get_releases(request: Request):
	db = get_db()
	page, per_page = queries.page_of(request.query_params)
	result = queries.page_result(
		db["fe_releases"],
		queries.release_filter(request.query_params),
		queries.sort_of(request.query_params, queries.RELEASE_SORTS),
		page, per_page, projection={"dedupe_key": 0},
	)
	if LABELER:
		# Joined per page: a label has to show the moment it is written, and the
		# read model is up to ten minutes behind.
		found = db["labels"].find(
			{"_id": {"$in": [release["_id"] for release in result["items"]]}},
			{"launch_class": 1, "source": 1},
		)
		labels = {label.pop("_id"): label for label in found}
		for release in result["items"]:
			release["label"] = labels.get(release["_id"])
	return result


@app.get("/api/companies")
def get_companies(request: Request):
	page, per_page = queries.page_of(request.query_params)
	return queries.page_result(
		get_db()["fe_companies"],
		queries.company_filter(request.query_params),
		queries.sort_of(request.query_params, queries.COMPANY_SORTS),
		page, per_page,
	)


def _csv_response(name, columns, rows):
	"""Stream the rows out as they come off the cursor, in chunks, so a 50k-row
	export neither buffers in this process nor waits on Mongo before the browser
	sees the download start."""
	def chunks():
		buffer = io.StringIO()
		writer = csv.writer(buffer)
		writer.writerow(columns)
		for row in rows:
			writer.writerow(row)
			if buffer.tell() > 64_000:
				yield buffer.getvalue()
				buffer.seek(0)
				buffer.truncate(0)
		yield buffer.getvalue()

	return StreamingResponse(
		chunks(),
		media_type="text/csv; charset=utf-8",
		headers={"Content-Disposition": f'attachment; filename="{name}"'},
	)


def _export_name(kind, meta):
	"""The date the slice was taken and the run that defined it — the two things
	a downloaded CSV needs to be citable a year later."""
	version = ((meta.get("launch") or {}).get("_id")) or "no-model"
	return f"{kind}-{datetime.now(timezone.utc):%Y-%m-%d}-{version}.csv"


@app.get("/api/releases.csv")
def get_releases_csv(request: Request):
	db = get_db()
	meta = queries.meta(db)
	versions = {
		task: (meta.get(task) or {}).get("_id") for task in readmodel.TASKS
	}
	cursor = db["fe_releases"].find(
		queries.release_filter(request.query_params)
	).sort(
		queries.sort_of(request.query_params, queries.RELEASE_SORTS)
	).limit(queries.CSV_MAX_ROWS)

	return _csv_response(
		_export_name("product-releases", meta),
		queries.RELEASE_CSV_COLUMNS,
		(queries.release_csv_row(doc, versions) for doc in cursor),
	)


@app.get("/api/companies.csv")
def get_companies_csv(request: Request):
	db = get_db()
	cursor = db["fe_companies"].find(
		queries.company_filter(request.query_params)
	).sort(
		queries.sort_of(request.query_params, queries.COMPANY_SORTS)
	).limit(queries.CSV_MAX_ROWS)

	return _csv_response(
		_export_name("companies", queries.meta(db)),
		queries.COMPANY_CSV_COLUMNS,
		(queries.company_csv_row(doc, readmodel.PRN_BASE) for doc in cursor),
	)


@app.post("/api/labels")
def post_label(article_id: str = Body(...), launch_class: int | None = Body(None)):
	"""Record a release as a false positive of the launch model, in the shape the
	labeler writes. `launch_class: null` clears a label recorded here."""
	db = get_db()
	if not LABELER or not db["fe_releases"].find_one({"_id": article_id}, {"_id": 1}):
		return JSONResponse({"detail": "Not Found"}, status_code=404)

	if launch_class is None:
		db["labels"].delete_one({"_id": article_id, "source": "explorer"})
		# A label from the labeler survives the delete and is still the answer.
		return {"label": db["labels"].find_one(
			{"_id": article_id}, {"_id": 0, "launch_class": 1, "source": 1}
		)}
	if launch_class not in FALSE_POSITIVE_CLASSES:
		raise BadQuery(f"launch_class must be one of {FALSE_POSITIVE_CLASSES}")

	db["labels"].replace_one({"_id": article_id}, {
		"launch_class": launch_class,
		"labeler": LABELER,
		"labeled_at": datetime.now(timezone.utc),
		"source": "explorer",
		# Drawn from one run's positives, so filterable out of base-rate estimates.
		"sample_method": "predicted_positive",
		"model_version": (queries.meta(db).get("launch") or {}).get("_id"),
	}, upsert=True)
	return {"label": {"launch_class": launch_class, "source": "explorer"}}


@app.get("/api/blacklist")
def get_blacklist():
	"""Senders whose releases the read model leaves out, newest first."""
	db = get_db()
	if not LABELER:
		return JSONResponse({"detail": "Not Found"}, status_code=404)
	items = list(db["sender_blacklist"].find().sort("added_at", DESCENDING))
	n_releases = {c["_id"]: c.get("n_releases") for c in db["companies"].find(
		{"_id": {"$in": [i["_id"] for i in items]}}, {"n_releases": 1})}
	for item in items:
		item["n_releases"] = n_releases.get(item["_id"])
	return {"items": queries.serializable(items)}


@app.post("/api/blacklist")
def post_blacklist(
	company_id: str = Body(...), blacklisted: bool = Body(...), reason: str | None = Body(None),
):
	"""Add a sender to the blacklist or take it off. Adding drops its releases
	from the read model at once; a removal is restored by the refresh started here."""
	db = get_db()
	if not LABELER:
		return JSONResponse({"detail": "Not Found"}, status_code=404)

	if blacklisted:
		name = ((db["companies"].find_one({"_id": company_id}, {"name": 1}) or {}).get("name")
			or (db["fe_releases"].find_one({"company_id": company_id}, {"company_name": 1}) or {}).get("company_name"))
		if not name:
			return JSONResponse({"detail": "Not Found"}, status_code=404)
		db["sender_blacklist"].replace_one({"_id": company_id}, {
			"name": name,
			"reason": (reason or "").strip() or None,
			"source": "explorer",
			"added_by": LABELER,
			"added_at": datetime.now(timezone.utc),
		}, upsert=True)
		db["fe_releases"].delete_many({"company_id": company_id})
		db["fe_companies"].delete_one({"_id": company_id})
	else:
		db["sender_blacklist"].delete_one({"_id": company_id})

	# Queued behind a running build, which may have read the blacklist before this edit.
	threading.Thread(target=readmodel.refresh, kwargs={"wait": True}, daemon=True).start()
	return {"company_id": company_id, "blacklisted": blacklisted}


@app.post("/api/refresh")
def post_refresh():
	return readmodel.refresh()


@app.get("/{path:path}")
def spa(path: str):
	"""The built SPA, with every unmatched path falling through to index.html so
	that /products and /companies survive a hard refresh."""
	if path.startswith("api/"):
		return JSONResponse({"detail": "Not Found"}, status_code=404)

	index = os.path.join(DIST, "index.html")
	if not os.path.isfile(index):
		return JSONResponse(
			{"detail": "SPA not built. Run `npm run build` in fe/, or use the Vite dev server."},
			status_code=404,
		)

	asset = os.path.normpath(os.path.join(DIST, path))
	if path and asset.startswith(DIST + os.sep) and os.path.isfile(asset):
		return FileResponse(asset)
	return FileResponse(index)
