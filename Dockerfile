FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY strategy_lab/ strategy_lab/

# Baked in at build time: the container has no .git to read it from, and a row that cannot
# say which build wrote it makes a Hit Rate spanning weeks impossible to attribute.
ARG STRATEGY_LAB_VERSION=unknown
ENV STRATEGY_LAB_VERSION=${STRATEGY_LAB_VERSION}

ENV STRATEGY_LAB_DB=/data/lab.db
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "strategy_lab.collector"]
