# ai-labels-for-review

Drop CSVs of LLM-suggested **AI-product** labels here. The labeler app (review
mode, `/review/ai`) lists every `*.csv` in this folder and walks you through
verifying each row. The CSVs themselves are git-ignored; only this README is
tracked.

This file is self-contained: point an LLM at it (e.g. "read
`data/ai-labels-for-review/README.md` and label these articles") and it has
everything needed to produce a review CSV. No other context required.

This is the *second* of two independent questions asked about each press
release. The first — "is this a product launch?" — lives in
[`../labels-for-review/README.md`](../labels-for-review/README.md) and is a
separate classifier with its own labels. Do not conflate them: a release can be
about an AI product without being a launch, and vice versa.

---

## Getting the articles to label (data access)

The articles live in a local MongoDB. The `label` app, the scrapers, and Mongo
all run under Docker Compose; only Mongo's port is published to the host
(`27017`), so connect to it directly from the host rather than through `docker`
(which needs `sudo` here).

- **DB / collections:** database `prn` (~396k docs). `articles` holds the press
  releases; `ai_labels` + `ai_label_skips` hold already-decided ids for *this*
  task (skip these); `labels` + `label_skips` belong to the launch task.
- **Article shape:** `_id` (the join key — a slug, never invent one), `header`,
  `body`, `url`, `published_date`, `category`, `status` (label only
  `status:"scraped"`), plus two precomputed fields described below.
- **Client:** no host-level `mongosh`; use the project venv's pymongo
  (`venv/bin/python`, `MongoClient("mongodb://localhost:27017")["prn"]`).

### Sampling

Every scraped article carries an indexed `ai_vocab` tier assigned by
`py/vocab.py` — use it instead of writing your own regex, and use Mongo's
`$sample`:

| `ai_vocab` | meaning | draw |
|---|---|---|
| `"genai"` | generative-AI vocabulary present (LLM, diffusion, agentic, …) | **50%** |
| `"generic"` | AI/ML wording only (machine learning, predictive analytics, "AI-powered") | **25%** |
| any | unrestricted, for an unbiased slice | **25%** |

The `generic` tier is deliberate: it is where the hard negatives live. Without
it the classifier learns "the word AI" rather than the concept, so do not skip
it because the answers feel obvious.

Exclude ids already present in `ai_labels` or `ai_label_skips`. Articles that
already carry a launch label (`labels` with `is_relevant: 1` or
`has_product_details: true`) are the highest-value rows of all, since the AI
model runs downstream of the launch model — label those first if any remain.

Dump `_id`/`header`/`body` to a file, label per the brief below, and write the
result back here as `<batch-name>.csv`. Because every `_id` is in Mongo, you can
omit `header`/`body` from the CSV — the app rejoins them by `id`.

---

## Labeling brief (for the LLM)

**Task.** You are labeling PRNewswire press releases to bootstrap a **four-way**
classifier over AI releases: is this an AI product (generative AI is a core part
of the value proposition), a non-AI product with a generative feature bolted on,
a product sold to the AI industry itself, or none of those? The four are separate
classes, not a positive and three shades of negative — the model is trained to
tell them apart. For each article, output a provisional label that a human will
then quickly verify. Be decisive
but honest: your `llm_confidence` and `llm_rationale` are what let the human skim
rather than re-read, and your agreement rate against the human is measured.

**What counts as AI here.** Generative models: large language models, diffusion
and other image/video/3D generation, voice synthesis, generative agents. It must
be *core to the value proposition, as judged by what the release emphasises* —
not merely present somewhere in the stack. A generative capability that is
present but peripheral gets its own label, `ai_feature`, and a product sold to
the AI industry itself gets a third, `ai_tooling` — both described below.

**Decision (the `decision` column) — pick exactly one:**

- `ai` — generative AI is central to what the product is and does.
- `not_ai` — there is a product, but generative AI is not central to it, and no
  generative capability is being introduced either. This includes products built
  on *classic* ML.
- `ai_feature` — not an AI product, *but* the
  release concerns a largely non-AI product that introduces a generative-AI
  feature: an assistant bolted onto an existing suite, an LLM summariser added
  to a CRM, genAI drafting inside a document editor. These are the sharpest hard
  cases in the task — full of generative vocabulary while the product itself
  is not an AI product — so give them their own class rather than lumping them
  into `not_ai`.
- `ai_tooling` — not an AI product, *but* a genuine product
  release aimed at the AI industry and AI developers themselves: vector / RAG
  databases, agent and orchestration frameworks, fine-tuning and training
  platforms, inference and model-serving platforms, GPU clouds and compute
  providers, AI accelerators, training data and annotation services, eval,
  observability and guardrail tooling. Generative AI is the value proposition,
  but what is sold is an input to somebody else's AI system rather than a
  generative product reaching an end market.
- `no_product` — the release isn't about a product at all (funding, M&A,
  earnings, hires, awards, events, pure research, corporate updates). The
  question presupposes a product, so these are recorded as a reasoned skip
  rather than a negative; they must not teach the model that "no product" means
  "not AI".

**Tiebreakers, in order.** First: who buys it? If the customer is someone
building, training or operating generative AI systems → `ai_tooling`. Otherwise
take the generative model away — is it still substantially the
same product? If no → `ai`. If it merely loses a feature → `ai_feature`. If
generative AI appears only in boilerplate, a CEO quote, or the company's
self-description — nothing generative is actually being shipped → `not_ai`.

**Explicitly `not_ai`, however AI-sounding:** predictive analytics, forecasting,
recommendation engines, fraud / risk / credit scoring, computer-vision
classification & detection, OCR, standalone speech recognition, robotic process
automation, rules engines and scripted chatbots, and any "AI-powered" claim that
never names a mechanism. **Classic ML is not generative AI** — this is the single
most important boundary in this task, and the `generic` sampling tier exists to
put these in front of you.

**Explicitly `ai_tooling`, not `ai`:** infrastructure and tooling whose stated
purpose is building, training or serving generative models — AI accelerators,
inference platforms, vector databases, eval and guardrail tooling. The value
proposition is generative AI, but the buyer is the AI industry, so these are
counted separately from AI products reaching an end market.

**Hard calls.** A genAI feature bolted onto an established non-AI product →
`ai_feature`, unless the release makes the generative capability the headline
*and* the product's reason to exist → `ai`. A foundation model or generative API
sold to developers is still `ai` — the thing being sold generates — whereas the
substrate it runs on or the framework that calls it is `ai_tooling`; developers
being the buyers is not by itself enough. General-purpose infrastructure that
lists AI workloads as one use among many → judge by what the release leads on.
Consultancies and services *about* AI → a defined product for AI builders is
`ai_tooling`, a defined product for an end market is `ai`, no defined product at
all is `no_product`. When genuinely torn between `ai` and one of the other three,
pick the other one and lower your confidence — the human will catch under-calls
during review. When torn between `ai_feature` and `not_ai`, ask only whether a
generative capability is being shipped at all; if none is, it's `not_ai`.

**Optional metadata** — fill only when `decision` is `ai`, `ai_feature` or
`ai_tooling` (for `ai_feature`, describe the feature; for `ai_tooling`, the
generative workload it serves); leave blank otherwise:

- `modality` — one of: `llm_text`, `image_gen`, `video_gen`, `audio_speech`,
  `multimodal`, `agentic`, `infrastructure`, `other`
- `notes` — short free text, e.g. the name of the product or the model it uses

**Optional second call** — `launch_decision`, one of `relevant`, `irrelevant`,
`product_info`: whether the same release is a *product launch*, per the rubric in
[`../labels-for-review/README.md`](../labels-for-review/README.md). Fill it if
you are confident; it pre-fills a dropdown so the human can confirm both calls in
one pass. It is stored separately from the labels produced by the launch labeler
proper, and never overwrites an existing one. Leave it blank when unsure.

**Output rules.** Emit one valid CSV with a header row, written to this folder as
`<batch-name>.csv`. Use the exact column names below; one row per article; never
invent an `id`. Quote any field containing commas, quotes, or newlines
(rationales usually need quoting). Order rows by `llm_confidence` ascending if you
can, so the human reviews your least-certain calls first.

Example:

```csv
id,decision,llm_confidence,llm_rationale,modality,notes,launch_decision
prn_0a1b,ai,0.94,"New assistant built on an LLM; the model is the product.",llm_text,Foo Assistant,relevant
prn_0a2c,not_ai,0.88,"Fraud-scoring engine — classic supervised ML, nothing generative.",,,relevant
prn_0a3d,no_product,0.91,"Series B funding announcement; no product described.",,,irrelevant
prn_0a4e,ai_tooling,0.66,"Inference appliance sold to AI teams for serving LLMs.",infrastructure,,relevant
prn_0a5f,ai_feature,0.74,"Established payroll suite adds an LLM assistant; the suite is still the product.",llm_text,PayCo Assistant,irrelevant
```

---

## CSV columns

| column            | required | meaning                                                                |
|-------------------|----------|------------------------------------------------------------------------|
| `id`              | yes      | article `_id` — join key into `articles` / `ai_labels`                 |
| `decision`        | no\*     | provisional label: `ai` \| `not_ai` \| `ai_feature` \| `ai_tooling` \| `no_product` |
| `llm_confidence`  | no       | number in `0`–`1`; shown to the reviewer and used for ordering          |
| `llm_rationale`   | no       | the model's one-sentence reasoning — shown prominently to speed review  |
| `modality`        | no       | suggested modality, prefilled                                          |
| `notes`           | no       | free-text notes, prefilled                                             |
| `launch_decision` | no       | optional launch call, prefills the secondary dropdown                  |
| `header`          | no       | article title; if absent, joined from Mongo by `id`                     |
| `body`            | no       | article text; if absent, joined from Mongo by `id`                      |

\* Only `id` is strictly required for the app to load a row, but the LLM should
always emit `decision` (plus `llm_confidence`/`llm_rationale`) — that's the whole
point. An invalid/blank `decision` just leaves the form unselected so the human
labels from scratch. `no_product` is valid but intentionally has no radio: it
leaves the form unselected and the human clicks the **no product / N/A** button,
which records agreement with your call.

## How review works

- Items already in `ai_labels` or `ai_label_skips` are skipped, so progress
  resumes automatically and re-loading a file never double-serves a row.
- The decision form arrives pre-filled from the CSV. Submitting unchanged =
  confirm; changing a radio = overwrite.
- Each verified row is written to `ai_labels` with `source="llm_reviewed"`,
  `llm_label`, `human_agreed`, and `review_file`, so you can later measure the
  LLM's accuracy and mine the disagreements. The decision is stored as a single
  `ai_class` — `0` `not_ai`, `1` `ai`, `2` `ai_feature`, `3` `ai_tooling` — which
  is literally the class index the model trains on. `no_product` calls land in
  `ai_label_skips` with `reason="no_product"`, also carrying `llm_label` and
  `human_agreed`.
