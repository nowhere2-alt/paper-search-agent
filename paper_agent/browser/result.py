"""瀏覽器搜尋的結果。"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Paper


@dataclass
class BrowserResult:
    papers: list[Paper]
    blocked: bool = False  # 遇到登入頁、CAPTCHA、機器人檢查或存取被拒，搜尋沒有完成
