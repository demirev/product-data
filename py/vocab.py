"""Precomputed vocabulary flags on articles, so the labeler can sample quickly.

Matching a keyword regex at query time is what the launch labeler does today, and
it is already slow (~3.5s per card over 378k articles). For the AI task it is not
viable at all: generative-AI vocabulary sits in the *body* far more often than the
header (~4.3% of articles vs 0.7%), and a $sample behind a body regex takes ~147s.

So every scraped article carries two precomputed, indexed fields:

  ai_vocab      "genai" | "generic" | "none"   (header + body)
  launch_vocab  bool                           (header only, matching the
                                                launch labeler's original regex)

`scrape_article` stamps them on new articles; this module's CLI backfills
existing ones:

    python py/vocab.py

The backfill only touches articles missing `ai_vocab`, so it is resumable and
safe to re-run — worth doing once more after a long run, since a cursor over a
collection being updated can drop documents.
"""
import os
import re
import sys

from pymongo import UpdateOne

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_db, ensure_indexes


# Generative AI proper: LLMs, diffusion/image/video/voice generation, agents.
# Named vendors and models are included only where the name is unambiguous —
# "claude", "gemini" and "llama" are left out on purpose, as they collide with
# common personal names, brands and animals.
GENAI_KEYWORDS = [
	r"generative a\.?i\b", r"generative artificial intelligence", r"\bgen[- ]?ai\b",
	r"large language model", r"\bllms?\b", r"foundation model",
	r"\bgpt-?[0-9]", r"chatgpt", r"\bopenai\b", r"\banthropic\b", r"\bcopilot\b",
	r"diffusion model", r"stable diffusion", r"dall-?e", r"midjourney",
	r"text-to-(image|video|speech|3d)", r"image generation",
	r"\bai agents?\b", r"agentic", r"prompt engineering", r"retrieval[- ]augmented",
	r"transformer (model|architecture|based)", r"synthetic media",
	r"voice (cloning|synthesis)",
]

# AI/ML vocabulary that is *not* evidence of generative AI. This tier is where the
# hard negatives live: classic-ML products (predictors, classifiers, forecasting)
# and releases that say "AI-powered" without naming a mechanism. Sampling it
# deliberately is what stops the AI classifier from learning "the word AI".
GENERIC_AI_KEYWORDS = [
	r"artificial intelligence", r"\bai[- ](powered|driven|based|enabled)\b",
	r"machine learning", r"deep learning", r"neural network",
	r"predictive analytics", r"computer vision", r"natural language processing",
	r"\bnlp\b", r"chatbot", r"conversational ai",
]

# Launch-ish vocabulary matched against the header to oversample likely
# positives. Deliberately omits "announce" (nearly every PRN release announces
# something) in favour of higher-precision terms. Stems catch inflections
# (launch -> launches/launched, introduc -> introduces/introducing).
LAUNCH_KEYWORDS = [
	r"launch", r"unveil", r"debut", r"introduc",
	r"roll(s|ing)? out", r"now available", r"general availability",
]

GENAI_REGEX = re.compile("|".join(GENAI_KEYWORDS), re.IGNORECASE)
GENERIC_AI_REGEX = re.compile("|".join(GENERIC_AI_KEYWORDS), re.IGNORECASE)
LAUNCH_REGEX = re.compile("|".join(LAUNCH_KEYWORDS), re.IGNORECASE)


def ai_vocab_tier(text):
	"""Coarsest AI vocabulary present in `text`, generative taking precedence."""
	if GENAI_REGEX.search(text):
		return "genai"
	if GENERIC_AI_REGEX.search(text):
		return "generic"
	return "none"


def vocab_flags(header, body):
	"""The pair of flags stored on every scraped article."""
	return {
		"ai_vocab": ai_vocab_tier((header or "") + " " + (body or "")),
		"launch_vocab": bool(LAUNCH_REGEX.search(header or "")),
	}


def backfill(batch_size=1000):
	articles = get_db()["articles"]
	query = {"status": "scraped", "ai_vocab": {"$exists": False}}
	remaining = articles.count_documents(query)
	print(f"Backfilling vocabulary flags for {remaining} articles")

	ops = []
	done = 0
	for doc in articles.find(query, {"header": 1, "body": 1}, batch_size=200):
		ops.append(UpdateOne(
			{"_id": doc["_id"]},
			{"$set": vocab_flags(doc.get("header"), doc.get("body"))},
		))
		if len(ops) >= batch_size:
			articles.bulk_write(ops, ordered=False)
			done += len(ops)
			ops = []
			print(f"Flagged {done}/{remaining}")

	if ops:
		articles.bulk_write(ops, ordered=False)
		done += len(ops)

	print(f"Flagged {done} articles; {articles.count_documents(query)} still missing "
		"(re-run to pick those up)")
	for tier in ("genai", "generic", "none"):
		n = articles.count_documents({"status": "scraped", "ai_vocab": tier})
		print(f"  ai_vocab={tier}: {n}")


if __name__ == "__main__":
	ensure_indexes()
	backfill()
