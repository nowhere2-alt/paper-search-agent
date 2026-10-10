"""載入設定檔：支援 include，讓 Claude 版與 Codex 版共用同一份 settings.yaml。

config.yaml / config.codex.yaml 只放各自引擎的設定，用 `include: settings.yaml` 引入共用設定。
合併規則：先載入 include 的檔案，再用本檔覆寫；字典逐層合併，清單與其他值整個取代。
"""

from __future__ import annotations

from pathlib import Path

import yaml


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path, _seen: frozenset = frozenset()) -> dict:
    path = Path(path).resolve()
    if path in _seen:
        raise ValueError(f"設定檔循環引用：{path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    includes = data.pop("include", [])
    if isinstance(includes, str):
        includes = [includes]
    merged: dict = {}
    for inc in includes:
        merged = deep_merge(merged, load_config(path.parent / inc, _seen | {path}))
    return deep_merge(merged, data)
