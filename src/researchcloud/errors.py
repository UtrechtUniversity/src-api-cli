from __future__ import annotations


class ResearchCloudError(Exception):
    """Base exception for the ResearchCloud SDK."""


class ApiError(ResearchCloudError):
    """Raised when the API returns a non-success response."""

    def __init__(self, status_code: int, url: str, body: object):
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status_code} for {url}: {body}")


class TransportError(ResearchCloudError):
    """Raised when the HTTP client cannot reach the API."""

    def __init__(self, url: str, cause: Exception):
        self.url = url
        self.cause = cause
        super().__init__(f"Request failed for {url}: {cause}")
