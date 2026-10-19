class ProcessingError(Exception):
    """Base exception for all processing errors."""


class ClassificationError(ProcessingError):
    """LLM classification call failed after all retries."""


class ExtractionError(ProcessingError):
    """LLM field extraction call failed after all retries."""


class CourseResolutionError(ProcessingError):
    """Course resolution failed unexpectedly."""


class PersistenceError(ProcessingError):
    """Database write failed."""


class TransientPersistenceError(PersistenceError):
    """Transient persistence failure that should be retried at batch scope."""


class RowScopedPersistenceError(PersistenceError):
    """Persistence failure likely caused by one or more specific rows."""
