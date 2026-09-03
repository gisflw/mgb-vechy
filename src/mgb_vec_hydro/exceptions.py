class MgbVecHydroError(Exception):
    """Base error for package-level failures."""


class MissingColumnsError(MgbVecHydroError):
    """Raised when an input table does not contain required columns."""


class InvalidInputSchemaError(MgbVecHydroError):
    """Raised when vector inputs do not match the expected schema."""


class MissingCrsError(MgbVecHydroError):
    """Raised when an input layer has no CRS for geometry measurements."""


class DuplicateSegmentIdError(MgbVecHydroError):
    """Raised when segment IDs are duplicated in a topology table."""


class OutletNotFoundError(MgbVecHydroError):
    """Raised when a requested outlet segment ID is absent."""


class TopologyCycleError(MgbVecHydroError):
    """Raised when upstream traversal detects a cycle."""


class UnsupportedOutputFormatError(MgbVecHydroError):
    """Raised when the requested vector output format is unsupported."""


class TerrainProductsError(MgbVecHydroError):
    """Raised when terrain-product inputs cannot be routed safely."""


class MiniSamplingError(MgbVecHydroError):
    """Raised when mini-basin attributes cannot be sampled safely."""


class PreparedDataError(MgbVecHydroError):
    """Raised when prepared input data cannot be created or validated."""


class ExecutionConfigurationError(MgbVecHydroError):
    """Raised when shared execution resource limits are invalid."""


class WorkMemoryError(MgbVecHydroError):
    """Raised when a work unit cannot fit in the configured memory budget."""


class WorkerExecutionError(MgbVecHydroError):
    """Raised when a local worker fails while processing a work unit."""

    def __init__(self, message: str, *, report=None):
        super().__init__(message)
        self.report = report


class ExecutionCancelledError(MgbVecHydroError):
    """Raised when shared execution is cancelled before completion."""

    def __init__(self, message: str, *, report=None):
        super().__init__(message)
        self.report = report


class CheckpointError(MgbVecHydroError):
    """Raised for incompatible, missing, or corrupt checkpoint state."""


class RasterGridError(MgbVecHydroError):
    """Raised when a raster does not match the canonical prepared grid."""


class RasterWriteConflictError(MgbVecHydroError):
    """Raised when raster patches claim the same output cell."""


class PublicationError(MgbVecHydroError):
    """Raised when staged outputs cannot be published atomically."""
