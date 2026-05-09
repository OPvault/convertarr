"""`convertarr-worker` — remote worker entrypoint.

This package is intentionally kept distinct from `convertarr.workers/`,
which holds the host-side job machinery. A worker process imports the
shared pieces it needs (`hwdetect`, `plan`, `runner`, `execution.JobDispatch`)
but does not require the host's `models`, `db`, or `web` modules — it
talks to the host purely over HTTP.
"""
