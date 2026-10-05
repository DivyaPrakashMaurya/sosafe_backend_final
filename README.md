# SoSafe backend

Trip-planning lookups for the SoSafe app, in front of Ola Maps: place search, a
place's coordinates, the address of a spot, and a route with its safety
check-in schedule. Everything else (SOS, chat, contacts, alerts) lives in
Firebase and its Cloud Functions, not here.

## Layout

```
app/
  main.py            app factory: settings, shared clients, middleware, routes
  config.py          every setting, from the environment (.env.example lists them)
  api/
    v1/location.py   GET /api/v1/location/{search,coordinates,reverse}
    v1/route.py      POST /api/v1/route/calculate
    health.py        /, /healthz, /readyz
    deps.py          who is calling (sign-in), rate limits
  services/places.py the four lookups: cache first, then Ola
  clients/ola.py     Ola Maps: one async connection pool, timeouts, one retry, circuit breaker
  domain/route_schedule.py  total time + check-in schedule (the numbers the phone's timers use)
  cache.py           Redis (shared) or memory; one Ola call for many identical requests
  rate_limit.py      per person (signed in) or per network address
  auth.py            Firebase ID tokens, checked against Google's public keys
  errors.py          one error shape for everything
  middleware.py      request id, body size limit, access log
  log.py             JSON logs, nothing personal, never the Ola key
tests/               pytest, against pretend Ola / Redis / Google
loadtest/            in_process.py (the app alone), run_load.py (a running server)
render.yaml          Render: web service (2-6 instances) + Key Value (Redis)
Dockerfile           the same server for any container host
main.py              only so an old `uvicorn main:app` start command still works
```

## API

The success bodies are the ones the app has always read. Errors are
`{"status": "error", "error": {"code", "message"}, "request_id"}`.

| Request | Success | Errors |
|---|---|---|
| `GET /api/v1/location/search?query=` (1-200 chars) | `{"status":"success","results":[{"place_name","place_id"}]}` | 422 |
| `GET /api/v1/location/coordinates?place_id=` | `{"status":"success","latitude","longitude"}` | 404 `place_not_found` |
| `GET /api/v1/location/reverse?lat=&lng=` | `{"status":"success","address"}` | 404 `address_not_found` |
| `POST /api/v1/route/calculate` | `{"status":"success","data":{mode, ola_raw_mins, total_duration_mins, distance_km, checkpoint_count, safety_interval_mins, trigger_schedule, overview_polyline}}` | 404 `no_route` |

Route body: `start_lat, start_lng, end_lat, end_lng` (valid coordinates),
`mode` = `walking` (default) | `driving` (cab) | `ride` (two-wheeler, routed as driving),
`added_buffer_mins` 0-240 (default 0), `checkpoint_count` 0-50 (default 2).

Common to all: 401 `unauthenticated` (AUTH_MODE=required, no or bad token),
422 `invalid_request`, 429 `rate_limited` (+ `Retry-After`), 502 `maps_error`,
503 `maps_unavailable` (+ `Retry-After`), 504 `maps_timeout`, 413 `body_too_large`.
Every response has `X-Request-ID`; lookups have `X-Cache: HIT | MISS | SHARED | OFF`.

## Run locally

```
python -m venv .venv-dev
.venv-dev\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env      (and put the Ola key in it)
.venv-dev\Scripts\python -m uvicorn app.main:create_app --factory --reload
```

Tests (no network needed): `.venv-dev\Scripts\python -m pytest`

## Deploy on Render

1. Dashboard -> New -> Blueprint -> this repository: it creates the web
   service and the Key Value store from `render.yaml`.
2. Fill in `OLA_API_KEY` and `FIREBASE_PROJECT_ID` (the `project_id` in the app's
   `google-services.json`) when asked.
3. If the existing service is kept instead: set its start command to the one in
   `render.yaml`, health check path `/healthz`, add the environment variables,
   and create a Key Value instance for `REDIS_URL`.
4. Check `/readyz` after the deploy: `{"status": "ok", "redis": "ok", ...}`.

## Sign-in rollout

1. Deploy with `AUTH_MODE=optional`: a token is checked when sent; the app
   versions that send none still work.
2. Release the app version that sends the token (ApiClient adds it).
3. When nearly everyone has updated, set `AUTH_MODE=required`: requests without
   a valid sign-in get 401.

## Scaling and the load balancer

Render's load balancer spreads requests over the instances (`scaling` in
render.yaml: 2 to 6, by CPU and memory). The app keeps nothing per instance
that another instance needs (cache and rate-limit counts are in Redis), so more
load is more instances. Each instance runs `WEB_CONCURRENCY` worker processes;
each worker serves many requests at once, because every call to Ola is awaited,
never blocking.

Measured (`python -m loadtest.in_process --old`, Ola answering in 200 ms, cache
off so every request reaches Ola), one process:

| | requests/second |
|---|---|
| the old backend's pattern (`requests` inside `async def`) | 5 |
| this backend | 734 (with the test's load generator in the same process) |

What really limits how many people this can serve is Ola Maps' quota and price.
The cache is what keeps that down; check `cache HIT` / `MISS` in the logs.
Before a large launch, run `loadtest/run_load.py` against the deployed server.

## Operations

* Logs are one JSON object per line: `event`, `status`, `duration_ms`,
  `request_id`, `upstream`, `failure`. Never logged: the Ola key, tokens,
  searched text, coordinates, user ids, addresses.
* `/healthz` is the load balancer's check (no outside calls).
  `/readyz` reports Redis and whether Ola is being rested after failures.
* When Ola fails 5 times in a row it is not asked for 15 s: requests get 503 at
  once instead of each waiting out a timeout.
* Redis down: the service carries on with per-process memory (cache and limits).

## Security notes

* The old backend put Ola's error text, which contains the full URL with
  `api_key=...`, into its responses and logs. Rotate the Ola API key.
* The old `/api/v1/sos/trigger-cascading` endpoint (removed) let anyone send SOS
  pushes to any user. The app no longer used it (Cloud Functions do the SOS
  cascade). `serviceAccountKey.json` was only for it: delete the file and
  disable that key in Google Cloud (IAM -> Service accounts -> Keys).
* Secrets only in the environment (`.env` locally, the dashboard on Render).
