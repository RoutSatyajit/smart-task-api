from datetime import date

from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from . import ai
from .errors import (
    error_response,
    not_found_response,
    validation_error_response,
)
from .filters import PriorityAwareOrderingFilter, TaskFilter
from .idempotency import run_idempotent
from .models import Task
from .serializers import TaskSerializer


def _subtask_not_allowed():
    return error_response(
        "SUBTASK_NOT_ALLOWED",
        "A subtask cannot have its own subtasks",
        [{"field": "parent", "issue": "task is already a subtask"}],
        status.HTTP_400_BAD_REQUEST,
    )


class TaskViewSet(viewsets.ModelViewSet):
    """
    CRUD for tasks plus:
      POST /api/tasks/from-text/          natural-language creation
      GET  /api/tasks/{id}/subtasks/      list subtasks
      POST /api/tasks/{id}/subtasks/      create one subtask
      POST /api/tasks/{id}/breakdown/     auto-split a task into subtasks
    """

    serializer_class = TaskSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, PriorityAwareOrderingFilter]
    filterset_class = TaskFilter
    search_fields = ["title", "description"]
    # ?ordering=priority is mapped to priority_rank (low < medium < high)
    # instead of sorting the strings alphabetically.
    ordering_fields = ["due_date", "created_at", "updated_at", "title", "priority_rank"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return Task.objects.annotate(
            priority_rank=Case(
                When(priority="low", then=Value(1)),
                When(priority="medium", then=Value(2)),
                When(priority="high", then=Value(3)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _get_task(pk):
        try:
            return Task.objects.get(pk=int(pk))
        except (Task.DoesNotExist, ValueError, TypeError):
            return None

    # --------------------------------------------------------------------- CRUD
    def create(self, request, *args, **kwargs):
        return run_idempotent(request, lambda: self._create(request))

    def _create(self, request):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        task = self._get_task(kwargs.get("pk"))
        if task is None:
            return not_found_response()
        return Response(self.get_serializer(task).data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        task = self._get_task(kwargs.get("pk"))
        if task is None:
            return not_found_response()
        serializer = self.get_serializer(task, data=request.data, partial=partial)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save()
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        task = self._get_task(kwargs.get("pk"))
        if task is None:
            return not_found_response()
        task.delete()  # CASCADE removes its subtasks too
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------- natural-language create
    @action(detail=False, methods=["post"], url_path="from-text")
    def from_text(self, request):
        return run_idempotent(request, lambda: self._from_text(request))

    def _from_text(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            issue = "must not be empty" if isinstance(text, str) or text is None else "must be a string"
            return error_response(
                "VALIDATION_ERROR", "Validation failed",
                [{"field": "text", "issue": issue}],
                status.HTTP_400_BAD_REQUEST,
            )

        parser = ai.get_task_parser()
        try:
            parsed = parser(text, date.today())
        except ai.LLMNotConfiguredError:
            return error_response(
                "LLM_NOT_CONFIGURED",
                "The language model is not configured (LLM_API_KEY missing)",
                [], status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:  # any parser crash -> 502, nothing saved
            return error_response(
                "PARSER_ERROR", "Could not parse task text",
                [{"field": "text", "issue": str(exc) or exc.__class__.__name__}],
                status.HTTP_502_BAD_GATEWAY,
            )

        if not isinstance(parsed, dict):
            return error_response(
                "INVALID_PARSER_OUTPUT", "Parser returned an invalid result",
                [{"field": "parser", "issue": "must return an object"}],
                status.HTTP_502_BAD_GATEWAY,
            )

        # "Never trust the output": validate exactly like user input.
        serializer = self.get_serializer(data=parsed)
        if not serializer.is_valid():
            return validation_error_response(
                serializer.errors,
                status=status.HTTP_502_BAD_GATEWAY,
                code="INVALID_PARSER_OUTPUT",
                message="Parser returned data that failed validation",
            )
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    # ----------------------------------------------------------------- subtasks
    @action(detail=True, methods=["get", "post"], url_path="subtasks")
    def subtasks(self, request, pk=None):
        task = self._get_task(pk)
        if task is None:
            return not_found_response()

        if request.method == "GET":
            serializer = self.get_serializer(task.subtasks.all().order_by("created_at"), many=True)
            return Response(serializer.data)

        if task.parent_id is not None:
            return _subtask_not_allowed()
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save(parent=task)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="breakdown")
    def breakdown(self, request, pk=None):
        return run_idempotent(request, lambda: self._breakdown(pk))

    def _breakdown(self, pk):
        task = self._get_task(pk)
        if task is None:
            return not_found_response()
        if task.parent_id is not None:
            return _subtask_not_allowed()

        try:
            items = ai.break_down_task(task)
        except Exception as exc:
            return error_response(
                "BREAKDOWN_ERROR", "Could not break down task",
                [{"field": "task", "issue": str(exc) or exc.__class__.__name__}],
                status.HTTP_502_BAD_GATEWAY,
            )

        if not isinstance(items, list) or not items:
            return error_response(
                "INVALID_BREAKDOWN_OUTPUT", "Breakdown returned no subtasks",
                [{"field": "subtasks", "issue": "must be a non-empty list"}],
                status.HTTP_502_BAD_GATEWAY,
            )

        # Validate EVERY item before saving ANY of them.
        serializer = self.get_serializer(data=items, many=True)
        if not serializer.is_valid():
            return validation_error_response(
                serializer.errors,
                status=status.HTTP_502_BAD_GATEWAY,
                code="INVALID_BREAKDOWN_OUTPUT",
                message="Breakdown returned data that failed validation",
                prefix="subtasks",
            )
        with transaction.atomic():
            serializer.save(parent=task)

        return Response(
            {"parent": task.id, "count": len(serializer.data), "subtasks": serializer.data},
            status=status.HTTP_201_CREATED,
        )
