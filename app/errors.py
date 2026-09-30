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


class AppointmentNotFoundError(ApiError):
    status_code = 404
    code = "appointment_not_found"


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


class IdempotencyConflictError(ApiError):
    """Same (point, idempotency_key) reused with a different request body."""

    status_code = 409
    code = "idempotency_conflict"


class AppointmentConflictError(ApiError):
    """UTC time range overlaps another active appointment at the same point."""

    status_code = 409
    code = "appointment_conflict"


class AppointmentRuleConflictError(ApiError):
    """A rule/timezone change would strand future active appointments."""

    status_code = 409
    code = "appointment_rule_conflict"


class CalendarChangedError(ApiError):
    """The calendar epoch advanced between validation and insert (retryable)."""

    status_code = 409
    code = "calendar_changed"


class OutsideOpenHoursError(ApiError):
    """The requested UTC range is not fully inside one continuous open interval."""

    status_code = 422
    code = "outside_open_hours"


class InvalidCancelTokenError(ApiError):
    status_code = 403
    code = "invalid_cancel_token"
