from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "priority", "due_date", "completed", "recurrence", "parent")
    list_filter = ("priority", "completed", "recurrence")
    search_fields = ("title", "description")
