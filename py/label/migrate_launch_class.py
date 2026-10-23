"""One-off: fold the launch task's `is_relevant` + `has_product_details` pair
into the single `launch_class` index the labeler now writes.

Both tasks now record one integer per label — the index of the decision that was
clicked — and leave the narrowing to training (see TASKS in
py/classify/finetune.py). The launch task predates that: it stored the *trained*
value directly and flagged the hard negative in a field beside it, which left
nowhere to put a fourth decision. The mapping is exact and reversible:

    is_relevant=1                       -> launch_class=1  relevant
    is_relevant=0, has_product_details  -> launch_class=2  irrelevant w/ product info
    is_relevant=0                       -> launch_class=0  irrelevant
                                           launch_class=3  feature release (new)

Run once against the labels collection, from the host:

    venv/bin/python py/label/migrate_launch_class.py [--dry-run]

Idempotent: only documents still carrying `is_relevant` are touched, and the two
legacy fields are unset by the same write, so a second run finds nothing to do.
"""
import argparse
import os
import sys
from collections import Counter

from pymongo import UpdateOne

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_db


DECISIONS = {0: "irrelevant", 1: "relevant", 2: "irrelevant w/ product info"}


def migrate(dry_run=False):
	labels = get_db()["labels"]
	pending = list(labels.find(
		{"is_relevant": {"$exists": True}},
		{"is_relevant": 1, "has_product_details": 1, "launch_class": 1},
	))
	print(f"{labels.count_documents({})} labels, {len(pending)} still on the old shape")

	counts, conflicts, ops = Counter(), [], []
	for doc in pending:
		launch_class = 2 if doc.get("has_product_details") else int(doc.get("is_relevant") == 1)
		# A doc holding both shapes would mean a half-finished earlier run against
		# a labeler already writing the new field; report rather than overwrite.
		if doc.get("launch_class") not in (None, launch_class):
			conflicts.append((doc["_id"], doc["launch_class"], launch_class))
			continue
		counts[launch_class] += 1
		ops.append(UpdateOne(
			{"_id": doc["_id"]},
			{
				"$set": {"launch_class": launch_class},
				"$unset": {"is_relevant": "", "has_product_details": ""},
			},
		))

	for value, name in DECISIONS.items():
		print(f"  launch_class={value} ({name}): {counts[value]}")
	for _id, existing, derived in conflicts:
		print(f"  CONFLICT {_id}: launch_class={existing} but old fields imply {derived}")

	if dry_run:
		print("Dry run; nothing written.")
		return
	if ops:
		result = labels.bulk_write(ops, ordered=False)
		print(f"Migrated {result.modified_count} labels.")
	print(f"Remaining on the old shape: {labels.count_documents({'is_relevant': {'$exists': True}})}")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--dry-run", action="store_true", help="Report the mapping without writing")
	migrate(dry_run=parser.parse_args().dry_run)
