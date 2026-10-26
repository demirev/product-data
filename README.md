# product-data

PRNewswire press-release pipeline: scrape → label → train → classify → browse.
Services run under Docker Compose against one MongoDB (database `prn`). Two
binary classifiers: `launch` (is this a new product launch?) and `ai` (is
generative AI being shipped?); `ai` scores only launch-positives.

- [`REFERENCE.md`](REFERENCE.md): services, collections, label classes, field
  schemas, decision rules
- [`AGENT_NOTES.md`](AGENT_NOTES.md): design rationale, measurements, incidents
- [`fe/README.md`](fe/README.md): explorer API and CSV schemas

## Setup

```bash
docker compose build                                                    # again after changing requirements.txt / Dockerfile
docker compose --profile labeling run --rm labeler python py/vocab.py   # once: backfill vocab flags (resumable)
```

## Scrape

```bash
docker compose up -d mongo link-scraper article-scraper article-scraper-1
docker compose logs -f link-scraper article-scraper article-scraper-1
```

`scrape_prn.py` flags: `--no-links`, `--no-articles`, `--start-date YYYY-MM-DD`,
`--end-date YYYY-MM-DD`, `--categories a,b`, `--loop`, `--idle-sleep SECONDS`,
`--rollup-when-idle`, `--workers N`, `--shard K/N`.

Compose runs two shards (`--shard K/N`) of four workers. When changing `N`,
change every replica's `K/N` together. After raising concurrency, watch for
`403` in `last_status`.

## Back-fill older releases

PRN's dated listings reach back 365 days, so never set `--start-date` more than a
year back. For older months, find URLs from the sitemaps, then Wayback for months
that come up short. Both write `pending` stubs that `article-scraper` fills in.

```bash
python py/scrape/scrape_sitemap.py --list                    # survey month counts, no writes
python py/scrape/scrape_sitemap.py --from 2025-03 --to 2025-10
python py/scrape/scrape_sitemap.py --months 2024-05,2024-06
python py/scrape/scrape_sitemap.py --all                     # every month in the index

python py/scrape/scrape_wayback.py --months 2025-06,2025-08,2025-09   # slow: minutes per month
python py/scrape/scrape_wayback.py --from 2024-06 --to 2024-07
```

Flags: `--dry-run`, `--sleep` (both); `--pad-days`, `--timeout` (Wayback).

## Company data

Runs automatically while scraping. Re-run by hand only after an extractor version
bump:

```bash
docker compose --profile company run --rm company-facts                                                  # per-release facts
docker compose --profile company run --rm company-facts python py/company.py --rollup                    # rebuild companies
docker compose --profile company run --rm company-facts python py/company.py --rollup --no-enrichment    # heuristics only
```

## External enrichment

`ENRICH_CONTACT` is required (sent as the contact address in the `User-Agent`);
set it in your shell or `.env`.

```bash
export ENRICH_CONTACT="you@example.com"
docker compose up -d enrich                                                         # continuous, resumable
docker compose run --rm enrich python py/enrich.py --refresh                        # force a bulk re-download
docker compose run --rm enrich python py/enrich.py --stats                          # match rates
docker compose run --rm enrich python py/enrich.py --retry-misses                   # re-ask recorded non-matches
docker compose run --rm enrich python py/enrich.py --only sec,gleif --limit 100
docker compose run --rm enrich python py/enrich.py --companies /news/veeva-systems/
```

Other flags: `--no-resolve`, `--loop`, `--idle-sleep SECONDS`.

## Label

```bash
docker compose --profile labeling up -d labeler
```

| URL | what |
|---|---|
| http://localhost:8000 | launch labeler |
| http://localhost:8000/ai | AI labeler |
| http://localhost:8000/review/launch | review LLM labels from `data/labels-for-review/` |
| http://localhost:8000/review/ai | review LLM labels from `data/ai-labels-for-review/` |

Set your name in `LABELER_NAME` in `docker-compose.yml`. Class definitions are in
[`REFERENCE.md`](REFERENCE.md#labelling).

Convert legacy launch labels (`is_relevant` / `has_product_details`), once:

```bash
venv/bin/python py/label/migrate_launch_class.py --dry-run
venv/bin/python py/label/migrate_launch_class.py
```

## Train

```bash
docker compose --profile training run --rm train      # launch -> /models/launch/<version>
docker compose --profile training run --rm ai-train   # ai     -> /models/ai/<version>

docker compose --profile training run --rm ai-train \
  python py/classify/finetune.py --task ai --epochs 5 --batch-size 8
```

Flags: `--task {launch,ai}`, `--epochs` (default 10), `--batch-size` (default 8),
`--model-name` (default `distilbert-base-uncased`). Each run trains from the base
model; re-run after adding labels.

## Classify

```bash
docker compose up -d classifier      # all scraped articles
docker compose up -d ai-classifier   # only launch-positives
```

Flags: `--classify`, `--task`, `--loop`, `--idle-sleep SECONDS`,
`--model-version` (or `MODEL_VERSION`), `--require-relevant-from`, `--overwrite`,
`--batch-size`. The newest run is served unless `MODEL_VERSION` is set in
`docker-compose.yml`.

## Browse

```bash
docker compose up -d web      # http://localhost:8080
```

Read-only unless `EXPLORER_LABELER` is set in `.env`, which adds false-positive
and blacklist controls.

## Stop

```bash
docker compose down           # add -v to also drop the mongo_data, models and enrich_data volumes
```
