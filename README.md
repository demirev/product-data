# product-data

PRNewswire press-release pipeline: scrape → label → train → classify → browse.
All services run via Docker Compose against a shared MongoDB (database `prn`).

Two independent questions are asked about each release, each with its own labels
and its own binary classifier:

| task | question | classes | labels | model versions |
|---|---|---|---|---|
| `launch` | is this a new product/service launch? | `irrelevant`, `relevant` | `labels` | `distilbert-finetuned-<timestamp>` |
| `ai` | is generative AI being shipped here? | `not_ai`, `ai` | `ai_labels` | `distilbert-ai-<timestamp>` |

Index 1 is the positive class in both label spaces, so one code path reports AUC
and false-positive rate for either head and gates on `predicted_class: 1`. At
serve time the two compose as `launch AND ai`: the AI model runs only over
launch-positives.

Design rationale, measurements and incident history are in
[`AGENT_NOTES.md`](AGENT_NOTES.md).

## Contents

- [Services](#services)
- [Collections](#collections)
- [First-time setup](#first-time-setup)
- [Run the scrapers](#run-the-scrapers)
- [Back-fill older releases](#back-fill-older-releases)
- [Company data](#company-data)
- [External enrichment](#external-enrichment)
- [Label articles](#label-articles)
- [Train the classifiers](#train-the-classifiers)
- [Run inference](#run-inference)
- [Browse the data](#browse-the-data)
- [Stop everything](#stop-everything)

## Services

| service | profile | kind | what it does |
|---|---|---|---|
| `mongo` | | continuous | MongoDB 7 on `27017`, volume `mongo_data` |
| `link-scraper` | | continuous | discovers release URLs from PRN's dated category listings |
| `article-scraper`, `article-scraper-1` | | continuous | fetch and parse release bodies, one hash shard each |
| `classifier` | | continuous | scores all scraped articles with the `launch` model |
| `ai-classifier` | | continuous | scores launch-positives with the `ai` model |
| `enrich` | | continuous | resolves companies against external registries |
| `web` | | continuous | explorer on `8080`; read-only unless `EXPLORER_LABELER` is set |
| `company-facts` | `company` | one-shot | back-fills per-release company facts, rebuilds `companies` |
| `labeler` | `labeling` | on demand | labelling UI on `8000` |
| `train`, `ai-train` | `training` | one-shot | fine-tune the `launch` / `ai` classifier |

Every service reads `MONGO_URI` and `MONGO_DB`. Outside Compose they default to
`mongodb://localhost:27017` and `prn`.

## Collections

| collection | contents |
|---|---|
| `articles` | one document per release, keyed by URL slug: status, body, vocab flags, company facts |
| `labels`, `ai_labels` | hand and LLM-reviewed labels for the two tasks |
| `ai_label_skips` | articles skipped in the AI labeler, with a `reason` |
| `model_runs` | one document per training run, `_id` = model version |
| `predictions` | one document per `(article_id, model_version)` |
| `companies` | one document per sender, keyed by `uploader_link`; rebuilt by the rollup |
| `company_enrichment` | raw per-source registry answers, keyed by `uploader_link` |
| `sec_tickers`, `sec_cik_lookup`, `gleif_lei`, `gleif_rr`, `gleif_isin`, `irs_bmf`, `ror` | staging for the bulk enrichment sources |
| `sender_blacklist` | senders whose releases the explorer leaves out, keyed by `uploader_link` |
| `fe_releases`, `fe_companies`, `fe_meta` | the explorer's denormalized read model (a cache) |

## First-time setup

```bash
docker compose build
```

Rebuild after changes to `requirements.txt` or `Dockerfile`.

### Vocabulary flags

The labeler samples on precomputed, indexed `ai_vocab` / `launch_vocab` fields on
each article. New articles are stamped at scrape time. Backfill existing ones
once (resumable, safe to re-run):

```bash
docker compose --profile labeling run --rm labeler python py/vocab.py
```

## Run the scrapers

Starts MongoDB plus the scrapers (link discovery and article fetching) in a
continuous loop:

```bash
docker compose up -d mongo link-scraper article-scraper article-scraper-1
```

Logs:

```bash
docker compose logs -f link-scraper article-scraper article-scraper-1
```

`scrape_prn.py` flags: `--no-links`, `--no-articles`, `--start-date YYYY-MM-DD`,
`--end-date YYYY-MM-DD`, `--categories a,b`, `--loop`, `--idle-sleep SECONDS`,
`--rollup-when-idle`, `--workers N`, `--shard K/N`.

### Parallel article fetching

Two knobs control concurrency, both read from the environment or passed as flags
to `scrape_prn.py`:

| knob | flag | env | what it does |
|---|---|---|---|
| threads per process | `--workers N` | `SCRAPE_WORKERS` | fetch `N` articles at a time from one queue |
| processes | `--shard K/N` | `SCRAPE_SHARD` | keep only stubs whose hashed `_id` lands in bucket `K` of `N` |

Threads overlap the network wait. Parsing holds the GIL, so one process tops out
around ten articles a second regardless of thread count, and additional
throughput comes from additional processes. The shard hash is `crc32` of the
`_id`, so every process agrees on the buckets without coordination.

`docker-compose.yml` runs two shards of four workers (eight fetches in flight).
Every bucket must be owned by exactly one running process: when changing `N`,
change every replica's `K/N` together and add or remove replicas to match. Only
shard 0 carries `--rollup-when-idle`.

After raising concurrency, watch the rate of `403` in `last_status` and back off
if it climbs.

Fetches go through a module-level `requests.Session` with three transport-level
retries (exponential backoff, covering `429` and 5xx) and a `(10, 30)`
connect/read timeout. A page that answers with an error records the numeric code
in `last_status` next to the message in `last_error`; DNS failures, timeouts and
unparseable markup leave `last_status` null. To re-queue persistent `403`s, reset
`attempts` on `{status: "failed", last_status: 403}`.

### Article statuses

| status | meaning |
|---|---|
| `pending` | discovered, not yet fetched |
| `scraped` | body stored, vocab flags and company facts stamped |
| `failed` | transient: DNS, timeout, 5xx, unparseable markup. Re-queued on later passes until `attempts` reaches `MAX_ATTEMPTS` (3) |
| `gone` | terminal: the release 404s. Never retried |

## Back-fill older releases

PRN's dated category listings reach back **365 days**. A listing URL older than
that 301s to the undated listing and serves today's releases, so
`scrape_prn.py --start-date` must never be set more than a year back: it would
harvest today's articles and stamp them with the old `listing_date`.

Release pages themselves stay live indefinitely, so back-filling is a matter of
finding the URLs. Two scripts do that. Both write only `status="pending"` stubs,
which the ordinary `article-scraper` then fills in from the live pages, and both
are idempotent (`$setOnInsert` only, so a re-run never demotes a scraped article
back to pending). Stubs carry a `source` field (`"sitemap"` / `"wayback"`) and no
`category` or `listing_date`; `published_at` arrives with the body.

**Sitemaps first.** `robots.txt` advertises `sitemap-gz.xml`, an index of gzipped
monthly files reaching back to Dec 2010. This is the fast path, seconds per
month.

```bash
python py/scrape/scrape_sitemap.py --list                    # survey, no writes
python py/scrape/scrape_sitemap.py --from 2025-03 --to 2025-10
python py/scrape/scrape_sitemap.py --months 2024-05,2024-06
python py/scrape/scrape_sitemap.py --all                     # every month in the index
```

Some monthly files are served truncated or empty by PRN, and retrying does not
help. Truncated gzip decodes up to the cut, so partial months are kept and
flagged: `--list` prints each month's count (a healthy month carries ~16-20k
releases) and a run ends by naming the months that came up short. `--dry-run`
counts without writing; `--sleep` sets the pause between files.

**Wayback second,** for the months the sitemaps came up short on. It is much
slower (several minutes per month).

```bash
python py/scrape/scrape_wayback.py --months 2025-06,2025-08,2025-09
python py/scrape/scrape_wayback.py --from 2024-06 --to 2024-07
```

Wayback windows are scoped by **capture date**, so a month's query returns
everything the crawler touched that month, and the printed counts are therefore
not month totals. `--pad-days` (default 14) extends each window past month end,
since captures lag publication. The script walks one CDX window at a time and
halves any window that times out, down to single days. Only URLs come from
Wayback; bodies are always read from the live site. The two sources are
complementary, so run both for a short month. Other flags: `--dry-run`,
`--timeout`, `--sleep`.

## Company data

Company information is mined from the release bodies already in Mongo: the
dateline city, printed tickers, the "About X" boilerplate paragraph, linked
websites and the `SOURCE` line.

`uploader_link` (`/news/divi-resorts/`) is the company key. PR Newswire assigns
it, so it is stable across spelling drift in `uploader`.

There are two layers. In steady state neither needs running by hand:
`scrape_article` stamps the facts as it scrapes, and the article scraper rebuilds
`companies` whenever its queue empties (`--rollup-when-idle`). The one-shot jobs
are for after an extractor change bumps a version:

```bash
docker compose --profile company run --rm company-facts                          # per-release facts
docker compose --profile company run --rm company-facts python py/company.py --rollup   # companies
docker compose --profile company run --rm company-facts python py/company.py --rollup --no-enrichment   # heuristics only
```

### Per-release facts

Stamped onto each article, since a company's dateline and boilerplate change over
time. The backfill only touches articles whose `facts_version` is behind
`FACTS_VERSION` in `py/company.py`, so it is resumable, and a re-run after an
extractor change re-does exactly the stale ones.

| field | contents |
|---|---|
| `dateline_city` | city the release was issued from |
| `dateline_region` | state or country, as PRN prints it (`Calif.`, `Espagne`) |
| `source_org` | the `SOURCE X` line, a cleaner issuer name than `uploader` |
| `domains` | from URLs in the body (PRN rewrites every e-mail address) |
| `about` | `{subject, text}` of the "About X" paragraph |
| `tickers` | `[{exchange, symbol, near_name}]` |
| `fwd_looking` | safe-harbour boilerplate present |
| `litigation_vocab` | securities-litigation vocabulary present |
| `report_vocab` | market-research-report vocabulary present |

### The `companies` collection

Rebuilt from those facts by `--rollup`. Nothing in it is authored, so it can be
dropped and rebuilt at any time; `rollup_version` records which generation
produced it. It is a full rebuild every time (~30s).

`is_public` is decided by the concentration of ticker mentions across a sender's
releases:

| rule | `is_public` | confidence |
|---|---|---|
| modal ticker in >=30% of releases | `true` | high |
| safe-harbour boilerplate in >=50% of releases | `true` | medium |
| >=5 distinct tickers mentioned, modal share <10% | `false` | medium |
| >=5 releases, modal ticker share <10%, <10% safe-harbour | `false` | low |
| otherwise | `null` | |

Senders with too few releases to decide stay `null`.

`org_type` is decided by behaviour, via the vocabulary flags aggregated to a
share per company:

| rule | `org_type` | confidence |
|---|---|---|
| `.gov`/`.mil` domain, or name starting `U.S.`/`Department of` | `government` | high |
| `.edu` domain, or `University`/`College` in the name | `academic` | high |
| `litigation_vocab_share >= 0.5` | `law_firm` | high |
| `report_vocab_share >= 0.5` | `research_publisher` | high |
| >=5 distinct tickers, modal share <10% | `stock_commentary` | medium |
| `.org` domain and non-profit wording in the About | `nonprofit` | medium |
| otherwise | `operating_company` | low |

`law_firm`, `research_publisher` and `stock_commentary` senders write about other
companies and do not launch products, so they are usable as a filter on the
launch model's input or output.

Example document (heuristic fields):

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

### Known limitations

- Translations are counted as separate releases, so `n_releases` is inflated for
  senders who distribute multilingually.
- The modal `domain` can be a customer's when a sender names customers more often
  than itself.
- A sender that discusses many companies' shares without much report vocabulary
  lands in `stock_commentary` even when it is a research publisher.
- `hq` holds the raw modal dateline; the normalised location is in `country` /
  `region` / `hq_city`, added by enrichment.

## External enrichment

`py/enrich.py` resolves each rolled-up company against six public registries and
stores what each returns, whole, in `company_enrichment`, keyed by the same
`uploader_link`. `--rollup` then folds a curated subset onto `companies`.

```bash
export ENRICH_CONTACT="you@example.com"   # required: see below
docker compose up -d enrich                                     # continuous, resumable
docker compose run --rm enrich python py/enrich.py --refresh    # force a bulk re-download
docker compose run --rm enrich python py/enrich.py --stats      # match rates
docker compose run --rm enrich python py/enrich.py --retry-misses          # re-ask recorded non-matches
docker compose run --rm enrich python py/enrich.py --only sec,gleif --limit 100
docker compose run --rm enrich python py/enrich.py --companies /news/veeva-systems/
```

Other flags: `--no-resolve` (stop after the bulk refresh), `--loop`,
`--idle-sleep SECONDS`.

**`ENRICH_CONTACT` is required.** SEC refuses requests whose `User-Agent` names
no contact address, and the value is sent to all six services on every request.
It has no default; set it in your shell or in `.env`. The enricher warns at
startup when it is unset.

| # | source | what it is for | route |
|---|---|---|---|
| I | **Wikidata** | the crosswalk hub: CIK, LEI, ISIN, parent, inception | `list=exturlusage` by domain, then `wbgetentities` |
| II | **SEC** | tickers/exchange, cik-lookup, submissions, companyfacts, Form D | bulk for the first two, per-CIK for the rest |
| III | **GLEIF** | legal entity, incorporation date, jurisdiction, parent, ISIN count | golden copy |
| IV | **RDAP** | the domain's registration record | IANA bootstrap, then the registry |
| V | **IRS BMF** | non-profits: EIN, NTEE code, revenue, ruling date | `eo1`–`eo4` + `eo_xx` |
| VI | **ROR** | research institutions | Zenodo dump |

The bulk sources land in staging collections (`sec_tickers`, `sec_cik_lookup`,
`gleif_lei`, `gleif_rr`, `gleif_isin`, `irs_bmf`, `ror`) and the resolver does
indexed point lookups against them. The downloads (~600 MB) are kept in the
`enrich_data` volume (`ENRICH_DATA_DIR`). SEC's per-company artefacts are fetched
per CIK.

### Resumption

A company is due for a source only when that source's block is missing or its
`resolver_version` is behind. Nothing re-fetches on a schedule, so `--refresh`
updates staging without re-resolving companies that have already matched. Misses
are recorded and not retried unless `--retry-misses` is passed. A transient error
is not recorded as a miss, so it is picked up again on the next pass.

Each source is wrapped independently, so a failure in one does not discard the
others' results. Companies are resolved biggest sender first. A full sweep runs
at about 3.6 s/company, dominated by the RDAP and Wikidata rate limits.

### Matching

Wikidata runs first because one match supplies P5531 (CIK), P1278 (LEI) and P946
(ISIN), which turn the downstream matches into exact-key lookups.

| source | 1st | 2nd | last resort |
|---|---|---|---|
| Wikidata | `exturlusage(domain)`, verified against P856 | | |
| SEC | Wikidata P5531 | ticker **and** filer name | `name_norm` in cik-lookup |
| GLEIF | Wikidata P1278 | `name_norm` + country | `name_norm` worldwide |
| IRS | EIN from SEC submissions | `name_norm` + state | `name_norm` nationwide |
| ROR | Wikidata QID | domain | `name_norm` (aliases included) |
| RDAP | domain | | |

- A name block must come back with exactly one row to count as a match.
- `matched_by` records which tier answered, so a tier's matches can be dropped
  wholesale.
- The ticker path requires the filer's name to agree with the company's. When the
  modal ticker belongs to a differently named filer (a subsidiary printing its
  parent's symbol), the mismatch is recorded and feeds the `is_public` rules
  below.
- `exturlusage` returns every item that cites a domain, so each candidate is
  verified against its P856 (official website).

### Fields folded onto `companies`

The fold happens inside `rollup`, so the article scraper's `--rollup-when-idle`
picks up new enrichment with no extra step.

| field | precedence |
|---|---|
| `wikidata_id` `cik` `lei` `ein` `ror_id` | join keys |
| `founded_year` + `_src` | Wikidata P571 › GLEIF creation date › ROR established › Form D › IRS ruling › SEC first filing |
| `domain_registered_year` | RDAP |
| `country` `region` `hq_city` + `location_src` | SEC (US only) › GLEIF › Wikidata › ROR › IRS › dateline |
| `domicile` + `_src` | SEC state of incorporation › GLEIF jurisdiction › Form D › Wikidata P740 |
| `sic` `sic_desc` `ntee_code` `legal_form` | SEC, IRS, GLEIF respectively |
| `is_public` + `_method` + `_confidence`, `ticker`, `listings` | see below |
| `revenue` `revenue_unit` `revenue_year`, `assets`, `employees` | SEC XBRL › IRS BMF; employees from Wikidata P1128 |
| `parent_wikidata_id` `parent_lei` `parent_name` | Wikidata P749/P127, GLEIF level 2 |
| `enrichment_sources` `enriched_at` `enrichment_version` | which sources answered |

Monetary fields carry their unit (`revenue_unit`, `assets_unit`), since filers
report in their own currency. SEC addresses are used only when they are in the
US, because EDGAR codes foreign locations in its own scheme. `region` is
normalised to a state or ISO code; `hq` keeps the raw modal dateline.

External evidence overrides the heuristic `is_public` as follows:

| rule | `is_public` | confidence |
|---|---|---|
| a symbol in SEC's own ticker/exchange list | `true` | high |
| Wikidata P414 stock exchange | `true` | medium |
| modal ticker belongs to a differently-named filer, and another registry knows this company | `false` | medium |
| GLEIF knows the entity and maps zero ISINs | `false` | medium |
| otherwise | the heuristic's answer | |

The GLEIF rule is withheld when the heuristic found a concentrated self-ticker.
An external `org_type` is applied only when the heuristic fell back to its
`operating_company` default, so the behavioural classes (`law_firm`,
`research_publisher`, `stock_commentary`) always stand.

`python py/company.py --rollup --no-enrichment` rebuilds the heuristic-only
documents, which makes every override reversible and measurable.

Example document (enrichment fields):

```json
{
  "_id": "/news/smithfield-foods%2C-inc./",
  "name": "Smithfield Foods, Inc.",
  "wikidata_id": "Q1477575", "cik": "0000091388",
  "lei": "CVTIPZRZJC0JQEZLL598",
  "founded_year": 1936, "founded_year_src": "wikidata_p571",
  "domain_registered_year": 1997,
  "country": "US", "region": "VA", "hq_city": "Smithfield", "location_src": "sec",
  "domicile": "US-VA", "domicile_src": "sec_state_of_inc",
  "sic": "2011", "sic_desc": "Meat Packing Plants",
  "legal_form": "HOV4", "legal_form_src": "gleif_elf",
  "is_public": true, "is_public_method": "sec_ticker_exchange",
  "is_public_confidence": "high",
  "ticker": {"exchange": "Nasdaq", "symbol": "SFD", "source": "sec"},
  "revenue": 15531000000, "revenue_unit": "USD", "revenue_year": 2025,
  "assets": 12177000000, "assets_unit": "USD",
  "parent_wikidata_id": "Q17499655", "parent_name": "WH Group",
  "parent_lei": "254900A4AYEDRWYSE928",
  "parent_relation": "IS_ULTIMATELY_CONSOLIDATED_BY",
  "enrichment_sources": ["wikidata", "sec", "gleif", "rdap"],
  "enrichment_version": "tier1-1"
}
```

### Known limitations

- Private companies whose registry name does not normalise to the sender's name
  are missed, because name blocks match exactly.
- ROR lags renames, so an institution filed under a former name and domain is
  missed by ROR even when another source finds it.
- GLEIF and IRS name blocks are cut by country or state where one is known, and
  fewer than half of datelines name one.

## Label articles

Web UI for building the `labels` and `ai_labels` collections.

```bash
docker compose --profile labeling up -d labeler
# http://localhost:8000     product-launch labeler
# http://localhost:8000/ai  AI-product labeler
```

Set the labeler name by editing `LABELER_NAME` in `docker-compose.yml`.

Both labelers work the same way: a sampled article, a rubric, a decision, and
optional metadata. Each records its decision as a single integer, `launch_class`
or `ai_class`, the index of the button that was clicked.

| `launch_class` | decision | what it means |
|---|---|---|
| `0` | irrelevant | not announcing anything newly available |
| `1` | relevant | a specific product or service is newly available from the issuer |
| `2` | irrelevant w/ product info | nothing new ships, but there is substantive detail about an already-launched product |
| `3` | feature release | something new ships, but inside an existing product |

| `ai_class` | decision | what it means |
|---|---|---|
| `0` | not AI | there is a product, but nothing generative is central to it or being shipped |
| `1` | AI product | generative AI is the value proposition, sold into an end market |
| `2` | AI feature | a largely non-AI product adding a generative feature |
| `3` | AI tooling | the buyer is the AI industry itself: RAG and vector databases, agent frameworks, inference and compute providers, eval tooling |

For `launch_class` `1` versus `3`: a major release (its own name, its own
pricing, buyable on its own, a version customers must move to) is a product; an
incremental improvement to what customers already have is a feature. The rubric
asks for judgement. The breakdown on each labeler page shows how many examples
each of the four classes has.

### Four labels per task, two classes per head

Labels are stored four-way and training folds them to two:

| stored | `launch_class` | trains as | `ai_class` | trains as |
|---|---|---|---|---|
| `0` | irrelevant | negative | not AI | negative |
| `1` | relevant | **positive** | AI product | **positive** |
| `2` | irrelevant w/ product info | negative | AI feature | **positive** |
| `3` | feature release | negative | AI tooling | **positive** |

Each task's fold is one dict, `TASKS[...]["collapse"]` in
[`py/classify/finetune.py`](py/classify/finetune.py), applied as the labels are
loaded. To widen a head, name the classes in that task's `classes`, map the
stored values through `collapse`, and retrain. No label migration is needed,
because Mongo holds the four-way value. The change takes effect on the task's
next training run; earlier runs keep their own meaning of `predicted_class`. Runs
of either shape share the task's version prefix and are told apart by the
`classes` recorded on each `model_runs` document, which is also what every
prediction's `class_probabilities` is indexed by.

### Sampling

- **Launch** oversamples launch-keyword headers 70/30 against an unbiased random
  stream.
- **AI** serves articles that already carry a launch label first (they match what
  the AI model sees at serve time), then falls back to a tiered draw on
  `ai_vocab`: 50% generative-AI vocabulary, 25% generic AI/ML wording (classic-ML
  and AI-washing hard negatives) and 25% unrestricted. A **no product / N/A**
  button sends the article to `ai_label_skips` with `reason="no_product"` and
  keeps it out of the training set.

Each card also offers an optional dropdown for the other task when that task has
not yet labeled or skipped the article: a **launch** call on the AI card, an **AI
product** call on the launch card. Those labels are written with
`source="ai_side"` or `source="launch_side"`, never overwrite an existing
decision, and carry the card's `sample_method`, so they can be filtered out of
base-rate estimates.

### Review LLM-suggested labels

Drop CSVs of provisional labels into `data/labels-for-review/` (launch) or
`data/ai-labels-for-review/` (AI). Both folders are git-ignored apart from a
`README.md` documenting the CSV schema. Then open the labeler and click **review
mode** (http://localhost:8000/review/launch, http://localhost:8000/review/ai).
Pick a file and verify each row: the decision form arrives pre-filled from the
CSV, so submitting unchanged confirms the LLM's call and changing a radio
overwrites it. Progress resumes automatically. Each verified row lands in the
task's label collection tagged `source="llm_reviewed"` with the original
`llm_label` and a `human_agreed` flag.

Each folder's README is a self-contained brief that an LLM can be pointed at to
produce a review CSV:
[launch](data/labels-for-review/README.md) ·
[AI](data/ai-labels-for-review/README.md).

### False positives from the explorer

With `EXPLORER_LABELER` set in `.env`, each release card in the explorer has a
**False positive…** menu offering the three negative launch decisions. A choice
is written to `labels` with `source="explorer"` and
`sample_method="predicted_positive"`, and trains like any other label. The same
setting adds a **Blacklist sender** button to release and company cards and a
**Blacklist** tab; a blacklisted sender's releases are left out of the explorer
but still scored and trained on. See
[`fe/README.md`](fe/README.md#marking-false-positives).

### Migrating legacy launch labels

Launch labels stored as `is_relevant` plus `has_product_details` are converted to
`launch_class` by a one-off, idempotent script (`is_relevant: 1` → `1`,
`has_product_details` → `2`, otherwise `0`), which unsets the two legacy fields:

```bash
venv/bin/python py/label/migrate_launch_class.py --dry-run   # report, write nothing
venv/bin/python py/label/migrate_launch_class.py
```

## Train the classifiers

One-shot jobs. Each reads the task's labels and `articles` from Mongo and writes
a checkpoint to `<models>/<task>/<version>`. Requires network access to download
`distilbert-base-uncased` (the container is `--rm`, so nothing caches between
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

Training flags: `--task {launch,ai}`, `--epochs` (default 10), `--batch-size`
(default 8), `--model-name` (default `distilbert-base-uncased`).

Every run fine-tunes from the base model; there is no incremental training.
Re-run after adding labels.

### Runs and versions

A run is named for the moment it started, in UTC:
`distilbert-finetuned-20260819232201`. That string is the directory it lives in,
the `model_version` its predictions carry, and its `_id` in the `model_runs`
collection, so any prediction traces back to the weights and the eval numbers
that produced it. Runs accumulate side by side.

```
/models/launch/distilbert-finetuned-20260819232201/
├── config.json, model.safetensors, tokenizer*   # the artefact inference loads
├── metrics.json                                 # accuracy, ROC-AUC, confusion matrix,
│                                                #   per-class report, sizes, label counts
└── eval_predictions.csv                         # per-example true/predicted class + p(class)
```

Per-epoch Trainer checkpoints go to a scratch directory that is deleted when
training returns.

`metrics.json` is also written to Mongo as a `model_runs` document:

```javascript
db.model_runs.find({task: "launch"}, {_id: 1, trained_at: 1, n_train: 1,
                                      "metrics.eval_accuracy": 1, "metrics.eval_roc_auc": 1})
             .sort({trained_at: -1})
```

Headline `roc_auc`, `false_positive_rate` and `false_negative_rate` are
one-vs-rest against class 1 (`relevant` / `ai`) on both tasks, so they are
comparable across runs and across head widths. The full picture is in
`metrics.confusion_matrix` and the per-class report.

Two caveats when comparing runs. The 80/20 split is stratified with a fixed seed
but drawn over whatever labels exist at the time, so consecutive runs hold out
different articles. Stratification is dropped for a run where some class has
fewer than two labelled examples; the run prints a line when that happens.

## Run inference

Continuous services. Each waits for a checkpoint, then classifies articles that
do not yet have a prediction for the version it is serving, looping forever.

```bash
docker compose up -d classifier      # all scraped articles
docker compose up -d ai-classifier   # only articles the launch model called relevant
```

Classify flags: `--classify`, `--task`, `--loop`, `--idle-sleep SECONDS`,
`--model-version` (or `MODEL_VERSION`), `--require-relevant-from`, `--overwrite`
(re-score articles that already have a prediction for this version),
`--batch-size`.

**Which model gets served.** Unpinned, each pass re-resolves the newest run under
`/models/<task>`, so a retrain is picked up on the next pass with no restart and
its predictions land under the new version. A new version has no predictions, so
picking one up means re-scoring the corpus from zero. Set `MODEL_VERSION` in
`docker-compose.yml` to pin one run and switch deliberately.

`ai-classifier` passes `--require-relevant-from launch`, which tracks the newest
launch run; pass a version string to pin the gate. It stays idle until that model
has produced positives. The gate matches `predicted_class: 1`.

Each prediction carries `predicted_class`, an index into the `classes` recorded
on that run's `model_runs` document, and `class_probabilities`, the whole
distribution, so a threshold can be re-tuned without a second pass over the
corpus.

Predictions are keyed by `(article_id, model_version)`, so every model and every
generation of it writes to the same `predictions` collection without colliding.
The resume filter and the gate are `$lookup` joins on that unique index.

Both classifier services set `OMP_NUM_THREADS=4` to leave cores for the scraper
shards.

## Browse the data

```bash
docker compose up -d web      # http://localhost:8080
```

A web app over what the pipeline produced: the product releases the
served `launch` model called relevant, joined to the companies that sent them.
It has three tabs: an overview of the corpus with the model runs behind it, a
filterable grid of releases, and one of companies. Every filter is in the URL, so
a slice can be shared, and whatever is on screen can be exported as CSV.

It keeps its own denormalized read model (`fe_releases`, `fe_companies`,
`fe_meta`), rebuilt on startup and every ten minutes. Those three collections are
a cache: drop them and the next refresh rebuilds them. The app loads no model,
so its image carries no torch.

It is read-only by default. Setting `EXPLORER_LABELER` in `.env` adds a
false-positive menu to every release card, which writes launch labels to
`labels`; leave it unset on a public deployment.

It serves pages before the read model has anything in it. `is_ai` is `null` for a
release no `ai` run has scored, and `false` only when one has scored it negative.

See [`fe/README.md`](fe/README.md) for the API reference, the CSV schemas, the
refresh semantics and how a release is deduplicated.

## Stop everything

```bash
docker compose down
```

Add `-v` to also drop the `mongo_data`, `models` and `enrich_data` volumes.
