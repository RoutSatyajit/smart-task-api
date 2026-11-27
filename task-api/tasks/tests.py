"""
Run with:  python manage.py test tasks -v 2

Covers CRUD, validation + error shape, natural-language creation (including
bad parser output), subtasks, breakdown, idempotency, and filtering /
search / ordering / pagination together.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from . import ai
from .idempotency import clear_store
from .models import Task

LIST_URL = "/api/tasks/"
FROM_TEXT_URL = "/api/tasks/from-text/"


def detail_url(pk):
    return f"/api/tasks/{pk}/"


def subtasks_url(pk):
    return f"/api/tasks/{pk}/subtasks/"


def breakdown_url(pk):
    return f"/api/tasks/{pk}/breakdown/"


VALID = {"title": "Write report", "description": "Quarterly report", "priority": "high"}


class ErrorShapeMixin:
    def assertErrorShape(self, response, status_code, code=None, field=None):
        self.assertEqual(response.status_code, status_code, response.content)
        body = response.json()
        self.assertEqual(set(body.keys()), {"error"})
        err = body["error"]
        self.assertEqual(set(err.keys()), {"code", "message", "details"})
        self.assertIsInstance(err["code"], str)
        self.assertIsInstance(err["message"], str)
        self.assertIsInstance(err["details"], list)
        for item in err["details"]:
            self.assertEqual(set(item.keys()), {"field", "issue"})
        if code:
            self.assertEqual(err["code"], code)
        if field:
            self.assertIn(field, [d["field"] for d in err["details"]])
        return err


class BaseAPITest(ErrorShapeMixin, APITestCase):
    def setUp(self):
        clear_store()

    def make_task(self, **kwargs):
        data = {"title": "Task", "description": "Desc", **kwargs}
        return Task.objects.create(**data)


# ---------------------------------------------------------------------------
# Step 2 - CRUD
# ---------------------------------------------------------------------------
class CrudTests(BaseAPITest):
    def test_create_task(self):
        r = self.client.post(LIST_URL, VALID, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["title"], "Write report")
        self.assertFalse(r.json()["completed"])
        self.assertEqual(Task.objects.count(), 1)

    def test_default_priority_is_medium(self):
        r = self.client.post(LIST_URL, {"title": "a", "description": "b"}, format="json")
        self.assertEqual(r.json()["priority"], "medium")

    def test_list_is_paginated(self):
        self.make_task()
        r = self.client.get(LIST_URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json().keys()), {"count", "next", "previous", "results"})

    def test_retrieve(self):
        t = self.make_task()
        r = self.client.get(detail_url(t.id))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], t.id)

    def test_put_and_patch(self):
        t = self.make_task()
        r = self.client.put(detail_url(t.id), {**VALID, "completed": True}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["completed"])
        r = self.client.patch(detail_url(t.id), {"priority": "low"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["priority"], "low")
        self.assertEqual(r.json()["title"], "Write report")

    def test_delete(self):
        t = self.make_task()
        r = self.client.delete(detail_url(t.id))
        self.assertEqual(r.status_code, 204)
        self.assertFalse(Task.objects.exists())


# ---------------------------------------------------------------------------
# Step 3 - validation and error shape
# ---------------------------------------------------------------------------
class ValidationTests(BaseAPITest):
    def test_empty_title(self):
        r = self.client.post(LIST_URL, {**VALID, "title": ""}, format="json")
        err = self.assertErrorShape(r, 400, "VALIDATION_ERROR", "title")
        self.assertIn({"field": "title", "issue": "must not be empty"}, err["details"])

    def test_whitespace_title(self):
        r = self.client.post(LIST_URL, {**VALID, "title": "   "}, format="json")
        self.assertErrorShape(r, 400, "VALIDATION_ERROR", "title")

    def test_empty_description(self):
        r = self.client.post(LIST_URL, {**VALID, "description": ""}, format="json")
        self.assertErrorShape(r, 400, "VALIDATION_ERROR", "description")

    def test_missing_fields(self):
        r = self.client.post(LIST_URL, {}, format="json")
        err = self.assertErrorShape(r, 400, "VALIDATION_ERROR")
        fields = {d["field"] for d in err["details"]}
        self.assertEqual(fields, {"title", "description"})

    def test_title_wrong_type(self):
        r = self.client.post(LIST_URL, {**VALID, "title": 123}, format="json")
        self.assertErrorShape(r, 400, "VALIDATION_ERROR", "title")

    def test_completed_must_be_boolean(self):
        for bad in ["yes", "true", 1, 0, None]:
            r = self.client.post(LIST_URL, {**VALID, "completed": bad}, format="json")
            self.assertErrorShape(r, 400, "VALIDATION_ERROR", "completed")

    def test_impossible_date_rejected(self):
        r = self.client.post(LIST_URL, {**VALID, "due_date": "2026-02-30"}, format="json")
        err = self.assertErrorShape(r, 400, "VALIDATION_ERROR", "due_date")
        self.assertIn("YYYY-MM-DD", err["details"][0]["issue"])

    def test_wrong_date_format_rejected(self):
        for bad in ["30-01-2026", "2026/01/30", "tomorrow", "2026-1-5x"]:
            r = self.client.post(LIST_URL, {**VALID, "due_date": bad}, format="json")
            self.assertErrorShape(r, 400, "VALIDATION_ERROR", "due_date")

    def test_valid_date_and_leap_day(self):
        r = self.client.post(LIST_URL, {**VALID, "due_date": "2028-02-29"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["due_date"], "2028-02-29")

    def test_null_due_date_allowed(self):
        r = self.client.post(LIST_URL, {**VALID, "due_date": None}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_invalid_priority(self):
        r = self.client.post(LIST_URL, {**VALID, "priority": "urgent"}, format="json")
        err = self.assertErrorShape(r, 400, "VALIDATION_ERROR", "priority")
        self.assertEqual(err["details"][0]["issue"], "must be one of low, medium, high")

    def test_multiple_errors_reported_together(self):
        r = self.client.post(
            LIST_URL,
            {"title": "", "description": "", "priority": "x", "due_date": "2026-13-01"},
            format="json",
        )
        err = self.assertErrorShape(r, 400, "VALIDATION_ERROR")
        self.assertEqual(
            {d["field"] for d in err["details"]},
            {"title", "description", "priority", "due_date"},
        )

    def test_update_validation(self):
        t = self.make_task()
        r = self.client.patch(detail_url(t.id), {"title": ""}, format="json")
        self.assertErrorShape(r, 400, "VALIDATION_ERROR", "title")

    def test_malformed_json(self):
        r = self.client.post(LIST_URL, "{bad json", content_type="application/json")
        self.assertErrorShape(r, 400, "PARSE_ERROR")

    def test_404_shape_on_retrieve_update_delete(self):
        for method in ("get", "put", "patch", "delete"):
            r = getattr(self.client, method)(detail_url(99999), VALID, format="json")
            self.assertErrorShape(r, 404, "NOT_FOUND")

    def test_404_on_non_numeric_id(self):
        r = self.client.get(detail_url("abc"))
        self.assertErrorShape(r, 404, "NOT_FOUND")


# ---------------------------------------------------------------------------
# Step 4 - natural-language creation
# ---------------------------------------------------------------------------
class FromTextTests(BaseAPITest):
    def test_rent_example(self):
        r = self.client.post(
            FROM_TEXT_URL, {"text": "Remind me to pay rent on the 1st of every month"}, format="json"
        )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["title"], "Pay rent")
        self.assertEqual(body["recurrence"], "monthly")
        self.assertTrue(body["due_date"].endswith("-01"))

    def test_tomorrow(self):
        r = self.client.post(FROM_TEXT_URL, {"text": "Call the bank tomorrow"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["due_date"], (date.today() + timedelta(days=1)).isoformat())

    def test_empty_text_400(self):
        for body in ({}, {"text": ""}, {"text": "   "}, {"text": 42}):
            r = self.client.post(FROM_TEXT_URL, body, format="json")
            self.assertErrorShape(r, 400, "VALIDATION_ERROR", "text")
        self.assertEqual(Task.objects.count(), 0)

    def test_text_that_parses_to_empty_title_gives_502(self):
        r = self.client.post(FROM_TEXT_URL, {"text": "tomorrow"}, format="json")
        self.assertErrorShape(r, 502, "INVALID_PARSER_OUTPUT", "title")
        self.assertEqual(Task.objects.count(), 0)

    @patch("tasks.ai.parse_task_text")
    def test_bad_parser_output_empty_title(self, mock_parse):
        mock_parse.return_value = {"title": "", "description": "x", "due_date": None}
        r = self.client.post(FROM_TEXT_URL, {"text": "anything"}, format="json")
        self.assertErrorShape(r, 502, "INVALID_PARSER_OUTPUT", "title")
        self.assertEqual(Task.objects.count(), 0)

    @patch("tasks.ai.parse_task_text")
    def test_bad_parser_output_wrong_types(self, mock_parse):
        mock_parse.return_value = {
            "title": 123, "description": "x", "due_date": "2026-02-30", "priority": "urgent"
        }
        r = self.client.post(FROM_TEXT_URL, {"text": "anything"}, format="json")
        err = self.assertErrorShape(r, 502, "INVALID_PARSER_OUTPUT")
        self.assertEqual({d["field"] for d in err["details"]}, {"title", "due_date", "priority"})
        self.assertEqual(Task.objects.count(), 0)

    @patch("tasks.ai.parse_task_text")
    def test_parser_returns_non_dict(self, mock_parse):
        mock_parse.return_value = ["not", "a", "dict"]
        r = self.client.post(FROM_TEXT_URL, {"text": "anything"}, format="json")
        self.assertErrorShape(r, 502, "INVALID_PARSER_OUTPUT")

    @patch("tasks.ai.parse_task_text", side_effect=RuntimeError("boom"))
    def test_parser_exception_502(self, _):
        r = self.client.post(FROM_TEXT_URL, {"text": "anything"}, format="json")
        self.assertErrorShape(r, 502, "PARSER_ERROR")
        self.assertEqual(Task.objects.count(), 0)

    @override_settings(TASK_PARSER="llm", LLM_API_KEY=None)
    def test_llm_without_key_503(self):
        r = self.client.post(FROM_TEXT_URL, {"text": "pay rent tomorrow"}, format="json")
        self.assertErrorShape(r, 503, "LLM_NOT_CONFIGURED")
        self.assertEqual(Task.objects.count(), 0)


# ---------------------------------------------------------------------------
# Step 5 - subtasks and breakdown
# ---------------------------------------------------------------------------
class SubtaskTests(BaseAPITest):
    def test_create_and_list_subtasks(self):
        parent = self.make_task()
        r = self.client.post(subtasks_url(parent.id), {"title": "s1", "description": "d"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["parent"], parent.id)
        r = self.client.get(subtasks_url(parent.id))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 1)

    def test_subtask_validation(self):
        parent = self.make_task()
        r = self.client.post(subtasks_url(parent.id), {"title": ""}, format="json")
        self.assertErrorShape(r, 400, "VALIDATION_ERROR", "title")

    def test_subtask_of_subtask_rejected(self):
        parent = self.make_task()
        child = self.make_task(parent=parent)
        r = self.client.post(subtasks_url(child.id), {"title": "x", "description": "y"}, format="json")
        self.assertErrorShape(r, 400, "SUBTASK_NOT_ALLOWED")

    def test_subtasks_unknown_parent(self):
        r = self.client.get(subtasks_url(99999))
        self.assertErrorShape(r, 404, "NOT_FOUND")
        r = self.client.post(subtasks_url(99999), {"title": "x", "description": "y"}, format="json")
        self.assertErrorShape(r, 404, "NOT_FOUND")

    def test_cascade_delete(self):
        parent = self.make_task()
        self.make_task(parent=parent)
        self.make_task(parent=parent)
        self.assertEqual(Task.objects.count(), 3)
        self.client.delete(detail_url(parent.id))
        self.assertEqual(Task.objects.count(), 0)

    def test_breakdown_splits_on_and_and_commas(self):
        parent = self.make_task(title="Book flights, reserve hotel and pack bags")
        r = self.client.post(breakdown_url(parent.id), {}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        titles = [s["title"] for s in r.json()["subtasks"]]
        self.assertEqual(titles, ["Book flights", "Reserve hotel", "Pack bags"])
        self.assertEqual(parent.subtasks.count(), 3)

    def test_breakdown_template_fallback(self):
        parent = self.make_task(title="Prepare demo", description="Nothing to split")
        r = self.client.post(breakdown_url(parent.id), {}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["count"], 3)

    def test_breakdown_of_subtask_rejected(self):
        parent = self.make_task()
        child = self.make_task(parent=parent)
        r = self.client.post(breakdown_url(child.id), {}, format="json")
        self.assertErrorShape(r, 400, "SUBTASK_NOT_ALLOWED")

    def test_breakdown_unknown_task(self):
        r = self.client.post(breakdown_url(99999), {}, format="json")
        self.assertErrorShape(r, 404, "NOT_FOUND")

    @patch("tasks.ai.break_down_task")
    def test_breakdown_one_invalid_item_saves_none(self, mock_breakdown):
        mock_breakdown.return_value = [
            {"title": "ok", "description": "fine"},
            {"title": "", "description": "bad"},
        ]
        parent = self.make_task()
        r = self.client.post(breakdown_url(parent.id), {}, format="json")
        self.assertErrorShape(r, 502, "INVALID_BREAKDOWN_OUTPUT", "subtasks[1].title")
        self.assertEqual(parent.subtasks.count(), 0)


# ---------------------------------------------------------------------------
# Step 6 - idempotency
# ---------------------------------------------------------------------------
class IdempotencyTests(BaseAPITest):
    def test_same_key_same_body_creates_one_row(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "abc-123"}
        r1 = self.client.post(LIST_URL, VALID, format="json", **h)
        r2 = self.client.post(LIST_URL, VALID, format="json", **h)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r1.json(), r2.json())
        self.assertEqual(r2["Idempotent-Replayed"], "true")
        self.assertEqual(Task.objects.count(), 1)

    def test_same_key_different_body_422(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "abc-123"}
        self.client.post(LIST_URL, VALID, format="json", **h)
        r = self.client.post(LIST_URL, {**VALID, "title": "Other"}, format="json", **h)
        self.assertErrorShape(r, 422, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(Task.objects.count(), 1)

    def test_failed_request_not_stored(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "fix-me"}
        r = self.client.post(LIST_URL, {**VALID, "title": ""}, format="json", **h)
        self.assertEqual(r.status_code, 400)
        r = self.client.post(LIST_URL, VALID, format="json", **h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Task.objects.count(), 1)

    def test_no_key_creates_duplicates(self):
        self.client.post(LIST_URL, VALID, format="json")
        self.client.post(LIST_URL, VALID, format="json")
        self.assertEqual(Task.objects.count(), 2)

    def test_from_text_replay_does_not_call_parser_again(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "text-1"}
        body = {"text": "Pay rent tomorrow"}
        with patch("tasks.ai.parse_task_text", wraps=ai.parse_task_text) as spy:
            self.client.post(FROM_TEXT_URL, body, format="json", **h)
            self.client.post(FROM_TEXT_URL, body, format="json", **h)
            self.assertEqual(spy.call_count, 1)
        self.assertEqual(Task.objects.count(), 1)

    def test_breakdown_replay(self):
        parent = self.make_task(title="A and B")
        h = {"HTTP_IDEMPOTENCY_KEY": "bd-1"}
        self.client.post(breakdown_url(parent.id), {}, format="json", **h)
        r = self.client.post(breakdown_url(parent.id), {}, format="json", **h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(parent.subtasks.count(), 2)

    def test_same_key_other_endpoint_422(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "shared"}
        a = self.make_task(title="A and B")
        b = self.make_task(title="C and D")
        self.client.post(breakdown_url(a.id), {}, format="json", **h)
        r = self.client.post(breakdown_url(b.id), {}, format="json", **h)
        self.assertErrorShape(r, 422, "IDEMPOTENCY_KEY_REUSED")


# ---------------------------------------------------------------------------
# Step 7 - filtering, search, ordering, pagination
# ---------------------------------------------------------------------------
class ListQueryTests(BaseAPITest):
    def setUp(self):
        super().setUp()
        self.make_task(title="Pay rent", priority="high", due_date=date(2026, 12, 1))
        self.make_task(title="Pay electricity bill", priority="high", due_date=date(2026, 12, 10))
        self.make_task(title="Pay gym", priority="low", due_date=date(2026, 12, 5))
        self.make_task(title="Read book", priority="high", due_date=date(2026, 12, 2))
        self.make_task(title="Pay tax", priority="high", completed=True, due_date=date(2026, 12, 3))

    def test_filter(self):
        r = self.client.get(LIST_URL, {"priority": "high", "completed": "false"})
        self.assertEqual(r.json()["count"], 3)

    def test_search(self):
        r = self.client.get(LIST_URL, {"search": "pay"})
        self.assertEqual(r.json()["count"], 4)

    def test_date_range(self):
        r = self.client.get(LIST_URL, {"due_after": "2026-12-02", "due_before": "2026-12-05"})
        self.assertEqual(r.json()["count"], 3)

    def test_ordering_by_priority_rank(self):
        r = self.client.get(LIST_URL, {"ordering": "priority"})
        self.assertEqual(r.json()["results"][0]["priority"], "low")

    def test_all_together(self):
        r = self.client.get(
            LIST_URL,
            {"priority": "high", "completed": "false", "search": "pay",
             "ordering": "due_date", "page_size": 1, "page": 2},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 2)  # Pay rent, Pay electricity bill
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["title"], "Pay electricity bill")
        self.assertIsNotNone(body["previous"])
        self.assertIsNone(body["next"])

    def test_invalid_page_404_shape(self):
        r = self.client.get(LIST_URL, {"page": 999})
        self.assertErrorShape(r, 404, "NOT_FOUND")

    def test_invalid_filter_value_400_shape(self):
        r = self.client.get(LIST_URL, {"priority": "urgent"})
        self.assertErrorShape(r, 400, "VALIDATION_ERROR", "priority")

    def test_top_level_only(self):
        parent = Task.objects.first()
        self.make_task(parent=parent)
        r = self.client.get(LIST_URL, {"is_subtask": "false"})
        self.assertEqual(r.json()["count"], 5)


# ---------------------------------------------------------------------------
# Pure parser unit tests (no database)
# ---------------------------------------------------------------------------
class ParserUnitTests(SimpleTestCase):
    today = date(2026, 11, 27)  # a Friday

    def test_rent(self):
        out = ai.parse_task_text("Remind me to pay rent on the 1st of every month", self.today)
        self.assertEqual(out["title"], "Pay rent")
        self.assertEqual(out["due_date"], "2026-12-01")
        self.assertEqual(out["recurrence"], "monthly")

    def test_relative_and_weekday(self):
        self.assertEqual(ai.parse_task_text("Call mom tomorrow", self.today)["due_date"], "2026-11-28")
        self.assertEqual(ai.parse_task_text("Gym every monday", self.today)["recurrence"], "weekly")
        self.assertEqual(ai.parse_task_text("Buy milk on friday", self.today)["due_date"], "2026-12-04")

    def test_priority_and_iso(self):
        out = ai.parse_task_text("Submit report by 2026-12-05 urgent", self.today)
        self.assertEqual(out["priority"], "high")
        self.assertEqual(out["due_date"], "2026-12-05")

    def test_31st_skips_short_months(self):
        self.assertEqual(ai.next_day_of_month(date(2027, 4, 1), 31), date(2027, 5, 31))

    def test_breakdown(self):
        task = SimpleNamespace(title="Wash car and clean room", description="", priority="low", due_date=None)
        self.assertEqual([s["title"] for s in ai.break_down_task(task)], ["Wash car", "Clean room"])
