FROM python:3.15-slim
RUN apt-get update && apt-get install --yes --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir psycopg==3.3.9
ARG VERSION
ARG CALL_OUT_URL
ENV PRETEND_VERSION=$VERSION PRETEND_CALL_OUT_URL=$CALL_OUT_URL PYTHONUNBUFFERED=1
COPY app.py /app.py
CMD ["python", "/app.py", "web"]
