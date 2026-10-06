"""Write compact audit manifests for published processing steps."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def write_manifest(directory: Path, step: str, parameters: Any) -> str:
    path = directory / f"manifest-{step}.json"
    values = asdict(parameters) if is_dataclass(parameters) else parameters
    path.write_text(
        json.dumps(
            {"step": step, "parameters": values},
            default=lambda value: (
                str(value.resolve()) if isinstance(value, Path) else value
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path.name
