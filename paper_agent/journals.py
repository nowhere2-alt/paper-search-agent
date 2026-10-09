"""優先期刊清單：載入 journals.yaml，並自動查詢每本期刊的 ISSN（結果會快取）。"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import requests
import yaml

log = logging.getLogger(__name__)


@dataclass
class Journal:
    name: str
    publisher: str = ""
    tier: int = 1
    field: str = ""
    issns: list[str] = dc_field(default_factory=list)


@dataclass
class Tier:
    tier: int
    label: str
    journals: list[Journal]


def normalize(name: str) -> str:
    """比對期刊名稱用：小寫、& → and、去掉標點與開頭的 the。"""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"^the ", "", " ".join(s.split()))
    return s


def _mailto() -> dict:
    email = os.environ.get("CONTACT_EMAIL") or os.environ.get("OPENALEX_EMAIL")
    return {"mailto": email} if email else {}


def _lookup_openalex(name: str) -> list[str]:
    resp = requests.get("https://api.openalex.org/sources",
                        params={"search": name, "per-page": 10, **_mailto()}, timeout=30)
    resp.raise_for_status()
    for s in resp.json().get("results", []):
        if normalize(s.get("display_name") or "") == normalize(name):
            return s.get("issn") or ([s["issn_l"]] if s.get("issn_l") else [])
    return []


def _lookup_crossref(name: str) -> list[str]:
    resp = requests.get("https://api.crossref.org/journals",
                        params={"query": name, "rows": 10, **_mailto()}, timeout=30)
    resp.raise_for_status()
    for item in resp.json().get("message", {}).get("items", []):
        if normalize(item.get("title") or "") == normalize(name):
            return item.get("ISSN") or []
    return []


def load_tiers(path: Path, cache_path: Path) -> list[Tier]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    tiers, seen = [], set()
    for t in data["tiers"]:
        journals = []
        for j in t["journals"]:
            key = normalize(j["name"])
            if key in seen:  # 清單中重複列出的期刊只保留第一次
                continue
            seen.add(key)
            issns = j.get("issn") or cache.get(key) or []
            if isinstance(issns, str):
                issns = [issns]
            if not issns:
                for lookup in (_lookup_openalex, _lookup_crossref):
                    try:
                        issns = lookup(j["name"])
                    except requests.RequestException as e:
                        log.debug("查詢 ISSN 失敗 %s (%s): %s", j["name"], lookup.__name__, e)
                    if issns:
                        cache[key] = issns
                        break
                else:
                    log.warning("找不到期刊 ISSN：%s（可在 journals.yaml 手動加上 issn）", j["name"])
            journals.append(Journal(name=j["name"], publisher=j.get("publisher", ""),
                                    tier=t["tier"], field=j.get("field", ""), issns=issns))
        tiers.append(Tier(tier=t["tier"], label=t.get("label", f"Tier {t['tier']}"), journals=journals))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return tiers
