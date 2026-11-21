"""Startup: verify the database is reachable, then serve. Exits non-zero if the DB is unreachable."""
import sys
import time

from waitress import serve

from main import DB, app, connect, log

ATTEMPTS = 3

log.info("starting shop-api, database host=%s port=%s db=%s", DB["host"], DB["port"], DB["dbname"])
for attempt in range(1, ATTEMPTS + 1):
    try:
        connect().close()
        log.info("database connection OK")
        break
    except Exception as e:
        log.error("database connection attempt %d/%d failed: %s", attempt, ATTEMPTS, str(e).strip())
        time.sleep(1)
else:
    log.critical("cannot reach database at %s:%s, exiting", DB["host"], DB["port"])
    sys.exit(1)

log.info("listening on 0.0.0.0:8000")
serve(app, host="0.0.0.0", port=8000, threads=8, _quiet=True)
