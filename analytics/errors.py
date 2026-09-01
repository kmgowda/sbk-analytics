"""User-facing exceptions and stable command exit codes."""


class SbkAnalyticsError(RuntimeError):
    """Base class for expected, actionable sbk-analytics failures."""


class ConfigurationError(SbkAnalyticsError):
    """The supplied properties or YAML configuration is invalid."""


class DependencyResolutionError(SbkAnalyticsError):
    """A required executable could not be safely resolved."""


class LocalPackageError(DependencyResolutionError):
    """An explicitly selected local package is missing or unusable."""


class CacheError(DependencyResolutionError):
    """A managed dependency cache could not be installed or validated."""


class LifecycleError(SbkAnalyticsError):
    """A workload could not be placed under durable lifecycle ownership."""
