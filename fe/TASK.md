# Product Release Explorer — build brief

Build a read-only web application that gives researchers access to the product
release database this repository produces: a Vue 3 single-page app in `fe/`
backed by a JSON API in `py/api/`, both served by one container.

Three tabs: **Overview**, **Products**, **Companies**.

---

## 1. Context you need before starting

Read the root [`README.md`](../README.md) first — at minimum the intro table,
"Four labels per task, two classes per head", "Company data", and "External
enrichment". The short version:

PR Newswire press releases are scraped into MongoDB (`articles`), hand- and
LLM-labelled, and scored by two independent DistilBERT heads:

| task | question | positive class | prediction version prefix |
|---|---|---|---|
| `launch` | is this a new product/service launch? | `predicted_class: 1` (`relevant`) | `distilbert-finetuned-<ts>` |
| `ai` | is generative AI being shipped here? | `predicted_class: 1` (`ai`) | `distilbert-ai-<ts>` |

The `ai` head runs only over `launch` positives, so the two compose as
`launch AND ai`. Company facts are mined from release bodies into `companies`
and enriched against six public registries.

**The product release database is the set of `launch` positives.** That set does
not exist as a stored collection; §4 defines how you derive it.

Nothing in this app writes to a pipeline collection, and nothing in it
re-implements pipeline logic (extraction, the enrichment fold, label collapse).
Read what the pipeline produced; do not recompute it.

---

## 2. Architecture

```
fe/                      Vue 3 + Vite SPA (this directory)
py/api/                  FastAPI JSON API + CSV export + read-model builder
```

Python lives in `py/` alongside `py/label/` and `py/scrape/`; the browser app
lives in `fe/`. One container serves both: FastAPI mounts the built SPA at `/`
and the API under `/api`.

### Backend

- FastAPI + uvicorn + pymongo. Versions are pinned in the root
  `requirements.txt`; do not add Python dependencies.
- Reuse `py/db.py` for the Mongo handle. Import it the way `py/label/app.py`
  does (`sys.path` insert of the `py/` directory, then `from db import get_db`),
  since `PYTHONPATH=/app/py` in the image.
- Files: `py/api/app.py` (routes, CSV), `py/api/queries.py` (Mongo reads),
  `py/api/readmodel.py` (builds the read model, §4.3). Do not add a fourth
  module unless it earns its place.

### Frontend

- Vue 3 with `<script setup>` SFCs, Vite, `vue-router` in history mode.
- No state-management library. Filter state lives in the URL (§7); anything
  shared beyond that goes in a composable.
- Charts: `chart.js` + `vue-chartjs`. No other UI or component framework.
- Plain CSS: one global stylesheet defining design tokens (colors, spacing,
  radii, type scale) as custom properties, plus scoped styles per component.

### Suggested layout

```
fe/
  index.html  package.json  vite.config.js
  src/
    main.js  App.vue  router.js  api.js  styles.css
    views/      OverviewView.vue  ProductsView.vue  CompaniesView.vue
    components/ ReleaseCard.vue  CompanyCard.vue  FilterPanel.vue
                StatTile.vue  Badge.vue  Pagination.vue  TimeSeries.vue  BarChart.vue
    composables/useQueryState.js
```

### Code conventions

- Python: tabs for indentation, matching the rest of `py/`.
- Be parsimonious with new named things. Do not create a function or component
  whose body is a single expression wrapping another call — inline it.
- Comment the non-obvious decisions, not the syntax. The existing modules are
  the reference for tone.

---

## 3. Running it

**Development** — two processes against the developer's local Mongo:

```bash
MONGO_URI=mongodb://localhost:27017 PYTHONPATH=py venv/bin/uvicorn api.app:app --reload --port 8080
cd fe && npm run dev      # 5173, vite.config.js proxies /api -> localhost:8080
```

**Container** — add a `web` service to `docker-compose.yml` (port 8080,
`MONGO_URI: mongodb://mongo:27017`, `MONGO_DB: prn`, `restart: unless-stopped`,
`depends_on: mongo` healthy). It builds from a new `fe/Dockerfile` with the repo
root as context, multi-stage:

1. `node:22-alpine` — `npm ci && npm run build` → `fe/dist`.
2. `python:3.12-slim` — install only `fastapi`, `starlette`, `uvicorn`,
   `pymongo` and `python-multipart` at the versions pinned in
   `requirements.txt`, copy `py/` and the built `fe/dist`, set
   `PYTHONPATH=/app/py`, run uvicorn.

Stage 2 deliberately does not install torch/transformers — the API never loads
a model, it reads predictions the classifiers already wrote.

The service must start and serve pages when the read model is empty or the
`ai` head has no run yet.

---

## 4. Data contract

### 4.1 Source collections

Everything below is read-only input.

**`articles`** — `_id` is the URL slug.

| field | notes |
|---|---|
| `header` | headline; carries trailing tabs/newlines, normalize (§4.4) |
| `body` | full text with PRN furniture in it, normalize (§4.4) |
| `url` | site-relative, e.g. `/news-releases/<slug>.html` |
| `published_at` | authoritative datetime; sort and bucket on this, not `listing_date` |
| `category` | PRN category slug, e.g. `consumer-technology`; absent on some releases |
| `uploader_link` | **the company key**, e.g. `/news/veeva-systems/`; joins to `companies._id` |
| `dateline_city`, `dateline_region` | city the release was issued from |
| `tickers` | `[{exchange, symbol, near_name}]` |
| `status` | only `"scraped"` documents have a body |

NB: (important!)some companies publish the same release across multiple categories - dedupe on header and uploader_link for the entire webpage

**`predictions`** — one document per (article, model version).

| field | notes |
|---|---|
| `article_id` | → `articles._id` |
| `model_version` | the run that produced it |
| `predicted_class` | index into that run's `classes`; `1` is the positive class on both tasks |
| `class_probabilities` | full distribution, e.g. `[0.42, 0.58]` |
| `predicted_at` | datetime |

**`model_runs`** — one document per training run, `_id` is the version string.
Carries `task`, `trained_at`, `classes`, `n_train`, `n_eval`, `label_counts`,
and `metrics` (`eval_accuracy`, `eval_roc_auc`, `eval_false_positive_rate`,
`eval_false_negative_rate`, `eval_confusion_matrix`, per-class report).

**`companies`** — `_id` is the `uploader_link`. Heuristic fields from the
release bodies plus enrichment folded on by the rollup:

`name`, `n_releases`, `first_release`, `last_release`, `categories` (map of
slug→count), `domain`, `about {subject, text, as_of}`, `hq {city, region,
share, method}`, `ticker {exchange, symbol}`, `is_public` +
`is_public_method` + `is_public_confidence`, `org_type` + `org_type_method` +
`org_type_confidence`, `country`, `region`, `hq_city`, `founded_year`,
`domain_registered_year`, `domicile`, `sic`, `sic_desc`, `legal_form`,
`revenue` + `revenue_unit` + `revenue_year`, `assets`, `employees`,
`wikidata_id`, `cik`, `lei`, `ein`, `ror_id`, `parent_name` +
`parent_wikidata_id`, `enrichment_sources`.

`org_type` is one of `operating_company`, `law_firm`, `research_publisher`,
`nonprofit`, `stock_commentary`, `government`, `academic`.

`is_public` is **three-valued**: `true`, `false`, or `null`. `null` means the
evidence did not decide, and it is a real answer that the UI must show as such
(§6.3) — never render it as "private".

### 4.2 What a product release is

1. The **served run** for a task is the newest `model_runs` document for that
   task: `find({task}).sort("trained_at", -1).limit(1)`. Resolve `launch` and
   `ai` this way. (The classifier services resolve the same version off the
   models volume, which the API does not mount — `model_runs` is the API's
   authority.)
2. A **product release** is an article with a `predictions` document for the
   served `launch` version and `predicted_class: 1`.
3. Its **launch confidence** is `class_probabilities[1]`.
4. It is an **AI release** if it also has a prediction for the served `ai`
   version with `predicted_class: 1`; `ai_confidence` is that document's
   `class_probabilities[1]`. If the served `ai` run does not exist, or an
   article has no prediction under it, `is_ai` is `null` — not `false`.
5. Its **company** is `companies[article.uploader_link]`.

Positive-class index 1 is an invariant of both tasks; read the class names off
the run's `classes` array rather than hardcoding them for display.

### 4.3 The read model

At full scale the serving corpus is on the order of 10⁵ product releases, and
resolving `predictions → articles → companies` per request would mean that many
lookups per page view. The API therefore owns a denormalized read model it
builds and refreshes itself. It is a cache, derived entirely from the
collections in §4.1, and safe to drop at any time.

Three collections, all owned by this app and named with the `fe_` prefix so
their ownership is unambiguous:

**`fe_releases`** — one document per product release (deduped by uploader and title).

```json
{
  "_id": "<article slug>",
  "title": "Acme Launches the Widget 3000",
  "summary": "<normalized snippet, ~400 chars>",
  "url": "https://www.prnewswire.com/news-releases/<slug>.html",
  "published_at": "2026-04-21T18:45:00Z",
  "category": "consumer-technology",
  "dateline": {"city": "PEORIA", "region": "Ariz."},
  "tickers": [{"exchange": "NASDAQ", "symbol": "CTSH"}],
  "confidence": 0.9412,
  "is_ai": true, "ai_confidence": 0.88,
  "company_id": "/news/acme/",
  "company_name": "Acme",
  "company_org_type": "operating_company",
  "company_is_public": true,
  "company_country": "US",
  "company_domain": "acme.com"
}
```

The `company_*` fields are copied in so the Products tab can filter and sort on
company attributes without a join.

**`fe_companies`** — one document per company that has at least one product
release. The whole `companies` document (minus `dateline_places`, `domains`,
`aliases`, `source_orgs`) plus:

```json
{
  "n_product_releases": 32,
  "first_product_release": "2023-08-08T20:00:00Z",
  "last_product_release": "2026-08-11T14:08:00Z",
  "n_ai_releases": 7,
  "product_categories": {"consumer-technology": 20, "health": 12}
}
```

Companies with no product release are out of scope entirely — they do not enter
this collection and there is no view that lists them.

**`fe_meta`** — a single document: the served `launch` and `ai` versions and
their `model_runs` metrics, `built_at`, and the facet values the filter panels
need (distinct categories, org types, countries, `is_public` states, and the
min/max `published_at`), computed once at refresh instead of per request.

**Refresh.** `py/api/readmodel.py` exposes one entry point that brings the read
model up to date and is safe to call concurrently:

- Full rebuild when the served `launch` version differs from the one in
  `fe_meta`, otherwise incremental over predictions with
  `predicted_at > fe_meta.built_at`.
- Build `fe_releases` with an aggregation over `predictions` (`$match` served
  version + `predicted_class: 1`, `$lookup` `articles`, `$lookup` `companies`,
  `$lookup` the `ai` predictions, `$merge` into `fe_releases`). Rebuild
  `fe_companies` from `fe_releases` afterwards — it is a single `$group` plus a
  `$lookup`, cheap enough to redo whole every time.
- Call it on startup and every 10 minutes from a background task. Expose it as
  `POST /api/refresh` too.

**Indexes.** `fe_releases` on `published_at`, `category`, `company_id`,
`confidence`, `is_ai`, `company_org_type`, and a text index on `title` +
`summary`. `fe_companies` on `n_product_releases`, `org_type`, `is_public`,
`country`, `name_norm`. Create them in `readmodel.py` — `py/db.py`'s
`ensure_indexes` covers pipeline collections and should stay that way, with one
exception: add `[("model_version", 1), ("predicted_class", 1)]` on
`predictions` there, because that index serves the classifiers as much as it
serves this app.

### 4.4 Normalization

Do this once, in the read-model builder, never in the browser.

- **Headlines and company names**: `re.sub(r"\s+", " ", value).strip()` —
  scraped headers end in runs of tabs and newlines.
- **Summary**: take `body`, cut it at the first `Continue Reading` occurring
  after the first 200 characters (what follows is image-caption furniture),
  collapse whitespace, then truncate to ~400 characters on a word boundary.
- **URLs**: article `url` and company `_id` are both site-relative — prefix
  `https://www.prnewswire.com`. Company `_id`s are already percent-encoded
  (`/news/garmin-international%2C-inc./`); do not re-encode them.
- **Datetimes**: serialize as ISO-8601 UTC strings.

---

## 5. HTTP API

All responses JSON, all list endpoints `{"items": [...], "total": n, "page": p,
"per_page": k}`. Unknown query parameters are ignored, invalid ones are a 400.

| endpoint | purpose |
|---|---|
| `GET /api/overview` | everything the Overview tab renders (§6.1) |
| `GET /api/filters` | facet values and bounds from `fe_meta` |
| `GET /api/releases` | filtered, sorted, paginated product releases |
| `GET /api/releases.csv` | the same set as CSV, one row per product release |
| `GET /api/companies` | filtered, sorted, paginated companies |
| `GET /api/companies.csv` | the same set as CSV, one row per company |
| `POST /api/refresh` | rebuild the read model |

**Release filters**: `q` (text search), `from` / `to` (`published_at`),
`category` (repeatable), `company` (company id), `org_type` (repeatable),
`is_public` (`true` / `false` / `unknown`), `ai` (`true` / `false`),
`min_confidence` (0–1), `sort` (`-published_at` default, `published_at`,
`-confidence`), `page`, `per_page` (default 24, max 200).

**Company filters**: `q` (name), `org_type` (repeatable), `is_public`
(`true` / `false` / `unknown`), `country` (repeatable), `has_ticker`,
`min_releases` / `max_releases` (product releases), `founded_from` /
`founded_to`, `sort` (`-n_product_releases` default, `name`,
`-last_product_release`, `founded_year`), `page`, `per_page`.

Both CSV endpoints accept the identical filter set, ignore `page`/`per_page`,
stream the full filtered set (cap 50,000 rows), and set a
`Content-Disposition` filename carrying the date and the served model version.

`releases.csv` columns:

```
release_id, title, url, published_at, category, dateline_city, dateline_region,
launch_confidence, is_ai, ai_confidence, launch_model_version, ai_model_version,
company_id, company_name, company_domain, company_org_type, company_is_public,
company_country, company_region, company_hq_city, company_ticker_exchange,
company_ticker_symbol, summary
```

`companies.csv` columns:

```
company_id, name, prn_url, domain, org_type, org_type_confidence, is_public,
is_public_confidence, is_public_method, ticker_exchange, ticker_symbol, country,
region, hq_city, founded_year, employees, revenue, revenue_unit, revenue_year,
sic, sic_desc, legal_form, wikidata_id, cik, lei, ror_id, ein, parent_name,
n_press_releases, n_product_releases, n_ai_releases, first_product_release,
last_product_release, top_categories, about
```

`top_categories` is a `slug:count` list joined by `;`. Empty values are empty
cells, not `None` or `null`.

---

## 6. The three tabs

### 6.1 Overview

Answers "what is in this database" at a glance.

**Stat tiles** (a row of four): product releases; companies represented; the
date range covered; share of releases flagged AI (drop this tile and rebalance
the row when no `ai` run is served).

**Releases over time** — the primary chart. Count of product releases by
`published_at`, with a month / quarter / year granularity toggle. When the `ai`
head is served, offer a second series or a stacked split for the AI share.

**Two supporting charts**:
- Releases by PRN category — horizontal bar, sorted, top 10 plus "other". (TODO: we may change this one out)
- Releases by company `org_type` — horizontal bar. (Most senders are
  `operating_company`; the interest is in the tail.)

**Latest releases** — the 10 most recent by `published_at`, compact: headline,
company, date, AI badge. Each links to the release; the company name links into
the Companies tab filtered to that company.

**Most active companies** — top 10 by `n_product_releases`, as a small table:
name, org type, product releases, last release. Rows link into Products
filtered by that company.

**Dataset provenance** — a compact panel at the bottom naming the served
`launch` and `ai` model versions, their `trained_at`, and their
`eval_accuracy` / `eval_roc_auc` / false-positive and false-negative rates from
`model_runs`. Researchers need this to cite the dataset and to reproduce a
slice; keep it factual and unobtrusive.

### 6.2 Products

One card per product release, in a responsive grid, 24 per page.

**Card**: headline (links to the PR Newswire release, new tab) · company name
(links to that company's card) · publication date · category badge · dateline
city/region · AI badge when `is_ai` · launch confidence rendered as a compact
indicator, not raw text · normalized summary, clamped to about four lines ·
ticker chips when present.

**Filter panel** (persistent sidebar on wide screens, collapsible above the
grid on narrow): full-text search; date range; category multi-select; company
search-and-select; AI toggle; minimum confidence slider; company `org_type`
multi-select; `is_public` tri-state. Show the active filters as removable chips
above the results with the result count and a "clear all".

**Sort**: newest, oldest, highest confidence.

**Export**: a "Download CSV" control that hits `/api/releases.csv` with the
active filters, labelled with the row count it will produce.

### 6.3 Companies

One card per company that has product releases. Same grid, filter panel, chip,
sort and export mechanics as Products.

**Card**:
- Name (links to the company's PR Newswire page) and domain (links out).
- **Public status**, three-valued and always shown: Public / Private /
  Unknown. `unknown` is the honest answer for a sender whose releases carry no
  concentrated ticker and which no registry matched, and it must be visually
  distinct from Private rather than collapsed into it. Surface
  `is_public_confidence` and `is_public_method` on hover or as small print, and
  show the ticker as `NASDAQ:CTSH` when there is one.
- `org_type` badge.
- Location: prefer enriched `hq_city` / `region` / `country`, falling back to
  `hq.city` / `hq.region`.
- Counts: product releases (primary), total press releases, AI releases; first
  and last product release dates.
- Category mix of that company's product releases — a few chips or a thin
  stacked bar.
- Registry facts when present: `founded_year`, `employees`, `revenue` **with
  its unit and year** (a bare number is meaningless — Toyota reports in JPY),
  `sic_desc`, `legal_form`, `parent_name`.
- External identifiers as links: Wikidata (`wikidata.org/wiki/<qid>`), SEC
  EDGAR (`sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<cik>`), GLEIF
  (`search.gleif.org/#/record/<lei>`), ROR (the `ror_id` is already a URL).
- `about.text`, truncated with an expand control.

**Filters**: name search; `org_type`; `is_public` tri-state including Unknown;
country; has-ticker; product-release count range; founded-year range.

**Sort**: most product releases, name A–Z, most recent release, founding year.

---

## 7. Cross-cutting requirements

- **Filter state lives in the URL.** Every filter, sort and page on Products
  and Companies is a query parameter, so a researcher can bookmark and share a
  slice: `/products?q=robot&category=consumer-technology&ai=true&sort=-confidence&page=2`.
  Back/forward must restore state. Put this in one composable used by both
  views; do not duplicate the sync logic.
- **Debounce** text inputs (~300 ms). Cancel superseded in-flight requests.
- **Loading and empty states** on every list and chart — skeletons or a spinner,
  never a flash of "0 results" while a request is in flight. An empty result
  set says which filter to relax.
- **Errors** surface as a retryable message, not a blank page.
- **Responsive**: the card grid goes 3 → 2 → 1 columns; the filter panel
  collapses on narrow screens.
- **Accessible**: real `<button>`/`<a>` elements, labelled form controls,
  keyboard-navigable filters, visible focus, and chart data also reachable as
  text (a table or `aria-label` summary).
- **Charts**: one coherent palette from the CSS tokens, categorical colors that
  stay distinguishable, axes labelled with units, no gridline clutter, no pie
  charts with more than five slices, tooltips on hover.
- **Deep links**: a company card is reachable at a stable URL, and the Products
  tab accepts `?company=<id>`.

---

## 8. Non-goals

- No authentication, no accounts, no writes to any pipeline collection.
- No labelling, training, or inference — that is what `py/label/` and
  `py/classify/` are for, and this app must not import them.
- No browsing of unclassified articles or of senders without product releases.
- No re-implementation of `enrichment_fields`, the label collapse, or the fact
  extractors. If a field is not on `companies`, it is not available here.
- No server-side rendering, no Tailwind or component library, no state
  management library, no Python dependencies beyond those already pinned.

---

## 9. Acceptance checks

Run against a live Mongo. Counts move as the classifiers work, so assert
agreement between the API and the database rather than fixed numbers.

1. `POST /api/refresh` completes, and `fe_releases` count equals

   ```javascript
   v = db.model_runs.find({task:"launch"}).sort({trained_at:-1}).limit(1)[0]._id
   db.predictions.countDocuments({model_version: v, predicted_class: 1})
   ```

2. `fe_companies` count equals the number of distinct `uploader_link` values
   across those articles, and no `fe_companies` document has
   `n_product_releases: 0`.
3. `/api/overview` totals match tiles 1 and 2; the time series sums to the
   release total.
4. Products: each filter alone changes the count in the expected direction;
   two filters compose; `total` matches the number of cards paged through;
   sorting by confidence puts the highest first; a shared URL reproduces the
   same result set in a fresh tab.
5. Companies: the `is_public` filter's three states partition the set exactly
   (`true` + `false` + `unknown` = total), and Unknown companies render as
   Unknown rather than Private.
6. `releases.csv` and `companies.csv` download with the documented headers,
   one row per release / per company, row counts matching the on-screen
   `total`, and correct quoting for headlines and About text containing commas,
   quotes and newlines.
7. The app renders correctly when the served `ai` run does not exist: no AI
   badge, no AI filter, no AI tile, no console errors, and `is_ai` columns
   present but empty in the CSV.
8. A company with no enrichment (no `wikidata_id`, `founded_year`, `revenue`)
   renders a clean card with those rows omitted, not with blanks or `null`.
9. `docker compose up -d web` serves the built SPA and the API on 8080, and
   client-side routes deep-link correctly on a hard refresh.

---

## 10. Documentation to update

- `fe/README.md` — what the app is, how to run it in dev and in Compose, the
  read model and its refresh, and the API reference including the CSV schemas.
- Root `README.md` — a "Browse the data" section between "Run inference" and
  "Stop everything", with the Compose command and the URL.
- `docker-compose.yml` — the `web` service, commented in the style of the
  services around it.
