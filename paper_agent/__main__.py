"""命令列入口：python -m paper_agent --config config.yaml"""

import argparse
import logging

import yaml

from .pipeline import run


def main() -> None:
    ap = argparse.ArgumentParser(description="論文追蹤 Agent")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, help="往回搜尋幾天（覆寫設定檔）")
    ap.add_argument("--dry-run", action="store_true", help="只搜尋並列出新論文，不呼叫 Claude、不下載")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for path in run(cfg, days=args.days, dry_run=args.dry_run):
        print(f"快報：{path}")


if __name__ == "__main__":
    main()
