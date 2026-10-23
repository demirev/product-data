import os
import sys
import re
import json
import time
import tempfile
import argparse
import torch
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
	classification_report,
	accuracy_score,
	roc_auc_score,
	confusion_matrix,
)
from transformers import (
	DistilBertTokenizer,
	DistilBertForSequenceClassification,
	Trainer,
	TrainingArguments,
	EarlyStoppingCallback,
	DataCollatorWithPadding
)
from datasets import Dataset
from pymongo import UpdateOne

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db, ensure_indexes


# Group related constants together
MODEL_CONFIG = {
	"model_name": "distilbert-base-uncased",  
	"tokenizer": None,  # Will be initialized later
	"model": None,      # Will be initialized later
	"classes": None,    # the task's label space, set by initialize_models
	"max_length": 512
}

MODEL_ROOT = os.environ.get("MODEL_DIR", "models")
RUNS_COLLECTION = "model_runs"
# A run directory counts as servable only once both of these exist. Training
# writes the model first and the tokenizer second, so the tokenizer's presence
# means the checkpoint is complete — a classifier resolving "newest" against a
# volume being written to can never pick up a half-saved run.
RUN_FILES = ("config.json", "tokenizer_config.json")
IDLE_SLEEP_SECONDS = 24 * 60 * 60

# The two independent questions asked about a press release, each with its own
# label collection, checkpoint and prediction version. See py/label/app.py.
#
# A training run is identified by "<version_prefix>-<UTC timestamp>", and that
# one string is the directory it lives in under <MODEL_DIR>/<task>, the
# `model_version` its predictions carry, and its `_id` in `model_runs` — so any
# prediction traces back to the weights and the eval stats that produced it.
#
# `field` is the integer the labeler records: the index of the decision it was
# given, in that task's *full* label space. `classes` is the narrower space this
# head is trained on, and `collapse` maps one onto the other as the labels are
# loaded. Both tasks currently record four decisions and train on two, because
# separating four classes needs several times the hand labels that separating
# two does — and both keep the discarded distinction in Mongo rather than at the
# point of labelling, so widening a head back out is these two lines (name the
# classes, remap them) plus a retrain, never a relabelling pass.
#
# Index 1 is the positive class in every task: that invariant is what lets one
# code path report a single AUC and false-positive rate, and gate on a single
# `predicted_class: 1`, whatever the head's width.
TASKS = {
	"launch": {
		"labels": "labels",
		"field": "launch_class",
		# 2 product_info (an already-launched product described in detail) and
		# 3 feature_release (an incremental improvement to one) are both
		# negatives here: neither announces something new to buy. They are the
		# near misses the model gets wrong most often, so they are worth their
		# own classes the day there are enough of them.
		"classes": ["irrelevant", "relevant"],
		"collapse": {0: 0, 1: 1, 2: 0, 3: 0},
		"version_prefix": "distilbert-finetuned",
		"legacy_version": "distilbert-finetuned-v1",
	},
	"ai": {
		"labels": "ai_labels",
		"field": "ai_class",
		# The other direction: 2 ai_feature and 3 ai_tooling fold into the
		# *positive* class, so the head learns "is any generative AI being
		# shipped" and only 0 not_ai is a negative to it.
		"classes": ["not_ai", "ai"],
		"collapse": {0: 0, 1: 1, 2: 1, 3: 1},
		"version_prefix": "distilbert-ai",
		"legacy_version": "distilbert-ai-v1",
	},
}


def resolve_version(task):
	"""Newest trained run for `task` as `(version, run_dir)`, or `(None, None)`.

	A run directory's name *is* its model version, so "newest" is the last one in
	lexicographic order — the %Y%m%d%H%M%S stamps sort chronologically.
	"""
	task_dir = os.path.join(MODEL_ROOT, task)
	if not os.path.isdir(task_dir):
		return None, None

	runs = sorted(
		d for d in os.listdir(task_dir)
		if all(os.path.isfile(os.path.join(task_dir, d, f)) for f in RUN_FILES)
	)
	if runs:
		return runs[-1], os.path.join(task_dir, runs[-1])

	# Pre-versioning layout: a single flat checkpoint in the task dir, whose
	# predictions are keyed by the old fixed version string. Still servable, so
	# versioning doesn't strand an already-trained model.
	if all(os.path.isfile(os.path.join(task_dir, f)) for f in RUN_FILES):
		return TASKS[task]["legacy_version"], task_dir
	return None, None


def search_keyphrases(content, keyphrases):
	"""Search for keyphrases in the content and return a dictionary with the results."""
	# append word separator to each keyphrase
	keyphrases = [" " + kp + " " for kp in keyphrases]
	# replace all word separators with a single space
	content = re.sub(r'\s+', ' ', content)
	# remove punctuation and convert to lowercase
	normalized_content = " " + re.sub(r'[^\w\s]', '', content.lower()) + " " # add spaces at beginning and end
	normalized_keyphrases = [re.sub(r'[^\w\s]', '', kp.lower()) for kp in keyphrases]
	found_keyphrases = {kp: kp in normalized_content for kp in normalized_keyphrases}
	return found_keyphrases


def count_keyphrases(content, keyphrases):
	"""Count the occurrences of keyphrases in the content."""
	found_keyphrases = search_keyphrases(content, keyphrases)
	return sum(found_keyphrases.values())


def construct_press_release_list(keyphrases=None):
	"""Fetch scraped press releases from Mongo, optionally filtering by keyphrase presence."""
	cursor = get_db()["articles"].find(
		{"status": "scraped"},
		{"_id": 1, "header": 1, "body": 1, "published_date": 1},
	)
	results = []
	for data in cursor:
		header = data.get("header", "") or ""
		body = data.get("body", "") or ""
		if keyphrases is not None and count_keyphrases(header + " " + body, keyphrases) == 0:
			continue
		results.append({
			"_id": data["_id"],
			"header": header,
			"body": body,
			"date": data.get("published_date"),
		})
	return results


# Initialize models
def initialize_models(classes, model_name=None):
	"""Initialize the tokenizer and a head sized for the task's `classes`."""
	if model_name:
		MODEL_CONFIG["model_name"] = model_name
	
	# Initialize tokenizer
	MODEL_CONFIG["tokenizer"] = DistilBertTokenizer.from_pretrained(MODEL_CONFIG["model_name"])
	MODEL_CONFIG["classes"] = list(classes)
	
	# Initialize model for sequence classification. The width of this head is the
	# only place the tasks differ in shape — both are two-way today, and either
	# widens the moment its `classes` do.
	MODEL_CONFIG["model"] = DistilBertForSequenceClassification.from_pretrained(
		MODEL_CONFIG["model_name"], 
		num_labels=len(MODEL_CONFIG["classes"])
	)
	
	return MODEL_CONFIG["tokenizer"], MODEL_CONFIG["model"]


def clean_text(text):
	"""Remove artifacts like 3+ consecutive 't's or 'n's from text."""
	# First remove 3+ consecutive 't's
	text = re.sub(r't{3,}', ' ', text)
	# Then remove 3+ consecutive 'n's
	text = re.sub(r'n{3,}', ' ', text)
	
	# Remove promotional text and anything that follows it
	promo_text = "WANT YOUR COMPANY'S NEWS FEATURED ON PRNEWSWIRE"
	if promo_text in text:
		text = text.split(promo_text)[0].strip()
	
	return text


def load_labeled_data(task="launch"):
	"""Pull labels for `task` joined with article content from Mongo."""
	db = get_db()
	collection, field = TASKS[task]["labels"], TASKS[task]["field"]
	collapse = TASKS[task]["collapse"]
	labels = list(db[collection].find({}, {"_id": 1, field: 1}))
	ids = [l["_id"] for l in labels]
	articles = {
		a["_id"]: a
		for a in db["articles"].find(
			{"_id": {"$in": ids}, "status": "scraped"},
			{"_id": 1, "header": 1, "body": 1},
		)
	}

	rows, unlabeled = [], 0
	for label in labels:
		article = articles.get(label["_id"])
		if not article:
			continue
		# Either a label written before the task's label space changed, and so
		# carrying no value in the current field, or one whose value `collapse`
		# has no class for. Skipping beats crashing the run, but it is silent
		# data loss, so it gets counted and reported.
		value = collapse.get(label.get(field))
		if value is None:
			unlabeled += 1
			continue
		text = clean_text((article.get("header") or "") + " " + (article.get("body") or ""))
		if not text.strip():
			continue
		rows.append({
			"_id": label["_id"],
			"text": text,
			"label": value,
		})

	df = pd.DataFrame(rows)
	print(f"Loaded {len(df)} labeled press releases for task={task}")
	if unlabeled:
		print(f"Skipped {unlabeled} label(s) with no usable '{field}' — relabel or migrate them.")
	return df


def tokenize_data(texts, labels=None):
	"""Tokenize the texts for the model.

	No padding here: the examples stay ragged and the Trainer's collator pads
	each batch to its own longest member, so a short release costs its own
	length rather than a full `max_length` of attention. On the current corpus
	that buys almost nothing — 88% of labelled releases already reach the 512
	cap — but it stops the padding from being wasted work if shorter documents
	arrive later.
	"""
	tokenizer = MODEL_CONFIG["tokenizer"]
	encodings = tokenizer(
		texts, 
		truncation=True,
		max_length=MODEL_CONFIG["max_length"],
	)
	
	# Create a dataset that the trainer can use
	data = {}
	data['input_ids'] = encodings['input_ids']
	data['attention_mask'] = encodings['attention_mask']
	
	if labels is not None:
		data['labels'] = labels
	
	return data


def create_dataset(df, is_train=True):
	"""Create a HuggingFace dataset from a pandas dataframe."""
	texts = df['text'].tolist()
	
	if is_train:
		labels = df['label'].astype(int).tolist()
		data = tokenize_data(texts, labels)
	else:
		data = tokenize_data(texts)
	
	return Dataset.from_dict(data)


def train_run(df, task, version, output_dir, batch_size=8, epochs=10):
	"""Fine-tune one model on `df` and write everything the run produced.

	`output_dir` is left holding exactly the final artefact — weights, tokenizer
	— plus this run's stats (`metrics.json`, `eval_predictions.csv`). The
	Trainer's per-epoch checkpoints carry optimiser state and run several hundred
	MB each; they exist only so `load_best_model_at_end` can restore the best
	epoch, so they go to a scratch dir that is deleted when training returns.

	The same stats are recorded in the `model_runs` collection under
	`_id = version`, the string predictions carry as `model_version`.
	"""
	# Stratification refuses a class with a single member, which a thinly labelled
	# class hits easily — as the collapsed-away classes in both tasks would, were
	# they trained on. Falling back to an unstratified split costs balance;
	# failing the run costs the run.
	stratify = df["label"] if df["label"].value_counts().min() >= 2 else None
	if stratify is None:
		print("A class has fewer than 2 examples; splitting without stratification.")
	train_df, eval_df = train_test_split(
		df, test_size=0.2, stratify=stratify, random_state=42
	)
	print(f"Training on {len(train_df)} examples, validating on {len(eval_df)} examples")
	print(f"Class distribution in training set: {train_df['label'].value_counts().to_dict()}")

	train_dataset = create_dataset(train_df)
	eval_dataset = create_dataset(eval_df)
	os.makedirs(output_dir, exist_ok=True)

	# Scratch sits beside the run dir, so it lands on the same (roomy) volume.
	with tempfile.TemporaryDirectory(
		dir=os.path.dirname(output_dir), prefix=".trainer-"
	) as scratch:
		training_args = TrainingArguments(
			output_dir=scratch,
			num_train_epochs=epochs,
			per_device_train_batch_size=batch_size,
			per_device_eval_batch_size=batch_size,
			# A float here is a *ratio* of total steps, not a step count: at ~21
			# steps an epoch a fixed 500 would outlast the whole run, leaving the
			# LR still ramping when it ends.
			warmup_steps=0.1,
			weight_decay=0.01,
			logging_steps=10,
			eval_strategy="epoch",
			save_strategy="epoch",
			save_total_limit=1,
			load_best_model_at_end=True,
			# Accuracy cannot separate these runs — every model that collapses onto
			# the majority class scores exactly the negative rate — so it selects on
			# ranking quality instead.
			metric_for_best_model="eval_roc_auc",
			greater_is_better=True,
		)

		trainer = Trainer(
			model=MODEL_CONFIG["model"],
			args=training_args,
			train_dataset=train_dataset,
			eval_dataset=eval_dataset,
			data_collator=DataCollatorWithPadding(MODEL_CONFIG["tokenizer"]),
			compute_metrics=compute_metrics,
			callbacks=[EarlyStoppingCallback(early_stopping_patience=3)]
		)
		trainer.train()

		# After load_best_model_at_end this is the best epoch's weights, not the
		# last one's; save through the trainer so it saves the model it holds.
		trainer.save_model(output_dir)
		MODEL_CONFIG["tokenizer"].save_pretrained(output_dir)

		# Both eval passes happen while the scratch checkpoints still exist.
		metrics = trainer.evaluate()
		pred_output = trainer.predict(eval_dataset)

	logits = pred_output.predictions
	probabilities = torch.nn.functional.softmax(torch.tensor(logits), dim=1).numpy()
	classes = MODEL_CONFIG["classes"]
	eval_results_df = pd.DataFrame({
		"_id": eval_df["_id"].tolist(),
		"text": eval_df["text"].tolist(),
		"true_label": [classes[i] for i in pred_output.label_ids],
		"predicted_label": [classes[i] for i in np.argmax(logits, axis=1)],
		# One column per class rather than a single positive-class probability:
		# on a head wider than two the informative part of an error is which
		# other class the model reached for, and how close it was.
		**{f"p_{name}": probabilities[:, i] for i, name in enumerate(classes)},
	})
	eval_results_df["correct"] = (
		eval_results_df["true_label"] == eval_results_df["predicted_label"]
	)
	eval_results_path = os.path.join(output_dir, "eval_predictions.csv")
	eval_results_df.to_csv(eval_results_path, index=False)
	print(f"Saved per-example evaluation results to {eval_results_path}")

	now = datetime.now(timezone.utc)
	# The json round-trip coerces the numpy scalars sklearn and the Trainer return
	# into plain floats, which both the stats file and pymongo need.
	record = json.loads(json.dumps({
		"_id": version,
		"task": task,
		"trained_at": now.isoformat(),
		"base_model": MODEL_CONFIG["model_name"],
		"checkpoint_dir": output_dir,
		"epochs": epochs,
		"batch_size": batch_size,
		"max_length": MODEL_CONFIG["max_length"],
		"n_train": len(train_df),
		"n_eval": len(eval_df),
		# The order here is what `class_probabilities` on every prediction this
		# run writes is indexed by, so it belongs with the run, not in code.
		"classes": classes,
		"label_counts": {classes[int(k)]: int(v) for k, v in df["label"].value_counts().items()},
		"metrics": metrics,
	}, default=float))

	metrics_path = os.path.join(output_dir, "metrics.json")
	with open(metrics_path, "w") as f:
		json.dump(record, f, indent=2)
	print(f"Saved run stats to {metrics_path}")

	# Mongo gets a real date; the file keeps the ISO string.
	record["trained_at"] = now
	get_db()[RUNS_COLLECTION].replace_one({"_id": version}, record, upsert=True)
	print(f"Recorded run {version} in {RUNS_COLLECTION}")

	return record


def compute_metrics(eval_pred):
	"""Compute metrics for evaluation during training.

	The headline numbers are one-vs-rest against class 1 — the positive class in
	every task — so a two-way head and a wider one report the same names, and
	`metric_for_best_model` keeps selecting on the same quantity across both. Any
	detail beyond that lives in the confusion matrix and the per-class report.
	"""
	logits, labels = eval_pred
	classes = MODEL_CONFIG["classes"]
	predictions = np.argmax(logits, axis=1)
	
	# For AUC-ROC we need probabilities of the positive class
	probs = torch.nn.functional.softmax(torch.tensor(logits), dim=1).numpy()
	positive, called_positive = np.asarray(labels) == 1, predictions == 1
	roc_auc = roc_auc_score(positive, probs[:, 1])
	
	tp = int((positive & called_positive).sum())
	fp = int((~positive & called_positive).sum())
	fn = int((positive & ~called_positive).sum())
	tn = int((~positive & ~called_positive).sum())
	
	return {
		'accuracy': accuracy_score(labels, predictions),
		'roc_auc': roc_auc,
		# Each rate is taken over its own true class, so neither moves when the
		# split's balance does: missed launches over the launches, false alarms
		# over the non-launches. On a head wider than two, "negative" is every
		# other class, so an AI product read as tooling would count here exactly
		# as an AI product read as not_ai does.
		'false_positive_rate': fp / (fp + tn),
		'false_negative_rate': fn / (fn + tp),
		# Spelling out the labels keeps this square even on an epoch where the
		# model predicts a single class, which it does while the fresh head
		# settles — and on more than two classes it is the only place the
		# off-diagonal confusions (tooling read as product, say) show up at all.
		'confusion_matrix': confusion_matrix(
			labels, predictions, labels=range(len(classes))
		).tolist(),
		'report': classification_report(
			labels, predictions,
			labels=range(len(classes)), target_names=classes,
			output_dict=True, zero_division=0,
		),
	}


def predict(texts, model=None, tokenizer=None):
	"""
	Predict relevance of press releases using the fine-tuned model.
	
	Args:
		texts: List of texts to classify
		model: Optional model to use (uses MODEL_CONFIG["model"] if None)
		tokenizer: Optional tokenizer (uses MODEL_CONFIG["tokenizer"] if None)
	
	Returns:
		Dictionary with predictions and probabilities
	"""
	if model is None:
		model = MODEL_CONFIG["model"]
	
	if tokenizer is None:
		tokenizer = MODEL_CONFIG["tokenizer"]
	
	# Tokenize texts
	inputs = tokenizer(
		texts,
		padding=True,
		truncation=True,
		max_length=MODEL_CONFIG["max_length"],
		return_tensors="pt"
	)
	
	# Move inputs to the same device as model
	device = model.device
	inputs = {k: v.to(device) for k, v in inputs.items()}
	
	# Get predictions
	with torch.no_grad():
		outputs = model(**inputs)
	
	# Get probabilities using softmax
	probabilities = torch.nn.functional.softmax(outputs.logits, dim=1)
	
	# Convert to numpy for easier manipulation
	probabilities = probabilities.cpu().numpy()
	predictions = np.argmax(probabilities, axis=1)
	
	return {
		'predictions': predictions,
		'probabilities': probabilities
	}


def load_model_from_checkpoint(checkpoint_dir):
	"""Load a fine-tuned model from checkpoint."""
	tokenizer = DistilBertTokenizer.from_pretrained(checkpoint_dir)
	model = DistilBertForSequenceClassification.from_pretrained(checkpoint_dir)
	
	MODEL_CONFIG["tokenizer"] = tokenizer
	MODEL_CONFIG["model"] = model
	
	return tokenizer, model


def classify_press_releases(
	model_version,
	model_dir,
	batch_size=16,
	overwrite=False,
	require_relevant_from=None,
):
	"""
	Classify scraped press releases and write predictions to Mongo.

	Resume semantics: articles that already have a prediction for `model_version`
	are skipped unless `overwrite=True`.

	`require_relevant_from` restricts the candidate set to articles a previous
	model_version already called positive — the staged pass that lets the AI
	classifier run over launch-positives only instead of the whole corpus.

	Both of those are `$lookup` joins rather than id lists built client-side: the
	corpus runs to millions of articles and their `_id`s are URL slugs, so a
	`$nin`/`$in` of everything already done outgrows Mongo's 16MB query limit
	well before a full pass completes.
	"""
	tokenizer, model = load_model_from_checkpoint(model_dir)
	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
	model = model.to(device)

	db = get_db()
	articles_coll = db["articles"]
	predictions_coll = db["predictions"]

	# Both joins ride the unique (article_id, model_version) index on predictions,
	# and $limit: 1 keeps the joined array to one document per article.
	pipeline = [{"$match": {"status": "scraped"}}]

	if require_relevant_from:
		print(f"Gating on articles called positive by {require_relevant_from}.")
		pipeline += [
			{"$lookup": {
				"from": "predictions",
				"localField": "_id",
				"foreignField": "article_id",
				"pipeline": [
					{"$match": {
						"model_version": require_relevant_from,
						"predicted_class": 1,
					}},
					{"$limit": 1},
				],
				"as": "gate",
			}},
			{"$match": {"gate": {"$ne": []}}},
		]

	if not overwrite:
		pipeline += [
			{"$lookup": {
				"from": "predictions",
				"localField": "_id",
				"foreignField": "article_id",
				"pipeline": [
					{"$match": {"model_version": model_version}},
					{"$limit": 1},
				],
				"as": "done",
			}},
			{"$match": {"done": {"$eq": []}}},
		]

	pipeline.append({"$project": {"_id": 1, "header": 1, "body": 1}})

	# Safe against its own writes: an article is only marked done after it has
	# already streamed out of the cursor, so the join can never skip one.
	cursor = articles_coll.aggregate(pipeline)
	batch = []
	total = 0

	def flush(batch):
		texts = [clean_text((d.get("header") or "") + " " + (d.get("body") or "")) for d in batch]
		results = predict(texts, model, tokenizer)
		preds = results["predictions"]
		probs = results["probabilities"]
		ops = [
			UpdateOne(
				{"article_id": d["_id"], "model_version": model_version},
				{
					"$set": {
						"predicted_class": int(preds[j]),
						# The whole distribution, index-aligned with the run's
						# `classes` in model_runs, so a threshold can be re-tuned
						# without a second pass over the corpus.
						"class_probabilities": [float(pr) for pr in probs[j]],
						"predicted_at": datetime.now(timezone.utc),
					}
				},
				upsert=True,
			)
			for j, d in enumerate(batch)
		]
		predictions_coll.bulk_write(ops, ordered=False)

	for doc in cursor:
		batch.append(doc)
		if len(batch) >= batch_size:
			flush(batch)
			total += len(batch)
			print(f"Classified {total} articles")
			batch = []

	if batch:
		flush(batch)
		total += len(batch)

	print(f"Done. Wrote predictions for {total} articles under model_version={model_version}")


if __name__ == "__main__":
	print("Starting")
	print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
	
	parser = argparse.ArgumentParser(
		description="Fine-tune a press-release classifier (product launch, or AI product)"
	)
	parser.add_argument(
		"--task",
		type=str,
		choices=sorted(TASKS),
		help="Which question to train/serve; picks the label collection and field",
		default="launch"
	)
	parser.add_argument(
		"--classify",
		action="store_true",
		help="Run classification using a fine-tuned model",
		default=False
	)
	parser.add_argument(
		"--model-version",
		type=str,
		help="Pin a run to serve instead of the newest one under <MODEL_DIR>/<task> "
			"(classify mode); also settable as MODEL_VERSION",
		default=None
	)
	parser.add_argument(
		"--require-relevant-from",
		type=str,
		help="Only classify articles already called positive by another model: a "
			"task name ('launch') tracks that task's newest run, an explicit "
			"version pins one. Used to run the AI classifier over launch-positives",
		default=None
	)
	parser.add_argument(
		"--loop",
		action="store_true",
		help="Run classification forever, sleeping when no work is pending (classify mode)",
		default=False
	)
	parser.add_argument(
		"--idle-sleep",
		type=int,
		default=IDLE_SLEEP_SECONDS,
		help="Seconds to sleep between passes / while waiting for a checkpoint in --loop mode"
	)
	parser.add_argument(
		"--batch-size",
		type=int,
		help="Batch size for training and prediction",
		default=8
	)
	parser.add_argument(
		"--epochs",
		type=int,
		help="Number of training epochs",
		default=10
	)
	parser.add_argument(
		"--model-name",
		type=str,
		help="Pretrained model to fine-tune",
		default="distilbert-base-uncased"
	)
	parser.add_argument(
		"--overwrite",
		action="store_true",
		help="Reclassify articles even if predictions exist for this model_version",
		default=False
	)

	args = parser.parse_args()
	print(f"Task: {args.task}; model root: {os.path.join(MODEL_ROOT, args.task)}")
	ensure_indexes()

	if args.classify:
		print("Classification mode")
		# Pinning is opt-in. Unpinned, the newest run is re-resolved every pass, so
		# a retrain is picked up without restarting the service.
		pinned = args.model_version or os.environ.get("MODEL_VERSION")

		while True:
			if pinned:
				version, run_dir = pinned, os.path.join(MODEL_ROOT, args.task, pinned)
				if not os.path.isdir(run_dir):
					run_dir = os.path.join(MODEL_ROOT, args.task)  # pre-versioning layout
			else:
				version, run_dir = resolve_version(args.task)

			# A task name here means "whatever that task's newest run is"; without a
			# resolved gate the pass must not run at all, or it would classify the
			# whole corpus instead of the gated slice.
			gate = args.require_relevant_from
			if gate in TASKS:
				gate, _ = resolve_version(gate)

			if version is None or not all(
				os.path.isfile(os.path.join(run_dir, f)) for f in RUN_FILES
			):
				blocked = f"No trained run for task={args.task} under {os.path.join(MODEL_ROOT, args.task)}."
			elif args.require_relevant_from and gate is None:
				blocked = f"No trained run yet for gate '{args.require_relevant_from}'."
			else:
				blocked = None

			if blocked:
				if not args.loop:
					print(blocked + " Exiting.")
					break
				print(blocked + f" Sleeping {args.idle_sleep}s.")
				time.sleep(args.idle_sleep)
				continue

			print(f"Serving {version} from {run_dir}")
			classify_press_releases(
				model_version=version,
				model_dir=run_dir,
				batch_size=args.batch_size,
				overwrite=args.overwrite,
				require_relevant_from=gate,
			)

			if not args.loop:
				break
			print(f"Idle; sleeping {args.idle_sleep}s")
			time.sleep(args.idle_sleep)
	else:
		print("Training mode")
		initialize_models(TASKS[args.task]["classes"], model_name=args.model_name)

		labeled_data = load_labeled_data(args.task)
		if labeled_data.empty:
			print(f"No labels found for task={args.task} in the "
				f"'{TASKS[args.task]['labels']}' collection. Label some articles first "
				"(docker compose --profile labeling up -d labeler).")
			sys.exit(0)

		# The run is named for the moment it started; that name is what inference
		# resolves as "newest" and what its predictions are keyed by.
		version = "{}-{}".format(
			TASKS[args.task]["version_prefix"],
			datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
		)
		run_dir = os.path.join(MODEL_ROOT, args.task, version)
		print(f"Run {version} -> {run_dir}")

		record = train_run(
			labeled_data,
			task=args.task,
			version=version,
			output_dir=run_dir,
			batch_size=args.batch_size,
			epochs=args.epochs,
		)
		print("Evaluation results:")
		print(json.dumps(record["metrics"], indent=2))

	print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
	print("Done")
