import django_filters
from rest_framework.filters import OrderingFilter

from .models import Task


class TaskFilter(django_filters.FilterSet):
    """
    /api/tasks/?completed=false&priority=high&due_after=2026-12-01&due_before=2026-12-31
    /api/tasks/?is_subtask=false        -> only top-level tasks
    /api/tasks/?parent=5                -> subtasks of task 5
    """

    completed = django_filters.BooleanFilter()
    priority = django_filters.ChoiceFilter(choices=Task.Priority.choices)
    recurrence = django_filters.ChoiceFilter(choices=Task.Recurrence.choices)
    due_date = django_filters.DateFilter()
    due_before = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")
    due_after = django_filters.DateFilter(field_name="due_date", lookup_expr="gte")
    parent = django_filters.NumberFilter(field_name="parent_id")
    is_subtask = django_filters.BooleanFilter(method="filter_is_subtask")

    class Meta:
        model = Task
        fields = ["completed", "priority", "recurrence", "due_date", "parent"]

    def filter_is_subtask(self, queryset, name, value):
        return queryset.filter(parent__isnull=not value)


class PriorityAwareOrderingFilter(OrderingFilter):
    """?ordering=priority / -priority sorts by rank (low < medium < high)."""

    def remove_invalid_fields(self, queryset, fields, view, request):
        mapped = []
        for field in fields:
            if field.lstrip("-") == "priority":
                field = field.replace("priority", "priority_rank")
            mapped.append(field)
        return super().remove_invalid_fields(queryset, mapped, view, request)
