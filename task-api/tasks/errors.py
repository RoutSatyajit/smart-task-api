"""
One consistent error shape for every error response:

    {
      "error": {
        "code": "VALIDATION_ERROR",
        "message": "Validation failed",
        "details": [{"field": "title", "issue": "must not be empty"}]
      }
    }

`code` and `message` are always strings; `details` is always a list.
"""
from django.http import JsonResponse
from rest_framework import status as http_status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def error_body(code, message, details=None):
    return {
        "error": {
            "code": str(code),
            "message": str(message),
            "details": list(details or []),
        }
    }


def error_response(code, message, details=None, status=http_status.HTTP_400_BAD_REQUEST):
    """Return a DRF Response in the standard error shape."""
    return Response(error_body(code, message, details), status=status)


def serializer_errors_to_details(errors, prefix=""):
    """
    Convert serializer.errors into a flat list of {"field", "issue"} dicts.

    {"title": ["must not be empty"]}         -> [{"field": "title", ...}]
    [{}, {"title": ["must not be empty"]}]   -> [{"field": "subtasks[1].title", ...}]  (prefix="subtasks")
    """
    details = []
    if isinstance(errors, dict):
        for field, value in errors.items():
            name = f"{prefix}.{field}" if prefix else str(field)
            details.extend(serializer_errors_to_details(value, name))
    elif isinstance(errors, (list, tuple)):
        for index, item in enumerate(errors):
            if isinstance(item, (dict, list, tuple)):
                if item:  # valid items in a many=True list come back as {}
                    details.extend(serializer_errors_to_details(item, f"{prefix}[{index}]"))
            else:
                details.append({"field": prefix or "non_field_errors", "issue": str(item)})
    else:
        details.append({"field": prefix or "non_field_errors", "issue": str(errors)})
    return details


def validation_error_response(errors, status=http_status.HTTP_400_BAD_REQUEST,
                              code="VALIDATION_ERROR", message="Validation failed", prefix=""):
    return error_response(code, message, serializer_errors_to_details(errors, prefix), status)


def not_found_response(resource="Task"):
    return error_response(
        "NOT_FOUND", f"{resource} not found", [], http_status.HTTP_404_NOT_FOUND
    )


def custom_exception_handler(exc, context):
    """
    DRF-wide safety net. Anything DRF raises itself (malformed JSON, 405,
    invalid filter value, invalid page number, 404 from get_object) is
    re-shaped into the standard error body.
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # real server error -> let Django return 500

    if isinstance(exc, ValidationError):
        body = error_body("VALIDATION_ERROR", "Validation failed",
                          serializer_errors_to_details(exc.detail))
    else:
        detail = getattr(exc, "detail", "")
        if isinstance(detail, (dict, list)):
            details = serializer_errors_to_details(detail)
            message = "Request failed"
        else:
            details = []
            message = str(detail) or "Request failed"
        code = str(getattr(exc, "default_code", "error")).upper()
        body = error_body(code, message, details)

    response.data = body
    return response


def json_404_handler(request, exception=None):
    """Django-level 404 for URLs that match no route (active when DEBUG=False)."""
    return JsonResponse(error_body("NOT_FOUND", "Resource not found"), status=404)
