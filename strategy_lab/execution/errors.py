"""User-safe diagnostics: messages are fixed local text, never transport/secret data."""


class SetupError(ValueError):
    pass


class NotSubmitted(ValueError):
    """A buy was refused before any API request; safe to release its reservation."""


class BuyUncertain(ValueError):
    """Safe local classification of a buy response whose execution is unknown."""
