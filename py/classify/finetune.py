import os
import sys
import re
import time
import argparse
import torch
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score, roc_auc_score
from transformers import (
	DistilBertTokenizer,
	DistilBertForSequenceClassification,
	Trainer,
	TrainingArguments,
	EarlyStoppingCallback
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
	"max_length": 512
}

OUTPUT_DIR = os.environ.get("MODEL_DIR", "models/press_release_classifier")
DEFAULT_MODEL_VERSION = os.environ.get("MODEL_VERSION", "distilbert-finetuned-v1")
IDLE_SLEEP_SECONDS = 24 * 60 * 60

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
def initialize_models(model_name=None, num_labels=2):
	"""Initialize the tokenizer and model."""
	if model_name:
		MODEL_CONFIG["model_name"] = model_name
	
	# Initialize tokenizer
	MODEL_CONFIG["tokenizer"] = DistilBertTokenizer.from_pretrained(MODEL_CONFIG["model_name"])
	
	# Initialize model for sequence classification (binary in this case)
	MODEL_CONFIG["model"] = DistilBertForSequenceClassification.from_pretrained(
		MODEL_CONFIG["model_name"], 
		num_labels=num_labels
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


def load_labeled_data():
	"""Pull labels joined with article content from Mongo."""
	db = get_db()
	labels = list(db["labels"].find({}, {"_id": 1, "is_relevant": 1}))
	ids = [l["_id"] for l in labels]
	articles = {
		a["_id"]: a
		for a in db["articles"].find(
			{"_id": {"$in": ids}, "status": "scraped"},
			{"_id": 1, "header": 1, "body": 1},
		)
	}

	rows = []
	for label in labels:
		article = articles.get(label["_id"])
		if not article:
			continue
		text = clean_text((article.get("header") or "") + " " + (article.get("body") or ""))
		if not text.strip():
			continue
		rows.append({
			"_id": label["_id"],
			"text": text,
			"is_relevant": label["is_relevant"],
		})

	df = pd.DataFrame(rows)
	print(f"Loaded {len(df)} labeled press releases")
	return df


def tokenize_data(texts, labels=None):
	"""Tokenize the texts for the model."""
	tokenizer = MODEL_CONFIG["tokenizer"]
	encodings = tokenizer(
		texts, 
		truncation=True,
		padding='max_length',
		max_length=MODEL_CONFIG["max_length"],
		return_tensors="pt"
	)
	
	# Create a dataset that the trainer can use
	data = {}
	data['input_ids'] = encodings['input_ids']
	data['attention_mask'] = encodings['attention_mask']
	
	if labels is not None:
		data['labels'] = torch.tensor(labels, dtype=torch.long)
	
	return data


def create_dataset(df, is_train=True):
	"""Create a HuggingFace dataset from a pandas dataframe."""
	texts = df['text'].tolist()
	
	if is_train:
		labels = df['is_relevant'].astype(int).tolist()
		data = tokenize_data(texts, labels)
	else:
		data = tokenize_data(texts)
	
	return Dataset.from_dict(data)


def train_model(
		train_dataset, 
		eval_dataset, 
		output_dir=OUTPUT_DIR, 
		batch_size=8, 
		epochs=3
	):
	"""Train the model on the provided datasets."""
	# Create output directory if it doesn't exist
	os.makedirs(output_dir, exist_ok=True)
	
	# Define training arguments
	training_args = TrainingArguments(
		output_dir=output_dir,
		num_train_epochs=epochs,
		per_device_train_batch_size=batch_size,
		per_device_eval_batch_size=batch_size,
		warmup_steps=500,
		weight_decay=0.01,
		logging_dir=f"{output_dir}/logs",
		logging_steps=10,
		evaluation_strategy="epoch",
		save_strategy="epoch",
		load_best_model_at_end=True,
		metric_for_best_model="accuracy",
		greater_is_better=True,
	)
	
	# Calculate class weights for imbalanced dataset
	model = MODEL_CONFIG["model"]
	
	# Create trainer
	trainer = Trainer(
		model=model,
		args=training_args,
		train_dataset=train_dataset,
		eval_dataset=eval_dataset,
		compute_metrics=compute_metrics,
		callbacks=[EarlyStoppingCallback(early_stopping_patience=3)]
	)
	
	# Train the model
	trainer.train()
	
	# Save the model
	model.save_pretrained(output_dir)
	MODEL_CONFIG["tokenizer"].save_pretrained(output_dir)
	
	return trainer


def compute_metrics(eval_pred):
	"""Compute metrics for evaluation during training."""
	logits, labels = eval_pred
	predictions = np.argmax(logits, axis=1)
	
	# For AUC-ROC we need probabilities of the positive class
	probs = torch.nn.functional.softmax(torch.tensor(logits), dim=1).numpy()
	roc_auc = roc_auc_score(labels, probs[:, 1])  # Using probability of positive class
	
	return {
		'accuracy': accuracy_score(labels, predictions),
		'roc_auc': roc_auc,
		'report': classification_report(labels, predictions, output_dict=True)
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


def load_model_from_checkpoint(checkpoint_dir=OUTPUT_DIR):
	"""Load a fine-tuned model from checkpoint."""
	tokenizer = DistilBertTokenizer.from_pretrained(checkpoint_dir)
	model = DistilBertForSequenceClassification.from_pretrained(checkpoint_dir)
	
	MODEL_CONFIG["tokenizer"] = tokenizer
	MODEL_CONFIG["model"] = model
	
	return tokenizer, model


def classify_press_releases(
	model_version,
	model_dir=OUTPUT_DIR,
	batch_size=16,
	overwrite=False,
):
	"""
	Classify scraped press releases and write predictions to Mongo.

	Resume semantics: articles that already have a prediction for `model_version`
	are skipped unless `overwrite=True`.
	"""
	tokenizer, model = load_model_from_checkpoint(model_dir)
	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
	model = model.to(device)

	db = get_db()
	articles_coll = db["articles"]
	predictions_coll = db["predictions"]

	done_ids = set()
	if not overwrite:
		done_ids = {
			p["article_id"]
			for p in predictions_coll.find(
				{"model_version": model_version}, {"article_id": 1}
			)
		}
		print(f"Found {len(done_ids)} articles already classified for {model_version}.")

	query = {"status": "scraped"}
	if done_ids:
		query["_id"] = {"$nin": list(done_ids)}

	cursor = articles_coll.find(query, {"_id": 1, "header": 1, "body": 1})
	batch = []
	total = 0

	def flush(batch):
		texts = [clean_text((d.get("header") or "") + " " + (d.get("body") or "")) for d in batch]
		results = predict(texts, model, tokenizer)
		preds = results["predictions"]
		probs = results["probabilities"][:, 1]
		ops = [
			UpdateOne(
				{"article_id": d["_id"], "model_version": model_version},
				{
					"$set": {
						"is_relevant_predicted": int(preds[j]),
						"relevance_probability": float(probs[j]),
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
		description="Fine-tune a classifier for AI-related press releases"
	)
	parser.add_argument(
		"--output-dir",
		type=str,
		help="Directory to save the fine-tuned model",
		default=OUTPUT_DIR
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
		help="Identifier stored alongside predictions (classify mode)",
		default=DEFAULT_MODEL_VERSION
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
		default=3
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

	OUTPUT_DIR = args.output_dir
	ensure_indexes()

	if args.classify:
		print("Classification mode")
		while True:
			if not os.path.isfile(os.path.join(args.output_dir, "config.json")):
				msg = f"No checkpoint at {args.output_dir}."
				if not args.loop:
					print(msg + " Exiting.")
					break
				print(msg + f" Sleeping {args.idle_sleep}s.")
				time.sleep(args.idle_sleep)
				continue

			classify_press_releases(
				model_version=args.model_version,
				model_dir=args.output_dir,
				batch_size=args.batch_size,
				overwrite=args.overwrite,
			)

			if not args.loop:
				break
			print(f"Idle; sleeping {args.idle_sleep}s")
			time.sleep(args.idle_sleep)
	else:
		print("Training mode")
		initialize_models(model_name=args.model_name)

		labeled_data = load_labeled_data()

		train_df, eval_df = train_test_split(
			labeled_data,
			test_size=0.2,
			stratify=labeled_data['is_relevant'],
			random_state=42
		)

		print(f"Training on {len(train_df)} examples, validating on {len(eval_df)} examples")
		print(f"Class distribution in training set: {train_df['is_relevant'].value_counts().to_dict()}")

		train_dataset = create_dataset(train_df)
		eval_dataset = create_dataset(eval_df)

		trainer = train_model(
			train_dataset=train_dataset,
			eval_dataset=eval_dataset,
			output_dir=OUTPUT_DIR,
			batch_size=args.batch_size,
			epochs=args.epochs
		)

		eval_results = trainer.evaluate()
		print("Evaluation results:")
		print(eval_results)

		pred_output = trainer.predict(eval_dataset)
		logits = pred_output.predictions
		probabilities = torch.nn.functional.softmax(torch.tensor(logits), dim=1).numpy()
		predictions = np.argmax(logits, axis=1)
		true_labels = pred_output.label_ids

		eval_results_df = pd.DataFrame({
			'_id': eval_df['_id'].tolist(),
			'text': eval_df['text'].tolist(),
			'true_label': true_labels,
			'predicted_label': predictions,
			'probability_relevant': probabilities[:, 1]
		})
		eval_results_df['correct'] = eval_results_df['true_label'] == eval_results_df['predicted_label']

		eval_results_path = os.path.join(OUTPUT_DIR, "eval_predictions.csv")
		eval_results_df.to_csv(eval_results_path, index=False)
		print(f"Saved evaluation results to {eval_results_path}")

	print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
	print("Done")
