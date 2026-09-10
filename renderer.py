from __future__ import annotations

import io
import math
import os
import platform
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except Exception:  # App 数据源渲染图片时不强依赖 aiohttp，下面会回退 urllib
    aiohttp = None
from PIL import Image, ImageDraw, ImageFont

CANVAS_WIDTH = 740
PAGE_BG = '#efefe6'
PANEL_BG = '#e6e6db'
BORDER_GREEN = '#11841c'
TEXT_BLACK = '#111111'
TEXT_GRAY = '#7a7a7a'
TEXT_GREEN = '#4e7758'
TEXT_ORANGE = '#d58a1f'
TEXT_RED = '#d35a57'
TEXT_PURPLE = '#8f1aa8'
TEXT_YELLOW = '#c8a11e'
TEXT_BLUE = '#2b46d3'

SECTION_GAP = 10
SIDE_PAD = 10

FONT_CANDIDATE_FILES = [
    'fonts/NotoSansCJKsc-Regular.otf',
    'fonts/NotoSansSC-Regular.otf',
    'fonts/msyh.ttc',
    'fonts/msyh.ttf',
    'fonts/simhei.ttf',
]
FONT_PRIORITY_LIST = [
    'Noto Sans CJK SC',
    'Noto Sans CJK TC',
    'Noto Sans CJK JP',
    'Microsoft YaHei',
    'SimHei',
    'WenQuanYi Micro Hei',
    'PingFang SC',
    'Source Han Sans SC',
    'Source Han Sans CN',
]


@dataclass
class _FontSet:
    tiny: ImageFont.FreeTypeFont
    small: ImageFont.FreeTypeFont
    normal: ImageFont.FreeTypeFont
    medium: ImageFont.FreeTypeFont
    large: ImageFont.FreeTypeFont
    title: ImageFont.FreeTypeFont
    header: ImageFont.FreeTypeFont


class WarThunderStatsRenderer:
    def __init__(self, plugin_data_dir: Optional[str] = None):
        self.plugin_data_dir = Path(plugin_data_dir) if plugin_data_dir else Path.home() / '.astrbot' / 'wt_stats'
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.module_dir = Path(__file__).resolve().parent
        self.font_path: Optional[str] = None
        self.fonts: Optional[_FontSet] = None

    async def _ensure_font(self) -> str:
        if self.font_path and os.path.exists(self.font_path):
            return self.font_path

        local_dirs = [
            self.module_dir,
            self.plugin_data_dir,
            self.plugin_data_dir / 'fonts',
        ]
        for base in local_dirs:
            for rel in FONT_CANDIDATE_FILES:
                p = (base / rel) if not str(base).endswith('fonts') else (base / Path(rel).name)
                if p.exists():
                    self.font_path = str(p)
                    return self.font_path

        for name in FONT_PRIORITY_LIST:
            path = self._find_system_font(name)
            if path:
                self.font_path = path
                return path

        common_paths = [
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]
        for p in common_paths:
            if os.path.exists(p):
                self.font_path = p
                return p

        downloaded = await self._download_noto_font()
        if downloaded:
            self.font_path = downloaded
            return downloaded

        self.font_path = None
        return ''

    def _find_system_font(self, font_name: str) -> Optional[str]:
        system = platform.system()
        candidates: List[Path] = []
        if system == 'Windows':
            font_dir = Path(os.environ.get('WINDIR', 'C:\\Windows')) / 'Fonts'
            candidates = [
                font_dir / f'{font_name}.ttf',
                font_dir / f'{font_name}.ttc',
                font_dir / f'{font_name}.otf',
            ]
            aliases = {
                'Microsoft YaHei': ['msyh.ttc', 'msyh.ttf'],
                'SimHei': ['simhei.ttf'],
                'PingFang SC': ['PingFang Regular.ttf', 'PingFang.ttc'],
            }
            for alias in aliases.get(font_name, []):
                candidates.append(font_dir / alias)
        elif system == 'Darwin':
            for d in [
                Path.home() / 'Library' / 'Fonts',
                Path('/Library/Fonts'),
                Path('/System/Library/Fonts'),
                Path('/System/Library/Fonts/Supplemental'),
            ]:
                candidates.extend([d / f'{font_name}.ttf', d / f'{font_name}.ttc', d / f'{font_name}.otf'])
        else:
            for d in [
                Path.home() / '.fonts',
                Path.home() / '.local' / 'share' / 'fonts',
                Path('/usr/share/fonts'),
                Path('/usr/local/share/fonts'),
            ]:
                candidates.extend([d / f'{font_name}.ttf', d / f'{font_name}.ttc', d / f'{font_name}.otf'])
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    async def _download_noto_font(self) -> Optional[str]:
        save_path = self.plugin_data_dir / 'fonts' / 'NotoSansCJKsc-Regular.otf'
        save_path.parent.mkdir(parents=True, exist_ok=True)
        if save_path.exists():
            return str(save_path)

        urls = [
            'https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf',
            'https://fastly.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf',
        ]
        for try_url in urls:
            try:
                if aiohttp is not None:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                        async with session.get(try_url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                if len(data) > 1024:
                                    save_path.write_bytes(data)
                                    return str(save_path)
                else:
                    def _sync_fetch() -> bytes:
                        req = urllib.request.Request(try_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            return resp.read()
                    import asyncio
                    data = await asyncio.to_thread(_sync_fetch)
                    if len(data) > 1024:
                        save_path.write_bytes(data)
                        return str(save_path)
            except Exception:
                continue
        return None

    def _load_fonts(self, font_path: str) -> _FontSet:
        load_path = font_path if font_path and os.path.exists(font_path) else None

        def _load(size: int):
            try:
                if load_path:
                    return ImageFont.truetype(load_path, size)
                return ImageFont.load_default()
            except Exception:
                return ImageFont.load_default()

        return _FontSet(
            tiny=_load(11),
            small=_load(14),
            normal=_load(16),
            medium=_load(20),
            large=_load(24),
            title=_load(31),
            header=_load(18),
        )

    async def _get_fonts(self) -> _FontSet:
        if self.fonts is not None:
            return self.fonts
        self.fonts = self._load_fonts(await self._ensure_font())
        return self.fonts

    @staticmethod
    def _has_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            t = value.strip()
            if not t or t.upper() in ('N/A', 'NA', '--', '-', '—', 'NULL', 'NONE', '[无数据]'):
                return False
        return True

    @staticmethod
    def _fmt_number(value: Any) -> Optional[str]:
        if not WarThunderStatsRenderer._has_value(value):
            return None
        try:
            return f'{int(float(value)):,}'
        except Exception:
            return str(value)

    @staticmethod
    def _fmt_percent(value: Any) -> Optional[str]:
        if not WarThunderStatsRenderer._has_value(value):
            return None
        try:
            v = float(value)
            if v <= 1:
                v *= 100
            return f'{v:.2f}%'
        except Exception:
            return str(value)

    @staticmethod
    def _fmt_time(value: Any) -> Optional[str]:
        if not WarThunderStatsRenderer._has_value(value):
            return None
        if isinstance(value, (int, float)):
            seconds = int(value)
        else:
            text = str(value)
            total = 0
            for pattern, multiplier in [
                (r'(\d+)\s*d', 86400),
                (r'(\d+)\s*h', 3600),
                (r'(\d+)\s*m', 60),
                (r'(\d+)\s*s', 1),
            ]:
                m = re.search(pattern, text)
                if m:
                    total += int(m.group(1)) * multiplier
            seconds = total
        if seconds <= 0:
            return None
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        if days > 0:
            return f'{days} 天 {hours} 时'
        if hours > 0:
            return f'{hours} 时 {minutes} 分'
        return f'{minutes} 分'

    @staticmethod
    def _text_color(color: str) -> str:
        mapping = {
            'gray': TEXT_GRAY,
            'green': TEXT_GREEN,
            'orange': TEXT_ORANGE,
            'red': TEXT_RED,
            'purple': TEXT_PURPLE,
            'yellow': TEXT_YELLOW,
            'blue': TEXT_BLUE,
            'black': TEXT_BLACK,
        }
        return mapping.get(color, color if color.startswith('#') else TEXT_BLACK)

    def _text_size(self, draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def _line_height(self, draw: ImageDraw.ImageDraw, font) -> int:
        return self._text_size(draw, '中文Ag', font)[1] + 4

    def _draw_bold_text(self, draw: ImageDraw.ImageDraw, xy: Tuple[int, int], text: str, font, fill: str, strength: int = 1):
        """使用轻微偏移重复绘制实现伪加粗，避免依赖额外 Bold 字体文件。"""
        x, y = xy
        offsets = [(0, 0)]
        if strength >= 1:
            offsets.extend([(1, 0), (0, 1)])
        if strength >= 2:
            offsets.extend([(1, 1), (2, 0), (0, 2)])
        for dx, dy in offsets:
            draw.text((x + dx, y + dy), text, font=font, fill=fill)

    def _wrap_text(self, draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
        if not text:
            return []
        lines: List[str] = []
        cur = ''
        for ch in str(text):
            nxt = cur + ch
            w, _ = self._text_size(draw, nxt, font)
            if cur and w > max_width:
                lines.append(cur)
                cur = ch
            else:
                cur = nxt
        if cur:
            lines.append(cur)
        return lines or [str(text)]

    async def _load_avatar(self, avatar_url: str) -> Optional[Image.Image]:
        img = await self._load_remote_image(avatar_url)
        return img.convert('RGB') if img is not None else None

    async def _load_first_remote_image(self, urls: List[str]) -> Optional[Image.Image]:
        seen = set()
        for url in urls:
            if not self._has_value(url) or url in seen:
                continue
            seen.add(url)
            img = await self._load_remote_image(url)
            if img is not None:
                return img
        return None

    async def _load_remote_image(self, url: str) -> Optional[Image.Image]:
        if not self._has_value(url):
            return None
        url = str(url)
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'image/avif,image/webp,image/apng,image/png,image/*,*/*;q=0.8',
            'Referer': 'https://warthunder.com/',
        }
        # 优先 aiohttp；失败时回退 urllib，避免某些运行环境 aiohttp/SSL/代理问题导致头像不显示。
        if aiohttp is not None:
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data:
                                return Image.open(io.BytesIO(data)).convert('RGBA')
            except Exception:
                pass
        try:
            def _sync_fetch_httpx() -> bytes:
                import httpx
                with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    return resp.content
            import asyncio
            data = await asyncio.to_thread(_sync_fetch_httpx)
            if data:
                return Image.open(io.BytesIO(data)).convert('RGBA')
        except Exception:
            pass
        try:
            def _sync_fetch() -> bytes:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.read()
            import asyncio
            data = await asyncio.to_thread(_sync_fetch)
            if data:
                return Image.open(io.BytesIO(data)).convert('RGBA')
        except Exception:
            return None
        return None

    def _draw_page_border(self, draw: ImageDraw.ImageDraw, height: int):
        draw.rounded_rectangle((5, 5, CANVAS_WIDTH - 5, height - 5), radius=6, outline=BORDER_GREEN, width=4)

    def _draw_header(self, draw: ImageDraw.ImageDraw, fonts: _FontSet, data: Dict[str, Any], y: int) -> int:
        nickname = data.get('nickname') or data.get('profile', {}).get('nickname') or '未知玩家'
        self._draw_bold_text(draw, (10, y), f'玩家 {nickname} 的战绩总览', font=fonts.title, fill=TEXT_BLACK, strength=1)
        y += self._line_height(draw, fonts.title)

        disclaimer = '一切玩家数据均归 Gaijin 所有，本图片仅基于公开数据进行了计算和展示，仅供参考！'
        self._draw_bold_text(draw, (10, y), disclaimer, font=fonts.small, fill=TEXT_GRAY, strength=1)
        y += self._line_height(draw, fonts.small)

        snapshot_time = data.get('snapshot_time') or ''
        if snapshot_time:
            self._draw_bold_text(draw, (10, y), f'当前数据快照同步于 {snapshot_time}', font=fonts.medium, fill=TEXT_BLACK, strength=1)
            y += self._line_height(draw, fonts.medium)
        return y + 2

    def _draw_profile_block(
        self,
        draw: ImageDraw.ImageDraw,
        image: Optional[Image.Image],
        fonts: _FontSet,
        data: Dict[str, Any],
        y: int,
        avatar_img: Optional[Image.Image],
        frame_img: Optional[Image.Image] = None,
        measure_only: bool = False,
    ) -> int:
        profile = data.get('profile', {}) or {}
        top = y
        info_lines = data.get('profile_lines', []) or []
        line_height = self._line_height(draw, fonts.medium)
        block_h = max(154, 18 + max(5, len(info_lines)) * line_height + 24)
        draw.rounded_rectangle((10, top, CANVAS_WIDTH - 10, top + block_h), radius=3, fill=PANEL_BG)

        avatar_x, avatar_y, avatar_size = 24, top + 10, 100
        if frame_img is None:
            colors = ['#7e49ff', '#43e84a', '#f6e700', '#ff3c00']
            for idx, color in enumerate(colors):
                inset = idx * 2
                draw.rounded_rectangle(
                    (avatar_x - 4 + inset, avatar_y - 4 + inset, avatar_x + avatar_size + 4 - inset, avatar_y + avatar_size + 4 - inset),
                    radius=8,
                    outline=color,
                    width=2,
                )

        if (not measure_only) and image is not None:
            if avatar_img is not None:
                av = avatar_img.copy()
                av.thumbnail((avatar_size, avatar_size))
                bg = Image.new('RGB', (avatar_size, avatar_size), '#d8d5c9')
                px = (avatar_size - av.width) // 2
                py = (avatar_size - av.height) // 2
                bg.paste(av, (px, py))
                image.paste(bg, (avatar_x, avatar_y))
            else:
                draw.rectangle((avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size), fill='#d8d5c9', outline='#7d7866', width=1)
                init = (profile.get('nickname') or data.get('nickname') or '?')[:1]
                tw, th = self._text_size(draw, init, fonts.header)
                draw.text((avatar_x + (avatar_size - tw) // 2, avatar_y + (avatar_size - th) // 2), init, font=fonts.header, fill=TEXT_GRAY)
            draw.rectangle((avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size), outline='#7d7866', width=1)
            if frame_img is not None:
                frame_size = avatar_size + 28
                fr = frame_img.copy()
                fr.thumbnail((frame_size, frame_size))
                fx = avatar_x - (fr.width - avatar_size) // 2
                fy = avatar_y - (fr.height - avatar_size) // 2
                image.paste(fr, (fx, fy), fr)
        else:
            draw.rectangle((avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size), fill='#d8d5c9', outline='#7d7866', width=1)
            init = (profile.get('nickname') or data.get('nickname') or '?')[:1]
            tw, th = self._text_size(draw, init, fonts.header)
            draw.text((avatar_x + (avatar_size - tw) // 2, avatar_y + (avatar_size - th) // 2), init, font=fonts.header, fill=TEXT_GRAY)

        text_x = avatar_x + avatar_size + 20
        line_y = top + 6
        for item in info_lines:
            if not item:
                continue
            label = item.get('label', '')
            value = item.get('value', '')
            value_color = self._text_color(item.get('color', 'black'))
            label_text = f'{label}：'
            draw.text((text_x, line_y), label_text, font=fonts.medium, fill=TEXT_BLACK)
            lw, _ = self._text_size(draw, label_text, fonts.medium)
            draw.text((text_x + lw, line_y), str(value), font=fonts.medium, fill=value_color)
            line_y += line_height

        bar_y = top + block_h - 18
        bars = ['#000000', '#d63e39', '#d98b00', '#9a9100', '#628f36', '#587b5d', '#132ad8', '#8e0ea5']
        bar_x = 12
        for color in bars:
            draw.rectangle((bar_x, bar_y, bar_x + 40, bar_y + 6), fill=color)
            bar_x += 46
        return top + block_h + SECTION_GAP

    def _draw_medals(self, draw: ImageDraw.ImageDraw, fonts: _FontSet, data: Dict[str, Any], y: int) -> int:
        # 勋章墙需要完整图标资源映射；当前数据只能拿到不可读的枚举/数量。
        # 按用户反馈彻底隐藏该区块，避免出现一排数字圆点。
        return y

    def _draw_nations(self, draw: ImageDraw.ImageDraw, fonts: _FontSet, data: Dict[str, Any], y: int) -> int:
        nations = data.get('nations', []) or []
        if not nations:
            return y

        self._draw_bold_text(draw, (10, y), '各系载具', font=fonts.header, fill=TEXT_BLACK, strength=1)
        y += self._line_height(draw, fonts.header)

        name_x, owned_x, elite_x, rate_x = 10, 90, 245, 410
        total_owned = 0
        total_elite = 0
        row_height = self._line_height(draw, fonts.medium) - 1

        for row in nations:
            label = row.get('label')
            owned = row.get('owned')
            elite = row.get('elite')
            rate = row.get('rate')
            if label is None:
                continue

            owned_val = int(owned or 0)
            elite_val = int(elite or 0)
            total_owned += owned_val
            total_elite += elite_val

            draw.text((name_x, y), str(label), font=fonts.header, fill=TEXT_BLACK)
            draw.text((owned_x, y), f'拥有：{owned_val}', font=fonts.medium, fill=TEXT_BLACK)
            draw.text((elite_x, y), f'精英：{elite_val}', font=fonts.medium, fill=TEXT_BLACK)

            rate_value = float(rate or 0.0)
            rate_color = TEXT_GREEN if rate_value > 0 else TEXT_BLACK
            draw.text((rate_x, y), f'精英率：{rate_value:.2f}%', font=fonts.medium, fill=rate_color)
            y += row_height

        draw.line((10, y + 4, 476, y + 4), fill='#8d8d8d', width=3)
        y += 16
        total_rate = (total_elite / total_owned * 100.0) if total_owned > 0 else 0.0
        draw.text((10, y), '总计', font=fonts.header, fill=TEXT_BLACK)
        draw.text((owned_x, y), f'拥有：{total_owned}', font=fonts.medium, fill=TEXT_BLACK)
        draw.text((elite_x, y), f'精英：{total_elite}', font=fonts.medium, fill=TEXT_BLACK)
        draw.text((rate_x, y), f'精英率：{total_rate:.2f}%', font=fonts.medium, fill=TEXT_GREEN if total_rate > 0 else TEXT_BLACK)
        return y + self._line_height(draw, fonts.medium) + SECTION_GAP

    def _draw_leaderboards(self, draw: ImageDraw.ImageDraw, fonts: _FontSet, data: Dict[str, Any], y: int) -> int:
        boards = data.get('leaderboards', []) or []
        if not boards:
            return y

        self._draw_bold_text(draw, (10, y), '排行榜', font=fonts.header, fill=TEXT_BLACK, strength=1)
        y += self._line_height(draw, fonts.header)
        draw.text((10, y), '括号内的数字为排行榜排名', font=fonts.small, fill=TEXT_GRAY)
        y += self._line_height(draw, fonts.small)

        for block in boards:
            title = block.get('title')
            metrics = block.get('metrics', []) or []
            if not title or not metrics:
                continue
            self._draw_bold_text(draw, (10, y), str(title), font=fonts.header, fill=TEXT_BLACK, strength=1)
            y += self._line_height(draw, fonts.header) - 2

            half = math.ceil(len(metrics) / 2)
            left_metrics = metrics[:half]
            right_metrics = metrics[half:]
            lx = 10
            rx = 370
            row_h = self._line_height(draw, fonts.medium) - 1
            for idx in range(max(len(left_metrics), len(right_metrics))):
                if idx < len(left_metrics):
                    item = left_metrics[idx]
                    self._draw_metric_line(draw, fonts, lx, y + idx * row_h, item)
                if idx < len(right_metrics):
                    item = right_metrics[idx]
                    self._draw_metric_line(draw, fonts, rx, y + idx * row_h, item)
            y += max(len(left_metrics), len(right_metrics)) * row_h + 4
        return y + 2

    def _draw_metric_line(self, draw: ImageDraw.ImageDraw, fonts: _FontSet, x: int, y: int, item: Dict[str, Any]):
        label = str(item.get('label', ''))
        value = str(item.get('value', ''))
        extra = item.get('extra')
        color = self._text_color(item.get('color', 'black'))
        draw.text((x, y), f'{label}：', font=fonts.medium, fill=TEXT_BLACK)
        lw, _ = self._text_size(draw, f'{label}：', fonts.medium)
        draw.text((x + lw, y), value, font=fonts.medium, fill=color)
        if self._has_value(extra):
            vw, _ = self._text_size(draw, value, fonts.medium)
            draw.text((x + lw + vw + 10, y), str(extra), font=fonts.medium, fill=color)

    def _draw_detail_columns(self, draw: ImageDraw.ImageDraw, fonts: _FontSet, data: Dict[str, Any], y: int) -> int:
        columns = data.get('detail_columns', []) or []
        columns = [c for c in columns if (c.get('rows') or [])]
        if not columns:
            return y

        self._draw_bold_text(draw, (10, y), '战绩详情', font=fonts.header, fill=TEXT_BLACK, strength=1)
        y += self._line_height(draw, fonts.header)

        start_y = y
        xs = [10, 250, 490]
        max_bottom = y

        for idx, col in enumerate(columns[:3]):
            x = xs[idx]
            cur_y = start_y
            self._draw_bold_text(draw, (x, cur_y), str(col.get('title', '')), font=fonts.header, fill=TEXT_BLACK, strength=1)
            cur_y += self._line_height(draw, fonts.header) + 2
            for row in col.get('rows', []):
                if row.get('type') == 'gap':
                    cur_y += 12
                    continue
                label = row.get('label')
                value = row.get('value')
                if not self._has_value(value):
                    continue
                line = f'{label}： {value}'
                draw.text((x, cur_y), line, font=fonts.medium, fill=self._text_color(row.get('color', 'black')))
                cur_y += self._line_height(draw, fonts.medium) - 1
            max_bottom = max(max_bottom, cur_y)
        return max_bottom + SECTION_GAP

    def _draw_footer(self, draw: ImageDraw.ImageDraw, fonts: _FontSet, data: Dict[str, Any], y: int) -> int:
        source = data.get('source', 'official')
        if source == 'app':
            lines = [
                'AstrBot 为您生成',
                '• 本图片由插件绘制，数据来源于 Gaijin 官方 War Thunder 助手 App 接口',
                '• 本图绘制时使用的头像、称号、载具文本等版权均归属于 Gaijin 官方',
                '• 图片展示的数据可能存在缓存或同步延迟，请以游戏内显示为准',
            ]
        else:
            lines = [
                'AstrBot 为您生成',
                '• 本图片由插件绘制，数据来源于 War Thunder 官方公开页面',
                '• 图片展示的数据可能存在缓存或同步延迟，请以游戏内显示为准',
            ]
        for idx, line in enumerate(lines):
            font = fonts.header if idx == 0 else fonts.medium
            if idx == 0:
                self._draw_bold_text(draw, (10, y), line, font=font, fill=TEXT_BLACK, strength=1)
            else:
                draw.text((10, y), line, font=font, fill=TEXT_BLACK)
            y += self._line_height(draw, font)
        return y + SECTION_GAP

    async def _build_image(self, data: Dict[str, Any], mode: str = 'arcade') -> Image.Image:
        fonts = await self._get_fonts()
        profile = data.get('profile', {}) or {}

        avatar_urls = []
        avatar_icon = profile.get('avatar_icon')
        # App 返回的 avatar_icon 是最稳定的头像资源 key，优先用官方 public-configs HTTPS PNG。
        if self._has_value(avatar_icon):
            for ext in ('png', 'jpg', 'jpeg', 'webp', 'avif'):
                avatar_urls.extend([
                    f'https://public-configs.gaijin.net/gaijinPass/avatars/{avatar_icon}.{ext}',
                    f'http://public-configs.gaijin.net/gaijinPass/avatars/{avatar_icon}.{ext}',
                ])
        for raw_url in (profile.get('avatar_url', ''), data.get('avatar_url', '')):
            if not self._has_value(raw_url):
                continue
            url = str(raw_url)
            avatar_urls.append(url)
            # Pillow 环境经常不支持 AVIF；同源头像通常存在 PNG，自动追加 PNG 兜底。
            if url.lower().endswith('.avif'):
                avatar_urls.append(url[:-5] + '.png')
                avatar_urls.append(url[:-5] + '.jpg')
            if '/gaijinPass/avatars/' in url and not url.lower().endswith('.png'):
                avatar_urls.append(re.sub(r'\.[a-zA-Z0-9]+($|\?)', r'.png\1', url))
        avatar_img_rgba = await self._load_first_remote_image(avatar_urls)
        avatar_img = avatar_img_rgba.convert('RGB') if avatar_img_rgba is not None else None

        frame_urls = []
        if self._has_value(profile.get('frame_url', '')):
            frame_urls.append(str(profile.get('frame_url', '')))
        frame_key = profile.get('frame_key')
        if self._has_value(frame_key):
            frame_urls.extend([
                f'https://avatars.warthunder.com/frame/{frame_key}.png',
                f'http://avatars.warthunder.com/frame/{frame_key}.png',
            ])
        frame_img = await self._load_first_remote_image(frame_urls)

        temp = Image.new('RGB', (CANVAS_WIDTH, 5000), PAGE_BG)
        temp_draw = ImageDraw.Draw(temp)
        y = 12
        y = self._draw_header(temp_draw, fonts, data, y)
        y = self._draw_profile_block(temp_draw, None, fonts, data, y, avatar_img, frame_img, measure_only=True)
        y = self._draw_medals(temp_draw, fonts, data, y)
        y = self._draw_nations(temp_draw, fonts, data, y)
        y = self._draw_leaderboards(temp_draw, fonts, data, y)
        y = self._draw_detail_columns(temp_draw, fonts, data, y)
        y = self._draw_footer(temp_draw, fonts, data, y)
        height = y + 12

        img = Image.new('RGB', (CANVAS_WIDTH, height), PAGE_BG)
        draw = ImageDraw.Draw(img)
        y = 12
        y = self._draw_header(draw, fonts, data, y)
        y = self._draw_profile_block(draw, img, fonts, data, y, avatar_img, frame_img, measure_only=False)
        y = self._draw_medals(draw, fonts, data, y)
        y = self._draw_nations(draw, fonts, data, y)
        y = self._draw_leaderboards(draw, fonts, data, y)
        y = self._draw_detail_columns(draw, fonts, data, y)
        y = self._draw_footer(draw, fonts, data, y)
        self._draw_page_border(draw, height)
        return img

    async def render(self, data: Dict[str, Any], output_path: str, mode: str = 'arcade') -> str:
        img = await self._build_image(data, mode)
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or '.', exist_ok=True)
        img.save(output_path, 'PNG')
        return output_path

    async def render_to_bytes(self, data: Dict[str, Any], mode: str = 'arcade') -> bytes:
        img = await self._build_image(data, mode)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
