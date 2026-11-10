# export

Scores the whole corpus off-box. Run from the repo root unless noted.

`LAUNCH` and `AI` name the runs to score: the newest in `model_runs` for each
task. Each run's outputs go to `out/<run>`, because `score.py` skips any shard
whose output file already exists.

## 1. Export (local)

```bash
LAUNCH=distilbert-finetuned-20261030003448 AI=distilbert-ai-20261026190610
venv/bin/python py/export/export_articles.py data/export/articles
sudo docker compose --profile training run --rm --entrypoint tar train \
  -C /models/launch -cf - $LAUNCH > data/export/launch-model.tar
sudo docker compose --profile training run --rm --entrypoint tar ai-train \
  -C /models/ai -cf - $AI > data/export/ai-model.tar
cp py/export/score.py data/export/
rsync -av --progress data/export/ \
  nrm8302@ryaninstitute.quest.northwestern.edu:/kellogg/proj/dashun/nrm8302/product-data/data/export/
```

## 2. Score (server, in tmux)

```bash
LAUNCH=distilbert-finetuned-20261030003448 AI=distilbert-ai-20261026190610
cd /kellogg/proj/dashun/nrm8302/product-data/data/export
for f in *.tar; do tar xf $f; done
python3 -m venv ~/prn && ~/prn/bin/pip install torch==2.13.0 transformers==5.15.1 pandas pyarrow  # Python 3.10+
~/prn/bin/python -c "import torch; print(torch.cuda.is_available())"

# GPU
for k in 0 1; do ~/prn/bin/python score.py $LAUNCH articles out/$LAUNCH --part $k/2 > launch.$k.log 2>&1 & done; wait
# CPU: N = cores / 4; RAYON caps the tokenizer, MKL the matrix library
# N=8; for k in $(seq 0 $((N-1))); do OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 RAYON_NUM_THREADS=4 ~/prn/bin/python score.py $LAUNCH articles out/$LAUNCH --part $k/$N > launch.$k.log 2>&1 & done; wait

~/prn/bin/python score.py $AI articles out/$AI-gated-$LAUNCH --gate out/$LAUNCH
```

Re-running a command skips finished shards. The AI output directory names its
gate as well, since the same AI run scores a different set of articles behind
each launch run.

## 3. Import (local)

```bash
LAUNCH=distilbert-finetuned-20261030003448 AI=distilbert-ai-20261026190610
rsync -av nrm8302@ryaninstitute.quest.northwestern.edu:/kellogg/proj/dashun/nrm8302/product-data/data/export/out/{$LAUNCH,$AI-gated-$LAUNCH} data/export/out/
venv/bin/python py/export/import_predictions.py data/export/out/$LAUNCH $LAUNCH
venv/bin/python py/export/import_predictions.py data/export/out/$AI-gated-$LAUNCH $AI
```

Then start `classifier` and `ai-classifier`; they score only articles scraped after the export.
