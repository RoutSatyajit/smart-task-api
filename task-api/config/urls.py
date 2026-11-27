from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("tasks.urls")),
]

# Unknown URLs also answer in the standard error shape (used when DEBUG=False).
handler404 = "tasks.errors.json_404_handler"
