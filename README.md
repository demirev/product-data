# product-data

PRNewswire press-release pipeline: scrape → label → train → classify. All services run via Docker Compose against a shared MongoDB.

## First-time setup

```bash
docker compose build
```

Rebuild after changes to `requirements.txt` or `Dockerfile`.

## Run the scrapers

Starts MongoDB plus both scrapers (link discovery + article fetching) in a continuous loop:

```bash
docker compose up -d mongo link-scraper article-scraper
```

Logs:

```bash
docker compose logs -f link-scraper article-scraper
```

## Label articles

Web UI for building the `labels` collection. Profile-gated so it doesn't run by default.

```bash
docker compose --profile labeling up -d labeler
# open http://localhost:8000
```

Set the labeler name by editing `LABELER_NAME` in `docker-compose.yml`.

## Train the classifier

One-shot job. Reads `labels` + `articles` from Mongo, writes a checkpoint to the `models` volume. Requires network access on first run (downloads `distilbert-base-uncased`).

```bash
docker compose --profile training run --rm train
```

Optional flags (append after `train`):

```bash
docker compose --profile training run --rm train \
  python py/classify/finetune.py --output-dir /models --epochs 5 --batch-size 8
```

## Run inference

Continuous service. Waits for a checkpoint at `/models`, then classifies any `articles` with `status="scraped"` that don't yet have a prediction for the current `MODEL_VERSION`, looping forever.

```bash
docker compose up -d classifier
```

Change the model version by editing `MODEL_VERSION` in `docker-compose.yml`.

## Stop everything

```bash
docker compose down
```

Add `-v` to also drop the `mongo_data` and `models` volumes.
