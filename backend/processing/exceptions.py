class PermanentProcessingError(Exception):
    """Input or data error that cannot be fixed by retrying the task."""


class InvalidDataset(PermanentProcessingError):
    """The uploaded archive or its manifest violates ingestion limits."""


class InvalidImage(PermanentProcessingError):
    """The object is not a supported, decodable image."""


class MissingObject(PermanentProcessingError):
    """The expected uploaded object does not exist in object storage."""


class RetryableProcessingError(Exception):
    """A storage, network, or database failure that may recover on retry."""

