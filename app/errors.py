"""Distinguishable API errors.

Each error carries a stable machine-readable ``code`` so clients can tell the
mandatory failure cases apart (invalid period / unknown timezone / no
published rules / version conflict / not found).
"""

from __future__ import annotations


class ApiError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(ApiError):
    status_code = 404
    code = "point_not_found"


class VersionNotFoundError(ApiError):
    status_code = 404
    code = "version_not_found"


class VersionConflictError(ApiError):
    status_code = 409
    code = "version_conflict"


class NoPublishedVersionError(ApiError):
    status_code = 409
    code = "no_published_version"


class InvalidPeriodError(ApiError):
    status_code = 422
    code = "invalid_period"


class UnknownTimezoneError(ApiError):
    status_code = 422
    code = "unknown_timezone"
