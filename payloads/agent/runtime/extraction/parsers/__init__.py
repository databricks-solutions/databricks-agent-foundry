"""Per-format parsers for the file-extraction core.

Each module exposes callables with the uniform signature
``(data: bytes, max_rows: int) -> tuple[str, bool]`` returning
``(rendered_text, truncated_during_parse)``.  Heavy/optional parser
dependencies are imported lazily inside the functions that need them.
"""
