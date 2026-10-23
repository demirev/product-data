# The explorer: the Vue SPA built by stage one, served by the FastAPI app in
# stage two, which also answers /api. Build context is the repo root, because
# stage two needs py/ as well as this directory.
#
# Deliberately not built on the pipeline image: this process never loads a
# model. It reads predictions the classifiers already wrote, so it has no use
# for torch, transformers or datasets — several GB it would otherwise carry.

FROM node:22-alpine AS spa
WORKDIR /build
COPY fe/package.json fe/package-lock.json ./
RUN npm ci
COPY fe/ ./
RUN npm run build


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1 \
	PYTHONPATH=/app/py

WORKDIR /app

# Versions pinned to match requirements.txt; the API needs no more than these.
RUN pip install --upgrade pip && pip install \
	fastapi==0.115.6 \
	starlette==0.41.3 \
	uvicorn==0.32.1 \
	pymongo \
	python-multipart==0.0.12

COPY py/ /app/py/
COPY --from=spa /build/dist /app/fe/dist

EXPOSE 8080
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8080"]
