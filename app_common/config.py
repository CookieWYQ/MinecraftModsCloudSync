# -*- coding: utf-8 -*-
"""配置持久化：JSON 原子写入，配置项自动保存。"""
import json
import threading
from pathlib import Path
from typing import Any

from .logger import get_logger

log = get_logger("config")


class JsonStore:
    def __init__(self, path: Path, defaults: dict | None = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict = dict(defaults or {})
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data.update(loaded)
        except Exception as exc:
            log.exception("读取配置失败: %s", exc)

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                tmp.replace(self.path)
            except Exception as exc:
                log.exception("保存配置失败: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
        self.save()

    def set_many(self, mapping: dict) -> None:
        with self._lock:
            self._data.update(mapping)
        self.save()

    def replace_all(self, data: dict) -> None:
        """整体替换配置（用于从备份恢复），校验后原子保存。"""
        if not isinstance(data, dict):
            raise ValueError("配置备份格式不正确")
        with self._lock:
            self._data = dict(data)
        self.save()

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)
