"""User-safe diagnostics: messages are fixed local text, never transport/secret data."""


class SetupError(ValueError):
    pass
