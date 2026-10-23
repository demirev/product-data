# Product Release Explorer

A browser over what the pipeline produced: the **product releases** —
press releases the served `launch` model called relevant — joined to the
companies that sent them. A Vue 3 SPA in this directory, a FastAPI JSON API in
[`py/api/`](../py/api), both served by one container on port 8080.

Four tabs:

| tab | answers |
|---|---|
| **Overview** | what is in this database — counts, coverage, releases over time, category and sender mix, and the model runs behind all of it |
| **Products** | one card per product release, filterable and exportable |
| **Companies** | one card per sender that has at least one product release, same mechanics |
| **Blacklist** | senders left out of the other tabs; shown only with [`EXPLORER_LABELER`](#marking-false-positives) set |

Nothing here loads a model or re-implements pipeline logic. It reads what the
classifiers and the company rollup already wrote, and it writes to two pipeline
collections, `labels` and `sender_blacklist`, only when
[marking false positives](#marking-false-positives) is switched on. See the root [`README.md`](../README.md) for how any of that got
there.

---

## Running it

**Development** — two processes against your local Mongo:

```bash
MONGO_URI=mongodb://localhost:27017 PYTHONPATH=py venv/bin/uvicorn api.app:app --reload --port 8080
cd fe && npm install && npm run dev      # 5173; vite.config.js proxies /api -> 8080
```

Open http://localhost:5173. The API also serves the built SPA when `fe/dist`
exists, so `npm run build` and then browsing to 8080 exercises the production
path without Docker.

**Compose** — one service, built from [`Dockerfile`](Dockerfile):

```bash
docker compose up -d web     # http://localhost:8080
```

The image is deliberately *not* the pipeline image: this process never loads a
model, so it installs `fastapi`, `starlette`, `uvicorn`, `pymongo` and
`python-multipart` and leaves out torch, transformers and datasets.

Both images build with the repo root as their context, so the excludes live in
the root [`.dockerignore`](../.dockerignore) — Docker does not read one from
`fe/`.

---

## The read model

A product release is not a stored thing. It is an article with a `predictions`
document for the served `launch` version and `predicted_class: 1`, joined to
`articles` for its text and to `companies` for its sender. At 10⁵ releases,
resolving that per request would be 10⁵ lookups per page view.

So the API owns three denormalized collections, prefixed `fe_` so their
ownership is unambiguous:

| collection | one document per | notes |
|---|---|---|
| `fe_releases` | product release | title, summary, dateline, tickers, launch confidence, `is_ai`, plus the `company_*` fields the Products filters sort and filter on |
| `fe_companies` | company with ≥ 1 product release | the whole `companies` document plus `n_product_releases`, `n_ai_releases`, first/last product release, and a category mix |
| `fe_meta` | — | the served runs and their metrics, the build stamp, and the facet values the filter panels need |

**They are a cache.** Derived entirely from `predictions`, `articles`,
`companies` and `model_runs`; safe to drop at any moment; rebuilt by the next
refresh. Dropping them is the supported way to force a clean rebuild:

```javascript
db.fe_releases.drop(); db.fe_companies.drop(); db.fe_meta.drop()
```

### Which model is served

The newest `model_runs` document per task — `find({task}).sort({trained_at: -1})`
— for `launch` and for `ai` independently. `model_runs` is this app's authority
on that; it does not mount the models volume the classifiers resolve against.

Class names are read off the run's `classes` array rather than hardcoded. Index
1 is the positive class on both heads, which is the invariant the whole pipeline
rests on.

### `is_ai` is three-valued

The `ai` head runs only over `launch` positives and lags behind them, and it may
not have been trained at all. A release therefore has `is_ai: true`, `false`, or
**`null`** — meaning *not yet scored*, never *not AI*. When no `ai` run is
served the app drops every AI affordance: no badge, no filter, no tile, no
series in the time chart, and empty `is_ai` / `ai_confidence` / `ai_model_version`
columns in the CSV.

### Refresh

`readmodel.refresh()` is the single entry point. It runs on startup, every ten
minutes from a background task, and on `POST /api/refresh`. It is safe to call
concurrently — a caller arriving mid-build gets `{"status": "busy"}` rather than
a second build.

- **Full rebuild** when the served version of either head has moved (every
  release is keyed to the `launch` run that selected it, and every `is_ai` to the
  `ai` run that produced it), or when `fe_meta` is missing.
- **Incremental** otherwise: predictions with `predicted_at` newer than the last
  build, merged over what is there.

Both run server-side as one aggregation per collection — including the
normalization, so a rebuild never streams 10⁵ release bodies through Python.
`fe_companies` is regrouped from `fe_releases` whole every time; it is two orders
of magnitude smaller than `articles` and not worth an incremental path.

It can also be run on its own, without the server:

```bash
MONGO_URI=mongodb://localhost:27017 PYTHONPATH=py venv/bin/python py/api/readmodel.py --force
```

**Known gap.** The incremental path matches on `predicted_class: 1`, so a
prediction that flipped from positive to negative *under the same version*
leaves a stale release behind until the next full rebuild. Re-scoring under one
version is not something the pipeline does — a retrain gets a new version, which
forces a full rebuild — so this only matters if you rewrite predictions by hand.

### Normalization

Done once, here, never in the browser:

- **Headlines and company names** — `\s+` collapsed to single spaces; scraped
  headers end in runs of tabs and newlines.
- **Summary** — the body cut at the first `Continue Reading` occurring after the
  first 200 characters (what follows is image-caption furniture), whitespace
  collapsed, truncated to 400 characters on a word boundary.
- **URLs** — `https://www.prnewswire.com` prefixed onto the site-relative
  article `url` and company `_id`. Company ids are already percent-encoded
  (`/news/garmin-international%2C-inc./`) and are not re-encoded.
- **Datetimes** — ISO-8601 with an explicit `Z`.

### Deduplication

Some senders file the same release under several PRN categories, and PRN gives
each copy its own slug — so the copies are distinct articles carrying distinct
predictions. `fe_releases` keeps one per (normalized headline, sender); the
earliest published copy wins.

This is why `fe_releases` holds *fewer* documents than the served run has
positives. The count to compare against is:

```javascript
v = db.model_runs.find({task: "launch"}).sort({trained_at: -1}).limit(1)[0]._id
db.predictions.aggregate([
  {$match: {model_version: v, predicted_class: 1}},
  {$lookup: {from: "articles", localField: "article_id", foreignField: "_id", as: "a"}},
  {$unwind: "$a"},
  {$group: {_id: {h: {$toLower: "$a.header"}, u: "$a.uploader_link"}}},
  {$count: "n"},
])
```

Near-duplicates whose headlines differ — PRN's multi-language variants append
`USA - English USA - Deutsch …` to the headline — are *not* collapsed, because
they are not the same string. Widening that would mean fuzzy matching, which is
a pipeline concern rather than a serving one.

### Indexes

Created by `readmodel.ensure_indexes` alongside the collections they sit on;
`py/db.py`'s `ensure_indexes` stays responsible for pipeline collections. The one
addition made there is `predictions {model_version, predicted_class}`, which
serves the second-stage classifier's gate as much as it serves this app.

---

## Marking false positives

With `EXPLORER_LABELER` set, every release card carries a **False positive…**
menu. Choosing an entry writes a launch label for that article to `labels`, the
collection the `launch` classifier trains on, so the correction is part of the
next training run.

With the variable unset, which is the default, the cards have no menu,
`/api/releases` does no label lookup and `POST /api/labels` answers `404`. Leave
it unset on a public deployment: the app has no authentication, so while it is
set anyone who can reach the port can write labels.

Compose reads the value from `.env`; in development, export it for the `uvicorn`
process.

```bash
echo 'EXPLORER_LABELER=georgi' >> .env && docker compose up -d web
```

The menu offers the launch labeler's three negative decisions, stored as the same
`launch_class` values:

| menu entry | `launch_class` |
|---|---|
| Feature release | `3` |
| Product info, no launch | `2` |
| Irrelevant | `0` |

A label written here has the labeler's shape (`launch_class`, `labeler`,
`labeled_at`) plus `source: "explorer"`, `sample_method: "predicted_positive"`
and the `model_version` of the served `launch` run that made the call. It
replaces any label the article already had.

A marked card is dimmed and its menu shows the label. **Undo** deletes the label,
and only a label with `source: "explorer"`. Labels are joined onto each page of
`/api/releases` at request time, so a mark shows immediately and the read model
holds no label state. A marked release stays in the explorer until the served run
stops calling it positive.

### Sender blacklist

`sender_blacklist` holds senders whose releases are never product launches
(market research resellers, analyst firms, regulators, inventor-licensing and
software SEO feeds). The read model leaves their releases out, so they are absent
from every tab, count and export. The classifier still scores them, and labels on
their articles still train it.

One document per sender, keyed by `uploader_link`:

```javascript
{_id: "/news/reportlinker/", name: "Reportlinker", reason: "market research report reseller",
 source: "seed" | "explorer", added_by: "georgi", added_at: ISODate(...)}
```

With `EXPLORER_LABELER` set, release and company cards carry a **Blacklist
sender** button, which asks for an optional reason, and a **Blacklist** tab lists
every entry with a **Remove** button. Blacklisting deletes the sender's
`fe_releases` and `fe_companies` documents at once. Every change also starts a
refresh, queued behind any build already running, which brings the counts up to
date and re-reads a removed sender's releases by article.

---

## HTTP API

All responses are JSON. List endpoints answer
`{"items": [...], "total": n, "page": p, "per_page": k}`. Unknown query
parameters are ignored; invalid ones are a `400` with a `detail` string.

| endpoint | purpose |
|---|---|
| `GET /api/overview` | everything the Overview tab renders |
| `GET /api/filters` | facet values, date bounds, the served runs, and `labeling` (whether `EXPLORER_LABELER` is set) |
| `GET /api/releases` | filtered, sorted, paginated product releases |
| `GET /api/releases.csv` | the same set as CSV |
| `GET /api/companies` | filtered, sorted, paginated companies |
| `GET /api/companies.csv` | the same set as CSV |
| `POST /api/refresh` | rebuild the read model |
| `POST /api/labels` | body `{"article_id", "launch_class"}` with `launch_class` one of `0`, `2`, `3`, or `null` to undo; `404` unless `EXPLORER_LABELER` is set |
| `GET /api/blacklist` | the sender blacklist, newest first; `404` unless `EXPLORER_LABELER` is set |
| `POST /api/blacklist` | body `{"company_id", "blacklisted", "reason"}`; `blacklisted: false` removes the sender; `404` unless `EXPLORER_LABELER` is set |

Everything else falls through to the SPA's `index.html`, so `/products` and
`/companies` survive a hard refresh. An unmatched `/api/...` path is a 404, not
the SPA shell.

### Release filters

| parameter | values |
|---|---|
| `q` | full-text over title and summary |
| `from`, `to` | ISO date or datetime on `published_at`; a bare date as `to` means the whole of that day |
| `category` | PRN category slug, repeatable |
| `company` | company id (`/news/acme/`) |
| `org_type` | repeatable |
| `is_public` | `true` / `false` / `unknown` — the three partition the set exactly |
| `ai` | `true` / `false`; neither matches an unscored release |
| `min_confidence` | 0–1 |
| `sort` | `-published_at` (default), `published_at`, `-confidence` |
| `page`, `per_page` | `per_page` defaults to 24, maximum 200 |

### Company filters

| parameter | values |
|---|---|
| `q` | case-insensitive substring of the name |
| `company` | company id — one company at a stable URL, which is what release cards link to |
| `org_type`, `country` | repeatable |
| `is_public` | `true` / `false` / `unknown` |
| `has_ticker` | `true` / `false` |
| `min_releases`, `max_releases` | product releases, inclusive |
| `founded_from`, `founded_to` | `founded_year`, inclusive |
| `sort` | `-n_product_releases` (default), `name`, `-last_product_release`, `founded_year` |
| `page`, `per_page` | as above |

`company` is the one parameter not in the original brief's list. It exists
because a company card has to be reachable at a stable URL, and its name is not
a stable key.

### CSV export

Both CSV endpoints take the identical filter set, ignore `page` and `per_page`,
stream the full filtered set (capped at 50,000 rows) and set a
`Content-Disposition` filename carrying the export date and the served `launch`
version — `product-releases-2026-08-29-distilbert-finetuned-20260828161251.csv` —
so a downloaded slice stays citable.

Empty values are empty cells. Booleans are `true`/`false`. Datetimes are
ISO-8601 UTC. Quoting is `csv.writer`'s, so headlines and About text containing
commas, quotes and newlines round-trip.

`releases.csv`:

```
release_id, title, url, published_at, category, dateline_city, dateline_region,
launch_confidence, is_ai, ai_confidence, launch_model_version, ai_model_version,
company_id, company_name, company_domain, company_org_type, company_is_public,
company_country, company_region, company_hq_city, company_ticker_exchange,
company_ticker_symbol, summary
```

`companies.csv`:

```
company_id, name, prn_url, domain, org_type, org_type_confidence, is_public,
is_public_confidence, is_public_method, ticker_exchange, ticker_symbol, country,
region, hq_city, founded_year, employees, revenue, revenue_unit, revenue_year,
sic, sic_desc, legal_form, wikidata_id, cik, lei, ror_id, ein, parent_name,
n_press_releases, n_product_releases, n_ai_releases, first_product_release,
last_product_release, top_categories, about
```

`top_categories` is a `slug:count` list joined by `;`. `revenue` is meaningless
without `revenue_unit` and `revenue_year` — Toyota reports in JPY — so the three
travel together, in the CSV and on the card.

---

## The browser app

```
src/
  main.js  App.vue  router.js  api.js  charts.js  styles.css
  views/       OverviewView.vue  ProductsView.vue  CompaniesView.vue  BlacklistView.vue
  components/  ReleaseCard.vue  CompanyCard.vue  FilterPanel.vue  FilterChips.vue
               StatTile.vue  Badge.vue  Pagination.vue  TimeSeries.vue  BarChart.vue
  composables/ useQueryState.js  useFetch.js
```

Vue 3 `<script setup>`, `vue-router` in history mode, `chart.js` via
`vue-chartjs`, plain CSS. No state-management library, no component framework.

**Filter state lives in the URL and nowhere else.** `useQueryState` derives the
filters from `route.query`, so the query string *is* the API's parameter set:
what the address bar says is literally what is sent. Bookmarking, sharing and
back/forward all work because there is no second copy of the state to fall out
of step.

```
/products?q=robot&category=consumer-technology&sort=-confidence&page=2
```

**`FilterPanel` is one component driven by data.** The two tabs' filter sets
differ in which fields they have, not in how a field behaves, so each view passes
a list of descriptors (`{kind, key, label, options}`) rather than its own panel.

**Requests are cancellable.** `useFetch` aborts a superseded request, so an
out-of-order response can never paint the previous filter's results under the
current chips. Text inputs and the confidence slider commit on a 300 ms
debounce, and with `router.replace`, so typing does not fill the history.

**Charts** read their colors from the CSS custom properties in `styles.css`, so
there is one palette rather than a light one and a dark one drifting apart. The
categorical steps are validated for colorblind separation, chroma and contrast
against both surfaces. Every chart also publishes its data as a table behind a
`Show the numbers` disclosure, and a month with no releases is drawn as zero
rather than dropped — the back-fill leaves real gaps in the corpus, and a
category axis would otherwise hide them.

**Three-valued public status.** `Unknown` renders as its own thing — dashed
outline, no fill — never as a paler `Private`. It is the honest answer for a
sender whose releases carry no concentrated ticker and which no registry
matched, and the card shows the deciding method and confidence beneath it.

---

## Checking it

The brief's acceptance checks are mechanical; the two scripts that ran them are
not kept in the tree, but the checks themselves are worth repeating after a
schema change:

1. `fe_releases` matches the served run's positives, modulo the dedupe query above.
2. `fe_companies` is exactly the distinct senders of those releases, and no row
   has `n_product_releases: 0`.
3. `/api/overview` totals match, and the time series sums to the release total.
4. Each release filter narrows the set; two compose; `total` matches the number
   of cards paged through; `-confidence` sorts descending; a shared URL
   reproduces the same set.
5. `is_public`'s three states partition both collections exactly.
6. Both CSVs carry the documented headers, one row each, matching the on-screen
   `total`, with correct quoting.
7. With no `ai` run served: no badge, no filter, no tile, no console errors, and
   empty `is_ai` columns in the CSV.
8. A company with no enrichment renders a clean card — the absent rows are
   omitted, not blank or `null`.
9. `docker compose up -d web` serves the SPA and the API on 8080, and
   `/products` deep-links on a hard refresh.
