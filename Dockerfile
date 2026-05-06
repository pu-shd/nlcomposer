FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --upgrade pip && pip install -r requirements-dev.txt

COPY . .

EXPOSE 8000

# Default to running the app; tests can be triggered via:
#   docker-compose run --rm app pytest -q
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
