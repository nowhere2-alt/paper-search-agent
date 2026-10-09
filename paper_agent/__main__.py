"""命令列入口：python -m paper_agent --config config.yaml"""

import argparse
import logging
from pathlib import Path

import yaml

from .journals import load_tiers
from .pipeline import run


def list_journals(cfg: dict) -> None:
    root = Path(cfg.get("output_dir", "library"))
    for t in load_tiers(Path(cfg.get("journal_list", "journals.yaml")), root / ".state" / "journals.json"):
        print(f"\n{t.label}")
        for j in t.journals:
            print(f"  {', '.join(j.issns) or '（找不到 ISSN）':<22} {j.name}  [{j.publisher}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="論文追蹤 Agent")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, help="往回搜尋幾天（覆寫設定檔）")
    ap.add_argument("--dry-run", action="store_true", help="只搜尋並列出新論文，不呼叫 Claude、不下載")
    ap.add_argument("--list-journals", action="store_true", help="列出期刊清單與自動查到的 ISSN")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if args.list_journals:
        list_journals(cfg)
        return
    for path in run(cfg, days=args.days, dry_run=args.dry_run):
        print(f"快報：{path}")


if __name__ == "__main__":
    main()
