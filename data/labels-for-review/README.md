# labels-for-review

Drop CSVs of LLM-suggested labels here. The labeler app (review mode, `/review`)
lists every `*.csv` in this folder and walks you through verifying each row.
The CSVs themselves are git-ignored; only this README is tracked.

This file is self-contained: point an LLM at it (e.g. "read
`data/labels-for-review/README.md` and label these articles") and it has
everything needed to produce a review CSV. No other context required.

This is the *first* of two independent questions asked about each press
release. The second, "is this an AI product?", lives in
[`../ai-labels-for-review/README.md`](../ai-labels-for-review/README.md) and is a
separate classifier with its own labels. A batch here can optionally suggest that
call too (the `ai_decision` column below).

---

## Getting the articles to label (data access)

The articles live in a local MongoDB. The `label` app, the scrapers, and Mongo
all run under Docker Compose; only Mongo's port is published to the host
(`27017`), so connect to it directly from the host rather than through `docker`
(which needs `sudo` here).

- **DB / collections:** database `prn` (~374k docs). `articles` holds the press
  releases; `labels` + `label_skips` hold already-decided ids (skip these);
  `predictions` is for model output.
- **Article shape:** `_id` (the join key — a slug, never invent one), `header`,
  `body`, `url`, `published_date`, `category`, `status` (label only
  `status:"scraped"`).
- **Client:** no host-level `mongosh`; use the project venv's pymongo
  (`venv/bin/python`, `MongoClient("mongodb://localhost:27017")["prn"]`).

Sample the same way the app does (`py/label/app.py:sample_launch_article`):
exclude ids already in `labels`/`label_skips`, then draw ~70% from headers matching the
launch-keyword regex (`launch|unveil|debut|introduc|roll(s|ing)? out|now
available|general availability`, case-insensitive) and ~30% random for an
unbiased slice. Use Mongo's `$sample` for the random draw. Dump
`_id`/`header`/`body` to a file, label per the brief below, and write the result
back here as `<batch-name>.csv`. Because every `_id` is in Mongo, you can omit
`header`/`body` from the CSV — the app rejoins them by `id`.

---

## Labeling brief (for the LLM)

**Task.** You are labeling PRNewswire press releases to bootstrap a classifier
that detects **new product/service launches**. For each article, output a
provisional label that a human will then quickly verify. Be decisive but honest:
your `llm_confidence` and `llm_rationale` are what let the human skim rather than
re-read, and your agreement rate against the human is measured.

There are four decisions but the model trained on them is currently **binary**:
`relevant` is the positive class, and the other three are all negatives. Label
all four anyway — the distinction is stored exactly as you emit it, and the
classes are split out later once each has enough examples; a call collapsed at
labelling time cannot be recovered.

**Decision (the `decision` column) — pick exactly one:**

- `relevant` — the release's *primary purpose* is to announce a specific product
  or service that is **newly available** from the issuing company. New product
  launches; major releases with their own name; public beta / early access (if
  signups open); new services with defined scope; co-developed products.
- `feature_release` — something new *is* shipping, but as an addition to an
  existing product rather than as a product in its own right: a capability,
  module or integration inside something customers already have.
- `irrelevant` — anything else: funding / M&A / earnings / hires; awards &
  analyst rankings; partnerships; regional expansion of an existing product;
  rebrands; clinical/research milestones without a product; events; corporate
  updates; discontinuations.
- `product_info` — **not a launch**, and nothing new shipping either, *but* the
  release carries substantive detail about an already-launched product:
  capabilities, specs, pricing, customer wins. These are kept as hard negatives
  to curb false positives, so flag them rather than lumping them into
  `irrelevant`.

**Tiebreaker:** after reading it, can you name a specific thing a customer could
now buy, sign up for, or download that they couldn't before? If yes →
`relevant`. "Industry's first" hype does **not** count — apply the tiebreaker. A
named & priced thing → new product. Free / open-source releases count if they're
a discrete thing. When genuinely torn between `relevant` and a negative, prefer
the negative and lower your confidence (the human will catch under-calls during
review).

**`relevant` or `feature_release`?** The line between a product and a feature is
genuinely blurred, and there is no rule that settles every case — **use your best
judgement, and say which way you leaned in the `llm_rationale`.** As a guide: a
*major* release counts as a new product — it has its own name, its own pricing,
is buyable on its own, or is a version customers have to move to. An
*incremental* improvement to what they already have is a `feature_release` — a
capability added to an existing tier, a new integration, a performance or UI
update, a patch. When those signals conflict, weigh how the release itself frames
the thing, and lower your confidence so the human looks closely.

**Optional metadata** — fill only when `decision` is `relevant`,
`feature_release` or `product_info`; leave blank otherwise:

- `audience` — one of: `b2b`, `b2c`, `b2g`, `mixed`
- `domain` — one of: `ai_ml`, `software`, `hardware_iot`, `biotech_pharma`,
  `healthcare`, `fintech`, `energy_cleantech`, `automotive_mobility`,
  `industrial`, `consumer_goods`, `media_entertainment`, `other`
  (security products live under `software`)
- `notes` — short free text, e.g. the name of the launched product (for a
  `feature_release`, the feature and the product it lands in)

**Optional second call:** `ai_decision`, one of `ai`, `not_ai`, `ai_feature`,
`ai_tooling`: whether the same release is about an *AI product*, per the brief in
[`../ai-labels-for-review/README.md`](../ai-labels-for-review/README.md). Fill it
if you are confident; it pre-fills a dropdown so the human can confirm both calls
in one pass. It is stored separately from the labels produced by the AI labeler
proper, and never overwrites an existing one. Leave it blank when unsure, when the
release describes no product at all (that brief's `no_product`), and for articles
already in `ai_labels` or `ai_label_skips`: the dropdown is shown only for
articles the AI task has not decided.

**Output rules.** Emit one valid CSV with a header row, written to this folder as
`<batch-name>.csv`. Use the exact column names below; one row per article; never
invent an `id`. Quote any field containing commas, quotes, or newlines
(rationales usually need quoting). Order rows by `llm_confidence` ascending if you
can, so the human reviews your least-certain calls first.

Example:

```csv
id,decision,llm_confidence,llm_rationale,audience,domain,notes,ai_decision
prn_0a1b,relevant,0.93,"Names a new API customers can sign up for today.",b2b,software,Foo API,ai
prn_0a2c,irrelevant,0.88,"Series A funding announcement; no purchasable product.",,,,
prn_0a3d,product_info,0.71,"Not a launch, but gives specs/pricing for an existing device.",b2c,hardware_iot,,not_ai
prn_0a4e,feature_release,0.62,"Adds an export module to an existing suite; no separate SKU, so a feature rather than a product.",b2b,software,Foo Suite export,not_ai
```

---

## CSV columns

| column          | required | meaning                                                                 |
|-----------------|----------|-------------------------------------------------------------------------|
| `id`            | yes      | article `_id` — join key into the `articles` / `labels` collections     |
| `decision`      | no\*     | provisional label: `relevant` \| `feature_release` \| `irrelevant` \| `product_info` |
| `llm_confidence`| no       | number in `0`–`1`; shown to the reviewer and used for ordering           |
| `llm_rationale` | no       | the model's one-sentence reasoning — shown prominently to speed review   |
| `audience`      | no       | suggested audience (`b2b`/`b2c`/`b2g`/`mixed`), prefilled               |
| `domain`        | no       | suggested domain, prefilled                                             |
| `notes`         | no       | free-text notes, prefilled                                             |
| `ai_decision`   | no       | optional AI call, prefills the secondary dropdown                       |
| `header`        | no       | article title; if absent, joined from Mongo by `id`                     |
| `body`          | no       | article text; if absent, joined from Mongo by `id`                      |

\* Only `id` is strictly required for the app to load a row, but the LLM should
always emit `decision` (plus `llm_confidence`/`llm_rationale`) — that's the whole
point. An invalid/blank `decision` just leaves the form unselected so the human
labels from scratch. Include `header`/`body` only if the articles aren't already
in Mongo.

## How review works

- Items already in `labels` or `label_skips` are skipped, so progress resumes
  automatically and re-loading a file never double-serves a row.
- The decision form arrives pre-filled from the CSV. Submitting unchanged =
  confirm; changing a radio = overwrite.
- Cards with `llm_confidence` of 0.9 or more are shown in green. The **accept N
  ≥ 0.9** button in the header records every pending row at that confidence as
  the LLM called it, including its `ai_decision`, with `source="llm_accepted"`
  and no `human_agreed`, so those rows stay out of the agreement rate.
- A confirmed `ai_decision` is written to `ai_labels` with `source="launch_side"`.
- Each verified row is written to the `labels` collection with
  `source="llm_reviewed"`, `llm_label`, `human_agreed`, and `review_file`, so
  you can later measure the LLM's accuracy and mine the disagreements. The
  decision is stored as a single `launch_class` — `0` `irrelevant`, `1`
  `relevant`, `2` `product_info`, `3` `feature_release` — which training
  collapses to the binary head's class index (`1` stays `1`; `0`, `2` and `3`
  all become `0`).
