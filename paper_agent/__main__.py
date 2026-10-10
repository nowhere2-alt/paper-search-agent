"""命令列入口：python -m paper_agent --config config.yaml"""

import argparse
import logging
import os
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


def browser_login(cfg: dict, site_key: str) -> None:
    """開啟瀏覽器讓你手動登入資料庫（例如透過學校 SSO 登入 WoS）；登入狀態會保存在專用設定檔中。"""
    from playwright.sync_api import sync_playwright

    from .browser.sites import SITES

    if site_key not in SITES:
        raise SystemExit(f"未知的網站：{site_key}，可用：{', '.join(SITES)}")
    bcfg = cfg.get("browser", {})
    if cfg.get("engine", "claude") == "codex":
        from .browser.codex_agent import codex_profile_dir
        profile = codex_profile_dir(bcfg)  # 與 Codex 版的 Playwright MCP 共用
    else:
        profile = Path(os.path.expanduser(bcfg.get("profile_dir", "~/.paper-agent/browser-profile")))
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), headless=False,
                                                    executable_path=bcfg.get("executable_path"))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SITES[site_key]["start_url"])
        input(f"請在開啟的瀏覽器中登入 {SITES[site_key]['label']}，完成後回到這裡按 Enter…")
        ctx.close()
    print("登入狀態已保存。若登入過程經過學校的 SSO 網域，請把該網域加到 config.yaml 的 browser.extra_allowed_hosts。")


def main() -> None:
    ap = argparse.ArgumentParser(description="論文追蹤 Agent")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, help="往回搜尋幾天（覆寫設定檔）")
    ap.add_argument("--dry-run", action="store_true", help="只搜尋並列出新論文，不呼叫 Claude、不下載")
    ap.add_argument("--list-journals", action="store_true", help="列出期刊清單與自動查到的 ISSN")
    ap.add_argument("--browser-login", metavar="SITE",
                    help="開啟瀏覽器手動登入資料庫，供瀏覽器備援使用（例如 wos）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if args.list_journals:
        list_journals(cfg)
        return
    if args.browser_login:
        browser_login(cfg, args.browser_login)
        return
    digests = run(cfg, days=args.days, dry_run=args.dry_run)
    for path in digests:
        print(f"快報：{path}")
    if digests.ai_failed:
        # 以錯誤碼結束，排程（cron、GitHub Actions）才會顯示失敗並通知你
        raise SystemExit("AI 服務連續失敗：請檢查 API 金鑰（ANTHROPIC_API_KEY）、額度，或 Codex 是否已登入。")


if __name__ == "__main__":
    main()
