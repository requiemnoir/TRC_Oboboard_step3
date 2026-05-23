import json
import os
import time
from typing import Any, Dict, Optional


class ConfigStore:
    """Simple JSON config persistence with atomic writes."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def load(self) -> Dict[str, Any]:
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError('config must be an object')

        payload: Dict[str, Any] = {
            'version': 1,
            'saved_at_ms': int(time.time() * 1000),
            'config': data,
        }

        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        return payload

    def get_config_only(self) -> Dict[str, Any]:
        data = self.load()
        cfg = data.get('config') if isinstance(data, dict) else None
        return cfg if isinstance(cfg, dict) else {}

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_config_only()
        if not isinstance(patch, dict):
            raise ValueError('patch must be an object')
        current.update(patch)
        return self.save(current)
