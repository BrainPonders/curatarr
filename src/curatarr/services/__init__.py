"""Application services built on Curatarr domain and adapters."""

from importlib import import_module

__all__ = [
    "PendingDecisionService",
    "PersistedDecisionSummary",
    "TelegramButton",
    "TelegramPrompt",
    "TelegramSearchCommand",
    "search_add_state_fingerprint",
]


def __getattr__(name: str):
    # Workflows import identity services; eager workflow-service exports create an import cycle.
    if name not in __all__:
        raise AttributeError(name)
    module = "telegram" if name.startswith("Telegram") else "decisions"
    return getattr(import_module(f"curatarr.services.{module}"), name)
