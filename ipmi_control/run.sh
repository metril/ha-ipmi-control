#!/usr/bin/with-contenv bashio

MAX_CONCURRENT=$(bashio::config 'max_concurrent')
export MAX_CONCURRENT="${MAX_CONCURRENT:-8}"

PORT=8099
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --log-level info
