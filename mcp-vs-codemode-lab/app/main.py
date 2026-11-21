"""Tiny shop API used as the benchmark target."""
import logging
import os
import sys
import time

import psycopg2
from flask import Flask, jsonify, request

logging.basicConfig(
    stream=sys.stdout,
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [shop-api] %(message)s",
)
log = logging.getLogger("shop-api")

DB = dict(
    host=os.environ.get("DB_HOST", "db"),
    port=int(os.environ.get("DB_PORT", "5432")),
    dbname=os.environ.get("DB_NAME", "lab"),
    user=os.environ.get("DB_USER", "appuser"),
    password=os.environ.get("DB_PASSWORD", ""),
    connect_timeout=3,
    application_name="shop-api",
)
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "3000"))

app = Flask(__name__)


def connect():
    return psycopg2.connect(**DB, options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")


def query(sql, params=None):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


@app.after_request
def access_log(resp):
    took = (time.perf_counter() - request.environ.get("t0", time.perf_counter())) * 1000
    log.info("%s %s -> %s (%.0f ms)", request.method, request.full_path.rstrip("?"), resp.status_code, took)
    return resp


@app.before_request
def start_timer():
    request.environ["t0"] = time.perf_counter()


@app.get("/")
def index():
    return jsonify(service="shop-api", endpoints=["/health", "/items", "/report"])


@app.get("/health")
def health():
    try:
        query("SELECT 1")
        return jsonify(status="ok", db="ok")
    except Exception as e:
        log.error("health check failed: database error: %s", str(e).strip())
        return jsonify(status="degraded", db="error", detail=str(e).strip()), 503


@app.get("/items")
def items():
    limit = min(int(request.args.get("limit", 20)), 500)
    try:
        rows = query("SELECT id, name, price FROM items ORDER BY id LIMIT %s", (limit,))
        return jsonify(items=[{"id": r[0], "name": r[1], "price": float(r[2])} for r in rows])
    except Exception as e:
        log.error("GET /items failed: %s: %s", type(e).__name__, str(e).strip())
        return jsonify(error="database error", detail=str(e).strip()), 500


@app.get("/report")
def report():
    """Builds a sales report fully in memory (deliberately memory hungry)."""
    mb = int(request.args.get("mb", 10))
    log.info("building in-memory report (~%d MB)", mb)
    chunks = [bytearray(1024 * 1024) for _ in range(mb)]
    return jsonify(report="ok", size_mb=len(chunks))
