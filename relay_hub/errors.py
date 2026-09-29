"""Domain exceptions translated to HTTP responses by the API layer."""
from __future__ import annotations


class AppError(Exception):
    status = 400

    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationFailed(AppError):
    status = 422


class NotFound(AppError):
    status = 404


class Forbidden(AppError):
    status = 403


class Conflict(AppError):
    """Record-version or state conflict. `details` carries the current state."""
    status = 409
