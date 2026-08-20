# product-data

PRNewswire press-release pipeline: scrape → label → train → classify. All services run via Docker Compose against a shared MongoDB.

Two independent questions are asked about each release, each with its own labels
and its own binary classifier:

| task | question | labels | model versions |
|---|---|---|---|
| `launch` | is this a new product/service launch? | `labels` | `distilbert-finetuned-<timestamp>` |
| `ai` | is generative AI core to the product's value proposition? | `ai_labels` | `distilbert-ai-<timestamp>` |

They are deliberately *not* one multi-class model: the axes are orthogonal (a
release can describe an AI product without being a launch), the label
populations differ, and separate heads give independently tunable thresholds.
At serve time they compose as `launch AND ai`, with the AI model running only
over launch-positives.

## First-time setup

```bash
docker compose build
```

Rebuild after changes to `requirements.txt` or `Dockerfile`.

### Vocabulary flags

The labeler samples on precomputed `ai_vocab` / `launch_vocab` fields rather than
scanning with a regex — a `$sample` behind a body regex over 378k articles takes
~147s, versus milliseconds on an index. New articles are stamped at scrape time;
backfill existing ones once (resumable, safe to re-run):

```bash
docker compose --profile labeling run --rm labeler python py/vocab.py
```

## Run the scrapers

Starts MongoDB plus both scrapers (link discovery + article fetching) in a continuous loop:

```bash
docker compose up -d mongo link-scraper article-scraper
```

Logs:

```bash
docker compose logs -f link-scraper article-scraper
```

### Article statuses

`pending` → `scraped` is the happy path. The two failure states differ in
whether they are ever retried:

| status | meaning |
|---|---|
| `pending` | discovered, not yet fetched |
| `scraped` | body stored, vocab flags and company facts stamped |
| `failed` | transient — DNS, timeout, 5xx, unparseable markup. Re-queued on later passes until `attempts` reaches `MAX_ATTEMPTS` (3) |
| `gone` | terminal — the release 404s. Never retried |

The distinction exists because a single blip used to be permanent: on
2026-08-19 the container's DNS stopped resolving mid-pass and, because a
resolution failure returns in milliseconds, the loop burned through **94,630**
stubs marking each one `failed` for good. `scrape_article` now fetches through a
module-level `requests.Session`, so a pass costs one DNS lookup and one TLS
handshake instead of one of each per article — the query storm that exhausted
Docker's embedded resolver is gone at the source — with transport-level retries
(3, exponential backoff) beneath that and a `(10, 30)` connect/read timeout so a
hung socket cannot stall a pass.

## Back-fill older releases

PRN's dated category listings only reach back **365 days**. Past that they don't
404 — they 301 to the undated listing and quietly serve *today's* releases, so
`scrape_prn.py --start-date 2024-05-01` would harvest today's articles and stamp
them with a 2024 `listing_date`. Never use it to reach further back than a year.

Discovery is the only thing that expires. The release pages stay live
indefinitely and `scrape_article` parses a 2011 release as happily as a 2026 one,
so back-filling is purely a matter of finding the URLs. Two scripts do that,
both writing nothing but `status="pending"` stubs that the ordinary
`article-scraper` then fills in from the live pages. Both are idempotent —
`$setOnInsert` only, so a re-run can never demote a scraped article back to
pending.

**Sitemaps first.** `robots.txt` advertises `sitemap-gz.xml`, an index of ~188
gzipped monthly files reaching back to Dec 2010 (~2.35M releases). This is the
fast path — seconds per month.

```bash
python py/scrape/scrape_sitemap.py --list                    # survey, no writes
python py/scrape/scrape_sitemap.py --from 2025-03 --to 2025-10
python py/scrape/scrape_sitemap.py --months 2024-05,2024-06
```

Roughly 70 of those monthly files are served truncated or empty by PRN,
deterministically — retrying does not help. Truncated gzip still decodes up to
the cut, so partial months are kept and flagged; `--list` prints each month
against the ~16-20k releases a healthy one carries, and the run ends by naming
the months that came up short.

**Wayback second,** for exactly those months — but as a supplement, not a
substitute, and it is much slower (several minutes per month).

```bash
python py/scrape/scrape_wayback.py --months 2025-06,2025-08,2025-09
python py/scrape/scrape_wayback.py --from 2024-06 --to 2024-07
```

Two things to know about what it returns. Its windows are scoped by **capture
date, not publication date**, so a month's query yields everything the crawler
touched that month — for Sep 2025, ~27k releases of which only ~41% are actually
Sep 2025 publications. The printed counts are therefore not month totals, and
recall for a single target month is roughly 60%. And the two sources are
complementary rather than nested: for Sep 2025 the sitemap held 647 releases
Wayback never returned, so run both. `--pad-days` (default 14) extends each
window past month end, since captures lag publication.

Broad CDX queries time out, so it walks one window at a time and halves any that
fails, down to single days. Only URLs come from Wayback; bodies are always read
from the live site.

Stubs from either script carry a `source` field (`"sitemap"` / `"wayback"`) and
deliberately no `category` or `listing_date` — neither archive exposes one, and
the authoritative `published_at` arrives with the body.

## Company data

Every release says more about its sender than the `uploader` string the scraper
keeps. The dateline names the city it was issued from, a listed company prints
its ticker, half carry an "About X" boilerplate paragraph, and three in five
link a website. None of that needs an external data source, so it is
mined from the bodies already in Mongo.

`uploader_link` (`/news/divi-resorts/`) is the company key. PR Newswire assigns
it, so it survives spelling drift in `uploader` and needs no entity resolution
of ours. There are ~78k of them across the scraped corpus.

Two layers, mirroring how `py/vocab.py` precomputes its sampling flags:

Neither layer needs running by hand in steady state: `scrape_article` stamps the
facts as it scrapes, and the article scraper rebuilds `companies` whenever its
queue empties (`--rollup-when-idle`, set on the service in `docker-compose.yml`).
The one-shot jobs exist for after an extractor change bumps a version:

```bash
docker compose --profile enrich run --rm enrich                            # per-release facts
docker compose --profile enrich run --rm enrich python py/company.py --rollup   # companies
```

**Per-release facts** are stamped onto each article — they belong to the
release, not the company, because a company's dateline moves over the years and
that is signal. `scrape_article` stamps new articles; the backfill catches up
existing ones and only touches articles whose `facts_version` is behind, so it
is resumable and a re-run after an extractor change re-does exactly the stale
ones. Measured extraction rates per release:

| field | rate | notes |
|---|---|---|
| `dateline_city` | 97.1% | misses are non-Latin-script releases |
| `dateline_region` | 48.4% | only about half of datelines name a state or country |
| `source_org` | 79.9% | the `SOURCE X` line, a cleaner issuer name than `uploader` |
| `domains` | 59.7% | from URLs only — PRN rewrites every e-mail to `[email protected]` |
| `about` | 53.3% | `{subject, text}` |
| `tickers` | 20.1% | `[{exchange, symbol, near_name}]` |
| `fwd_looking` | 9.5% | safe-harbour boilerplate |
| `litigation_vocab` | 7.2% | behavioural flag, see `org_type` below |
| `report_vocab` | 6.4% | behavioural flag, see `org_type` below |

Measured over the 424,766 scraped articles stamped at `tier0-1`.

**The `companies` collection** is rebuilt from those facts by `--rollup`.
Nothing in it is authored, so it can be dropped and rebuilt after any change to
the extractors; `rollup_version` records which generation produced it. It is a
full rebuild every time — it reads the corpus rather than a delta — which is why
it hangs off the scraper's idle moment rather than running per batch.

### Deciding `is_public`

Ticker ownership is decided by **concentration, not proximity**. The obvious
rule — a ticker is yours if your name sits next to it — fails outright, because
a securities firm names itself just as close to a symbol as an issuer does: The
Schall Law Firm scores 99.5% on it. What separates them is that an issuer
repeats one symbol (Veeva, `NYSE:VEEV`, in 97.8% of its releases) while a
securities firm cycles through hundreds (Pomerantz mentions 1,001 distinct
symbols and owns none).

| rule | `is_public` | confidence |
|---|---|---|
| modal ticker in >=30% of releases | `true` | high |
| safe-harbour boilerplate in >=50% of releases | `true` | medium |
| >=5 distinct tickers mentioned, modal share <10% | `false` | medium |
| >=5 releases, modal ticker share <10%, <10% safe-harbour | `false` | low |
| otherwise | `null` | — |

Note that the fourth rule keys on the modal *share*, not on whether any ticker
appears at all. HelloNation names one symbol across 2,473 releases and is
plainly not listed; a rule demanding zero mentions left it, and 15% of the
50-releases-and-up band, undecided.

The last row matters: **half of all senders have exactly one release**, and one
release is not evidence of anything. They stay `null` rather than being coded
private, which is what keeps the base rate honest. Undecided senders are
therefore concentrated almost entirely in the thin tail:

| releases per sender | senders | `is_public` null |
|---|---|---|
| 1 | 38,991 | 93.9% |
| 2-4 | 22,102 | 91.5% |
| 5-9 | 9,094 | 2.4% |
| 10-49 | 7,021 | 3.8% |
| 50+ | 987 | 4.4% |

### `org_type`

The head of the corpus by volume does not launch products. The four largest
senders are two market-research publishers (Research and Markets, Technavio) and
two securities firms (Pomerantz, the Rosen Law Firm). Names do not identify
them — "Greenberg Traurig, LLP" says nothing about law and "Persistent Systems"
is not a research house — so the rollup reads *behaviour* instead, via the two
vocabulary flags stamped on every release, aggregated to a share per company.

| rule | `org_type` | confidence |
|---|---|---|
| `.gov`/`.mil` domain, or name starting `U.S.`/`Department of` | `government` | high |
| `.edu` domain, or `University`/`College` in the name | `academic` | high |
| `litigation_vocab_share >= 0.5` | `law_firm` | high |
| `report_vocab_share >= 0.5` | `research_publisher` | high |
| >=5 distinct tickers, modal share <10% | `stock_commentary` | medium |
| `.org` domain and non-profit wording in the About | `nonprofit` | medium |
| otherwise | `operating_company` | low |

`stock_commentary` is the catch-all for senders that are neither law firms nor
research houses but still only ever write about other companies' shares —
InvestorsObserver, USA News Group.

### What is in there

78,195 senders over 424,780 scraped releases. The rollup itself takes ~27s; the
per-release backfill runs at ~16k articles/min, so a full pass over the scraped
corpus is around half an hour.

| | senders | releases |
|---|---|---|
| `is_public: true` | 6,931 (8.9%) | 19.4% |
| `is_public: false` | 13,883 (17.8%) | 56.8% |
| `is_public: null` | 57,381 (73.4%) | 23.7% |

| `org_type` | senders | releases |
|---|---|---|
| `operating_company` | 73,295 | 82.8% |
| `law_firm` | 1,668 | 6.6% |
| `research_publisher` | 656 | 5.9% |
| `nonprofit` | 1,217 | 1.9% |
| `stock_commentary` | 61 | 1.1% |
| `government` | 576 | 0.9% |
| `academic` | 722 | 0.9% |

Worth reading twice: **13.6% of the corpus comes from senders that never launch
anything** — securities firms, research publishers and stock commentators. They
are a structural negative class for the launch model, available now as a filter
rather than as something the classifier has to learn.

Coverage of the derived fields, per sender: `hq` 98.6%, `domain` 66.1%,
`about` 65.7%, `ticker` 7.4%.

```json
{
  "_id": "/news/veeva-systems/",
  "name": "Veeva Systems",
  "name_norm": "veeva systems",
  "n_releases": 313,
  "first_release": "2023-08-08T20:00:00Z",
  "last_release": "2026-08-11T14:08:00Z",
  "categories": {"health": 134, "business-technology": 79, "consumer-technology": 60},
  "ticker": {"exchange": "NYSE", "symbol": "VEEV", "share_of_releases": 0.978},
  "tickers_mentioned_distinct": 2,
  "n_releases_with_ticker": 306,
  "third_party_ticker_ratio": 0.0,
  "fwd_looking_share": 0.387,
  "litigation_vocab_share": 0.0,
  "report_vocab_share": 0.0,
  "hq": {"city": "PLEASANTON", "region": "Calif.", "share": 0.297, "method": "modal_dateline"},
  "dateline_places": [{"city": "PLEASANTON", "region": "Calif.", "n": 93},
                      {"city": "BARCELONA", "region": "Spain", "n": 44}],
  "domain": "ir.veeva.com",
  "domains": [{"domain": "ir.veeva.com", "n": 15}, {"domain": "biomarin.com", "n": 5}],
  "about": {"subject": "Veeva Systems", "as_of": "2026-08-11T11:03:00Z",
            "n_releases_with_about": 142,
            "text": "Veeva delivers the industry cloud for life sciences with ..."},
  "is_public": true, "is_public_method": "self_ticker", "is_public_confidence": "high",
  "org_type": "operating_company", "org_type_method": "default", "org_type_confidence": "low",
  "rollup_version": "tier0-1", "built_at": "2026-08-19T22:58:38Z"
}
```

### Known gaps

Translations are counted as separate releases. Veeva's 313 include the same
Barcelona event as `BARCELONA/Spain`, `BARCELONE/Espagne`, `BARCELONA/Spanien`
and `BARCELONA/España`, so per-company release counts are inflated for anyone
who distributes multilingually. Regions are also stored as PRN prints them
(`N.C.`, `Calif.`, `Espagne`) rather than normalised, and the modal domain can
be a customer's rather than the sender's when a company names customers more
often than itself — Veeva's is `ir.veeva.com` with a customer's `biomarin.com`
close behind, and RTX's is `prattwhitney.com`.

Subsidiaries inherit their parent's listing. Toyota Motor North America is
called public off `NYSE:TM`, which belongs to the parent; separating the two
needs the parent/child data in the next tier. And Frost & Sullivan lands in
`stock_commentary` rather than `research_publisher`, because it discusses many
companies' shares without using enough report vocabulary to trip that rule
first.

## Label articles

Web UI for building the `labels` and `ai_labels` collections. Profile-gated so it
doesn't run by default.

```bash
docker compose --profile labeling up -d labeler
# http://localhost:8000     product-launch labeler
# http://localhost:8000/ai  AI-product labeler
```

Set the labeler name by editing `LABELER_NAME` in `docker-compose.yml`.

Both labelers work the same way: a sampled article, a rubric, a decision, and
optional metadata. Each also has a third decision that records a *flagged hard
negative* — trained as a negative, but retrievable as a group: **irrelevant w/
product info** (`has_product_details`) for launch, and **AI feature**
(`has_ai_feature`) for AI, the case where a largely non-AI product introduces a
generative-AI feature. What differs otherwise is the question, the sampling, and
the drop folder for LLM batches.

- **Launch** oversamples launch-keyword headers 70/30 against an unbiased random
  stream.
- **AI** serves articles that already carry a launch label first (they match what
  the AI model sees at serve time), then falls back to a tiered draw on
  `ai_vocab`: 50% generative-AI vocabulary, 25% generic AI/ML wording — the
  classic-ML and AI-washing hard negatives — and 25% unrestricted. It has an extra
  **no product / N/A** button, because "is the product an AI product?"
  presupposes a product; those land in `ai_label_skips` with
  `reason="no_product"` instead of polluting the training set with negatives.

The AI card also offers an optional **launch** dropdown when the article has no
launch label yet — you're reading the article anyway. Those are written with
`source="ai_side"` and never overwrite an existing label; they carry the AI
sampler's `sample_method`, so they can be filtered out of base-rate estimates
(they come from an AI-biased sample).

### Review LLM-suggested labels

Drop CSVs of provisional labels into `data/labels-for-review/` (launch) or
`data/ai-labels-for-review/` (AI) — both git-ignored, with the schema documented
in each folder's `README.md` — then open the labeler and click **review mode**
(http://localhost:8000/review/launch, http://localhost:8000/review/ai). Pick a
file and verify each row: the decision form arrives pre-filled from the CSV, so
submitting unchanged confirms the LLM's call and changing a radio overwrites it.
Progress resumes automatically and each verified row lands in the task's label
collection tagged `source="llm_reviewed"` with the original `llm_label` and a
`human_agreed` flag, so you can later score the LLM and mine its mistakes.

Each folder's README is a self-contained brief — point an LLM at it and it has
everything needed to produce a review CSV:
[launch](data/labels-for-review/README.md) ·
[AI](data/ai-labels-for-review/README.md).

## Train the classifiers

One-shot jobs. Read the task's labels + `articles` from Mongo and write a
checkpoint to `<models>/<task>/<version>`. Requires network access (downloads
`distilbert-base-uncased`; the container is `--rm`, so nothing caches between
runs).

```bash
docker compose --profile training run --rm train      # launch  -> /models/launch/<version>
docker compose --profile training run --rm ai-train   # ai      -> /models/ai/<version>
```

Optional flags (append after the service name):

```bash
docker compose --profile training run --rm ai-train \
  python py/classify/finetune.py --task ai --epochs 5 --batch-size 8
```

Every run is from scratch off `distilbert-base-uncased` — there is no incremental
training. Re-run whenever you have added labels.

### Runs and versions

A run is named for the moment it started, in UTC:
`distilbert-finetuned-20260819232201`. That one string is the directory it lives
in, the `model_version` its predictions carry, and its `_id` in the `model_runs`
collection, so any prediction traces back to the weights and the eval numbers
that produced it. Runs accumulate side by side; nothing is overwritten.

```
/models/launch/distilbert-finetuned-20260819232201/
├── config.json, model.safetensors, tokenizer*   # the artefact inference loads
├── metrics.json                                 # accuracy, ROC-AUC, per-class report,
│                                                #   sizes, label counts, hyperparameters
└── eval_predictions.csv                         # per-example true/predicted/probability
```

The Trainer's per-epoch checkpoints carry optimiser state and run several hundred
MB each; they exist only so the best epoch can be restored at the end, so they go
to a scratch directory that is deleted when training returns. Only the final
artefact is kept.

`metrics.json` is also written to Mongo as a `model_runs` document, which is how
you compare runs without going to the volume:

```javascript
db.model_runs.find({task: "launch"}, {_id: 1, trained_at: 1, n_train: 1,
                                      "metrics.eval_accuracy": 1, "metrics.eval_roc_auc": 1})
             .sort({trained_at: -1})
```

One caveat when comparing: the 80/20 split is stratified with a fixed seed, but it
is drawn over whatever labels exist at the time, so consecutive runs hold out
different articles. The numbers track a moving evaluation set, not a fixed one.

## Run inference

Continuous services. Each waits for a checkpoint, then classifies articles that
don't yet have a prediction for the version it is serving, looping forever.

```bash
docker compose up -d classifier      # all scraped articles
docker compose up -d ai-classifier   # only articles the launch model called relevant
```

**Which model gets served.** Unpinned, each pass re-resolves the newest run under
`/models/<task>` — so a retrain is picked up on the next pass with no restart, and
its predictions land under the new version. Set `MODEL_VERSION` in
`docker-compose.yml` to pin one run instead.

That default has a cost worth knowing: a new version has no predictions, so
picking one up means re-scoring the corpus from zero — 3M+ articles for `launch`.
If you would rather retrain freely and switch deliberately, pin `MODEL_VERSION`
and move it when you mean to.

`ai-classifier` passes `--require-relevant-from launch`, which tracks the newest
*launch* run; pass a version string instead to pin the gate. Either way it stays
idle until that model has produced positives.

Predictions are keyed by `(article_id, model_version)`, so every model and every
generation of it writes to the same `predictions` collection without colliding.
Both the resume filter and the gate are `$lookup` joins on that unique index
rather than id lists assembled in Python — with 3M+ articles whose `_id`s are URL
slugs, a `$nin` of everything already classified outgrows Mongo's 16MB query
limit as soon as one full pass completes.

## Stop everything

```bash
docker compose down
```

Add `-v` to also drop the `mongo_data` and `models` volumes.
