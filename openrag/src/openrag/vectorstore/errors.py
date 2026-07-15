"""Stable vector-store contract failures."""


class VectorStoreError(RuntimeError):
    def __init__(self, code: str, public_message: str, *, retryable: bool = False):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable


class VectorSchemaMismatchError(VectorStoreError):
    def __init__(self, public_message: str):
        super().__init__("VECTOR_SCHEMA_MISMATCH", public_message)


class VectorWriteIncompleteError(VectorStoreError):
    def __init__(self, public_message: str, *, retryable: bool = False):
        super().__init__(
            "VECTOR_WRITE_INCOMPLETE", public_message, retryable=retryable
        )
