#!/usr/bin/with-contenv bashio

MAX_CONCURRENT=$(bashio::config 'max_concurrent')
export MAX_CONCURRENT="${MAX_CONCURRENT:-8}"

exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --log-level info
