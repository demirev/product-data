import os
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db


LABELER = os.environ.get("LABELER_NAME", "anonymous")

DOMAINS = [
	"ai_ml", "software", "hardware_iot", "biotech_pharma", "healthcare",
	"fintech", "energy_cleantech", "automotive_mobility", "industrial",
	"consumer_goods", "media_entertainment", "other",
]
AUDIENCES = ["b2b", "b2c", "b2g", "mixed"]

app = FastAPI()
templates = Jinja2Templates(
	directory=os.path.join(os.path.dirname(__file__), "templates")
)


def sample_article():
	db = get_db()
	pipeline = [
		{"$match": {"status": "scraped"}},
		{"$lookup": {"from": "labels", "localField": "_id", "foreignField": "_id", "as": "_l"}},
		{"$match": {"_l": {"$eq": []}}},
		{"$lookup": {"from": "label_skips", "localField": "_id", "foreignField": "_id", "as": "_s"}},
		{"$match": {"_s": {"$eq": []}}},
		{"$sample": {"size": 1}},
		{"$project": {"_id": 1, "header": 1, "body": 1, "url": 1, "published_date": 1}},
	]
	result = list(db["articles"].aggregate(pipeline))
	return result[0] if result else None


def stats():
	db = get_db()
	return {
		"labeled": db["labels"].count_documents({}),
		"relevant": db["labels"].count_documents({"is_relevant": 1}),
		"irrelevant": db["labels"].count_documents({"is_relevant": 0}),
		"skipped": db["label_skips"].count_documents({}),
	}


def render_card(request):
	return templates.TemplateResponse("card.html", {
		"request": request,
		"article": sample_article(),
		"stats": stats(),
		"domains": DOMAINS,
		"audiences": AUDIENCES,
	})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
	return templates.TemplateResponse("index.html", {
		"request": request,
		"article": sample_article(),
		"stats": stats(),
		"domains": DOMAINS,
		"audiences": AUDIENCES,
		"labeler": LABELER,
	})


@app.get("/card", response_class=HTMLResponse)
def card(request: Request):
	return render_card(request)


@app.post("/label", response_class=HTMLResponse)
def label(
	request: Request,
	article_id: str = Form(...),
	is_relevant: int = Form(...),
	audience: str = Form(""),
	domain: str = Form(""),
	notes: str = Form(""),
):
	doc = {
		"is_relevant": int(is_relevant),
		"labeler": LABELER,
		"labeled_at": datetime.now(timezone.utc),
	}
	if is_relevant == 1:
		metadata = {}
		if audience:
			metadata["audience"] = audience
		if domain:
			metadata["domain"] = domain
		if notes.strip():
			metadata["notes"] = notes.strip()
		if metadata:
			doc["metadata"] = metadata
	get_db()["labels"].update_one({"_id": article_id}, {"$set": doc}, upsert=True)
	return render_card(request)


@app.post("/skip", response_class=HTMLResponse)
def skip(request: Request, article_id: str = Form(...)):
	get_db()["label_skips"].update_one(
		{"_id": article_id},
		{"$set": {"labeler": LABELER, "skipped_at": datetime.now(timezone.utc)}},
		upsert=True,
	)
	return render_card(request)
