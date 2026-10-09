"""用 Playwright 實作 Claude browser use toolset（browser_toolset_20260801）的瀏覽器驅動程式。

Claude 看截圖或頁面文字，決定要點哪裡、輸入什麼；這個類別負責在真實的 Chromium 中執行那些動作。
安全設計：
- 只能開 url_policy 允許的網域（每個資料庫各自設定），連結點擊與網頁自行發出的請求也在 _guard 中攔截
- 不允許下載檔案、不啟用 javascript_exec / file_upload
- 使用專用的瀏覽器設定檔（不是你平常的 Chrome），只登入要搜尋的資料庫
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from anthropic.tools import ToolError
from anthropic.tools.browser import (
    BetaAbstractBrowserToolset20260801,
    BetaBrowserNavigateResult,
    BetaBrowserState,
    BetaNavigationRefused,
    BetaScreenshotResult,
    BetaToolsetCallContext,
    BetaURLContext,
)
from anthropic.types.beta import BetaBrowserStateTabEntryParam

log = logging.getLogger(__name__)
SCHEME_PREFIX = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
VIEWPORT = {"width": 1280, "height": 800}  # 官方建議的網頁應用解析度

# 在頁面中為可互動元素與標題加上 data-claude-ref，回傳 [ref_N] 格式的清單
_READ_JS = r"""
([filter, query, startRef]) => {
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],' +
              '[role=checkbox],[role=option],[role=menuitem],[role=combobox],[contenteditable=true]';
  const headSel = 'h1,h2,h3,h4,label,[role=heading]';
  let n = window.__claudeRef || 0;
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const inView = el => { const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth; };
  const root = startRef ? document.querySelector(`[data-claude-ref="${startRef}"]`) : document.body;
  if (!root) return 'ERROR: ref not found';
  const cands = [...root.querySelectorAll(filter === 'interactive' ? sel : sel + ',' + headSel)];
  const out = [];
  for (const el of cands) {
    if (!visible(el)) continue;
    if (filter !== 'all' && !query && !inView(el)) continue;
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                   el.title || el.name || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    if (query && !label.toLowerCase().includes(query.toLowerCase())) continue;
    let ref = el.getAttribute('data-claude-ref');
    if (!ref) { ref = 'ref_' + (++n); el.setAttribute('data-claude-ref', ref); }
    const tag = el.tagName.toLowerCase();
    const type = el.type ? ` type=${el.type}` : '';
    const href = tag === 'a' ? ` href=${el.getAttribute('href').slice(0, 150)}` : '';
    const val = (tag === 'input' || tag === 'textarea') && el.value ? ` value="${el.value.slice(0, 60)}"` : '';
    out.push(`[${ref}] ${tag}${type} "${label}"${val}${href}`);
    if (query && out.length >= 20) break;
  }
  window.__claudeRef = n;
  return out.join('\n') || '(no matching elements)';
}
"""


def is_private(url: str) -> bool:
    """本機與內網位址（含雲端 metadata 169.254.169.254）一律不允許存取。"""
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def make_url_policy(allowed_hosts: list[str]):
    def is_allowed(url: str) -> bool:
        if url.lower() == "about:blank":
            return True
        with_scheme = url if SCHEME_PREFIX.match(url) else f"https://{url}"
        try:
            parts = urlsplit(with_scheme.replace("\\", "/"))
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        listed = any(host == h or host.endswith(f".{h}") for h in allowed_hosts)
        return parts.scheme in ("http", "https") and listed

    def url_policy(context: BetaURLContext, url: str) -> None:
        if not is_allowed(url):
            raise ToolError(f"blocked: {url} is not on an allowed host for this database")

    return is_allowed, url_policy


class PlaywrightBrowser(BetaAbstractBrowserToolset20260801):
    """以持久化設定檔啟動 Chromium，讓你事先手動登入的資料庫（如學校的 WoS）保持登入狀態。"""

    def __init__(self, profile_dir: Path, allowed_hosts: list[str], *, headless: bool = True,
                 executable_path: str | None = None, **options):
        self.is_allowed, url_policy = make_url_policy(allowed_hosts)
        super().__init__(url_policy=url_policy, **options)
        from playwright.sync_api import sync_playwright  # 延遲載入：沒裝 Playwright 也能跑其他功能

        self._pw = sync_playwright().start()
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, viewport=VIEWPORT,
            accept_downloads=False, executable_path=executable_path,
            service_workers="block",
        )
        self._pages: dict = {}
        self._next_id = 0
        self._changes: list = []
        self.ctx.route("**/*", self._guard)
        self.ctx.on("page", self._on_new_page)
        for p in self.ctx.pages:
            self._register(p)
        if not self._pages:
            self._register(self.ctx.new_page())
        self.active = next(iter(self._pages))

    # ---------- 分頁管理 ----------
    def _register(self, page) -> str:
        for tab_id, p in self._pages.items():
            if p is page:
                return tab_id
        self._next_id += 1
        tab_id = f"tab_{self._next_id}"
        self._pages[tab_id] = page
        page.on("dialog", lambda d: d.dismiss())
        return tab_id

    def _on_new_page(self, page) -> None:
        self.active = self._register(page)  # 網站開新分頁時自動切過去

    def _page(self, tab_id: str | None):
        tab_id = tab_id or self.active
        if tab_id not in self._pages:
            raise ToolError(f"no tab {tab_id}")
        return self._pages[tab_id]

    def _entry(self, tab_id: str) -> BetaBrowserStateTabEntryParam:
        p = self._pages[tab_id]
        try:
            title = p.title()
        except Exception:
            title = ""
        return BetaBrowserStateTabEntryParam(tab_id=tab_id, title=title, url=p.url,
                                             active=tab_id == self.active)

    def _guard(self, route) -> None:
        """頁面導覽（點連結、轉址、送出表單）只能到允許的網域；
        網站自己載入的圖片、樣式、腳本可以來自其他 CDN，但不能連到本機或內網。"""
        req = route.request
        url = req.url
        if url.startswith(("data:", "blob:")) or self.is_allowed(url):
            route.continue_()
        elif req.is_navigation_request() and req.frame == req.frame.page.main_frame:
            self._changes.append(BetaNavigationRefused())
            route.abort("blockedbyclient")
        elif url.startswith(("http:", "https:")) and not is_private(url):
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def _browser_state(self, context: BetaToolsetCallContext) -> BetaBrowserState:
        changes, self._changes = self._changes, []
        return BetaBrowserState(tabs=[self._entry(t) for t in self._pages], state_changes=changes)

    # ---------- 導覽與擷取 ----------
    def navigate(self, context, input) -> BetaBrowserNavigateResult:
        page = self._page(input.tab_id)
        try:
            if input.url in ("back", "forward", "reload"):
                resp = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[input.url](
                    wait_until="domcontentloaded")
            else:
                url = input.url if SCHEME_PREFIX.match(input.url) else f"https://{input.url}"
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)  # 讓動態載入的搜尋結果出現
        except Exception as e:
            raise ToolError(f"navigation failed: {type(e).__name__}")
        return BetaBrowserNavigateResult(url=page.url, status=resp.status if resp else 200, title=page.title())

    def screenshot(self, context, input) -> BetaScreenshotResult:
        data = self._page(input.tab_id).screenshot(type="png")
        return BetaScreenshotResult(data=base64.b64encode(data).decode(), media_type="image/png")

    def zoom(self, context, input) -> BetaScreenshotResult:
        x0, y0, x1, y1 = input.region
        clip = {"x": x0, "y": y0, "width": max(1, x1 - x0), "height": max(1, y1 - y0)}
        data = self._page(input.tab_id).screenshot(type="png", clip=clip)
        return BetaScreenshotResult(data=base64.b64encode(data).decode(), media_type="image/png")

    def get_page_text(self, context, input) -> str:
        page = self._page(input.tab_id)
        text = page.evaluate("() => (document.querySelector('main') || document.body).innerText")
        return re.sub(r"\n{3,}", "\n\n", text or "")[:50000]

    def read_page(self, context, input) -> str:
        return self._page(input.tab_id).evaluate(_READ_JS, [input.filter, None, input.ref])[:50000]

    def find(self, context, input) -> str:
        return self._page(input.tab_id).evaluate(_READ_JS, ["all", input.query, None])

    # ---------- 滑鼠與鍵盤 ----------
    def _locator(self, page, ref: str):
        loc = page.locator(f'[data-claude-ref="{ref}"]')
        if loc.count() == 0:
            raise ToolError(f"{ref} not found; call read_page or find again")
        return loc.first

    def left_click(self, context, input) -> None:
        page = self._page(input.tab_id)
        mods = [m.capitalize().replace("Ctrl", "Control").replace("Super", "Meta")
                for m in (input.modifiers or "").split("+") if m]
        try:
            if input.target.type == "ref":
                self._locator(page, input.target.ref).click(modifiers=mods or None, timeout=10000)
            else:
                page.mouse.click(input.target.x, input.target.y)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"click failed: {type(e).__name__}")

    def scroll(self, context, input) -> None:
        page = self._page(input.tab_id)
        page.mouse.move(input.target.x, input.target.y)
        step = 300 * (input.scroll_amount or 3)
        dx, dy = {"up": (0, -step), "down": (0, step), "left": (-step, 0), "right": (step, 0)}[input.scroll_direction]
        page.mouse.wheel(dx, dy)
        page.wait_for_timeout(500)

    def scroll_to(self, context, input) -> None:
        self._locator(self._page(input.tab_id), input.target.ref).scroll_into_view_if_needed()

    def type(self, context, input) -> None:
        self._page(input.tab_id).keyboard.type(input.text, delay=20)

    def key(self, context, input) -> None:
        page = self._page(input.tab_id)
        for _ in range(input.repeat or 1):
            for combo in input.text.split(" "):
                parts = [p.capitalize().replace("Ctrl", "Control").replace("Return", "Enter")
                         .replace("Esc", "Escape").replace("Super", "Meta") if len(p) > 1 else p
                         for p in combo.split("+")]
                page.keyboard.press("+".join(parts))
        page.wait_for_timeout(800)

    def form_input(self, context, input) -> None:
        loc = self._locator(self._page(input.tab_id), input.target.ref)
        tag = loc.evaluate("el => el.tagName.toLowerCase() + ':' + (el.type || '')")
        try:
            if tag.startswith("select"):
                try:
                    loc.select_option(label=str(input.value), timeout=3000)
                except Exception:
                    loc.select_option(value=str(input.value), timeout=3000)
            elif tag.endswith(("checkbox", "radio")):
                loc.set_checked(bool(input.value))
            else:
                loc.fill(str(input.value))
        except Exception as e:
            raise ToolError(f"form_input failed: {type(e).__name__}")

    def wait(self, context, input) -> None:
        self._page(input.tab_id).wait_for_timeout(min(input.duration, 30) * 1000)

    # ---------- 分頁 ----------
    def new_tab(self, context, input) -> BetaBrowserStateTabEntryParam:
        tab_id = self._register(self.ctx.new_page())
        self.active = tab_id
        return self._entry(tab_id)

    def switch_tab(self, context, input) -> BetaBrowserStateTabEntryParam:
        self._page(input.tab_id).bring_to_front()
        self.active = input.tab_id
        return self._entry(input.tab_id)

    def list_tabs(self, context, input) -> list[BetaBrowserStateTabEntryParam]:
        return [self._entry(t) for t in self._pages]

    def close_tab(self, context, input) -> None:
        self._page(input.tab_id).close()
        del self._pages[input.tab_id]
        if not self._pages:
            self._register(self.ctx.new_page())
        if self.active not in self._pages:
            self.active = next(iter(self._pages))

    def close(self) -> None:
        super().close()
        try:
            self.ctx.close()
        finally:
            self._pw.stop()
