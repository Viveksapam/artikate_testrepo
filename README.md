# Field Asset Check-Out Service

A Django REST Framework service for tracking physical equipment check-outs, returns, overdue notices, and employee hold statistics.

---

## Screen Recording

- Recording link: [Google drive](https://drive.google.com/file/d/1LISqI2deU1rAx16gClsRn07rVG5snejo/view?usp=sharing)

---

## Setup Instructions

Follow these exact commands from clone to a running API.

### Option 1: Docker Compose Setup (Recommended)

1. Clone the repository and navigate into the project directory:
   ```bash
   git clone <repository_url>
   cd artikate
   ```

2. Copy the environment configuration file:
   ```bash
   cp .env.example .env
   ```

3. Build and launch all four container services (web, db, redis, celery):
   ```bash
   docker compose up --build -d
   ```

4. Apply database migrations:
   ```bash
   docker compose exec web python manage.py migrate
   ```

5. Seed demo data:
   ```bash
   docker compose exec web python manage.py seed_demo_data
   ```

6. Create an admin user for API authentication:
   ```bash
   docker compose exec web python manage.py createsuperuser
   ```

7. Run test suite inside the container:
   ```bash
   docker compose exec web pytest
   ```

The API is accessible at `http://localhost:8000/`.

---

### Option 2: Local Virtual Environment Setup

1. Create and activate a Python 3.12+ virtual environment:
   ```bash
   python -m venv venv
   # On Linux/macOS:
   source venv/bin/activate
   # On Windows PowerShell:
   .\venv\Scripts\Activate.ps1
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Ensure PostgreSQL and Redis instances are running locally, and configure `.env`:
   ```bash
   cp .env.example .env
   ```

4. Run database migrations:
   ```bash
   python manage.py migrate
   ```

5. Seed demo data:
   ```bash
   python manage.py seed_demo_data
   ```

6. Run the test suite:
   ```bash
   pytest
   ```

7. Start the development server:
   ```bash
   python manage.py runserver 0.0.0.0:8000
   ```

8. Start the Celery worker with embedded Beat scheduler:
   ```bash
   celery -A config worker -B -l info
   ```

---

## Authentication Mechanism

This project uses JSON Web Token (JWT) authentication via `djangorestframework-simplejwt`.

### Rationale:
JWT authentication enables stateless authentication for API clients without requiring server-side session lookups on each request.

Better alternative:
In high-security enterprise environments where immediate token revocation is mandated, session authentication with server-side Redis token blacklisting or standard DRF Token Authentication provides immediate server-side revocation capability.

### Authentication Endpoints:
- Obtain Token Pair: `POST /api/v1/token/`
  - Body: `{"username": "<username>", "password": "<password>"}`
  - Response: `{"access": "<access_token>", "refresh": "<refresh_token>"}`
- Refresh Token: `POST /api/v1/token/refresh/`
  - Body: `{"refresh": "<refresh_token>"}`
  - Response: `{"access": "<new_access_token>"}`

All endpoints except `GET /api/v1/health/` require the access token passed in the Authorization header:
```http
Authorization: Bearer <access_token>
```

---

## API Endpoints Reference

All application endpoints are versioned under `/api/v1/`. List endpoints are paginated at 20 records per page.

| Method | Path | Auth Required | Description |
|---|---|---|---|
| `GET` | `/api/v1/health/` | No | Reports database connectivity. Returns `200 OK`. |
| `POST` | `/api/v1/assets/` | Yes | Create an asset. |
| `GET` | `/api/v1/assets/` | Yes | List assets. Supports `status` and `category` query filters, plus `search` on `name` or `asset_tag`. Paginated at 20 per page. |
| `GET` | `/api/v1/assets/{id}/` | Yes | Retrieve one asset with `current_holder` (null if available, or employee code and full name). |
| `POST` | `/api/v1/checkouts/` | Yes | Check out an asset. Body: `{"asset_tag": "...", "employee_code": "...", "due_at": "..."}`. Enforces business rules 1 to 5, 7, and 8. |
| `POST` | `/api/v1/checkouts/{id}/return/` | Yes | Return an asset. Body: `{"condition_note": "...", "needs_maintenance": false}`. Enforces rule 6. |
| `GET` | `/api/v1/employees/{employee_code}/summary/` | Yes | Aggregates lifetime count, currently held count, currently overdue count, and mean hold duration in days via a single database ORM query. |
| `GET` | `/api/v1/reports/overdue/` | Yes | Returns open checkouts past `due_at`, ordered by most overdue first. Does not issue N+1 queries. |

---

## Concurrency and Transaction Isolation (Rule 7)

Concurrent check-out attempts for the same asset are handled at the database level using pessimistic row-level locking:
1. `Asset.objects.select_for_update().get(asset_tag=asset_tag)` is executed inside an atomic transaction block (`transaction.atomic()`).
2. The initial transaction acquires a row lock on the asset record in PostgreSQL.
3. A concurrent transaction attempting to read the same asset blocks at the database level until the initial transaction commits or rolls back.
4. When the second transaction unblocks, it reads the updated status (`CHECKED_OUT`) and immediately raises a `ConflictException`, returning `HTTP 409 Conflict`.
5. Application-level flags and sleep calls are not used.

---

## Background Task and Scheduler

- Task Name: `core.tasks.flag_overdue_checkouts`
- Execution: Hourly via Celery Beat schedule configured in `config/celery.py`.
- Broker and Result Backend: Redis running on port 6379.
- Idempotency: `OverdueNotice` enforces a database-level unique constraint on `(checkout, notice_date)`. The task utilizes `get_or_create` to prevent duplicate notice creation during retries or multiple executions on the same date.

---

## Management Command: Seed Demo Data

The management command seeds reproducible, idempotent data for testing:
```bash
python manage.py seed_demo_data
```

The command seeds:
- 4 Employees (3 active, 1 inactive).
- 8 Assets across all four categories (`CAMERA`, `LAPTOP`, `SENSOR`, `VEHICLE`).
- 5 Check-out records covering all required test conditions:
  - 2 currently overdue check-outs.
  - 2 check-outs returned on time.
  - 1 check-out returned late.

---

## Assumptions

1. **Overdue Threshold Inclusion**:
   A check-out whose `due_at` timestamp is equal to or less than `timezone.now()` is treated as overdue.

2. **Mean Hold Duration Calculation**:
   Mean hold duration in `GET /employees/{employee_code}/summary/` includes only completed check-outs where `returned_at` is not null. Duration is calculated as `returned_at - checked_out_at` in days. If an employee has zero completed check-outs, `mean_hold_duration_days` returns `null`.

3. **Asset Detail View Current Holder**:
   `GET /api/v1/assets/{id}/` returns `current_holder` as `null` when the asset status is `AVAILABLE`. When the asset status is `CHECKED_OUT`, it returns an object containing `employee_code` and `full_name`.

4. **Return Endpoint Path**:
   The return endpoint is exposed at `POST /api/v1/checkouts/{id}/return/` matching section A3 of the assessment specification.

5. **Celery Beat Embedding**:
   Celery Beat is started with the `-B` flag inside the Celery worker container service (`celery -A config worker -B -l info`). This fulfills the four-service docker-compose constraint without requiring an additional standalone scheduler container.

---

## Known Gaps

1. **Celery Task Worker Concurrency Under Heavy Load**:
   The current Celery task iterates through overdue records sequentially. If overdue checkouts exceed 100,000 records, batching with Celery chord groups and bulk database operations is required to prevent worker execution timeouts.

2. **JWT Refresh Token Blacklist**:
   Token rotation and blacklisting apps (`rest_framework_simplejwt.token_blacklist`) are not installed. Tokens remain valid until their expiration lifetime (60 minutes for access tokens) elapses.
