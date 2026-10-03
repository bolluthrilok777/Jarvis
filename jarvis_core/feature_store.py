"""Safe JSON persistence for feature-orchestrator state.

Only safe structured state is stored: the FeatureRequest model has no secret
fields, and text is redacted via feature_utils.redact_sensitive before writing.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from jarvis_core.config import BASE_DIR
from jarvis_core.feature_models import FeatureRequest
from jarvis_core.feature_utils import contains_sensitive, redact_sensitive

FEATURES_DIR = BASE_DIR / "data" / "features"


class FeatureStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root is not None else FEATURES_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._features: dict[str, FeatureRequest] = {}
        self._load_all()

    def _path(self, feature_id: str) -> Path:
        return self._root / f"{feature_id}.json"

    def _load_all(self) -> None:
        for path in self._root.glob("*.json"):
            try:
                data = path.read_text(encoding="utf-8")
                model = FeatureRequest.model_validate_json(data)
                self._features[model.feature_id] = model
            except Exception:
                # A corrupt feature file must not take down other features.
                continue

    def save(self, feature: FeatureRequest) -> None:
        data = feature.model_dump_json()
        if contains_sensitive(data):
            # Redact every textual field defensively before persisting.
            for field in feature.model_fields_set | set(feature.model_fields):
                value = getattr(feature, field, None)
                if isinstance(value, str):
                    setattr(feature, field, redact_sensitive(value))
            data = feature.model_dump_json()
        with self._lock:
            self._features[feature.feature_id] = feature
            self._path(feature.feature_id).write_text(data, encoding="utf-8")

    def get(self, feature_id: str) -> Optional[FeatureRequest]:
        with self._lock:
            return self._features.get(feature_id)

    def by_task_id(self, task_id: str) -> Optional[FeatureRequest]:
        with self._lock:
            for feature in self._features.values():
                if feature.task_id == task_id:
                    return feature
        return None

    def all(self) -> list[FeatureRequest]:
        with self._lock:
            features = list(self._features.values())
        return sorted(features, key=lambda f: f.created_at, reverse=True)

    def list_status(self, status: Optional[str] = None) -> list[FeatureRequest]:
        items = self.all()
        if status:
            return [f for f in items if f.status == status]
        return items

    def pending(self) -> list[FeatureRequest]:
        """Features waiting for a user decision (resource review, approval or
        review)."""
        return [
            f for f in self.all()
            if f.status in (
                "resource_review_pending", "resource_review", "approval_pending",
                "chatgpt_review_pending",
            )
        ]

    def remove(self, feature_id: str) -> bool:
        with self._lock:
            self._features.pop(feature_id, None)
        path = self._path(feature_id)
        if path.exists():
            path.unlink()
            return True
        return False