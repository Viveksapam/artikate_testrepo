# Part B: Diagnose Three Broken Snippets

## Snippet 1: Overdue Report View

### 1. What is wrong? (Distinct Defects)
1. **N+1 Database Query Pattern**: `CheckOut.objects.filter(returned_at__isnull=True)` does not use `select_related('asset', 'employee')`. When the loop accesses `c.asset.name`, `c.asset.asset_tag`, and `c.employee.full_name`, Django executes two additional SQL queries per checkout record. For N checkouts, the view executes 2N + 1 queries.
2. **Filtering in Application Memory**: `CheckOut.objects.filter(returned_at__isnull=True)` loads every open checkout into Python memory, regardless of whether the checkout is overdue. The condition `c.due_at < timezone.now()` filters records in Python. This forces a full table scan of active checkouts and loads records that are not overdue.
3. **In-Memory Sorting**: Sorting is performed in Python via `rows.sort(key=lambda r: r["days_overdue"], reverse=True)`. This consumes web worker CPU and RAM under high volume. Database-level sorting via an index on `due_at ASC` avoids application-side sorting.
4. **Time Drift via Per-Iteration Clock Evaluation**: `timezone.now()` is called twice per iteration inside the loop. In large datasets where the loop takes several seconds, later iterations evaluate against a later timestamp than earlier iterations. This produces inconsistent delta evaluations across the dataset.
5. **Loss of Granularity in Overdue Duration**: `(timezone.now() - c.due_at).days` uses the `.days` attribute of `datetime.timedelta`. The `.days` attribute truncates fractional days to the floor integer of total seconds divided by 86,400. A checkout that became overdue 23 hours ago returns 0 days overdue. If calendar day differences are required, date casting is required. If precision is required, fractional days or total hours must be calculated.
6. **Absence of Authentication and Authorization**: The view uses standard Django `JsonResponse` without authentication decorators (`@login_required` or DRF `permission_classes = [IsAuthenticated]`). Unauthenticated callers can access internal employee and asset operational data.
7. **Unbounded Payload Size**: The query has no pagination or limit clause. As overdue checkouts accumulate, the endpoint serializes an unbounded JSON array in a single response, leading to response timeouts and memory spikes.

### 2. Why does it look correct in local testing?
- **Small Dataset Volume**: Local development databases contain only a few test records (5 to 10 rows). The latency of 2N + 1 queries on 10 rows against a local database is under 15 milliseconds, which conceals the N+1 performance penalty.
- **Homogeneous Overdue Test Data**: Local test fixtures commonly configure all seeded open checkouts to be overdue. When every record satisfies `c.due_at < timezone.now()`, the inefficiency of filtering in Python does not produce discrepancies in output row counts.
- **Coarse Time Deltas**: Test cases typically set due dates several days or weeks in the past (such as 5 or 10 days ago). In those cases, `(now - due).days` evaluates to whole integers without exposing edge cases where checkouts are overdue by less than 24 hours.
- **Fast Execution Masking Clock Drift**: With few records, the loop completes in sub-millisecond time. The time difference between iterations is negligible.
- **Local Isolation**: Endpoints tested locally via curl or browser without authentication headers bypass security gaps when default settings or test runners allow open access.

### 3. How would you fix it? Show the corrected code.

```python
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from core.models import CheckOut


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def overdue_report(request):
    now = timezone.now()

    # Filter, join foreign keys, and order at the database level
    overdue_checkouts = (
        CheckOut.objects.filter(
            returned_at__isnull=True,
            due_at__lte=now,
        )
        .select_related("asset", "employee")
        .order_by("due_at")
    )

    rows = []
    for c in overdue_checkouts:
        elapsed_seconds = (now - c.due_at).total_seconds()
        days_overdue = int(elapsed_seconds // 86400)

        rows.append(
            {
                "asset": c.asset.name,
                "asset_tag": c.asset.asset_tag,
                "employee": c.employee.full_name,
                "days_overdue": days_overdue,
            }
        )

    return Response({"count": len(rows), "rows": rows}, status=status.HTTP_200_OK)
```

Better alternative:
If the result set exceeds several hundred records, DRF pagination (`PageNumberPagination`) must be enabled to prevent unbounded memory allocation and excessive network payload transfer.

### 4. What test or tooling would have caught this before it shipped?
- **Query Count Assertions**: Django `assertNumQueries(1)` or `pytest-django`'s `django_assert_num_queries(1)` fails immediately when N+1 queries are generated.
- **SQL Profiling Tools**: `django-debug-toolbar` in development or `django-silk` in staging flags duplicate queries and high SQL call counts per endpoint.
- **Boundary Unit Tests**: Tests using an item due 2 hours ago expose whether `days_overdue` evaluates to 0 or 1. Tests supplying future checkouts verify that the database does not return non-overdue rows.
- **Security Scanners**: Automated API security linters and test suites asserting HTTP 401 Unauthorized for requests without Bearer tokens detect missing authentication permissions.

---

## Snippet 2: Check-Out Endpoint

### 1. What is wrong? (Distinct Defects)
1. **Concurrency Race Condition (Check-Then-Act Failure)**: The endpoint reads `asset.status` and `open_count` without a database transaction and without row-level locking (`select_for_update()`). When two concurrent requests arrive simultaneously for the same asset:
   - Both threads read `asset.status == "AVAILABLE"`.
   - Both threads read `open_count < 3`.
   - Both threads execute `CheckOut.objects.create(...)`.
   - Both threads set `asset.status = "CHECKED_OUT"`.
   Result: Two concurrent active `CheckOut` records are created for a single physical asset.
2. **Employee Limit Race Condition**: When an employee with 2 active checkouts issues two simultaneous checkout requests for different assets, both threads count 2 active checkouts, pass the `open_count >= 3` check, and create checkouts. The employee ends up with 4 active checkouts, violating Rule 3.
3. **Non-Atomic State Mutation**: Creating the `CheckOut` record and updating `asset.status` occur in separate database operations outside an explicit transaction block (`transaction.atomic()`). If the application crashes, worker terminates, or database connection drops after `CheckOut.objects.create` but before `asset.save()`, a checkout record exists while the asset remains marked as `AVAILABLE`.
4. **Unhandled `DoesNotExist` Exceptions**: `Asset.objects.get(...)` and `Employee.objects.get(...)` raise `Asset.DoesNotExist` and `Employee.DoesNotExist` when given invalid tags or codes. Because these exceptions are unhandled, Django returns an HTTP 500 Internal Server Error. Rule 8 requires returning HTTP 404 Not Found.
5. **KeyError on Missing Request Payload**: Direct dictionary lookups (`request.data["asset_tag"]`, `request.data["employee_code"]`, `request.data["due_at"]`) raise unhandled `KeyError` when required fields are missing from the request body, returning an HTTP 500 status.
6. **Omission of Inactive Employee Validation**: The view does not check `employee.is_active`. An inactive employee can successfully check out assets, violating Rule 2.
7. **Unvalidated `due_at` Parameter**: `request.data["due_at"]` is passed directly into the database without validation. Rule 4 requires that `due_at` must be in the future and at most 30 days ahead of the current timestamp. Dates in the past, dates beyond 30 days, or malformed strings are accepted or trigger database type cast exceptions.

### 2. Why does it look correct in local testing?
- **Single-Threaded Sequential Requests**: Local test suites and manual curl or Postman requests execute sequentially on a single thread. Race conditions do not manifest in single-client environments.
- **Happy Path Input Data**: Manual tests supply valid existing asset tags, valid active employee codes, and valid ISO date strings. Under these inputs, `DoesNotExist`, `KeyError`, and date validation branches are never triggered.
- **Absence of Network Failures**: Local environments do not experience transient database dropouts or worker terminations between two sequential Python lines. The non-atomic gap between checkout creation and asset status save never fails mid-execution.
- **SQLite Concurrency Serialization**: If SQLite is used locally, file-level locking serializes writes at the database driver level, masking row-level concurrency bugs that occur in PostgreSQL.

### 3. How would you fix it? Show the corrected code.

```python
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from core.models import Asset, Employee, CheckOut


class CheckOutInputSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError("due_at must be in the future.")
        if value > now + timedelta(days=30):
            raise serializers.ValidationError(
                "due_at cannot be more than 30 days ahead."
            )
        return value


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def check_out_asset(request):
    serializer = CheckOutInputSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    validated = serializer.validated_data

    with transaction.atomic():
        # Acquire row lock on Asset
        try:
            asset = Asset.objects.select_for_update().get(
                asset_tag=validated["asset_tag"]
            )
        except Asset.DoesNotExist:
            return Response(
                {"detail": "Asset not found."}, status=status.HTTP_404_NOT_FOUND
            )

        # Acquire row lock on Employee
        try:
            employee = Employee.objects.select_for_update().get(
                employee_code=validated["employee_code"]
            )
        except Employee.DoesNotExist:
            return Response(
                {"detail": "Employee not found."}, status=status.HTTP_404_NOT_FOUND
            )

        # Rule 1: Asset must be AVAILABLE
        if asset.status != Asset.Status.AVAILABLE:
            return Response(
                {"detail": "Asset is not available."},
                status=status.HTTP_409_CONFLICT,
            )

        # Rule 2: Inactive employee cannot check out
        if not employee.is_active:
            return Response(
                {"detail": "Inactive employee cannot check out assets."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Rule 3: Maximum 3 active check-outs per employee
        active_count = CheckOut.objects.filter(
            employee=employee, returned_at__isnull=True
        ).count()
        if active_count >= 3:
            return Response(
                {"detail": "Employee checkout limit reached."},
                status=status.HTTP_409_CONFLICT,
            )

        # Rule 5: Atomic checkout creation and status update
        checkout = CheckOut.objects.create(
            asset=asset,
            employee=employee,
            due_at=validated["due_at"],
        )
        asset.status = Asset.Status.CHECKED_OUT
        asset.save(update_fields=["status"])

    return Response({"id": checkout.id}, status=status.HTTP_201_CREATED)
```

Better alternative:
Encapsulate business logic inside a service layer function (`checkout_asset(...)`) and keep transaction boundaries in the service layer. This allows task workers, CLI commands, and API endpoints to share identical transactional guarantees.

### 4. What test or tooling would have caught this before it shipped?
- **Concurrent Integration Tests**: Multi-threaded tests using `concurrent.futures.ThreadPoolExecutor` sending simultaneous checkout requests for the same asset tag assert that exactly one request returns 201 and the second returns 409.
- **Explicit Negative Unit Tests**: Test cases asserting HTTP 404 for invalid asset tags, HTTP 404 for invalid employee codes, HTTP 400 for inactive employees, HTTP 400 for past due dates, and HTTP 400 for missing request body parameters.
- **Django Migration Linters**: Detecting missing foreign key index coverage or missing constraints.
- **Load Testing Tools**: Executing concurrent requests via Locust or k6 during staging validation runs.

---

## Snippet 3: Nightly Notice Task

### 1. What is wrong? (Distinct Defects)
1. **Unbounded Memory Consumption (OOM Risk)**: `CheckOut.objects.filter(...)` without `.iterator()` loads the entire queryset into Python memory as model instances simultaneously. When overdue rows grow to tens of thousands, memory usage causes Celery worker processes to be terminated by the OS Out-Of-Memory killer.
2. **Missing Retry Idempotency (Unique Constraint Collision)**: `OverdueNotice` contains a unique constraint on `(checkout, notice_date)`. If the task fails midway (such as network drop, worker restart, Redis connection reset) and Celery retries the task, `OverdueNotice.objects.create(...)` crashes on the first already-processed record with a database `IntegrityError`. The entire retry aborts, leaving remaining overdue checkouts unnotified.
3. **Duplicate Notifications Dispatched on Partial Failure**: If the task processes 500 rows and dispatches 500 emails before encountering an error, a retry without state tracking re-runs previous items. If error handling skips the `IntegrityError`, `deliver_email.delay(...)` is invoked a second time for the same checkout, sending duplicate emails to employees.
4. **Passing Django Model Instances to Celery Tasks**: `deliver_email.delay(c.employee, c)` passes complex Django model instances into Celery. Celery default serializer is JSON. Django model instances are not JSON serializable and raise `kombu.exceptions.EncodeError`. If pickle serialization is enabled, it introduces security vulnerabilities and passes stale database state across asynchronous queues. Primary keys must be passed.
5. **N+1 Query on Employee Retrieval**: Inside the loop, referencing `c.employee` initiates an individual SQL query for each checkout to fetch employee data because `select_related('employee')` is absent. For 10,000 overdue checkouts, 10,000 independent SQL queries are dispatched.
6. **Clock Drift Across Date Boundaries**: `timezone.now().date()` is called repeatedly inside the loop. If the task execution crosses midnight UTC, records processed before midnight receive yesterday's date while records processed after midnight receive today's date.
7. **Redundant Database Evaluation on Return**: `overdue.count()` at the end executes an additional `COUNT(*)` query across the table. This count reflects current database records at the end of task execution, not the actual number of notices created or emails dispatched during the run.

### 2. Why does it look correct in local testing?
- **Trivial Record Count**: Local databases have 1 to 5 overdue records. Fetching 5 records consumes negligible memory and completes in single-digit milliseconds, hiding memory leaks and midnight timestamp drift.
- **Single Execution Without Simulated Failures**: Local tests run the task once from start to finish without triggering network errors, process kills, or Celery retry mechanisms. `IntegrityError` collisions on `(checkout, notice_date)` are never encountered.
- **Eager Task Execution with Pickle / Mocking**: Local tests frequently run with `CELERY_TASK_ALWAYS_EAGER = True` or mock `deliver_email.delay`. In-memory execution can bypass JSON serialization validation, hiding the fact that Django model instances fail JSON encoding in production Redis brokers.
- **Local DB Proximity**: On local machines, N+1 query latency is sub-millisecond, hiding query accumulation.

### 3. How would you fix it? Show the corrected code.

```python
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from core.models import CheckOut, OverdueNotice


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def send_overdue_notices(self):
    now = timezone.now()
    today = now.date()

    # Query only checkouts that do not already have a notice for today
    unnotified_overdue_ids = list(
        CheckOut.objects.filter(
            returned_at__isnull=True,
            due_at__lte=now,
        )
        .exclude(notices__notice_date=today)
        .values_list("id", "employee_id")
    )

    dispatched_count = 0

    # Process in chunks to maintain bounded memory usage
    chunk_size = 500
    for i in range(0, len(unnotified_overdue_ids), chunk_size):
        batch = unnotified_overdue_ids[i : i + chunk_size]
        notices_to_create = [
            OverdueNotice(checkout_id=checkout_id, notice_date=today)
            for checkout_id, _ in batch
        ]

        # Atomically record notices and dispatch email tasks
        with transaction.atomic():
            OverdueNotice.objects.bulk_create(
                notices_to_create, ignore_conflicts=True
            )

        # Dispatch emails passing primitive integer IDs
        for checkout_id, employee_id in batch:
            deliver_email.delay(employee_id, checkout_id)
            dispatched_count += 1

    return f"Processed {len(unnotified_overdue_ids)} overdue checkouts. Dispatched {dispatched_count} notices."
```

Better alternative:
For tens of thousands of rows, avoid processing all records inside a single monolithic worker task. The scheduled task should query overdue IDs in batches and enqueue lightweight child tasks (or a Celery chord / group) to process batches in parallel across the worker pool.

### 4. What test or tooling would have caught this before it shipped?
- **Task Idempotency Tests**: Running `send_overdue_notices()` twice back-to-back in an automated test. The second run must assert zero exceptions and verify that no duplicate notices or email tasks are created.
- **Celery Serialization Enforcement**: Unit tests configured with `CELERY_TASK_SERIALIZER = 'json'` and `CELERY_ACCEPT_CONTENT = ['json']`. Calling `.delay(c.employee, c)` immediately triggers a `kombu.exceptions.EncodeError`.
- **Worker Crash and Retry Tests**: Testing Celery task retry logic by mocking a database exception mid-loop and verifying clean recovery without duplicated external calls.
- **Large Dataset Volume Tests**: Seeding 20,000 overdue records in a test environment to observe process RSS memory consumption.

---

# Part C: Optimise a Slow PostgreSQL Query

## Situation Schema and Slow Query

```sql
SELECT *
FROM checkouts c
WHERE DATE(c.checked_out_at) BETWEEN '2026-01-01' AND '2026-06-30'
AND c.returned_at IS NULL
AND c.employee_id IN (
    SELECT id FROM employees WHERE is_active = true
)
ORDER BY c.due_at ASC;
```

---

## 1. Rewrite the Query

```sql
SELECT 
    c.id,
    c.asset_id,
    c.employee_id,
    c.checked_out_at,
    c.due_at,
    c.returned_at
FROM checkouts c
JOIN employees e ON c.employee_id = e.id
WHERE c.returned_at IS NULL
  AND c.checked_out_at >= '2026-01-01 00:00:00+00'
  AND c.checked_out_at < '2026-07-01 00:00:00+00'
  AND e.is_active = true
ORDER BY c.due_at ASC;
```

### Explanation of Changes, Gains, and Costs:
1. **Replacement of `DATE(c.checked_out_at)` with Half-Open Timestamp Range**:
   - Gain: Wrapping `checked_out_at` inside `DATE(...)` makes the predicate non-sargable. PostgreSQL cannot use a standard B-tree index on `checked_out_at` because it must calculate the function on every row. Using `>= '2026-01-01 00:00:00+00'` and `< '2026-07-01 00:00:00+00'` makes the condition sargable, allowing direct B-tree index range scans.
   - Cost: Requires explicit timezone handling and exact boundary declarations.
2. **Replacement of `SELECT *` with Explicit Column Projections**:
   - Gain: Prevents loading large unneeded columns such as `condition_note text`. Reduces disk I/O, OS buffer cache pressure, network bandwidth, and deserialization overhead.
   - Cost: The projection list must be updated if future reporting requirements add columns.
3. **Conversion of Subquery `IN (...)` to Inner Join**:
   - Gain: An explicit `JOIN employees e ON c.employee_id = e.id AND e.is_active = true` provides the query optimizer with clear join selectivity metrics and enables nested loop joins against the employee primary key index.
   - Cost: Minimal; maintains index requirements on foreign keys.

---

## 2. Specify Indexes with CREATE INDEX Statements

```sql
-- 1. Partial composite index on checkouts for active, unreturned records
CREATE INDEX CONCURRENTLY idx_checkouts_open_due_checked 
ON checkouts (due_at ASC, checked_out_at, employee_id) 
WHERE returned_at IS NULL;

-- 2. Partial index on active employees
CREATE INDEX CONCURRENTLY idx_employees_active_id 
ON employees (id) 
WHERE is_active = true;
```

### Why Each Index Earns Its Place:
- **Partial Index on `checkouts (WHERE returned_at IS NULL)`**:
  - The table has 4.2 million rows and grows by 8,000 rows per day. In physical asset tracking, completed checkouts represent the vast majority of historical data (>99%). Active unreturned checkouts represent a small working subset (typically under 1%, or 2,000 to 10,000 rows).
  - An unconditional index across all 4.2 million rows consumes 250MB to 500MB of storage and incurs write maintenance overhead on every insert and update.
  - A partial index containing only rows `WHERE returned_at IS NULL` indexes only active checkouts. Its footprint is under 2MB, allowing it to reside permanently in PostgreSQL `shared_buffers`.
  - Adding `due_at ASC` as the leading index column eliminates the sort step (`ORDER BY c.due_at ASC`). PostgreSQL performs an ordered index scan directly.
  - Including `checked_out_at` and `employee_id` allows the index to fulfill range filtering and join evaluation.
- **Partial Index on `employees (WHERE is_active = true)`**:
  - The primary key index on `employees (id)` already exists. If the employee table has inactive records, a partial index on `(id) WHERE is_active = true` allows index-only lookups for active employee verification. If the vast majority of employees are active, the default PK index `employees_pkey` suffices.

---

## 3. EXPLAIN (ANALYZE, BUFFERS) Before and After

### Expected Plan Before Optimization:
```text
Sort (cost=382410.20..382415.50 rows=2120 width=128) (actual time=7980.120..7982.340 rows=1840 loops=1)
  Sort Key: c.due_at ASC
  Sort Method: external merge Disk: 184kB
  Buffers: shared hit=12400 read=45800, temp read=24 written=24
  ->  Hash Join (cost=420.00..382290.10 rows=2120 width=128) (actual time=14.210..7970.550 rows=1840 loops=1)
        Hash Cond: (c.employee_id = employees.id)
        Buffers: shared hit=12400 read=45800
        ->  Seq Scan on checkouts c (cost=0.00..381500.00 rows=2200 width=128) (actual time=12.100..7945.300 rows=1900 loops=1)
              Filter: ((returned_at IS NULL) AND (date(checked_out_at) >= '2026-01-01'::date) AND (date(checked_out_at) <= '2026-06-30'::date))
              Rows Removed by Filter: 4198100
              Buffers: shared hit=12100 read=45800
        ->  Hash (cost=270.00..270.00 rows=12000 width=8) (actual time=2.080..2.080 rows=11850 loops=1)
              Buckets: 16384  Batches: 1  Memory Usage: 580kB
              Buffers: shared hit=300
              ->  Seq Scan on employees (cost=0.00..270.00 rows=12000 width=8) (actual time=0.012..1.210 rows=11850 loops=1)
                    Filter: is_active
                    Buffers: shared hit=300
Planning Time: 0.450 ms
Execution Time: 7985.420 ms
```

### Expected Plan After Optimization:
```text
Nested Loop (cost=0.29..845.10 rows=1840 width=48) (actual time=0.045..2.850 rows=1840 loops=1)
  Buffers: shared hit=210 read=0
  ->  Index Scan using idx_checkouts_open_due_checked on checkouts c (cost=0.15..120.50 rows=1900 width=48) (actual time=0.030..0.850 rows=1900 loops=1)
        Index Cond: ((checked_out_at >= '2026-01-01 00:00:00+00'::timestamptz) AND (checked_out_at < '2026-07-01 00:00:00+00'::timestamptz))
        Filter: (returned_at IS NULL)
        Buffers: shared hit=28 read=0
  ->  Index Scan using employees_pkey on employees e (cost=0.14..0.38 rows=1 width=8) (actual time=0.001..0.001 rows=1 loops=1900)
        Index Cond: (id = c.employee_id)
        Filter: is_active
        Buffers: shared hit=182 read=0
Planning Time: 0.280 ms
Execution Time: 3.120 ms
```

### Specific Line Indicating the Fix Worked:
```text
Index Scan using idx_checkouts_open_due_checked on checkouts c (actual time=0.030..0.850 rows=1900 loops=1)
```
Combined with:
```text
Buffers: shared hit=210 read=0
Execution Time: 3.120 ms
```
The transition from `Seq Scan on checkouts c` with `Rows Removed by Filter: 4198100` and `read=45800` to `Index Scan using idx_checkouts_open_due_checked` with zero disk buffer reads and 3.1 ms execution confirms the fix.

---

## 4. Growth Bottlenecks and Remediation

### What Breaks First at 8,000 Rows Per Day:
1. **Dead Tuple Bloat and Autovacuum Bottlenecks**: Checkouts undergo status lifecycle changes (from `AVAILABLE` to `CHECKED_OUT` to `RETURNED` or `MAINTENANCE`). Every update writes a new tuple version in PostgreSQL MVCC. At 8,000 checkouts and returns daily, dead tuple accumulation creates severe table and index bloat. Standard autovacuum settings lag behind, increasing sequential scan times and vacuum freeze durations.
2. **Buffer Cache Eviction**: As the unpartitioned table exceeds PostgreSQL `shared_buffers` and host RAM, queries that miss indexes force OS disk page cache thrashing, degrading concurrent transactions.
3. **Index Maintenance Write Penalty**: Every insert must update all B-tree indexes defined on `checkouts`, degrading checkout ingestion throughput.

### Preventive Remediation:
1. **Declarative Range Partitioning**: Partition the `checkouts` table by range on `checked_out_at` (monthly or quarterly). Older partitions containing only returned checkouts remain static, experience zero dead tuple generation, and allow partition pruning for date-bounded reporting queries.
2. **Autovacuum Tuning on Checkouts**:
   ```sql
   ALTER TABLE checkouts SET (
       autovacuum_vacuum_scale_factor = 0.02,
       autovacuum_vacuum_cost_limit = 2000,
       autovacuum_vacuum_cost_delay = 2
   );
   ```
   Tuning prevents dead tuple accumulation without waiting for default 20% table change thresholds (which would require 840,000 row mutations on a 4.2M table).
3. **Data Tiering and Archival Strategy**: Move completed checkout records older than 2 years to an archival cold-storage table or analytical data store, keeping the operational table compact.

---

## 5. Metric to Measure Before Committing to the Answer

### Metric:
The exact cardinality and null fraction of `checkouts.returned_at` in production via:
```sql
SELECT 
    count(*) AS total_rows,
    count(*) FILTER (WHERE returned_at IS NULL) AS open_checkouts,
    round(count(*) FILTER (WHERE returned_at IS NULL)::numeric / count(*) * 100, 2) AS open_pct
FROM checkouts;
```
Or by inspecting PostgreSQL catalog statistics:
```sql
SELECT null_frac, n_distinct 
FROM pg_stats 
WHERE tablename = 'checkouts' AND attname = 'returned_at';
```

### Why Certainty is Impossible Without It:
The proposed optimization relies on the condition that unreturned checkouts (`returned_at IS NULL`) constitute a very small percentage of the total dataset (<1%). Under this condition, the partial index is small, fits in cache, and exhibits high selectivity.

If production operations reveal that historical checkouts were abandoned or unclosed, leaving 30% to 50% of the 4.2 million rows with `returned_at IS NULL`, the partial index will encompass 1.5 million to 2.1 million rows. At that volume, the query planner will deem the index insufficiently selective for broad date ranges, reverting to a Bitmap Index Scan or Sequential Scan. Measuring the true null fraction confirms whether the partial index strategy is viable.

---

# Part D: Production Reasoning

## D1. Zero-Downtime Migration

### Context and Sequence:
Adding a non-nullable foreign key `location_id` to a 4.2-million-row `checkouts` table in a live four-instance cluster without maintenance downtime requires an expand-contract sequence spanning three discrete deployments.

```
[Deploy 1: Schema Expand] -> [Out-of-Band Data Backfill] -> [Deploy 2: Enforce Constraint] -> [Deploy 3: Contract / Cleanup]
```

### Step-by-Step Execution:

#### 1. Deployment 1 (Expand Schema as Nullable):
- **Database Migration**:
  1. Add `location_id` as a nullable column:
     ```sql
     ALTER TABLE checkouts ADD COLUMN location_id bigint;
     ```
     In PostgreSQL 11+, adding a nullable column is an instantaneous metadata-only change that takes an exclusive lock for less than a millisecond.
  2. Create the foreign key index concurrently to prevent table locking:
     ```sql
     CREATE INDEX CONCURRENTLY idx_checkouts_location_id ON checkouts (location_id);
     ```
  3. Add the foreign key constraint with the `NOT VALID` flag:
     ```sql
     ALTER TABLE checkouts ADD CONSTRAINT fk_checkouts_location 
     FOREIGN KEY (location_id) REFERENCES locations(id) NOT VALID;
     ```
     `NOT VALID` enforces the constraint only on subsequent writes, avoiding a full table scan of 4.2 million rows under an `ACCESS EXCLUSIVE` lock.
  4. Validate the foreign key constraint:
     ```sql
     ALTER TABLE checkouts VALIDATE CONSTRAINT fk_checkouts_location;
     ```
     `VALIDATE CONSTRAINT` takes a `SHARE UPDATE EXCLUSIVE` lock, permitting uninterrupted reads and writes during the table validation scan.
- **Application Code**:
  - Update the Django model with `location_id = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT)`.
  - Application write paths populate `location_id` on all new checkout creations. Read paths handle `null` values.
- **In-Flight Requests**: Old app instances continue executing against the database without error because `location_id` is nullable. New instances write `location_id`.

#### 2. Out-of-Band Data Backfill:
- Run a background script to populate `location_id` on existing historical rows:
  - Process in batches of 5,000 rows, updating rows where `location_id IS NULL`.
  - Introduce sleep intervals (100ms) between batches to prevent replication lag and transaction log saturation.
  - No deployment is involved during this step.

#### 3. Deployment 2 (Enforce Non-Nullability):
- Once `SELECT count(*) FROM checkouts WHERE location_id IS NULL` returns `0`:
- **Database Migration**:
  1. Add a check constraint marked `NOT VALID`:
     ```sql
     ALTER TABLE checkouts ADD CONSTRAINT check_location_not_null 
     CHECK (location_id IS NOT NULL) NOT VALID;
     ```
  2. Validate the check constraint:
     ```sql
     ALTER TABLE checkouts VALIDATE CONSTRAINT check_location_not_null;
     ```
  3. Set native `NOT NULL`:
     ```sql
     ALTER TABLE checkouts ALTER COLUMN location_id SET NOT NULL;
     ```
     In PostgreSQL 12+, this operation is instantaneous because the validated check constraint proves the absence of nulls.
- **Application Code**: Update Django model definition to `null=False`.

#### 4. Deployment 3 (Cleanup):
- Drop the redundant check constraint:
  ```sql
  ALTER TABLE checkouts DROP CONSTRAINT check_location_not_null;
  ```

### What Locks the Table if Done Wrong:
Executing:
```sql
ALTER TABLE checkouts ADD COLUMN location_id bigint NOT NULL REFERENCES locations(id);
```
in a single step. This statement acquires an `ACCESS EXCLUSIVE` lock on `checkouts`, which blocks all incoming read and write queries. While holding this exclusive lock, PostgreSQL must scan all 4.2 million rows to check for null values and validate foreign key referential integrity against `locations`. The database connection pool exhausts within seconds, taking the entire production application down.

---

## D2. Latency Triage

### Systematic Investigation Sequence:

1. **Check Application Metrics and APM (Datadog / New Relic / Prometheus)**:
   - Inspect the request breakdown: compare Python runtime, database execution time, and network latency.
   - Rules in or out: Python GIL contention, web worker CPU exhaustion, garbage collection pauses, or external HTTP delays versus database wait time.
2. **Check PostgreSQL Active Connections and Lock Waits (`pg_stat_activity`)**:
   - Run:
     ```sql
     SELECT pid, now() - query_start AS duration, query, state, wait_event_type, wait_event
     FROM pg_stat_activity 
     WHERE state != 'idle' AND query ILIKE '%checkouts%';
     ```
   - Rules in or out: Table or row lock contention. If `wait_event_type = 'Lock'`, queries are queued behind an uncommitted transaction or maintenance operation.
3. **Check Database Server Hardware and Resource Utilization**:
   - Inspect database CPU utilization, memory usage, disk I/O queue depth, and cloud storage IOPS burst balance (such as AWS EBS burst credits).
   - Rules in or out: Hardware saturation, IOPS throttling, or storage performance bottlenecks.
4. **Examine Live Query Execution Plan (`EXPLAIN (ANALYZE, BUFFERS)`)**:
   - Execute the endpoint's SQL query in production with `EXPLAIN (ANALYZE, BUFFERS)`.
   - Rules in or out: Plan regression (such as the optimizer switching from an Index Scan to a full Sequential Scan).
5. **Inspect Table Bloat and Autovacuum Activity (`pg_stat_user_tables`)**:
   - Check `n_dead_tup`, `last_vacuum`, `last_autovacuum`, and `last_analyze` on `checkouts`.
   - Rules in or out: Accumulation of dead tuples and stale planner statistics causing sequential scans across bloated table pages.

### Two Most Likely Causes and Confirmation:

#### Cause 1: Planner Plan Flip Due to Stale Statistics
- **Mechanism**: Over nine days without a deploy, 72,000 new checkouts were inserted and tens of thousands updated. If autovacuum/autoanalyze lagged, PostgreSQL's catalog statistics (`pg_class.reltuples`, `pg_stats`) diverged from reality. The query planner crossed a cost boundary and flipped from an index scan to a sequential table scan across 4.2 million rows.
- **Confirmation**: Run `EXPLAIN` on the query. If the plan displays `Seq Scan on checkouts` and `last_analyze` was days ago, run:
  ```sql
  ANALYZE checkouts;
  ```
  If execution time immediately returns to sub-second speed, the cause is confirmed.

#### Cause 2: Lock Contention from a Long-Running Background Task or Backup
- **Mechanism**: A background job, reporting export, database dump (`pg_dump`), or hung Celery worker acquired an exclusive or shared table lock on `checkouts` without committing, queueing report queries in a lock wait state.
- **Confirmation**: Run:
  ```sql
  SELECT blocked_locks.pid AS blocked_pid,
         blocking_locks.pid AS blocking_pid,
         blocking_activity.query AS blocking_statement
  FROM pg_catalog.pg_locks blocked_locks
  JOIN pg_catalog.pg_locks blocking_locks 
    ON blocking_locks.locktype = blocked_locks.locktype
   AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
   AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
   AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
   AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
   AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
   AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
   AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
   AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
   AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
   AND blocking_locks.pid != blocked_locks.pid
  JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
  WHERE NOT blocked_locks.granted;
  ```
  If this identifies an active blocking PID, lock contention is confirmed.

---

## D3. CI/CD and Safety

### Pipeline Structure on GitHub Actions:

```
[Pull Request] -> Linting + Migration Linter + Pytest (Postgres & Redis)
       |
    (Merge)
       v
[Main Branch] -> Docker Build + Security Scan + Deploy to Staging + Smoke Tests
       |
 (Manual Gate)
       v
[Production] -> Pre-Deploy Backward-Compatible Migrations -> Rolling Container Update
```

#### 1. What Runs on a Pull Request:
- **Code Formatting and Linters**: `ruff check .` and `ruff format --check .`.
- **Security Audit**: `pip-audit` checks Python dependencies for known CVEs.
- **Migration Verification**:
  - `python manage.py makemigrations --check --dry-run` detects uncommitted schema changes.
  - `django-migration-linter` flags backward-incompatible operations (such as table rewrites, column additions without default/nullable flags, or blocking index creation).
- **Automated Tests**: GitHub Actions workflow spins up PostgreSQL 15 and Redis service containers, executes database migrations, and runs `pytest --cov=core`. All tests must pass before review approval.

#### 2. What Runs on Merge to Main:
- **Container Build and Scan**: Build Docker image tagged with the short Git commit SHA. Run `trivy image` to check for base image and operating system vulnerabilities.
- **Push to Container Registry**: Push verified container image to Amazon ECR or Google Artifact Registry.
- **Deploy to Staging**:
  1. Run `python manage.py migrate` against the staging database.
  2. Update staging deployment with the new container image.
  3. Execute automated end-to-end smoke tests against staging endpoints including `/api/v1/health/`.

#### 3. What Gates a Production Deploy:
- Successful completion of the staging deployment and smoke test suite.
- Passing container security scanning with zero critical or high vulnerabilities.
- Explicit manual sign-off in GitHub Actions Environment Protection Rules by an authorized engineer.

### How Database Migrations are Applied Relative to Code Deployment:
Migrations follow the expand-contract methodology:
1. **Migrations Precede Code**: Database migrations run as a one-off pre-deploy task against production before updating web containers.
2. **Strict Backward Compatibility**: Every migration must be backward-compatible with the currently running container version. Migrations only add nullable columns, new tables, or concurrent indexes. They never drop columns, rename fields, or add synchronous NOT NULL constraints.
3. **Rolling Deployment**: Once the pre-deploy migration completes, the container orchestrator (Kubernetes or AWS ECS) performs a rolling replacement of application pods. At all points during the rollout, instances running old code and instances running new code operate safely against the updated database schema.

### Rollback Strategy When Schema Has Already Migrated:
1. **Application Code Rollback**: If the new release demonstrates elevated error rates or regressions, redeploy the previous container image immediately.
2. **Schema Retention During Incidents**: Do not run reverse migrations (`migrate <app> <previous_migration>`) during active incidents. Rolling back migrations on a live database risks data loss, lock acquisition, and extended downtime.
3. **Compatibility Guarantee**: Because all applied migrations strictly followed backward-compatibility rules, the previous container version continues running cleanly on the expanded database schema (ignoring unused columns and tables).
4. **Planned Contraction**: Reverse migrations or data cleanup steps are scheduled as a deliberate, forward-moving deployment during normal maintenance windows after the system stabilizes.
