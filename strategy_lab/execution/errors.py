"""User-safe diagnostics: messages are fixed local text, never transport/secret data."""


class SetupError(ValueError):
    pass


class NotSubmitted(ValueError):
    """A buy was refused before any API request; safe to release its reservation."""


class BuyUncertain(ValueError):
    """Safe local classification of a buy response whose execution is unknown."""


class QuoteRefused(ValueError):
    """A live quote refused before any network call could carry anything sensitive.

    Every message is fixed local text naming the check that failed, so it is safe to put
    in front of a person. Reporting only the exception type — which is what happens to
    anything else — leaves nine different causes looking identical in the log.
    """
