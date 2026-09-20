import functools
import json
from pathlib import Path

from django.conf import settings


@functools.lru_cache(maxsize=1)
def load_schema() -> dict:
    """Loads schemas/edar-schema.json once per process. The single source of truth
    for what eDAR fields exist (ADR-006) - nothing in the codebase should hardcode a
    field list separately from this file."""
    schema_path = Path(settings.BASE_DIR) / 'schemas' / 'edar-schema.json'
    with open(schema_path, encoding='utf-8') as f:
        return json.load(f)
