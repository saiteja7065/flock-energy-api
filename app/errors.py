"""Domain errors raised by the portal adapter and the service layer.

Each error carries the HTTP status and machine-readable code our API returns,
so route handlers never need to translate portal failures themselves.
"""


class ServiceError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class InvalidRequest(ServiceError):
    status_code = 422
    code = "invalid_request"


class PortalError(ServiceError):
    """The upstream portal failed in a way we could not recover from."""

    status_code = 502
    code = "portal_error"


class PortalAuthError(PortalError):
    code = "portal_auth_failed"


class PortalRateLimited(PortalError):
    status_code = 503
    code = "portal_rate_limited"


class PortalUnavailable(PortalError):
    status_code = 503
    code = "portal_unavailable"
