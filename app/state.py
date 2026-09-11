"""Tiny JSON-file store for runtime state (Gmail history id, Drive channel, dedupe keys).

Files live in `settings.data_dir`, which is git-ignored and mounted as a volume in Docker.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class JsonState:
    def __init__(self, filename: str, directory: Path | None = None):
        self._filename = filename
        self._directory = directory
        self.lock = threading.RLock()

    @property
    def path(self) -> Path:
        return (self._directory or settings.data_dir) / self._filename

    def load(self) -> dict[str, Any]:
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                logger.warning("Could not read %s: %s", self.path, exc)
                return {}

    def save(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(self.path)

    def update(self, **values: Any) -> dict[str, Any]:
        with self.lock:
            data = {**self.load(), **values}
            self.save(data)
            return data
