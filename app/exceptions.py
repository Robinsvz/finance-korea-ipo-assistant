class AppError(Exception):
    """Base application error."""


class MissingCredentialError(AppError):
    """Raised when an optional external API credential is required but missing."""


class ExternalSourceError(AppError):
    """Raised when an external data source returns an invalid response."""
