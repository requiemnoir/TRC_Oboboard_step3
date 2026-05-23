"""
config.py — Config store atomico JSON.

Lettura e scrittura thread-safe via .tmp + os.replace().
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_BASE_DIR = Path(__file__).parent
_DEFAULT  = _BASE_DIR / 'config' / 'default.json'
_USER     = _BASE_DIR / 'config' / 'user.json'


class Config:
    """Config con merge default+user, scrittura atomica.

    Uso:
        cfg = Config()
        val = cfg.get('mirror_dest_port', 30490)
        cfg.set('mirror_dest_port', 31000)  # salva user.json
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._save_timer: threading.Timer | None = None
        self._save_debounce_s = 0.5
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        data: dict[str, Any] = {}
        for path in (_DEFAULT, _USER):
            if path.exists():
                try:
                    with open(path, encoding='utf-8') as f:
                        data.update(json.load(f))
                except Exception as e:
                    print(f'[Config] errore caricamento {path}: {e}', flush=True)
        self._data = data

    def reload(self) -> None:
        with self._lock:
            self._load()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._schedule_save()

    def update(self, values: dict) -> None:
        with self._lock:
            self._data.update(values)
            self._schedule_save()

    def _schedule_save(self) -> None:
        """Debounce: rimanda il save di _save_debounce_s. Sostituisce timer pendente."""
        self._dirty = True
        if self._save_timer is not None:
            try:
                self._save_timer.cancel()
            except Exception:
                pass
        self._save_timer = threading.Timer(self._save_debounce_s, self._do_save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _do_save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            self._save()

    def flush(self) -> None:
        """Forza un save sincrono, bypassando il debounce.

        Da chiamare nel signal handler per garantire la persistenza dei
        cambiamenti recenti anche se il processo termina entro il debounce.
        """
        if self._save_timer is not None:
            try:
                self._save_timer.cancel()
            except Exception:
                pass
            self._save_timer = None
        self._do_save()

    def _save(self) -> None:
        """Scrittura atomica user.json (sovrascrive solo le chiavi utente)."""
        _USER.parent.mkdir(parents=True, exist_ok=True)
        # Leggi il default per salvare solo le diff
        default: dict = {}
        if _DEFAULT.exists():
            try:
                with open(_DEFAULT, encoding='utf-8') as f:
                    default = json.load(f)
            except Exception:
                pass
        user_data = {k: v for k, v in self._data.items() if k not in default or default.get(k) != v}
        tmp = str(_USER) + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(user_data, f, indent=2)
            os.replace(tmp, str(_USER))
        except Exception as e:
            print(f'[Config] errore salvataggio: {e}', flush=True)


# Singleton globale
config = Config()
