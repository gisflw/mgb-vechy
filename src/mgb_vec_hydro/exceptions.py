class MgbVecHydroError(Exception):
    """Base error for package-level failures."""


class MissingColumnsError(MgbVecHydroError):
    """Raised when an input table does not contain required columns."""


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
