from __future__ import annotations


class GenarError(Exception):
    pass


class DatasetError(GenarError, ValueError):
    pass


class DatasetNotFoundError(GenarError, FileNotFoundError):
    pass


class ValidationError(GenarError, ValueError):
    pass


class FactError(GenarError, ValueError):
    pass


class EvidenceNotFoundError(GenarError, KeyError):
    pass


class ConfigurationError(GenarError, ValueError):
    pass


class RendererNotFoundError(GenarError, KeyError):
    pass


class ReviewFileError(GenarError, ValueError):
    pass


class LLMError(GenarError, RuntimeError):
    pass


class LLMConfigurationError(LLMError):
    pass


class QuotaExceededError(LLMError):
    pass


class IncompleteResponseError(LLMError):
    pass
