# Smart Task API

A Django REST Framework API where you can type a task in plain English, such as
*"Remind me to pay rent on the 1st of every month"*, and get a structured task back.

Features:

- Full CRUD for tasks, backed by Postgres
- Validation with **one consistent error shape** for every error
- **Natural-language task creation** behind a swappable `parse_task_text()` function (rule-based now, LLM later)
- **Subtasks** and automatic **breakdown** of a task into subtasks
- **Idempotent** POSTs using an `Idempotency-Key` header
- **Filtering, search, ordering and pagination**, all usable in one request
- Automated tests, a Postman / Echo API collection and a hand-written `openapi.yaml`

---

## Project structure

```
config/            Django project (settings, urls)
tasks/
  models.py        Task model (self-referencing parent FK for subtasks)
  serializers.py   TaskSerializer + strict field types
  errors.py        error_response(), serializer_errors_to_details(), exception handler
  idempotency.py   In-memory Idempotency-Key store
  ai.py            parse_task_text(), break_down_task(), optional LLM wrapper
  filters.py       django-filter FilterSet + priority-aware ordering
  pagination.py    Page number pagination
  views.py         TaskViewSet with @action endpoints
  tests.py         Automated tests (python manage.py test)
postman/           Postman / Echo API collection (63 requests)
openapi.yaml       Hand-written OpenAPI 3 spec
docker-compose.yml Postgres for local development
```

---

## Setup

Requirements: Python 3.10+, Docker Desktop.

### 1. Virtual environment and packages

Windows (PowerShell):

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Postgres in Docker

```bash
docker compose up -d
```

This starts Postgres 16 on `localhost:5432` with database `tasks_db`,
user `postgres`, password `postgres`. If you already run Postgres in Docker
from class, skip this and set the environment variables below to match it.

| Variable | Default |
|---|---|
| `POSTGRES_DB` | `tasks_db` |
| `POSTGRES_USER` | `postgres` |
| `POSTGRES_PASSWORD` | `postgres` |
| `POSTGRES_HOST` | `localhost` |
| `POSTGRES_PORT` | `5432` |

### 3. Migrate and run

```bash
python manage.py migrate
python manage.py runserver
```

The API is at `http://127.0.0.1:8000/api/tasks/`.

### 4. Run the tests

```bash
python manage.py test tasks -v 2
```

### 5. Optional: use a real LLM

The rule-based parser is used by default. To use an LLM (any
OpenAI-compatible chat completions API), set these before `runserver`:

```powershell
# PowerShell
$env:TASK_PARSER="llm"
$env:LLM_API_KEY="your-key-here"
```

```bash
# bash
export TASK_PARSER=llm
export LLM_API_KEY=your-key-here
```

`settings.py` reads the key with `os.environ.get("LLM_API_KEY")`. If
`TASK_PARSER=llm` and the key is missing, `/api/tasks/from-text/` returns
**503** in the standard error shape instead of crashing. `.env` is in
`.gitignore`; never commit a key.

---

## The swappable AI functions

Both live in `tasks/ai.py`:

```python
def parse_task_text(text: str, today: datetime.date) -> dict:
    """Returns:
    {
      "title": str,
      "description": str,
      "due_date": "YYYY-MM-DD" | None,
      "priority": "low" | "medium" | "high",
      "recurrence": "daily" | "weekly" | "monthly" | "yearly" | None
    }
    """

def break_down_task(task: Task) -> list[dict]:
    """Returns a list of subtask dicts with the same keys."""
```

To switch to an LLM, write a function with the same signature that returns
the same dict shape (`llm_parse_task_text` is an example) and select it with
`TASK_PARSER=llm`. The views do not change.

**Never trust the output:** whatever the parser returns goes through
`TaskSerializer` exactly like user input. If validation fails, the API returns
**502** in the standard error shape and saves nothing. If the parser raises
an exception, it also returns **502**.

The rule-based parser understands:

| Phrase | Result |
|---|---|
| `remind me to`, `please`, `don't forget to` | stripped from the title |
| `today`, `tonight`, `tomorrow`, `day after tomorrow`, `next week` | due date |
| `in 3 days`, `in 2 weeks` | due date |
| `on friday`, `next monday` | next matching weekday |
| `on the 1st`, `15th of the month` | next date with that day of month |
| `2026-12-05` | that date (still validated) |
| `every day` / `daily`, `every week` / `weekly`, `every monday`, `every month` / `monthly`, `every year` / `yearly` | recurrence |
| `urgent`, `asap`, `important` | priority `high` |
| `no rush`, `someday`, `low priority` | priority `low` |
| `at 5pm` | stripped (times are not stored) |

---

## Error shape

Every error response (400, 404, 422, 502, 503) looks like this. `code` and
`message` are always strings, and `details` is always a list.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "details": [{ "field": "title", "issue": "must not be empty" }]
  }
}
```

| Code | Status | When |
|---|---|---|
| `VALIDATION_ERROR` | 400 | Invalid body or invalid filter value |
| `PARSE_ERROR` | 400 | Malformed JSON |
| `SUBTASK_NOT_ALLOWED` | 400 | Adding subtasks to, or breaking down, a subtask |
| `NOT_FOUND` | 404 | Unknown task id or invalid page |
| `IDEMPOTENCY_KEY_REUSED` | 422 | Same `Idempotency-Key`, different body |
| `PARSER_ERROR` / `BREAKDOWN_ERROR` | 502 | The AI function raised an exception |
| `INVALID_PARSER_OUTPUT` / `INVALID_BREAKDOWN_OUTPUT` | 502 | The AI function returned data that failed validation |
| `LLM_NOT_CONFIGURED` | 503 | LLM parser selected but `LLM_API_KEY` missing |

How it is built: `error_response(code, message, details, status)` builds the
response, `serializer_errors_to_details()` flattens `serializer.errors` into
the `details` list, and `create` / `retrieve` / `update` / `destroy` are
overridden to call `is_valid()` themselves and catch the missing-object case.
A custom DRF `EXCEPTION_HANDLER` is a safety net for errors DRF raises on its
own (bad JSON, 405, invalid page, invalid filter).

### Validation rules

- `title` and `description` are required, must be strings, and must not be empty or whitespace only. `title` is at most 200 characters.
- `completed` must be a JSON boolean (`true` / `false`). `"yes"`, `"true"` and `1` are rejected.
- `due_date`, if given, must be a real `YYYY-MM-DD` date. `2026-02-30` is rejected. `null` is allowed.
- `priority` must be one of `low`, `medium`, `high` (default `medium`).
- `recurrence`, if given, must be one of `daily`, `weekly`, `monthly`, `yearly`.

---

## Endpoints

Base URL: `http://127.0.0.1:8000/api`

| Method | Path | Description |
|---|---|---|
| GET | `/tasks/` | List tasks (filter, search, order, paginate) |
| POST | `/tasks/` | Create a task (supports `Idempotency-Key`) |
| GET | `/tasks/{id}/` | Get one task |
| PUT | `/tasks/{id}/` | Replace a task |
| PATCH | `/tasks/{id}/` | Partially update a task |
| DELETE | `/tasks/{id}/` | Delete a task and its subtasks |
| POST | `/tasks/from-text/` | Create a task from plain English (supports `Idempotency-Key`) |
| GET | `/tasks/{id}/subtasks/` | List subtasks |
| POST | `/tasks/{id}/subtasks/` | Create one subtask |
| POST | `/tasks/{id}/breakdown/` | Auto-create subtasks (supports `Idempotency-Key`) |

### Create a task

```http
POST /api/tasks/
Content-Type: application/json

{ "title": "Write report", "description": "Quarterly report", "priority": "high", "due_date": "2026-12-15" }
```

`201 Created`

```json
{
  "id": 1,
  "title": "Write report",
  "description": "Quarterly report",
  "completed": false,
  "due_date": "2026-12-15",
  "priority": "high",
  "recurrence": null,
  "parent": null,
  "subtask_count": 0,
  "created_at": "2026-11-27T12:00:00+05:30",
  "updated_at": "2026-11-27T12:00:00+05:30"
}
```

### Validation error

```http
POST /api/tasks/
Content-Type: application/json

{ "title": "", "description": "x", "due_date": "2026-02-30", "priority": "urgent" }
```

`400 Bad Request`

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "details": [
      { "field": "title", "issue": "must not be empty" },
      { "field": "due_date", "issue": "must be a real date in YYYY-MM-DD format" },
      { "field": "priority", "issue": "must be one of low, medium, high" }
    ]
  }
}
```

### List tasks

```http
GET /api/tasks/
```

`200 OK`

```json
{ "count": 1, "next": null, "previous": null, "results": [ { "id": 1, "title": "Write report", "...": "..." } ] }
```

### Get, update, delete

```http
GET /api/tasks/1/                     -> 200 task
PUT /api/tasks/1/   {full body}       -> 200 task
PATCH /api/tasks/1/ {"completed": true} -> 200 task
DELETE /api/tasks/1/                  -> 204 (subtasks deleted too)
```

Unknown id:

```http
GET /api/tasks/999/
```

`404 Not Found`

```json
{ "error": { "code": "NOT_FOUND", "message": "Task not found", "details": [] } }
```

### Create from text (AI endpoint)

```http
POST /api/tasks/from-text/
Content-Type: application/json

{ "text": "Remind me to pay rent on the 1st of every month" }
```

`201 Created`

```json
{
  "id": 2,
  "title": "Pay rent",
  "description": "Remind me to pay rent on the 1st of every month",
  "completed": false,
  "due_date": "2026-12-01",
  "priority": "medium",
  "recurrence": "monthly",
  "parent": null,
  "subtask_count": 0,
  "created_at": "2026-11-27T12:01:00+05:30",
  "updated_at": "2026-11-27T12:01:00+05:30"
}
```

Bad parser output (for example `{"text": "tomorrow"}`, which leaves no title):

`502 Bad Gateway`

```json
{
  "error": {
    "code": "INVALID_PARSER_OUTPUT",
    "message": "Parser returned data that failed validation",
    "details": [{ "field": "title", "issue": "must not be empty" }]
  }
}
```

### Subtasks

```http
POST /api/tasks/3/subtasks/
Content-Type: application/json

{ "title": "Check visa", "description": "Passport valid?" }
```

`201 Created` — same task shape, with `"parent": 3`.

```http
GET /api/tasks/3/subtasks/
```

`200 OK` — a list of subtasks.

A subtask cannot have subtasks:

```http
POST /api/tasks/4/subtasks/      (task 4 is a subtask)
```

`400 Bad Request`

```json
{
  "error": {
    "code": "SUBTASK_NOT_ALLOWED",
    "message": "A subtask cannot have its own subtasks",
    "details": [{ "field": "parent", "issue": "task is already a subtask" }]
  }
}
```

### Breakdown (AI endpoint)

```http
POST /api/tasks/3/breakdown/
```

Task 3's title is "Book flights, reserve hotel and pack bags".

`201 Created`

```json
{
  "parent": 3,
  "count": 3,
  "subtasks": [
    { "id": 5, "title": "Book flights", "description": "Subtask of: Book flights, reserve hotel and pack bags", "parent": 3, "...": "..." },
    { "id": 6, "title": "Reserve hotel", "...": "..." },
    { "id": 7, "title": "Pack bags", "...": "..." }
  ]
}
```

`break_down_task` splits on commas, `and` and `then`. If the title has only
one part it tries the description; otherwise it uses a Plan / Do / Review
template. Every item is validated before any is saved; if one is invalid,
none are saved and the API returns 502.

### Idempotency

```http
POST /api/tasks/
Idempotency-Key: 7f9c-123
Content-Type: application/json

{ "title": "Pay rent", "description": "December" }
```

- Same key + same body again → the **stored** `201` response is returned (header `Idempotent-Replayed: true`). Nothing is created and the parser is not called again.
- Same key + different body → `422` with code `IDEMPOTENCY_KEY_REUSED`.
- Only successful responses are stored, so you can fix bad input and retry with the same key.

Works on `POST /tasks/`, `POST /tasks/from-text/` and `POST /tasks/{id}/breakdown/`.
The store is an in-memory dict, so it resets when the server restarts.

### Filtering, search, ordering, pagination

| Parameter | Example |
|---|---|
| `completed` | `?completed=false` |
| `priority` | `?priority=high` |
| `recurrence` | `?recurrence=monthly` |
| `due_date`, `due_after`, `due_before` | `?due_after=2026-12-01&due_before=2026-12-31` |
| `parent` | `?parent=3` |
| `is_subtask` | `?is_subtask=false` (top-level only) |
| `search` | `?search=rent` (title and description) |
| `ordering` | `?ordering=-priority,due_date` (`priority` sorts low < medium < high) |
| `page`, `page_size` | `?page=2&page_size=5` (max 100) |

All together:

```http
GET /api/tasks/?completed=false&priority=high&search=pay&ordering=due_date&page=1&page_size=2
```

---

## Testing

### Automated tests

`python manage.py test tasks -v 2` runs tests for CRUD, every validation rule,
the error shape, the AI endpoints (including bad parser output and a parser
crash, using `unittest.mock`), subtasks, cascade delete, breakdown,
idempotency, and filtering / search / ordering / pagination together.

### Postman / Echo API collection

Import `postman/SmartTaskAPI.postman_collection.json` into Postman or Echo
API. Set `baseUrl` if needed (default `http://127.0.0.1:8000/api`) and run
the folders in order. Every request has assertions, including checks of the
error shape.

### What the AI-generated checklist missed, and what I changed

I asked an AI assistant for a test checklist and Postman requests. It covered
the happy paths (create, list, get, update, delete, from-text with a simple
sentence) and a couple of obvious failures (empty title, unknown id). When I
ran the requests and critiqued the list, these were missing:

1. **Impossible dates.** It tested `"abc"` as a date but not `2026-02-30` or `2026-13-01`. I added both, plus a valid leap day (`2028-02-29`) to make sure real dates still pass.
2. **Wrong types.** Nothing sent `"completed": "yes"` or `1`, or `"title": 123`. DRF accepts all three by default (it turns `123` into `"123"` and `"yes"` into `true`). I added `StrictCharField` and `StrictBooleanField` to the serializer and requests for each case.
3. **Whitespace-only title** (`"   "`). Added.
4. **Bad parser output.** It assumed the parser always returns good data. I added requests and tests for an empty title, wrong types, a non-dict return value and a parser exception, and confirmed each returns 502 with no row saved. I also temporarily made `parse_task_text` return an empty title and checked it in the debugger and the DB client.
5. **Idempotency edge cases.** It sent the same key twice but never tested the same key with a **different body** (422), the same key on a **different endpoint**, or that a **failed** request is not stored. All added.
6. **Subtask of a subtask**, and breakdown of a subtask (400). Added.
7. **Breakdown with one invalid item.** Added a test that one bad item means none are saved.
8. **Error shape on every error.** It only checked status codes. Every error request now asserts `code` and `message` are strings and `details` is a list. This found that malformed JSON and invalid page numbers returned DRF's default `{"detail": ...}`, so I added the custom exception handler.
9. **All list features in one request.** It tested filter, search, ordering and pagination separately. Added one combined request.
10. **Priority ordering.** `?ordering=priority` sorted alphabetically (high, low, medium). I added a `priority_rank` annotation so it sorts low < medium < high.

---

## Verifying in the DB client

- **Idempotency:** send *Idempotency → 1* and *2* from the collection, then run `SELECT id, title FROM tasks_task WHERE title = 'Idempotent task';` — one row.
- **Cascade delete:** create a parent with subtasks, delete the parent, then `SELECT * FROM tasks_task WHERE parent_id = <id>;` — no rows.
- **Bad parser output:** send *Parser gives empty title -> 502* and confirm the row count did not change.

---

## OpenAPI

`openapi.yaml` describes every endpoint, request body, response and the error
schema. View it by pasting it into https://editor.swagger.io.
