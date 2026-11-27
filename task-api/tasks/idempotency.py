"""
In-memory idempotency store for POST endpoints.

    _STORE = {key: {"path": ..., "body": ..., "response": {"data": ..., "status": ...}}}

Rules (Step 6 of the brief):
  * No Idempotency-Key header      -> run the handler normally.
  * Same key + same path + body    -> return the stored response; the handler
                                      (and therefore the parser) is NOT called.
  * Same key + different body/path -> 422 in the standard error shape.
  * Only successful (2xx) responses are stored, so a client can fix bad input
    and retry with the same key.

The store lives in process memory: it is cleared when the server restarts and
is not shared between worker processes. That is fine for this project; in
production you would use Redis or a DB table with an expiry.
"""
import copy
import json
import threading

from rest_framework import status as http_status
from rest_framework.response import Response

from .errors import error_response

_STORE = {}
_LOCK = threading.Lock()

HEADER = "Idempotency-Key"


def _canonical_body(data):
    """Turn request.data into a stable string so two bodies can be compared."""
    if hasattr(data, "dict"):  # QueryDict (form data)
        data = data.dict()
    try:
        return json.dumps(data, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(data)


def run_idempotent(request, handler):
    """Run `handler()` (which returns a Response) with idempotency protection."""
    key = (request.headers.get(HEADER) or "").strip()
    if not key:
        return handler()

    body = _canonical_body(request.data)
    path = request.path

    # The lock makes "check, create, store" atomic, so two concurrent retries
    # with the same key cannot both create a row.
    with _LOCK:
        stored = _STORE.get(key)
        if stored is not None:
            if stored["body"] != body or stored["path"] != path:
                return error_response(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was already used with a different request body",
                    [{"field": HEADER, "issue": "reused with a different request"}],
                    http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            saved = stored["response"]
            replay = Response(copy.deepcopy(saved["data"]), status=saved["status"])
            replay["Idempotent-Replayed"] = "true"
            return replay

        response = handler()

        if 200 <= response.status_code < 300:
            _STORE[key] = {
                "path": path,
                "body": body,
                "response": {
                    "data": copy.deepcopy(response.data),
                    "status": response.status_code,
                },
            }
        return response


def clear_store():
    """Used by tests."""
    with _LOCK:
        _STORE.clear()
