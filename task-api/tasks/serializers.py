from rest_framework import serializers

from .models import Task

PRIORITY_CHOICES = [c[0] for c in Task.Priority.choices]
RECURRENCE_CHOICES = [c[0] for c in Task.Recurrence.choices]


class StrictCharField(serializers.CharField):
    """CharField that rejects non-strings (DRF would silently turn 123 into "123")."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    """Only JSON true/false are accepted. "yes", 1, "true" are rejected."""

    default_error_messages = {
        "invalid": "must be a boolean (true or false)",
        "null": "must be a boolean (true or false)",
    }

    def to_internal_value(self, data):
        if isinstance(data, bool):
            return data
        self.fail("invalid")


def _text_errors(field):
    return {
        "required": "is required",
        "blank": "must not be empty",
        "null": "must not be empty",
        "invalid": "must be a string",
        "max_length": f"must be at most {field} characters" if isinstance(field, int) else "is too long",
    }


class TaskSerializer(serializers.ModelSerializer):
    title = StrictCharField(max_length=200, error_messages=_text_errors(200))
    description = StrictCharField(error_messages=_text_errors(None))
    completed = StrictBooleanField(required=False)
    due_date = serializers.DateField(
        required=False,
        allow_null=True,
        input_formats=["%Y-%m-%d"],
        format="%Y-%m-%d",
        error_messages={
            "invalid": "must be a real date in YYYY-MM-DD format",
            "datetime": "must be a date (YYYY-MM-DD), not a datetime",
        },
    )
    priority = serializers.ChoiceField(
        choices=PRIORITY_CHOICES,
        required=False,
        error_messages={"invalid_choice": "must be one of low, medium, high"},
    )
    recurrence = serializers.ChoiceField(
        choices=RECURRENCE_CHOICES,
        required=False,
        allow_null=True,
        error_messages={"invalid_choice": "must be one of daily, weekly, monthly, yearly"},
    )
    parent = serializers.PrimaryKeyRelatedField(read_only=True)
    subtask_count = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "description",
            "completed",
            "due_date",
            "priority",
            "recurrence",
            "parent",
            "subtask_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "parent", "subtask_count", "created_at", "updated_at"]

    def get_subtask_count(self, obj):
        return obj.subtasks.count()
