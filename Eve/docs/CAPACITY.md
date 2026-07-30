# Capacity Analysis

What the system costs per request, what it therefore needs to serve a given
load, and where the current instance ceiling is. Derived from measured
staging evidence, not estimates.

**Evidence:** `loadtest/results/warmup_*` — 20 users, 638 requests over
118 s (5.4 req/s), zero HTTP failures, Railway **Hobby** plan, single
service, load generated from a laptop over the public internet.

## Method

Response time = **service time** (work the request actually does) +
**queueing** (waiting for a worker). The two are separated by reading the
*minimum* per endpoint: with no contention, the minimum is service time.
The spread between minimum and p95 is queueing.

Concurrency demand follows Little's law: `L = λ × W` — arrival rate times
service time gives the number of request slots that must exist for the
system to keep up.

*Caveat:* timings are client-side, so every figure includes internet
round-trip from the generator. Service times are therefore slightly
overstated; conclusions about *ratios* are unaffected.

## Measured service cost

| Endpoint | Service time (min) | What that cost is |
|---|---|---|
| `GET /` | 25 ms | Template render, no I/O — the floor |
| `GET /shop/catalogue/` | 38 ms | Mongo product cache read (cache is working) |
| `GET /shop/product/[hit]` | 174 ms | Cached product read |
| `GET /shop/cart/` | 469 ms | One Mongo cart operation |
| `POST /shop/cart/remove/` | 941 ms | Mongo write |
| `POST /shop/cart/add/` | 1103 ms | Several sequential Mongo round-trips |
| `GET /shop/product/[miss]` | 1303 ms | Upstream Saleor call |
| `POST /accounts/login/` | 3937 ms | PBKDF2 password hashing (CPU-bound) |

**Totals for the run:** 195 worker-seconds of service demand across 638
requests — a mean of **306 ms per request**, requiring **1.66 concurrent
request slots** at 5.4 req/s.

Composition: **12 % CPU-bound** (password hashing) and **88 % I/O-wait**
(MongoDB, Saleor, Redis). That split is the single most important number
here, because I/O-wait and CPU work scale differently.

## Why the warm-up missed its SLOs

At 5.4 req/s — trivial traffic — p95 latencies ran 3–6× over budget while a
static page with no I/O varied from 25 ms to 2234 ms. Requests were waiting,
not working. Three causes, in order of size:

1. **Oversized worker pool.** Workers were sized from
   `multiprocessing.cpu_count()`, which reports the *host's* cores inside a
   container — potentially dozens of processes contending for a fraction of
   a vCPU. Fixed: sizing now reads the cgroup quota (`gunicorn.conf.py`).
2. **Blocking workers.** 88 % of demand is I/O-wait, and synchronous workers
   block a whole process while waiting. Fixed: threaded workers
   (`gthread`, 4 threads) multiply I/O concurrency without more processes.
3. **Avoidable round-trips.** `get_cart()` upserted on every read, making a
   cart *view* cost a Mongo write; `add_to_cart` used four round-trips.
   Fixed: read-first and two round-trips on the common paths.

A fourth, still open: **~450 ms per MongoDB operation** is far too high for
co-located services and suggests the Atlas cluster is in a different region
from the Railway service. Verify before further tuning — it affects every
catalogue and cart request.

## Second measurement (after the worker and cart fixes)

Same profile, 723 requests at 6.1 req/s, zero failures. Aggregate p50 fell
**580 ms -> 170 ms** and p95 **2300 ms -> 1300 ms**; throughput rose 13 %.
The `Server-Timing` split showed in-app p50 147 ms against 28 ms of
queue+network - so at the median the application is now the cost, and
contention only dominates in the tail (p95: 713 ms app, 644 ms overhead).

**Two hypotheses were falsified by this run, and one was found.**

*Falsified - MongoDB round-trips.* Cutting `add_to_cart` from three
operations to two moved its minimum by 3 % (1103 -> 1067 ms). Per-endpoint
minimums barely moved anywhere. Cost is not proportional to Mongo
operations.

*Falsified - MongoDB latency generally.* Anonymous endpoints that read
MongoDB are fast: the catalogue costs 36 ms and a cached product 171 ms.
A slow or distant cluster could not produce those numbers.

*Found - the cost tracks authentication, not storage:*

| Anonymous | Authenticated |
|---|---|
| `GET /` 24 ms, catalogue 36 ms, login page 86 ms, product 171 ms | cart 456 ms, checkout 449 ms, history 736 ms, cart add 1067 ms, cart remove 914 ms |

Every authenticated endpoint carries a ~450 ms floor that no anonymous
endpoint pays. Session retrieval, the auth user lookup, and per-request
cache work are the candidates. `Server-Timing` now carries `db;dur` and
`mongo;dur`, and the load test prints a per-endpoint breakdown
(`client / app / postgres / mongo / other`), so the next run attributes
that 450 ms instead of inferring it.

### Password hashing, measured

Login's ~3.2 s floor is CPU-bound hashing, not queueing. Benchmarked
locally (`make_password`, same machine, 3 runs):

| Hasher | Cost per hash |
|---|---|
| PBKDF2-SHA256 (1.2M iterations, Django default) | **783 ms** |
| Argon2id (Django defaults) | **76 ms** |

Argon2id is **~10x cheaper** and is the OWASP-preferred hasher, so this is
a security improvement that also removes the largest single latency cost.
It is now the preferred hasher; existing PBKDF2 hashes keep verifying and
upgrade in place on each owner's next login. Budget memory accordingly:
Argon2 is memory-hard at ~100 MiB per concurrent hash.

## Projection to the go-live target (200 users)

Scaling the measured profile ×10:

| Quantity | At 200 users |
|---|---|
| Arrival rate | ~54 req/s |
| Concurrent request slots needed | **~17** |
| Sustained CPU for hashing | **~2 vCPU** |

With `gthread`, 17 slots is reachable on modest hardware (e.g. 3 workers ×
4 threads = 12, or 4 × 4 = 16), but the 2 sustained vCPU for password
hashing is not available on Hobby.

**The ramp is the harder constraint.** At `--spawn-rate 10`, 200 users log
in within ~20 s: 200 × 3.9 s = **787 CPU-seconds of hashing demanded in a
20-second window**. On roughly one vCPU that is ~13 minutes of work
compressed into 20 seconds. The run would collapse during ramp-up and
measure the login storm rather than steady-state capacity.

Two corrections when the run happens:

- **Ramp slowly** (`--spawn-rate 2`, ~100 s) so logins spread out. Real
  traffic has mostly-authenticated users, not 200 simultaneous logins.
- **Reconsider the hasher.** Argon2id is OWASP-preferred and typically far
  cheaper per login than 1.2 M PBKDF2 iterations on a weak vCPU; Django
  rehashes on next login automatically. Do this *after* re-measuring, since
  a 3.9 s floor points at CPU throttling as much as at the algorithm.

## Current ceiling

On the **Hobby** plan this service can serve roughly **5–10 req/s** within
its SLOs once the fixes above are deployed — adequate for demonstration and
functional testing, not for the 2× peak go-live gate.

Meeting the go-live SLOs at 200 users requires approximately **2–4 vCPU**
across one or more replicas (the app is stateless, so replicas scale
linearly), plus MongoDB co-located with the application region.

This is a capacity limit, not a defect: the system degrades by queueing,
not by failing (zero HTTP errors throughout).

## Re-measurement protocol

1. Redeploy and confirm the boot line: `gunicorn sizing: N workers x M
   threads (gthread); detected X.XX allocated CPU(s)`.
2. Re-run the 20-user warm-up (`loadtest/README.md`). Read the
   **"Where the time went"** block: if queue+network overhead still exceeds
   in-app time, capacity is still the constraint.
3. Compare per-endpoint minimums against the table above — those are the
   service-cost regressions or improvements.
4. Only when the warm-up passes, attempt the 200-user run with
   `--spawn-rate 2`, on an instance sized per the projection.
