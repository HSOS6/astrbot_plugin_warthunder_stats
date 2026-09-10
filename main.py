import asyncio
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .renderer import WarThunderStatsRenderer
except Exception as _renderer_err:
    WarThunderStatsRenderer = None
    logger.debug(f"[WarThunderStats] 图片渲染器未加载: {_renderer_err}")

try:
    from .wt_app_client import WarThunderAppClient, WarThunderAppError
except Exception as _app_client_err:
    WarThunderAppClient = None
    WarThunderAppError = RuntimeError
    logger.debug(f"[WarThunderStats] App 数据源未加载: {_app_client_err}")

BEIJING_TZ = timezone(timedelta(hours=8))
PLUGIN_NAME = "astrbot_plugin_warthunder_stats"

NATION_MAP = [
    ("USA", "美系"),
    ("USSR", "苏系"),
    ("Great Britain", "英系"),
    ("Germany", "德系"),
    ("Japan", "日系"),
    ("Italy", "意系"),
    ("France", "法系"),
    ("China", "中系"),
    ("Sweden", "瑞系"),
    ("Israel", "以系"),
]

MODE_NAMES = {
    "arcade": "街机",
    "realistic": "历史",
    "simulation": "全真",
}

STAT_HEADERS_OVERVIEW = [
    "Victories",
    "Completed missions",
    "Victories/battles ratio",
    "Deaths",
    "Lions earned",
    "Play time",
    "Air targets destroyed",
    "Ground targets destroyed",
    "Naval targets destroyed",
]

STAT_HEADERS_AVIATION = [
    "Air battles",
    "Air battles in fighters",
    "Air battles in bombers",
    "Air battles in attackers",
    "Time played in air battles",
    "Time played in fighter",
    "Time played in bomber",
    "Time played in attackers",
    "Total targets destroyed",
    "Air targets destroyed",
    "Ground targets destroyed",
    "Naval targets destroyed",
]

STAT_HEADERS_GROUND = [
    "Ground battles",
    "Ground battles in tanks",
    "Ground battles in SPGs",
    "Ground battles in heavy tanks",
    "Ground battles in SPAA",
    "Time played in ground battles",
    "Tank battle time",
    "Tank Destroyer battle time",
    "Heavy Tank battle time",
    "SPAA battle time",
    "Total targets destroyed",
    "Air targets destroyed",
    "Ground targets destroyed",
    "Naval targets destroyed",
]

STAT_HEADERS_NAVAL = [
    "Naval battles",
    "Ship battles",
    "Motor torpedo boat battles",
    "Motor gun boat battles",
    "Motor torpedo gun boat battles",
    "Sub-chaser battles",
    "Destroyer battles",
    "Naval ferry barge battles",
    "Time played naval",
    "Time played on ship",
    "Time played on motor torpedo boat",
    "Time played on motor gun boat",
    "Time played on motor torpedo gun boat",
    "Time played on sub-chaser",
    "Time played on destroyer",
    "Time played on naval ferry barge",
    "Total targets destroyed",
    "Air targets destroyed",
    "Ground targets destroyed",
    "Naval targets destroyed",
]


class WarThunderAPIError(RuntimeError):
    """战雷数据查询错误。"""


class _CloudflareBlockedError(WarThunderAPIError):
    """Cloudflare 拦截（内部使用，用于控制流）。"""


def _parse_int(text: str) -> Optional[int]:
    if not text or text.strip().upper() in ("N/A", "NA", "--", "-", "—", ""):
        return None
    t = text.strip().replace(",", "").replace(" ", "")
    if t.endswith("%"):
        t = t[:-1]
    if t.endswith("K") or t.endswith("k"):
        try:
            return int(float(t[:-1]) * 1000)
        except ValueError:
            return None
    if t.endswith("M") or t.endswith("m"):
        try:
            return int(float(t[:-1]) * 1000000)
        except ValueError:
            return None
    try:
        return int(float(t))
    except ValueError:
        return None


def _parse_time_to_seconds(text: str) -> Optional[int]:
    if not text or text.strip().upper() in ("N/A", "NA", "--", "-", "—", ""):
        return None
    t = text.strip()
    total = 0
    for pattern, multiplier in [
        (r"(\d+)\s*d", 86400),
        (r"(\d+)\s*h", 3600),
        (r"(\d+)\s*m", 60),
        (r"(\d+)\s*s", 1),
    ]:
        m = re.search(pattern, t)
        if m:
            total += int(m.group(1)) * multiplier
    return total if total > 0 else None


def _fmt_time(seconds: Optional[int]) -> str:
    if seconds is None:
        return "[无数据]"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days} 天")
    if hours > 0 or days > 0:
        parts.append(f"{hours} 时")
    parts.append(f"{minutes} 分")
    return "".join(parts)


def _fmt_num(value: Any) -> str:
    try:
        v = int(float(value))
        return f"{v:,}"
    except Exception:
        return str(value) if value is not None else "[无数据]"


def _safe_div(a: Optional[int], b: Optional[int], ndigits: int = 2) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return round(a / b, ndigits)


class WarThunderOfficialScraper:
    """从 War Thunder 官网爬取玩家数据。

    支持页面：
    - /en/community/userinfo/?nick=xxx       玩家基本信息+战绩
    - /en/community/claninfo/<tag>           联队详情
    - /en/community/leaderboard/?game=xxx    排行榜

    用户信息页 Cloudflare Turnstile 绕过方式（按优先级）：
    1. FlareSolverr 代理（服务器首选，无需浏览器，一键部署）
    2. curl_cffi + cf_clearance cookie（适用于同 IP 环境）
    3. nodriver 浏览器自动化（桌面/有 GUI 环境可用）
    """

    BASE_URL = "https://warthunder.com"
    USERINFO_PATH = "/en/community/userinfo/?nick={nick}"
    CLANINFO_PATH = "/en/community/claninfo/{tag}"

    def __init__(self, timeout: int = 20, cf_clearance: str = "", browser_user_agent: str = "",
                 browser_impersonate: str = "chrome131", flaresolverr_url: str = "",
                 nodriver_headless: bool = False):
        self.timeout = timeout
        self.cf_clearance = cf_clearance.strip()
        self.browser_user_agent = browser_user_agent.strip()
        self.browser_impersonate = browser_impersonate.strip() or "chrome131"
        self.flaresolverr_url = flaresolverr_url.strip().rstrip("/")
        self.nodriver_headless = nodriver_headless

        self._has_curl_cffi = False
        try:
            import curl_cffi  # noqa: F401

            self._has_curl_cffi = True
        except ImportError:
            self._has_curl_cffi = False

        self._has_nodriver = False
        try:
            import nodriver  # noqa: F401

            self._has_nodriver = True
        except ImportError:
            self._has_nodriver = False

        self._curl_session = None

    def _get_curl_session(self):
        from curl_cffi import requests as curl_requests

        if self._curl_session is None:
            self._curl_session = curl_requests.Session()
            if self.cf_clearance:
                self._curl_session.cookies.set(
                    "cf_clearance",
                    self.cf_clearance,
                    domain=".warthunder.com",
                )
        return self._curl_session

    def _build_headers(self, with_sec_fetch: bool = False) -> Dict[str, str]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if self.browser_user_agent:
            headers["User-Agent"] = self.browser_user_agent
        else:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        if with_sec_fetch:
            headers.update({
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-CH-UA-Arch": '"x86"',
                "Sec-CH-UA-Bitness": '"64"',
                "Sec-CH-UA-Full-Version": '"131.0.6778.265"',
                "Sec-CH-UA-Full-Version-List": '"Google Chrome";v="131.0.6778.265", "Chromium";v="131.0.6778.265", "Not_A Brand";v="24.0.0.0"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Model": '""',
                "Sec-CH-UA-Platform": '"Windows"',
                "Sec-CH-UA-Platform-Version": '"15.0.0"',
            })
        return headers

    async def fetch_userinfo_html(self, nickname: str) -> str:
        url = self.BASE_URL + self.USERINFO_PATH.format(nick=urllib.parse.quote(nickname))

        # 优先级 1: FlareSolverr（服务器首选，无需浏览器）
        if self.flaresolverr_url:
            try:
                html = await self._fetch_userinfo_with_flaresolverr(url)
                if "user-profile__data-list" in html:
                    return html
                if "Player not found" in html or "No such user" in html:
                    raise WarThunderAPIError("未找到该玩家，请检查昵称是否正确")
                logger.warning("[WTScraper] FlareSolverr 返回了非预期内容，尝试下一种方案...")
            except WarThunderAPIError:
                raise
            except Exception as e:
                logger.warning(f"[WTScraper] FlareSolverr 请求失败: {e}，尝试下一种方案...")

        # 优先级 2: curl_cffi + cf_clearance cookie
        if self._has_curl_cffi and self.cf_clearance:
            try:
                html = await self._fetch_userinfo_with_cf_cookie(url)
            except _CloudflareBlockedError as e:
                logger.warning(f"[WTScraper] {e}")
                self._curl_session = None
            else:
                if "user-profile__data-list" in html:
                    return html
                if "Player not found" in html or "No such user" in html:
                    raise WarThunderAPIError("未找到该玩家，请检查昵称是否正确")
                cf_indicators = [
                    "challenge-platform",
                    "turnstile",
                    "cf-turnstile",
                    "cf-chl",
                    "_cf_chl_opt",
                    "Checking your browser",
                    "cf-mitigated",
                    "Just a moment",
                ]
                if any(indicator.lower() in html.lower() for indicator in cf_indicators):
                    logger.warning("[WTScraper] cf_clearance cookie 被 Cloudflare 拒绝，尝试下一种方案...")
                    self._curl_session = None
                else:
                    preview = html[:300].replace("\n", " ")
                    logger.warning(
                        f"[WTScraper] cf_clearance 请求返回了未知内容 "
                        f"(len={len(html)}, 前300字符: {preview})"
                    )
                    self._curl_session = None

        # 优先级 3: nodriver 浏览器自动化
        if self._has_nodriver:
            return await self._fetch_with_nodriver(url)

        # 所有方案都失败，给出具体建议
        lines = ["用户信息页被 Cloudflare Turnstile 拦截，所有方案均失败。"]
        if not self.flaresolverr_url:
            lines.append("")
            lines.append("【强烈推荐·服务器方案】部署 FlareSolverr：")
            lines.append("  Docker 部署（一行命令）：")
            lines.append("  docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
            lines.append("  然后在插件配置中设置 flaresolverr_url = http://localhost:8191")
            lines.append("  无需浏览器，无需 cf_clearance，一劳永逸。")
        if not self.cf_clearance:
            lines.append("")
            lines.append("【备选方案】配置 cf_clearance cookie（需从浏览器获取）")
        if not self._has_nodriver:
            lines.append("")
            lines.append("【本地方案】pip install nodriver（需 Chrome/Edge）")
        raise WarThunderAPIError("\n".join(lines))

    async def _fetch_userinfo_with_flaresolverr(self, url: str) -> str:
        """通过 FlareSolverr 代理获取页面，自动绕过 Cloudflare。"""
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 60000,
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout + 60, connect=self.timeout, sock_read=self.timeout + 60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.flaresolverr_url}/v1",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        raise WarThunderAPIError(f"FlareSolverr 返回 HTTP {resp.status}")
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise WarThunderAPIError(f"无法连接 FlareSolverr ({self.flaresolverr_url}): {e}") from e
        except asyncio.TimeoutError:
            raise WarThunderAPIError("FlareSolverr 请求超时") from None

        status = data.get("status", "")
        if status != "ok":
            message = data.get("message", "未知错误")
            raise WarThunderAPIError(f"FlareSolverr 错误: {message}")

        solution = data.get("solution", {})
        html = solution.get("response", "")
        if not html:
            raise WarThunderAPIError("FlareSolverr 返回空内容")

        return html

    async def _fetch_userinfo_with_cf_cookie(self, url: str) -> str:
        from curl_cffi import requests as curl_requests

        session = self._get_curl_session()
        headers = self._build_headers(with_sec_fetch=True)

        def _sync() -> str:
            resp = session.get(url, impersonate=self.browser_impersonate, timeout=self.timeout, headers=headers)
            if resp.status_code == 404:
                raise WarThunderAPIError("未找到该页面")
            if resp.status_code in (403, 503):
                raise _CloudflareBlockedError(
                    f"Cloudflare 拦截 (HTTP {resp.status_code})，cf_clearance 可能已过期或 IP 不匹配"
                )
            return resp.text

        return await asyncio.to_thread(_sync)

    async def fetch_claninfo_html(self, clan_tag: str) -> str:
        url = self.BASE_URL + self.CLANINFO_PATH.format(tag=urllib.parse.quote(clan_tag))
        return await self._fetch_simple(url)

    async def _fetch_simple(self, url: str) -> str:
        """联队页面无需绕 Cloudflare，用 curl_cffi 或 aiohttp 即可。"""
        logger.debug(f"[WTScraper] fetch: {url} (curl_cffi={self._has_curl_cffi})")
        if self._has_curl_cffi:
            return await self._fetch_with_curl_cffi(url)
        return await self._fetch_with_aiohttp(url)

    async def _fetch_with_nodriver(self, url: str) -> str:
        """使用 nodriver 启动浏览器绕过 Cloudflare Turnstile JS 挑战。"""
        import nodriver as uc

        browser = None
        try:
            browser = await uc.start(headless=self.nodriver_headless)
            page = await browser.get(url)
            logger.debug(f"[WTScraper] nodriver 页面已加载 (headless={self.nodriver_headless})，等待 Cloudflare 挑战...")

            html = ""
            for i in range(30):
                await asyncio.sleep(2)
                html = await page.get_content()
                if "user-profile__data-list" in html:
                    logger.debug(f"[WTScraper] nodriver 成功获取数据 ({i*2}s)")
                    return html
                if "Player not found" in html or "No such user" in html:
                    raise WarThunderAPIError("未找到该玩家，请检查昵称是否正确")
                logger.debug(f"[WTScraper] 等待中 [{i*2}s] len={len(html)}")

            if "challenge-platform" in html or "Enable JavaScript" in html:
                raise WarThunderAPIError("Cloudflare 挑战超时，请重试")
            raise WarThunderAPIError("页面加载超时，未获取到战绩数据")

        except WarThunderAPIError:
            raise
        except Exception as exc:
            raise WarThunderAPIError(f"浏览器获取失败: {exc}") from exc
        finally:
            if browser:
                try:
                    browser.stop()
                except Exception:
                    pass

    async def _fetch_with_curl_cffi(self, url: str) -> str:
        from curl_cffi import requests as curl_requests

        session = self._get_curl_session() if self.cf_clearance else curl_requests
        headers = self._build_headers()

        def _sync() -> str:
            resp = session.get(
                url,
                impersonate=self.browser_impersonate,
                timeout=self.timeout,
                headers=headers,
            )
            if resp.status_code == 404:
                raise WarThunderAPIError("未找到该页面")
            if resp.status_code >= 400:
                raise WarThunderAPIError(f"HTTP {resp.status_code}")
            return resp.text

        return await asyncio.to_thread(_sync)

    async def _fetch_with_aiohttp(self, url: str) -> str:
        headers = self._build_headers()
        if "User-Agent" not in headers:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            )
        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=self.timeout, sock_read=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 404:
                        raise WarThunderAPIError("未找到该页面")
                    if resp.status >= 400:
                        raise WarThunderAPIError(f"HTTP {resp.status}")
                    return await resp.text()
        except asyncio.TimeoutError as exc:
            raise WarThunderAPIError(f"请求超时({self.timeout}秒)") from exc
        except WarThunderAPIError:
            raise
        except Exception as exc:
            raise WarThunderAPIError(f"请求失败: {exc}") from exc


class WarThunderHTMLParser:
    """解析 War Thunder 官网页面的 HTML。"""

    def __init__(self):
        try:
            from bs4 import BeautifulSoup

            self._soup_cls = BeautifulSoup
            self._has_bs4 = True
        except ImportError:
            self._soup_cls = None
            self._has_bs4 = False

    def parse_userinfo(self, html: str, nickname: str) -> Dict[str, Any]:
        if not self._has_bs4:
            raise WarThunderAPIError("需要安装 beautifulsoup4 库")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        result: Dict[str, Any] = {
            "profile": self._parse_profile_bs4(soup, nickname),
            "overview": self._parse_overview_bs4(soup),
            "aviation": self._parse_category_bs4(soup, 0, STAT_HEADERS_AVIATION),
            "ground": self._parse_category_bs4(soup, 1, STAT_HEADERS_GROUND),
            "naval": self._parse_category_bs4(soup, 2, STAT_HEADERS_NAVAL),
            "nations": self._parse_nations_bs4(soup),
            "achievements_preview": self._parse_achievements_preview_bs4(soup),
        }
        return result

    def parse_claninfo(self, html: str, tag: str) -> Dict[str, Any]:
        if not self._has_bs4:
            raise WarThunderAPIError("需要安装 beautifulsoup4 库")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        result: Dict[str, Any] = {
            "tag": tag,
            "name": "",
            "full_name": "",
            "description": "",
            "creation_date": "",
            "member_count": 0,
            "stats": {},
            "rating": {},
            "members": [],
        }

        profile_el = soup.select_one(".clan-profile") or soup.select_one(".squadron-profile") or soup
        h1 = profile_el.select_one("h1")
        if h1:
            result["full_name"] = h1.get_text(strip=True)
            m = re.match(r"\[(.+?)\]\s*(.+)", result["full_name"])
            if m:
                result["tag"] = m.group(1).strip()
                result["name"] = m.group(2).strip()
            else:
                result["name"] = result["full_name"]

        info_text = profile_el.get_text("\n", strip=True)
        lines = info_text.split("\n")
        for i, line in enumerate(lines):
            if "Number of players" in line or "players:" in line.lower():
                m = re.search(r"(\d+)", line)
                if m:
                    result["member_count"] = int(m.group(1))
            if "date of creation" in line.lower():
                m = re.search(r"(\d{2}\.\d{2}\.\d{4})", line)
                if m:
                    result["creation_date"] = m.group(1)

        desc_el = profile_el.select_one(".clan-profile__description") or profile_el.select_one(
            ".squadron-profile__description"
        )
        if desc_el:
            result["description"] = desc_el.get_text(strip=True)
        else:
            for i, line in enumerate(lines):
                if "Number of players" in line and i > 0:
                    result["description"] = lines[i - 1].strip()
                    break

        stats_block = soup.select_one(".clan-statistics") or soup.select_one(
            ".squadron-statistics"
        )
        if stats_block:
            stat_items = stats_block.select(".clan-statistics__item") or stats_block.select(
                ".squadron-statistics__item"
            )
            for item in stat_items:
                label_el = item.select_one(".clan-statistics__label") or item.select_one(
                    ".squadron-statistics__label"
                )
                value_el = item.select_one(".clan-statistics__value") or item.select_one(
                    ".squadron-statistics__value"
                )
                if label_el and value_el:
                    label = label_el.get_text(strip=True)
                    value = value_el.get_text(strip=True)
                    result["stats"][label] = value

        rating_block = soup.select_one(".clan-rating") or soup.select_one(".squadron-rating")
        if rating_block:
            result["rating_text"] = rating_block.get_text(" | ", strip=True)

        members_table = soup.select_one("table") or soup.select_one(".clan-members") or soup.select_one(
            ".squadron-members"
        )
        if members_table:
            rows = members_table.select("tr")
            for row in rows[1:]:
                cols = row.select("td")
                if len(cols) >= 5:
                    member = {
                        "num": cols[0].get_text(strip=True),
                        "player": cols[1].get_text(strip=True),
                        "rating": cols[2].get_text(strip=True),
                        "activity": cols[3].get_text(strip=True),
                        "role": cols[4].get_text(strip=True),
                    }
                    if len(cols) >= 6:
                        member["join_date"] = cols[5].get_text(strip=True)
                    result["members"].append(member)

        if not result["members"]:
            member_links = soup.select('a[href*="/community/userinfo/"]')
            seen = set()
            for a in member_links:
                name = a.get_text(strip=True)
                if name and name not in seen and len(name) > 1:
                    seen.add(name)
                    result["members"].append({"player": name})

        return result

    def _parse_profile_bs4(self, soup, nickname: str) -> Dict[str, Any]:
        profile = soup.select_one(".user-profile__data-list")
        data: Dict[str, Any] = {
            "nickname": nickname,
            "uid": None,
            "title": "",
            "clan": "",
            "clan_tag": "",
            "clan_role": "",
            "level": 0,
            "exp_to_next": None,
            "registration_date": "",
            "play_time_seconds": None,
            "avatar_url": "",
        }
        if not profile:
            return data

        nick_el = profile.select_one(".user-profile__data-nick")
        if nick_el:
            data["nickname"] = nick_el.get_text(strip=True)

        clan_el = profile.select_one(".user-profile__data-clan")
        if clan_el:
            clan_text = clan_el.get_text(strip=True)
            data["clan"] = clan_text
            m = re.match(r"\[(.+?)\]", clan_text)
            if m:
                data["clan_tag"] = m.group(1)

        profile_text = profile.get_text("\n", strip=True)

        for item in profile.select(".user-profile__data-item"):
            text = item.get_text(" ", strip=True)
            lower = text.lower()
            if text.startswith("Level"):
                m = re.search(r"Level\s+(\d+)", text)
                if m:
                    data["level"] = int(m.group(1))
                m = re.search(r"(\d[\d,]*)\s*to\s*next", lower)
                if m:
                    data["exp_to_next"] = _parse_int(m.group(1))
            elif "experience" in lower and "next" in lower:
                m = re.search(r"(\d[\d,]*)\s*(?:to|until)\s*next", lower)
                if m:
                    data["exp_to_next"] = _parse_int(m.group(1))
            elif "play time" in lower or "game time" in lower:
                parsed_seconds = _parse_time_to_seconds(text)
                if parsed_seconds:
                    data["play_time_seconds"] = parsed_seconds
            elif "title" in lower or "rank" in lower:
                data["title"] = text
            elif not data["title"] and not re.search(r"\b(level|play time|game time|experience|next)\b", lower):
                # 某些页面不会显式带 title/rank 关键词，这里保守兜底一次
                data["title"] = text

        if not data["level"]:
            m = re.search(r"Level\s+(\d+)", profile_text, re.IGNORECASE)
            if m:
                data["level"] = int(m.group(1))

        if data["exp_to_next"] is None:
            m = re.search(r"(\d[\d,]*)\s*(?:to|until)\s*next", profile_text, re.IGNORECASE)
            if m:
                data["exp_to_next"] = _parse_int(m.group(1))
        if data["exp_to_next"] is None:
            m = re.search(r"next[^0-9]*(\d[\d,]*)", profile_text, re.IGNORECASE)
            if m:
                data["exp_to_next"] = _parse_int(m.group(1))

        if data["play_time_seconds"] is None:
            m = re.search(r"(?:Play time|Game time)[^\n]*?((?:\d+\s*d\s*)?(?:\d+\s*h\s*)?(?:\d+\s*m\s*)?(?:\d+\s*s\s*)?)", profile_text, re.IGNORECASE)
            if m:
                parsed_seconds = _parse_time_to_seconds(m.group(1))
                if parsed_seconds:
                    data["play_time_seconds"] = parsed_seconds
        if data["play_time_seconds"] is None:
            m = re.search(r"((?:\d+\s*d\s*)?(?:\d+\s*h\s*)?(?:\d+\s*m\s*)?(?:\d+\s*s\s*)?)", profile_text, re.IGNORECASE)
            if m:
                parsed_seconds = _parse_time_to_seconds(m.group(1))
                if parsed_seconds:
                    data["play_time_seconds"] = parsed_seconds

        reg_el = profile.select_one(".user-profile__data-regdate")
        if reg_el:
            text = reg_el.get_text(strip=True)
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if m:
                data["registration_date"] = m.group(1)
        if not data["registration_date"]:
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", profile_text)
            if m:
                data["registration_date"] = m.group(1)

        avatar_el = (
            soup.select_one(".user-profile__avatar img")
            or soup.select_one(".user-info__avatar img")
            or soup.select_one(".user-profile img")
            or soup.select_one("img[src*='avatar']")
            or soup.select_one("img[src*='cardicon']")
            or soup.select_one("img[data-src*='avatar']")
            or soup.select_one("img[data-src*='cardicon']")
        )
        if avatar_el:
            src = (
                avatar_el.get("src", "")
                or avatar_el.get("data-src", "")
                or avatar_el.get("data-original", "")
                or avatar_el.get("data-lazy-src", "")
            )
            if src:
                data["avatar_url"] = urllib.parse.urljoin("https://warthunder.com", src)

        if not data["avatar_url"]:
            for el in soup.select("[style*='avatar'], [style*='cardicon'], [style*='gaijinPass']"):
                style = el.get("style", "")
                m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
                if m:
                    data["avatar_url"] = urllib.parse.urljoin("https://warthunder.com", m.group(1))
                    break

        for a in soup.select("a[href*='/userinfo/']"):
            href = a.get("href", "")
            m = re.search(r"[?&]nick=([^&]+)", href)
            if m:
                data["uid"] = None
                break

        return data

    def _parse_overview_bs4(self, soup) -> Dict[str, Dict[str, Any]]:
        result = {}
        stat_block = soup.select_one(".user-profile__stat")
        if not stat_block:
            return result

        modes = [("arcade", "arcadeFightTab"), ("realistic", "historyFightTab"), ("simulation", "simulationFightTab")]
        for mode_key, mode_cls in modes:
            row = stat_block.select_one(f".{mode_cls}")
            if not row:
                continue
            values = [li.get_text(strip=True) for li in row.select(".user-stat__list-item")]
            if len(values) > 0 and values[0].endswith("battles"):
                values = values[1:]
            result[mode_key] = self._zip_values(STAT_HEADERS_OVERVIEW, values)

        return result

    def _parse_category_bs4(self, soup, index: int, headers: List[str]) -> Dict[str, Dict[str, Any]]:
        result = {}
        rows = soup.select(".user-stat__list-row")
        target_idx = index + 1
        if len(rows) <= target_idx:
            return result

        row = rows[target_idx]
        modes = [("arcade", "arcadeFightTab"), ("realistic", "historyFightTab"), ("simulation", "simulationFightTab")]
        for mode_key, mode_cls in modes:
            ul = row.select_one(f".{mode_cls}")
            if not ul:
                continue
            values = [li.get_text(strip=True) for li in ul.select(".user-stat__list-item")]
            result[mode_key] = self._zip_values(headers, values)

        return result

    def _parse_nations_bs4(self, soup) -> List[Dict[str, Any]]:
        """解析各国载具数据。优先使用已知国家列表过滤脏标题，并在标题缺失时回退到默认国家顺序。"""
        known_nations = [name for name, _ in NATION_MAP]
        score = (
            soup.select_one(".user-profile__score")
            or soup.select_one(".user-profile__vehicles")
            or soup.select_one("[class*='score']")
            or soup.select_one("[class*='vehicle']")
        )
        if not score:
            for el in soup.select("*"):
                txt = el.get_text(" ", strip=True)
                if "Vehicles and rewards" in txt and "USA" in txt:
                    score = el
                    break

        if not score:
            logger.warning("[WTParser] 未找到载具数据容器，尝试的 CSS 选择器均未匹配")
            return []

        cols = score.select(".user-score__list-col") or score.select("[class*='col']") or score.select("td")
        numeric_cols: List[List[str]] = []
        for col in cols:
            items = col.select(".user-score__list-item") or col.select("li") or col.select("span")
            values = [it.get_text(strip=True) for it in items if it.get_text(strip=True)]
            if values:
                numeric_cols.append(values)

        if not numeric_cols:
            logger.warning("[WTParser] 未找到载具数据列")
            return []

        titles = score.select(".user-score__list-title") or score.select("[class*='title']") or score.select("th")
        nation_names: List[str] = []
        for t in titles:
            txt = t.get_text(" ", strip=True)
            if txt in known_nations and txt not in nation_names:
                nation_names.append(txt)

        owned_counts: List[str] = numeric_cols[0] if len(numeric_cols) >= 1 else []
        elite_counts: List[str] = numeric_cols[1] if len(numeric_cols) >= 2 else []

        if not nation_names:
            fallback_count = max(len(owned_counts), len(elite_counts))
            if fallback_count > 0:
                nation_names = known_nations[:fallback_count]

        if not nation_names:
            logger.warning("[WTParser] 未找到国家名称列表")
            return []

        max_count = min(len(nation_names), max(len(owned_counts), len(elite_counts)))
        nation_names = nation_names[:max_count]

        nations = []
        for i, eng_name in enumerate(nation_names):
            owned = _parse_int(owned_counts[i]) if i < len(owned_counts) else 0
            elite = _parse_int(elite_counts[i]) if i < len(elite_counts) else 0
            cn_name = dict(NATION_MAP).get(eng_name, eng_name)
            nations.append({
                "name_en": eng_name,
                "name_cn": cn_name,
                "total": None,
                "owned": owned or 0,
                "elite": elite or 0,
            })

        if nations:
            logger.debug(
                f"[WTParser] 载具解析成功: nations={[(n['name_en'], n['owned'], n['elite']) for n in nations]}"
            )
        return nations

    def _parse_achievements_preview_bs4(self, soup) -> Dict[str, Any]:
        ach_section = soup.select_one(".user-info-sprite-achievements")
        if not ach_section:
            return {}
        items = ach_section.select(".user-info-sprite-achievements__item") or ach_section.select(
            "[class*=achievement]"
        )
        count = len(items)
        return {"preview_count": count}

    def _zip_values(self, headers: List[str], values: List[str]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for i, h in enumerate(headers):
            if i >= len(values):
                data[h] = None
                continue
            raw = values[i]
            v = _parse_int(raw)
            if v is not None and ("time" in h.lower() or "play time" in h.lower() or "played" in h.lower()):
                data[h] = {"raw": raw, "seconds": _parse_time_to_seconds(raw)}
            elif v is not None and "ratio" in h.lower():
                data[h] = {"raw": raw, "percent": float(raw.replace("%", "").strip()) if "%" in raw else v}
            else:
                data[h] = v if v is not None else raw
        return data


class WarThunderCustomAPI:
    """自定义 API 数据源。"""

    def __init__(self, base_url: str, api_key: str = "", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout

    async def get_player_stats(self, nickname: str, mode: str = "arcade") -> Dict[str, Any]:
        url = f"{self.base_url}/api/player/{urllib.parse.quote(nickname)}"
        params = {"mode": mode}
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=self.timeout, sock_read=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 404:
                        raise WarThunderAPIError("未找到该玩家")
                    if resp.status >= 400:
                        text = await resp.text()
                        raise WarThunderAPIError(f"HTTP {resp.status}: {text[:200]}")
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and data.get("success") is False:
                        raise WarThunderAPIError(str(data.get("message") or "查询失败"))
                    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                        return data["data"]
                    return data
        except asyncio.TimeoutError as exc:
            raise WarThunderAPIError(f"请求超时({self.timeout}秒)") from exc
        except WarThunderAPIError:
            raise
        except Exception as exc:
            raise WarThunderAPIError(f"请求失败: {exc}") from exc


class WarThunderDataService:
    """数据服务层：整合多个页面的数据，提供统一接口。"""

    def __init__(self, scraper: WarThunderOfficialScraper, parser: WarThunderHTMLParser):
        self.scraper = scraper
        self.parser = parser

    async def get_full_player_stats(self, nickname: str, include_clan: bool = True) -> Dict[str, Any]:
        """获取玩家完整数据：基本信息+战绩+联队。"""
        result: Dict[str, Any] = {"nickname": nickname}

        userinfo_html = await self.scraper.fetch_userinfo_html(nickname)
        if "玩家未找到" in userinfo_html or "Player not found" in userinfo_html or "No such user" in userinfo_html:
            raise WarThunderAPIError("未找到该玩家，请检查昵称是否正确")

        user_data = self.parser.parse_userinfo(userinfo_html, nickname)
        result.update(user_data)

        profile = user_data.get("profile", {})
        clan_tag = profile.get("clan_tag", "")
        clan_name = profile.get("clan", "")

        if include_clan and clan_tag and clan_name:
            try:
                clan_full_name = clan_name.replace(f"[{clan_tag}]", "").strip()
                if not clan_full_name:
                    clan_full_name = clan_tag
                clan_html = await self.scraper.fetch_claninfo_html(clan_full_name)
                clan_data = self.parser.parse_claninfo(clan_html, clan_tag)
                result["clan_detail"] = clan_data

                member_found = None
                for m in clan_data.get("members", []):
                    if m.get("player", "").lower() == nickname.lower():
                        member_found = m
                        break
                if member_found:
                    result["clan_member_info"] = member_found
            except Exception as e:
                logger.warning(f"[WTDataService] 获取联队信息失败: {e}")
                result["clan_detail"] = None

        return result

    async def get_clan_info(self, clan_tag_or_name: str) -> Dict[str, Any]:
        html = await self.scraper.fetch_claninfo_html(clan_tag_or_name)
        return self.parser.parse_claninfo(html, clan_tag_or_name)


@register(
    PLUGIN_NAME,
    "星见雅",
    "战争雷霆战绩查询插件",
    "v2.2.0",
)
class WarThunderStatsPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_source = str(config.get("data_source", "app")).lower()
        self.app_login = str(config.get("app_login", "")).strip()
        self.app_password = str(config.get("app_password", "")).strip()
        self.api_base_url = str(config.get("api_base_url", "")).strip()
        self.api_key = str(config.get("api_key", "")).strip()
        self.api_timeout_seconds = int(config.get("api_timeout_seconds", 20))
        self.default_mode = str(config.get("default_mode", "arcade")).lower()
        if self.default_mode not in MODE_NAMES:
            self.default_mode = "arcade"

        self.enable_clan_info = bool(config.get("enable_clan_info", True))

        self.cf_clearance = str(config.get("cf_clearance", "")).strip()
        self.browser_user_agent = str(config.get("browser_user_agent", "")).strip()
        self.browser_impersonate = str(config.get("browser_impersonate", "chrome131")).strip()
        self.flaresolverr_url = str(config.get("flaresolverr_url", "")).strip()
        self.nodriver_headless = bool(config.get("nodriver_headless", False))

        self.scraper = WarThunderOfficialScraper(
            timeout=self.api_timeout_seconds,
            cf_clearance=self.cf_clearance,
            browser_user_agent=self.browser_user_agent,
            browser_impersonate=self.browser_impersonate,
            flaresolverr_url=self.flaresolverr_url,
            nodriver_headless=self.nodriver_headless,
        )
        self.parser = WarThunderHTMLParser()
        self.custom_api = WarThunderCustomAPI(
            base_url=self.api_base_url,
            api_key=self.api_key,
            timeout=self.api_timeout_seconds,
        ) if self.api_base_url else None
        self.data_service = WarThunderDataService(self.scraper, self.parser)
        self.app_client = None
        if WarThunderAppClient is not None:
            self.app_client = WarThunderAppClient(
                login=self.app_login,
                password=self.app_password,
                timeout=self.api_timeout_seconds,
                module_dir=Path(__file__).resolve().parent,
            )

        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.renderer = None
        if WarThunderStatsRenderer is not None:
            try:
                self.renderer = WarThunderStatsRenderer(str(self.data_dir))
            except Exception as e:
                logger.warning(f"[WarThunderStats] 图片渲染器初始化失败: {e}")

        self._check_libs()
        logger.info(
            f"[WarThunderStats] 插件已加载 (数据源={self.data_source}, "
            f"App账号={'已配置' if self.app_login and self.app_password else '未配置'}, "
            f"FlareSolverr={'已配置' if self.flaresolverr_url else '未配置'}, "
            f"cf_clearance={'已配置' if self.cf_clearance else '未配置'}, "
            f"nodriver={self.scraper._has_nodriver}, "
            f"curl_cffi={self.scraper._has_curl_cffi}, bs4={self.parser._has_bs4}, "
            f"renderer={'开' if self.renderer else '关'}, "
            f"联队信息={'开' if self.enable_clan_info else '关'})"
        )

    def _check_libs(self):
        missing = []
        if self.data_source == "app":
            if WarThunderAppClient is None:
                missing.append("blackboxprotobuf cryptography httpx")
            if self.renderer is None:
                try:
                    import PIL  # noqa: F401
                except ImportError:
                    missing.append("Pillow")
        if self.data_source == "official":
            if not self.parser._has_bs4:
                missing.append("beautifulsoup4")
            if not self.scraper._has_curl_cffi:
                missing.append("curl_cffi")
            if not self.cf_clearance and not self.flaresolverr_url and not self.scraper._has_nodriver:
                missing.append("nodriver")
            if self.renderer is None:
                try:
                    import PIL  # noqa: F401
                except ImportError:
                    missing.append("Pillow")
        if missing:
            lines = [f"[WarThunderStats] 缺少依赖库: {', '.join(missing)}。"]
            lines.append(f"  请执行: pip install {' '.join(missing)}")
            if "curl_cffi" in missing:
                lines.append("  curl_cffi - TLS指纹伪造")
            if "nodriver" in missing and not self.cf_clearance and not self.flaresolverr_url:
                lines.append("  nodriver - 浏览器自动化绕过Cloudflare（本地方案）")
                lines.append("  【服务器推荐】部署 FlareSolverr 或在配置中填写 flaresolverr_url")
            if "beautifulsoup4" in missing:
                lines.append("  beautifulsoup4 - HTML解析")
            if "Pillow" in missing or "pillow" in missing:
                lines.append("  Pillow - 图片生成功能所需")
            logger.warning("\n".join(lines))

    def _resolve_mode(self, mode: str) -> str:
        mode_lower = mode.lower()
        if mode_lower in ("街机", "arc", "arcade", ""):
            return "arcade"
        elif mode_lower in ("历史", "rb", "real", "realistic"):
            return "realistic"
        elif mode_lower in ("全真", "sb", "sim", "simulation"):
            return "simulation"
        return self.default_mode

    def _extract_nick_and_mode(self, event: AstrMessageEvent, nickname: str, mode: str) -> Tuple[str, str]:
        if not nickname:
            match = re.match(
                r"(?:战雷战绩|wt战绩|WT战绩|战争雷霆战绩|战绩总览)\s+(.+)",
                event.message_str.strip(),
                re.IGNORECASE,
            )
            if match:
                rest = match.group(1).strip()
                parts = rest.split()
                if len(parts) >= 1:
                    nickname = parts[0]
                if len(parts) >= 2:
                    mode = parts[1]
        return nickname, mode

    async def _fetch_player_data(self, nickname: str) -> Dict[str, Any]:
        if self.data_source == "app":
            if self.app_client is None:
                raise WarThunderAPIError("App 数据源不可用，请确认依赖 blackboxprotobuf/cryptography/httpx 已安装。")
            try:
                data = await self.app_client.get_full_player_stats(nickname)
                await self._try_fill_registration_date(data, nickname)
                return data
            except WarThunderAPIError:
                raise
            except Exception as exc:
                raise WarThunderAPIError(str(exc)) from exc

        if self.data_source == "custom":
            if self.custom_api is None:
                raise WarThunderAPIError("未配置 api_base_url，无法使用 custom 数据源。")
            return await self.custom_api.get_player_stats(nickname, self.default_mode)

        return await self.data_service.get_full_player_stats(
            nickname,
            include_clan=self.enable_clan_info,
        )

    async def _try_fill_registration_date(self, data: Dict[str, Any], nickname: str):
        """App 公共接口不返回注册时间；有官网绕过配置时，额外从官网补一次注册时间。

        失败不影响 App 主数据查询，避免因为 Cloudflare 导致整张图生成失败。
        """
        profile = data.get("profile", {}) or {}
        if profile.get("registration_date"):
            return
        if not (self.flaresolverr_url or self.cf_clearance or self.scraper._has_nodriver):
            return
        try:
            html = await self.scraper.fetch_userinfo_html(nickname)
            parsed = self.parser.parse_userinfo(html, nickname)
            parsed_profile = parsed.get("profile") or {}

            # 官网页如果能通过 Cloudflare，则它通常也包含当前展示头像；优先回填这个真实头像 URL。
            official_avatar = str(parsed_profile.get("avatar_url") or "").strip()
            if official_avatar:
                profile["avatar_url"] = official_avatar
                data["avatar_url"] = official_avatar

            reg_date = parsed_profile.get("registration_date")
            if reg_date:
                m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})$", str(reg_date).strip())
                reg_show = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else str(reg_date).strip()
                profile["registration_date"] = reg_show

                profile_lines = data.get("profile_lines", []) or []
                inserted = False
                for item in profile_lines:
                    if item.get("label") == "注册时间":
                        item["value"] = reg_show
                        inserted = True
                        break
                if not inserted:
                    insert_at = next(
                        (i + 1 for i, item in enumerate(profile_lines) if item.get("label") == "联队标识"),
                        min(3, len(profile_lines)),
                    )
                    profile_lines.insert(insert_at, {"label": "注册时间", "value": reg_show})
                data["profile_lines"] = profile_lines

            data["profile"] = profile
        except Exception as e:
            logger.debug(f"[WarThunderStats] App 数据源补注册时间失败: {e}")

    def _format_stats(self, data: Dict[str, Any], mode: str) -> str:
        lines: List[str] = []

        profile = data.get("profile", {}) or {}
        nick = profile.get("nickname", "未知")
        clan = profile.get("clan", "")
        level = profile.get("level", 0)
        reg_date = profile.get("registration_date", "")

        lines.append(f"🎖 {nick} 的战绩总览")
        lines.append("━━━━━━━━━━━━━━━━")
        if clan:
            lines.append(f"联队：{clan}")
        if level:
            lines.append(f"等级：{level} 级")
        if reg_date:
            lines.append(f"注册时间：{reg_date}")

        clan_member = data.get("clan_member_info")
        if clan_member:
            role = clan_member.get("role", "")
            activity = clan_member.get("activity", "")
            join_date = clan_member.get("join_date", "")
            extra = []
            if role:
                extra.append(f"职位：{role}")
            if activity:
                extra.append(f"活跃度：{activity}")
            if join_date:
                extra.append(f"入队：{join_date}")
            if extra:
                lines.append(f"👥 联队信息：{' | '.join(extra)}")

        overview = data.get("overview", {}) or {}
        mode_data = overview.get(mode, {})
        if mode_data:
            lines.append("")
            lines.append(f"📊 {MODE_NAMES.get(mode, mode)}模式总览")
            lines.append("───────")

            victories = mode_data.get("Victories")
            missions = mode_data.get("Completed missions")
            ratio = mode_data.get("Victories/battles ratio")
            deaths = mode_data.get("Deaths")
            lions = mode_data.get("Lions earned")
            play_time = mode_data.get("Play time")
            air_kills = mode_data.get("Air targets destroyed")
            ground_kills = mode_data.get("Ground targets destroyed")
            naval_kills = mode_data.get("Naval targets destroyed")

            missions_int = _parse_int(str(missions)) if missions else None
            victories_int = _parse_int(str(victories)) if victories else None
            deaths_int = _parse_int(str(deaths)) if deaths else None
            air_int = _parse_int(str(air_kills)) if air_kills else None
            ground_int = _parse_int(str(ground_kills)) if ground_kills else None

            total_kills_int = None
            if air_int is not None or ground_int is not None:
                total_kills_int = (air_int or 0) + (ground_int or 0)

            kd_ratio = _safe_div(total_kills_int, deaths_int)
            avg_kills = _safe_div(total_kills_int, missions_int, 3)
            avg_lions = _safe_div(_parse_int(str(lions)) if lions else None, missions_int)

            lines.append(f"战斗场次：{_fmt_num(missions)}")
            lines.append(f"胜利场次：{_fmt_num(victories)}")
            if isinstance(ratio, dict):
                lines.append(f"场次胜率：{ratio.get('raw', str(ratio))}")
            else:
                lines.append(f"场次胜率：{_fmt_num(ratio)}" if ratio else "场次胜率：[无数据]")
            lines.append(f"阵亡次数：{_fmt_num(deaths)}")
            if kd_ratio is not None:
                lines.append(f"击杀阵亡比（K/D）：{kd_ratio:.2f}")
            if avg_kills is not None:
                lines.append(f"场均击杀：{avg_kills:.3f}")
            if isinstance(lions, int):
                lines.append(f"获得银狮：{_fmt_num(lions)}")
                if avg_lions is not None:
                    lines.append(f"场均银狮：{_fmt_num(int(avg_lions))}")
            if isinstance(play_time, dict):
                lines.append(f"游玩时长：{_fmt_time(play_time.get('seconds'))}")
            lines.append(f"击毁空军：{_fmt_num(air_kills)}")
            lines.append(f"击毁陆军：{_fmt_num(ground_kills)}")
            if naval_kills is not None and str(naval_kills).upper() != "N/A":
                lines.append(f"击毁海军：{_fmt_num(naval_kills)}")

        categories = [
            ("aviation", "✈️ 空战数据", STAT_HEADERS_AVIATION),
            ("ground", "🚛 陆战数据", STAT_HEADERS_GROUND),
            ("naval", "🚢 海战数据", STAT_HEADERS_NAVAL),
        ]
        for cat_key, cat_title, cat_headers in categories:
            cat = data.get(cat_key, {}) or {}
            md = cat.get(mode, {})
            if not md:
                continue
            values = list(md.values())
            if all(v is None or (isinstance(v, str) and v.upper() == "N/A") for v in values[:5]):
                continue

            lines.append("")
            lines.append(f"{cat_title}（{MODE_NAMES.get(mode, mode)}）")
            lines.append("───────")

            battles = md.get(cat_headers[0])
            total_destroyed = md.get("Total targets destroyed")
            air_destroyed = md.get("Air targets destroyed")
            ground_destroyed = md.get("Ground targets destroyed")
            naval_destroyed = md.get("Naval targets destroyed")

            time_key = None
            for h in cat_headers:
                if "Time played" in h and (
                    "air battles" in h.lower() or "ground battles" in h.lower() or "naval" in h.lower()
                ):
                    time_key = h
                    break
            play_time_val = md.get(time_key) if time_key else None

            battles_int = _parse_int(str(battles)) if battles else None
            total_int = _parse_int(str(total_destroyed)) if total_destroyed else None

            if battles:
                lines.append(f"战斗场次：{_fmt_num(battles)}")
            if isinstance(play_time_val, dict):
                lines.append(f"游玩时长：{_fmt_time(play_time_val.get('seconds'))}")
            if total_destroyed is not None:
                lines.append(f"总击毁：{_fmt_num(total_destroyed)}")
                if battles_int and total_int:
                    eff = round(total_int / battles_int, 3)
                    lines.append(f"场均击毁：{eff:.3f}")
            if air_destroyed is not None:
                lines.append(f"  空军：{_fmt_num(air_destroyed)}")
            if ground_destroyed is not None:
                lines.append(f"  陆军：{_fmt_num(ground_destroyed)}")
            if naval_destroyed is not None and str(naval_destroyed).upper() != "N/A":
                lines.append(f"  海军：{_fmt_num(naval_destroyed)}")

            sub_types = []
            if cat_key == "aviation":
                sub_types = [
                    ("Air battles in fighters", "战斗机场次"),
                    ("Air battles in bombers", "轰炸机场次"),
                    ("Air battles in attackers", "攻击机场次"),
                ]
            elif cat_key == "ground":
                sub_types = [
                    ("Ground battles in tanks", "坦机场次"),
                    ("Ground battles in SPGs", "自行火炮场次"),
                    ("Ground battles in heavy tanks", "重型坦机场次"),
                    ("Ground battles in SPAA", "防空车场次"),
                ]
            elif cat_key == "naval":
                sub_types = [
                    ("Ship battles", "主力舰场次"),
                    ("Motor torpedo boat battles", "鱼雷艇场次"),
                    ("Motor gun boat battles", "炮艇场次"),
                    ("Destroyer battles", "驱逐舰场次"),
                ]
            sub_lines = []
            for key, label in sub_types:
                val = md.get(key)
                if val is not None and str(val).upper() != "N/A":
                    sub_lines.append(f"{label}：{_fmt_num(val)}")
            if sub_lines:
                lines.append("  " + " | ".join(sub_lines))

        nations = data.get("nations", []) or []
        if nations:
            total_owned = sum(n.get("owned", 0) or 0 for n in nations)
            total_elite = sum(n.get("elite", 0) or 0 for n in nations)
            if total_owned > 0:
                lines.append("")
                lines.append("📦 各系载具")
                lines.append("───────")
                for n in nations:
                    owned = n.get("owned", 0) or 0
                    elite = n.get("elite", 0) or 0
                    rate = (elite / owned * 100) if owned > 0 else 0.0
                    lines.append(f"{n['name_cn']}：拥有 {owned} / 精英 {elite}（精英率 {rate:.1f}%）")
                lines.append("───────")
                total_rate = (total_elite / total_owned * 100) if total_owned > 0 else 0.0
                lines.append(f"总计：拥有 {total_owned} / 精英 {total_elite}（精英率 {total_rate:.1f}%）")

        lines.append("")
        now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        data_source_text = "War Thunder 助手 App"
        if self.data_source == "official":
            data_source_text = "War Thunder 官网"
        if self.data_source == "custom":
            data_source_text = "自定义API"
        extra = []
        if self.enable_clan_info and data.get("clan_detail"):
            extra.append("含联队数据")
        extra_str = f"（{'，'.join(extra)}）" if extra else ""
        lines.append(f"⏰ 数据来源：{data_source_text}{extra_str} | 查询时间：{now}")

        return "\n".join(lines)

    def _build_render_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """将解析后的数据转换为参考图风格渲染器所需结构。"""
        if data.get("render_ready"):
            return data
        profile = data.get("profile", {}) or {}
        nations = data.get("nations", []) or []
        overview = data.get("overview", {}) or {}
        aviation = data.get("aviation", {}) or {}
        ground = data.get("ground", {}) or {}
        naval = data.get("naval", {}) or {}
        clan_member_info = data.get("clan_member_info", {}) or {}
        achievements_preview = data.get("achievements_preview", {}) or {}

        def _has_value(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                t = value.strip()
                if not t or t.upper() in ("N/A", "NA", "--", "-", "—", "NULL", "NONE", "[无数据]"):
                    return False
            return True

        def _raw(value: Any) -> Any:
            if isinstance(value, dict):
                return value.get("raw")
            return value

        def _seconds(value: Any) -> Optional[int]:
            if isinstance(value, dict):
                sec = value.get("seconds")
                if sec is not None:
                    return sec
                value = value.get("raw")
            if not _has_value(value):
                return None
            if isinstance(value, (int, float)):
                return int(value)
            return _parse_time_to_seconds(str(value))

        def _num(value: Any) -> Optional[int]:
            if not _has_value(value):
                return None
            return _parse_int(str(_raw(value)))

        def _fmt_date(date_text: str) -> str:
            text = str(date_text or "").strip()
            m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})$", text)
            if m:
                return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            return text

        def _fmt_hours_text(seconds: Optional[int]) -> Optional[str]:
            if seconds is None or seconds <= 0:
                return None
            hours = max(1, round(seconds / 3600))
            return f"{hours} 小时"

        def _fmt_time_text(value: Any) -> Optional[str]:
            sec = _seconds(value)
            if sec is None:
                return None
            return _fmt_time(sec)

        def _metric(label: str, value: Any, extra: Any = None, color: str = "black") -> Optional[Dict[str, Any]]:
            if not _has_value(value):
                return None
            item: Dict[str, Any] = {"label": label, "value": str(value), "color": color}
            if _has_value(extra):
                item["extra"] = str(extra)
            return item

        def _row(label: str, value: Any, color: str = "black") -> Optional[Dict[str, Any]]:
            if not _has_value(value):
                return None
            return {"label": label, "value": str(value), "color": color}

        def _percent_color(percent: Optional[float]) -> str:
            if percent is None:
                return "black"
            if percent >= 75:
                return "purple"
            if percent >= 55:
                return "orange"
            if percent >= 40:
                return "yellow"
            return "red"

        profile_lines: List[Dict[str, Any]] = []

        nick_value = str(profile.get("nickname", "未知"))
        uid_value = profile.get("uid")
        if _has_value(uid_value):
            nick_value = f"{nick_value}（{uid_value}）"
        profile_lines.append({"label": "玩家昵称", "value": nick_value})

        title_text = profile.get("title")
        if _has_value(title_text):
            clean_title = str(title_text).strip()
            clean_title = re.sub(r"^(title|rank)\s*[:：]?\s*", "", clean_title, flags=re.IGNORECASE)
            profile_lines.append({"label": "个人称号", "value": clean_title})

        clan_text = profile.get("clan")
        clan_tag = profile.get("clan_tag")
        clan_rating = clan_member_info.get("rating")
        if _has_value(clan_text) or _has_value(clan_tag):
            clan_show = str(clan_text or clan_tag or "")
            if _has_value(clan_rating):
                clan_show = f"{clan_show}（{clan_rating}）"
            profile_lines.append({"label": "联队标识", "value": clan_show})

        role_text = clan_member_info.get("role") or profile.get("clan_role")
        if not _has_value(role_text):
            clan_detail = data.get("clan_detail", {}) or {}
            members = clan_detail.get("members", []) or []
            nickname_lower = str(profile.get("nickname", "") or "").lower()
            for member in members:
                if str(member.get("player", "") or "").lower() == nickname_lower and _has_value(member.get("role")):
                    role_text = member.get("role")
                    break
        if _has_value(role_text):
            profile_lines.append({"label": "联队身份", "value": str(role_text)})

        reg_date = _fmt_date(profile.get("registration_date", ""))
        if _has_value(reg_date):
            profile_lines.append({"label": "注册时间", "value": reg_date})

        level = profile.get("level")
        exp_current = profile.get("exp_current")
        exp_to_next = profile.get("exp_to_next")
        if _has_value(level):
            level_text = f"{level} 级"
            if _has_value(exp_current) and _has_value(exp_to_next):
                # App 数据源：当前等级经验 / 下一级总经验，剩余经验需计算得出。
                remaining_exp = max(0, int(float(exp_to_next)) - int(float(exp_current)))
                level_text += f"（经验：{_fmt_num(exp_current)} / {_fmt_num(exp_to_next)}，还需 {_fmt_num(remaining_exp)}）"
            elif _has_value(exp_to_next):
                # 官网页面解析到的字段通常是 remaining/to next，保留“还需”语义。
                level_text += f"（还需 {_fmt_num(exp_to_next)} 经验到下一级）"
            profile_lines.append({"label": "当前等级", "value": level_text})

        service_seconds = profile.get("play_time_seconds")
        service_text = _fmt_hours_text(service_seconds)
        if _has_value(service_text):
            profile_lines.append({"label": "赛博服役", "value": service_text})

        nation_order = [
            ("USA", "美系"),
            ("Germany", "德系"),
            ("USSR", "苏系"),
            ("Great Britain", "英系"),
            ("Japan", "日系"),
            ("China", "中系"),
            ("Italy", "意系"),
            ("France", "法系"),
            ("Sweden", "瑞系"),
            ("Israel", "以系"),
        ]
        nation_map: Dict[str, Dict[str, Any]] = {}
        for n in nations:
            name_en = str(n.get("name_en", "") or "").strip()
            if name_en not in dict(nation_order):
                continue
            nation_map[name_en] = n

        nation_rows: List[Dict[str, Any]] = []
        for name_en, label in nation_order:
            n = nation_map.get(name_en)
            if not n:
                continue
            owned = int(n.get("owned", 0) or 0)
            elite = int(n.get("elite", 0) or 0)
            rate = (elite / owned * 100.0) if owned > 0 else 0.0
            nation_rows.append(
                {
                    "label": label,
                    "owned": owned,
                    "elite": elite,
                    "rate": round(rate, 2),
                }
            )

        leaderboards: List[Dict[str, Any]] = []

        def _mode_rows(mode: str) -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            ov = overview.get(mode, {}) or {}
            av = aviation.get(mode, {}) or {}
            gr = ground.get(mode, {}) or {}
            nv = naval.get(mode, {}) or {}

            ratio_obj = ov.get("Victories/battles ratio")
            ratio_raw = _raw(ratio_obj)
            ratio_percent: Optional[float] = None
            if isinstance(ratio_obj, dict) and ratio_obj.get("percent") is not None:
                try:
                    ratio_percent = float(ratio_obj["percent"])
                except Exception:
                    ratio_percent = None
            elif _has_value(ratio_raw):
                parsed_ratio = _parse_int(str(ratio_raw).replace("%", ""))
                ratio_percent = float(parsed_ratio) if parsed_ratio is not None else None

            sections: List[List[Optional[Dict[str, Any]]]] = [
                [
                    _row("战斗场次", _fmt_num(ov.get("Completed missions"))),
                    _row("胜利场次", _fmt_num(ov.get("Victories"))),
                    _row("场次胜率", str(ratio_raw), color=_percent_color(ratio_percent) if _has_value(ratio_raw) else "black"),
                    _row("游玩时长", _fmt_time_text(ov.get("Play time"))),
                ],
                [
                    _row("击毁空军", _fmt_num(ov.get("Air targets destroyed"))),
                    _row("击毁陆军", _fmt_num(ov.get("Ground targets destroyed"))),
                    _row("击毁海军", _fmt_num(ov.get("Naval targets destroyed"))),
                ],
                [
                    _row("空战场次", _fmt_num(av.get("Air battles"))),
                    _row("空战时长", _fmt_time_text(av.get("Time played in air battles"))),
                    _row("空战总击毁", _fmt_num(av.get("Total targets destroyed"))),
                    _row("空战击毁空军", _fmt_num(av.get("Air targets destroyed"))),
                    _row("空战击毁陆军", _fmt_num(av.get("Ground targets destroyed"))),
                    _row("空战击毁海军", _fmt_num(av.get("Naval targets destroyed"))),
                ],
                [
                    _row("战斗机时长", _fmt_time_text(av.get("Time played in fighter"))),
                    _row("轰炸机时长", _fmt_time_text(av.get("Time played in bomber"))),
                    _row("攻击机时长", _fmt_time_text(av.get("Time played in attackers"))),
                ],
                [
                    _row("陆战场次", _fmt_num(gr.get("Ground battles"))),
                    _row("陆战时长", _fmt_time_text(gr.get("Time played in ground battles"))),
                    _row("陆战总击毁", _fmt_num(gr.get("Total targets destroyed"))),
                    _row("陆战击毁空军", _fmt_num(gr.get("Air targets destroyed"))),
                    _row("陆战击毁陆军", _fmt_num(gr.get("Ground targets destroyed"))),
                    _row("陆战击毁海军", _fmt_num(gr.get("Naval targets destroyed"))),
                ],
                [
                    _row("主战坦克时长", _fmt_time_text(gr.get("Tank battle time"))),
                    _row("重型坦克时长", _fmt_time_text(gr.get("Heavy Tank battle time"))),
                    _row("坦歼时长", _fmt_time_text(gr.get("Tank Destroyer battle time"))),
                    _row("自行防空时长", _fmt_time_text(gr.get("SPAA battle time"))),
                ],
                [
                    _row("海战场次", _fmt_num(nv.get("Naval battles"))),
                    _row("海战时长", _fmt_time_text(nv.get("Time played naval"))),
                    _row("海战总击毁", _fmt_num(nv.get("Total targets destroyed"))),
                    _row("海战击毁空军", _fmt_num(nv.get("Air targets destroyed"))),
                    _row("海战击毁陆军", _fmt_num(nv.get("Ground targets destroyed"))),
                    _row("海战击毁海军", _fmt_num(nv.get("Naval targets destroyed"))),
                ],
                [
                    _row("巡洋舰时长", _fmt_time_text(nv.get("Time played on ship"))),
                    _row("驱逐舰时长", _fmt_time_text(nv.get("Time played on destroyer"))),
                ],
            ]

            for section in sections:
                valid_rows = [r for r in section if r]
                if not valid_rows:
                    continue
                if rows:
                    rows.append({"type": "gap"})
                rows.extend(valid_rows)

            while rows and rows[-1].get("type") == "gap":
                rows.pop()
            return rows

        detail_columns: List[Dict[str, Any]] = []
        for mode_key, title in [
            ("arcade", "PVP街机数据"),
            ("realistic", "PVP历史数据"),
            ("simulation", "PVP拟真数据"),
        ]:
            rows = _mode_rows(mode_key)
            if rows:
                detail_columns.append({"title": title, "rows": rows})

        footer_lines: List[str] = []

        render_data: Dict[str, Any] = {
            "nickname": profile.get("nickname", data.get("nickname", "未知")),
            "avatar_url": profile.get("avatar_url", ""),
            "profile": profile,
            "profile_lines": profile_lines,
            "medals": [],
            "medal_count": 0,
            "nations": nation_rows,
            "leaderboards": leaderboards,
            "detail_columns": detail_columns,
            "footer_lines": footer_lines,
            "snapshot_time": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        return render_data

    def _format_clan_info(self, data: Dict[str, Any]) -> str:
        lines: List[str] = []
        full_name = data.get("full_name", data.get("name", data.get("tag", "未知")))
        lines.append(f"⚔️ 联队信息：{full_name}")
        lines.append("━━━━━━━━━━━━━━━━")

        if data.get("description"):
            lines.append(f"描述：{data['description']}")
        if data.get("member_count"):
            lines.append(f"成员数：{data['member_count']} 人")
        if data.get("creation_date"):
            lines.append(f"创建时间：{data['creation_date']}")

        stats = data.get("stats", {})
        if stats:
            lines.append("")
            lines.append("📊 联队统计")
            lines.append("───────")
            for k, v in stats.items():
                lines.append(f"{k}：{v}")

        rating_text = data.get("rating_text", "")
        if rating_text:
            lines.append("")
            lines.append(f"⭐ 联队评级：{rating_text}")

        members = data.get("members", [])
        if members:
            lines.append("")
            lines.append(f"👥 成员列表（前 {min(len(members), 15)} 名）")
            lines.append("───────")
            for m in members[:15]:
                player = m.get("player", "")
                role = m.get("role", "")
                activity = m.get("activity", "")
                parts = [player]
                if role:
                    parts.append(f"[{role}]")
                if activity:
                    parts.append(f"活跃度:{activity}")
                lines.append(" ".join(parts))

        lines.append("")
        now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"⏰ 数据来源：War Thunder 官网 | 查询时间：{now}")
        return "\n".join(lines)

    # ────────────── 指令：战绩查询 ──────────────

    @filter.command("战雷战绩", alias={"wt战绩", "WT战绩", "战争雷霆战绩", "战绩总览"})
    async def warthunder_stats(self, event: AstrMessageEvent, nickname: str = "", mode: str = ""):
        """查询战争雷霆玩家战绩。用法：/战雷战绩 <昵称> [模式]"""
        event.stop_event()

        nickname, mode = self._extract_nick_and_mode(event, nickname, mode)

        if not nickname:
            yield event.plain_result(
                "用法：/战雷战绩 <玩家昵称> [模式]\n"
                "示例：/战雷战绩 HOSO6\n"
                "模式可选：街机 / 历史 / 全真\n"
                f"当前默认模式：{MODE_NAMES.get(self.default_mode, self.default_mode)}\n"
                f"数据源：{'War Thunder 助手 App（完整数据）' if self.data_source == 'app' else ('自定义 API' if self.data_source == 'custom' else 'War Thunder 官网（含联队）')}"
            )
            return

        query_mode = self._resolve_mode(mode)

        try:
            data = await asyncio.wait_for(
                self._fetch_player_data(nickname),
                timeout=self.api_timeout_seconds * 4 + 30,
            )
        except asyncio.TimeoutError:
            yield event.plain_result("查询超时，请稍后再试。\n（nodriver 需 10-15 秒通过 Cloudflare，可关闭联队信息以加快速度）")
            return
        except WarThunderAPIError as e:
            yield event.plain_result(f"查询失败：{e}")
            return
        except Exception as e:
            yield event.plain_result(f"查询失败：{e}")
            logger.error(f"[WarThunderStats] 查询异常 ({nickname}): {e}", exc_info=True)
            return

        # 默认输出图片；如果图片渲染器不可用则回退到文本
        if self.renderer is not None:
            try:
                render_data = self._build_render_data(data)
                file_name = f"wt_stats_{nickname}_{datetime.now(BEIJING_TZ).strftime('%Y%m%d%H%M%S')}.png"
                output_path = self.data_dir / file_name
                await self.renderer.render(render_data, str(output_path))
                yield event.image_result(str(output_path))
                return
            except Exception as e:
                logger.warning(f"[WarThunderStats] 图片生成失败，回退到文本: {e}")

        try:
            text = self._format_stats(data, query_mode)
            if len(text) > 3500:
                text = text[:3400] + "\n\n（内容过长，已截断）"
            yield event.plain_result(text)
        except Exception as e:
            yield event.plain_result(f"数据格式化失败：{e}")
            logger.error(f"[WarThunderStats] 格式化失败 ({nickname}): {e}", exc_info=True)

    @filter.command("战雷战绩文本", alias={"wt战绩文本", "WT战绩文本", "战争雷霆战绩文本", "战绩文本"})
    async def warthunder_stats_text(self, event: AstrMessageEvent, nickname: str = "", mode: str = ""):
        """查询战争雷霆玩家战绩并以文本形式返回。用法：/战雷战绩文本 <昵称> [模式]"""
        event.stop_event()

        nickname, mode = self._extract_nick_and_mode(event, nickname, mode)

        if not nickname:
            yield event.plain_result(
                "用法：/战雷战绩文本 <玩家昵称> [模式]\n"
                "示例：/战雷战绩文本 HOSO6\n"
                "模式可选：街机 / 历史 / 全真"
            )
            return

        query_mode = self._resolve_mode(mode)

        try:
            data = await asyncio.wait_for(
                self._fetch_player_data(nickname),
                timeout=self.api_timeout_seconds * 4 + 30,
            )
        except asyncio.TimeoutError:
            yield event.plain_result("查询超时，请稍后再试。")
            return
        except WarThunderAPIError as e:
            yield event.plain_result(f"查询失败：{e}")
            return
        except Exception as e:
            yield event.plain_result(f"查询失败：{e}")
            logger.error(f"[WarThunderStats] 文本查询异常 ({nickname}): {e}", exc_info=True)
            return

        try:
            text = self._format_stats(data, query_mode)
            if len(text) > 3500:
                text = text[:3400] + "\n\n（内容过长，已截断）"
            yield event.plain_result(text)
        except Exception as e:
            yield event.plain_result(f"数据格式化失败：{e}")
            logger.error(f"[WarThunderStats] 文本格式化失败 ({nickname}): {e}", exc_info=True)

    # ────────────── 指令：联队查询 ──────────────

    @filter.command("战雷联队", alias={"wt联队", "WT联队", "战雷战队", "战雷公会", "联队查询"})
    async def warthunder_clan(self, event: AstrMessageEvent, clan_name: str = ""):
        """查询战争雷霆联队信息。用法：/战雷联队 <联队名称>"""
        event.stop_event()

        if not clan_name:
            parts = event.message_str.strip().split()
            if len(parts) >= 2:
                clan_name = " ".join(parts[1:])

        if not clan_name:
            yield event.plain_result(
                "用法：/战雷联队 <联队名称>\n"
                "示例：/战雷联队 Long live the People\n"
                "提示：联队名称请用英文全名"
            )
            return

        try:
            data = await asyncio.wait_for(
                self.data_service.get_clan_info(clan_name),
                timeout=self.api_timeout_seconds + 5,
            )
        except asyncio.TimeoutError:
            yield event.plain_result(f"查询超时，请稍后再试。")
            return
        except WarThunderAPIError as e:
            yield event.plain_result(f"查询失败：{e}")
            return
        except Exception as e:
            yield event.plain_result(f"查询失败：{e}")
            logger.error(f"[WarThunderStats] 联队查询异常 ({clan_name}): {e}", exc_info=True)
            return

        try:
            text = self._format_clan_info(data)
            if len(text) > 3500:
                text = text[:3400] + "\n\n（内容过长，已截断）"
            yield event.plain_result(text)
        except Exception as e:
            yield event.plain_result(f"数据格式化失败：{e}")

    async def terminate(self):
        if self.app_client is not None:
            try:
                self.app_client.client.close()
            except Exception as e:
                logger.debug(f"[WarThunderStats] 关闭 App HTTP 客户端失败: {e}")
        logger.info("[WarThunderStats] 插件已卸载")
