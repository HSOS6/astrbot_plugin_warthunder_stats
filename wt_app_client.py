from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

try:
    import blackboxprotobuf
except Exception:  # pragma: no cover
    blackboxprotobuf = None


BEIJING_TZ = timezone(timedelta(hours=8))

APP_USER_AGENT = "wtr-assistant/235/1.10.3 (Android 10; MIX 2S)"
AUTH_URL = "https://auth.gaijinent.com/login.php"
CALL_URL = "https://companion-app.warthunder.com/call_signed/"

MODE_NAMES = {0: "街机", 1: "历史", 2: "全真"}
MODE_RENDER_TITLES = {0: "PVP街机数据", 1: "PVP历史数据", 2: "PVP拟真数据"}

NATION_LABELS = [
    "美系", "德系", "苏系", "英系", "日系", "中系", "意系", "法系", "瑞系", "以系"
]

PVP_FIELDS = {
    "1": "胜场",
    "2": "重坦时长",
    "3": "坦歼时长",
    "4": "坦克时长",
    "5": "防空时长",
    "6": "战斗机时长",
    "7": "轰炸机时长",
    "8": "攻击机时长",
    "9": "地面击杀",
    "10": "空中击杀",
    "11": "会话数",
    "12": "完成战斗数",
    "14": "鱼雷艇时长",
    "18": "驱逐舰时长",
    "20": "海上击杀",
}

LB_FIELDS = {
    "1": "胜利场次",
    "2": "各玩家胜利",
    "3": "地面击杀",
    "4": "空中击杀",
    "5": "出击次数",
    "6": "PVP时长",
    "7": "PVP比率",
    "8": "战争点数",
    "9": "在线经验",
    "10": "死亡数",
    "11": "各玩家会话",
    "12": "平均相对位置",
    "13": "平均主动击杀",
    "14": "平均脚本击杀",
    "15": "平均得分",
    "16": "海上击杀",
}

VEHICLE_FIELDS = {
    "2": "胜场",
    "3": "战斗数",
    "4": "胜率",
    "5": "死亡",
    "6": "出击",
    "7": "空中击杀",
    "8": "地面击杀",
    "9": "在线经验",
    "10": "战争点数",
    "12": "海上击杀",
}


class WarThunderAppError(RuntimeError):
    """战雷助手 App 接口查询错误。"""


def _as_double(value: Any) -> Any:
    if not isinstance(value, int):
        return value
    if value == 0:
        return 0.0
    try:
        return struct.unpack(">d", value.to_bytes(8, "big", signed=False))[0]
    except Exception:
        return value


def _fmt_num(value: Any) -> str:
    if value is None or value == "":
        return "[无数据]"
    try:
        return f"{int(float(value)):,}"
    except Exception:
        return str(value)


def _fmt_rank(value: Any) -> str:
    if value in (None, "", -1, -2):
        return "[无数据]"
    return f"{int(value):,}" if isinstance(value, (int, float)) else str(value)


def _fmt_score(value: Any) -> str:
    if value in (None, "", -1, -2):
        return "[无数据]"
    if isinstance(value, float):
        if abs(value) <= 1:
            return f"{value * 100:.2f}%"
        if abs(value) < 100:
            return f"{value:.2f}"
        return f"{value:,.0f}"
    return _fmt_num(value)


def _fmt_time(seconds: Any) -> str:
    try:
        sec = int(seconds)
    except Exception:
        return "[无数据]"
    if sec <= 0:
        return "[无数据]"
    days = sec // 86400
    hours = (sec % 86400) // 3600
    minutes = (sec % 3600) // 60
    if days > 0:
        text = f"{days} 天 {hours} 时"
        if minutes > 0:
            text += f" {minutes} 分"
        return text
    if hours > 0:
        return f"{hours} 时 {minutes} 分"
    return f"{minutes} 分"


def _ratio(kills: Any, deaths: Any) -> float:
    try:
        d = int(deaths)
        if d <= 0:
            return 0.0
        return round(int(kills or 0) / d, 2)
    except Exception:
        return 0.0


def _strip_game_spaces(text: str) -> str:
    return str(text or "").replace("\u2007", "").replace("\u200b", "").replace("\ufeff", "").strip()


def _first_scalar(value: Any) -> Any:
    """部分 protobuf 64 位字段会被解码成 dict，取其中第一个标量兜底。"""
    if isinstance(value, dict):
        for k in ("1", "6", "2", "3"):
            if k in value and not isinstance(value[k], (dict, list)):
                return value[k]
    return value


def _as_mapping(value: Any) -> Dict[str, Any]:
    """将 protobuf 不稳定的字段安全转换为映射，避免 list 导致渲染失败。"""
    if isinstance(value, dict):
        return value
    return {}


def _as_text(value: Any) -> str:
    """提取 protobuf 标量字段，避免头像 key 被拼成 list/dict。"""
    value = _first_scalar(value)
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip() if value is not None else ""


class WarThunderLocalization:
    def __init__(self, module_dir: Path):
        self.module_dir = module_dir
        self.title_map: Dict[str, Dict[str, str]] = {}
        self.unit_map: Dict[str, str] = {}
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        self._loaded = True
        self._load_titles()
        self._load_units()

    def _load_titles(self):
        json_path = self.module_dir / "_title_map.json"
        if json_path.exists():
            try:
                self.title_map = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                self.title_map = {}

        csv_path = self.module_dir / "_titles.csv"
        if not csv_path.exists():
            return
        try:
            rows = list(csv.reader(csv_path.open(encoding="utf-8"), delimiter=";", quotechar='"'))
            if not rows:
                return
            header = rows[0]
            zh_idx = next((i for i, h in enumerate(header) if h.strip("<>").lower() == "chinese"), 10)
            en_idx = next((i for i, h in enumerate(header) if h.strip("<>").lower() == "english"), 1)
            for row in rows[1:]:
                if len(row) <= max(zh_idx, en_idx):
                    continue
                rid = row[0]
                if not rid.startswith("title/"):
                    continue
                key = rid.split("/", 1)[1]
                self.title_map[key] = {"zh": _strip_game_spaces(row[zh_idx]), "en": row[en_idx].strip()}
        except Exception:
            self.title_map = {}

    def _load_units(self):
        csv_path = self.module_dir / "_units.csv"
        if not csv_path.exists():
            return
        try:
            rows = csv.reader(csv_path.open(encoding="utf-8"), delimiter=";", quotechar='"')
            header = next(rows, [])
            zh_idx = next((i for i, h in enumerate(header) if h.strip("<>").lower() == "chinese"), 10)
            for row in rows:
                if len(row) <= zh_idx:
                    continue
                key = row[0].strip('"')
                if not key:
                    continue
                self.unit_map[key] = _strip_game_spaces(row[zh_idx])
        except Exception:
            self.unit_map = {}

    def title(self, key: str) -> str:
        self.load()
        if not key:
            return ""
        raw_key = str(key).strip()
        candidates = [raw_key]
        for prefix in ("title/", "title:", "title_"):
            if raw_key.startswith(prefix):
                candidates.append(raw_key[len(prefix):])
        for candidate in candidates:
            item = self.title_map.get(candidate)
            if isinstance(item, dict) and item.get("zh"):
                return _strip_game_spaces(str(item["zh"]))

        # 某些接口版本返回英文称号文本而不是资源 key，反向查找本地语言表。
        normalized = _strip_game_spaces(raw_key).casefold()
        for item in self.title_map.values():
            if not isinstance(item, dict):
                continue
            if _strip_game_spaces(str(item.get("en", ""))).casefold() == normalized:
                return _strip_game_spaces(str(item.get("zh", ""))) or raw_key

        # JSON 映射是常用称号的快速表；CSV 由 _load_titles 合并后覆盖完整官方语言包。
        return raw_key

    def unit(self, vehicle_id: str) -> str:
        self.load()
        if not vehicle_id:
            return ""
        return (
            self.unit_map.get(f"{vehicle_id}_shop")
            or self.unit_map.get(vehicle_id)
            or vehicle_id
        )


class WarThunderAppClient:
    """War Thunder 战雷助手 App 私有接口客户端。

    用于替代官网 HTML 爬取：不受 Cloudflare 影响，可获取完整 profile、排行榜相对排名、
    装扮、载具明细等数据。需要用户在插件配置里填写 Gaijin 账号密码。
    """

    def __init__(
        self,
        login: str,
        password: str,
        timeout: int = 30,
        module_dir: Optional[Path] = None,
    ):
        self.login = login.strip()
        self.password = password
        self.timeout = timeout
        self.module_dir = module_dir or Path(__file__).resolve().parent
        self.loc = WarThunderLocalization(self.module_dir)

        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_pem = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        device_id = secrets.token_hex(8)
        self.factor = device_id + secrets.token_urlsafe(15)[:20]
        self.jwt = ""
        self.client = httpx.Client(
            timeout=httpx.Timeout(float(timeout)),
            headers={"User-Agent": APP_USER_AGENT},
            follow_redirects=True,
        )

    async def get_full_player_stats(self, nickname: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._get_full_player_stats_sync, nickname)

    def _get_full_player_stats_sync(self, nickname: str) -> Dict[str, Any]:
        if not self.login or not self.password:
            raise WarThunderAppError("未配置 Gaijin 账号密码，请在插件配置 app_login/app_password 中填写。")
        self._login()
        userid, found_nick = self._find_user(nickname)
        raw = self._get_profile(userid)
        raw["__queried_userid"] = userid
        raw["__queried_nick"] = found_nick or nickname
        return self._normalize_profile(raw)

    def _login(self):
        if self.jwt:
            return
        files = {
            "login": (None, self.login),
            "password": (None, self.password),
            "factor": (None, self.factor),
            "v": (None, "2"),
            "jwt_public_key": (None, self.public_pem),
            "lang": (None, "en"),
            "client": (None, self.factor),
        }
        resp = self.client.post(AUTH_URL, files=files)
        try:
            data = resp.json()
        except Exception as exc:
            raise WarThunderAppError(f"Gaijin 登录失败：HTTP {resp.status_code}") from exc
        jwt = data.get("jwt")
        if not jwt:
            msg = data.get("error") or data.get("message") or data
            raise WarThunderAppError(f"Gaijin 登录失败：{msg}")
        self.jwt = jwt

    def _bare_sign(self, message: bytes) -> bytes:
        digest = hashlib.sha256(message).digest()
        n = self.private_key.public_key().public_numbers().n
        d = self.private_key.private_numbers().d
        k = (n.bit_length() + 7) // 8
        em = b"\x00\x01" + (b"\xff" * (k - 3 - len(digest))) + b"\x00" + digest
        sig = pow(int.from_bytes(em, "big"), d, n)
        return sig.to_bytes(k, "big")

    def _make_sign(self, body: bytes) -> str:
        ts_le8 = int(time.time()).to_bytes(8, "little")
        hx = (b"\x01" + ts_le8 + self._bare_sign(body + ts_le8)).hex()
        return hx[1:] if hx[0] == "0" else hx

    def _call_signed(self, body_obj: Dict[str, Any], retry_auth: bool = True) -> bytes:
        body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        resp = self.client.post(
            CALL_URL,
            content=body,
            headers={
                "Authorization": "Bearer " + self.jwt,
                "request-sign": self._make_sign(body),
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        if resp.status_code in (401, 403) and retry_auth:
            self.jwt = ""
            self._login()
            return self._call_signed(body_obj, retry_auth=False)
        if resp.status_code != 200:
            raise WarThunderAppError(f"App 接口 HTTP {resp.status_code}: {resp.text[:160]}")
        return resp.content

    def _decode_pb(self, content: bytes) -> Dict[str, Any]:
        if blackboxprotobuf is None:
            raise WarThunderAppError("缺少依赖 blackboxprotobuf，请安装后重试。")
        msg, _typedef = blackboxprotobuf.decode_message(content)
        return self._decode_bytes(msg)

    def _decode_bytes(self, value: Any) -> Any:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except Exception:
                return value.hex()
        if isinstance(value, list):
            return [self._decode_bytes(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self._decode_bytes(v) for k, v in value.items()}
        return value

    def _find_user(self, nickname: str) -> Tuple[str, str]:
        content = self._call_signed(
            {
                "nick": nickname,
                "count": 10,
                "classname": "eaw_Contacts",
                "method": "jzx_findUsersByNickPrefix",
                "v": 8,
                "factor": self.factor,
            }
        )
        msg = self._decode_pb(content)
        candidates = msg.get("1", [])
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not candidates:
            raise WarThunderAppError("未找到该玩家，请检查昵称是否正确。")

        selected = None
        for item in candidates:
            nick = str(item.get("2", ""))
            if nick.lower() == nickname.lower():
                selected = item
                break
        selected = selected or candidates[0]
        userid = _first_scalar(selected.get("1"))
        if not userid:
            raise WarThunderAppError("已找到玩家但无法解析 userid。")
        return str(userid), str(selected.get("2") or nickname)

    def _get_profile(self, userid: str) -> Dict[str, Any]:
        content = self._call_signed(
            {
                "classname": "eaw_Profile",
                "method": "jzx_getPublic",
                "v": 8,
                "userid": str(userid),
                "lang": "en",
                "factor": self.factor,
            }
        )
        return self._decode_pb(content)

    def _normalize_profile(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        base = _as_mapping(obj.get("2"))
        level = _as_mapping(obj.get("3"))
        medals = obj.get("4", []) or []
        common = obj.get("5", []) or []
        vehicles = obj.get("6", []) or []
        top_leader = obj.get("7", {}) or {}
        decor = _as_mapping(obj.get("29"))

        user_id = _first_scalar(base.get("1")) or obj.get("__queried_userid")
        nick = base.get("2") or obj.get("__queried_nick") or "未知玩家"
        icon = _as_text(base.get("3"))
        title_key = _as_text(base.get("4"))
        clan_id = base.get("5")
        clan_tag = base.get("6") or ""

        # Pillow 默认环境经常不支持 AVIF，因此头像优先使用同源 PNG，避免图片里只显示字母占位。
        avatar_url = f"https://public-configs.gaijin.net/gaijinPass/avatars/{icon}.png" if icon else ""
        frame_key = _as_text(decor.get("2"))
        header_key = _as_text(decor.get("1"))

        profile = {
            "nickname": nick,
            "uid": user_id,
            "title": self.loc.title(str(title_key)),
            "title_key": title_key,
            "clan": clan_tag,
            "clan_tag": clan_tag,
            "clan_id": clan_id,
            "level": level.get("1"),
            "exp_current": level.get("2"),
            "exp_to_next": level.get("3"),
            "level_progress": _as_double(level.get("4")),
            "avatar_url": avatar_url,
            "avatar_icon": icon,
            "header_key": header_key,
            "header_url": f"https://avatars.warthunder.com/header/{header_key}.png" if header_key else "",
            "frame_key": frame_key,
            "frame_url": f"https://avatars.warthunder.com/frame/{frame_key}.png" if frame_key else "",
            "display_title_key": decor.get("3", ""),
            "display_title": self.loc.title(str(decor.get("3", ""))) if decor.get("3") else "",
        }

        return {
            "source": "app",
            "render_ready": True,
            "nickname": nick,
            "raw_profile": obj,
            "profile": profile,
            "avatar_url": avatar_url,
            "profile_lines": self._build_profile_lines(profile, common),
            # App 接口里的字段 4 实际用于各系载具统计，不是可直接展示的勋章图标；
            # 参考图的勋章墙需要独立图标资源映射，当前先移除，避免展示一排不可读数字圆点。
            "medals": [],
            "medal_count": 0,
            "nations": self._build_nations(medals),
            "leaderboards": self._build_leaderboards(common),
            "detail_columns": self._build_detail_columns(common),
            "vehicles": self._build_vehicles(vehicles),
            "snapshot_time": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _build_profile_lines(self, profile: Dict[str, Any], common: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        lines = []
        nick_value = profile.get("nickname") or "未知玩家"
        if profile.get("uid"):
            nick_value = f"{nick_value}（{profile.get('uid')}）"
        lines.append({"label": "玩家昵称", "value": nick_value})

        if profile.get("title"):
            lines.append({"label": "个人称号", "value": profile["title"]})

        clan = profile.get("clan_tag")
        clan_id = profile.get("clan_id")
        if clan:
            lines.append({"label": "联队标识", "value": f"{clan}（{clan_id}）" if clan_id else clan})

        # 注册时间：当前战雷助手 App 的 jzx_getPublic profile 实测不返回该字段。
        # 为避免伪造/猜测数据，这里保留明确占位；若后续配置 official/cf 源可再补精确日期。
        lines.append({"label": "注册时间", "value": profile.get("registration_date") or "[无数据]"})

        level = profile.get("level")
        exp_current = profile.get("exp_current")
        exp_to_next = profile.get("exp_to_next")
        if level is not None:
            level_text = f"{level} 级"
            # App 字段含义：当前等级经验 / 升下一级所需总经验，不是“剩余经验”。
            if exp_current is not None and exp_to_next is not None:
                remaining_exp = max(0, int(exp_to_next or 0) - int(exp_current or 0))
                level_text += f"（经验：{_fmt_num(exp_current)} / {_fmt_num(exp_to_next)}，还需 {_fmt_num(remaining_exp)}）"
            elif exp_to_next is not None:
                level_text += f"（下一级所需总经验：{_fmt_num(exp_to_next)}）"
            lines.append({"label": "当前等级", "value": level_text})

        # 赛博服役：所有游戏模式的总游玩时间。
        # App CommonStatistic 实测前 3 项分别对应 PVP 街机 / 历史 / 拟真；
        # 每项 PVP 字段只给出兵种时长，因此需要把三个模式的各兵种时长全部相加。
        service_seconds = self._sum_all_mode_time_seconds(common or [])
        if service_seconds > 0:
            lines.append({"label": "赛博服役", "value": _fmt_time(service_seconds)})

        display_title = profile.get("display_title")
        display_title_key = profile.get("display_title_key")
        if display_title and display_title != display_title_key:
            lines.append({"label": "展示称号", "value": display_title})
        return lines

    def _build_medals(self, medals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for item in medals[:12]:
            label = str(item.get("1") or item.get("2") or item.get("3") or "勋")
            result.append({"label": label})
        return result

    def _build_nations(self, medals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """解析各系载具拥有/精英数。

        App 顶层字段 4 的条目实测格式为：
        - 字段 3：该系拥有载具数
        - 字段 2：该系精英载具数（没有则 0）
        - 字段 1：国家枚举；美国条目缺省字段 1
        """
        nation_id_to_label = {
            None: "美系",
            1: "德系",
            2: "苏系",
            3: "英系",
            4: "日系",
            5: "意系",
            6: "法系",
            7: "中系",
            8: "瑞系",
            9: "以系",
        }
        by_label: Dict[str, Dict[str, Any]] = {label: {"label": label, "owned": 0, "elite": 0, "rate": 0.0} for label in NATION_LABELS}
        for item in medals:
            if not isinstance(item, dict):
                continue
            nation_id = item.get("1")
            label = nation_id_to_label.get(nation_id)
            if not label:
                continue
            owned = int(item.get("3") or 0)
            elite = int(item.get("2") or 0)
            if owned <= 0 and elite <= 0:
                continue
            rate = (elite / owned * 100.0) if owned > 0 else 0.0
            by_label[label] = {"label": label, "owned": owned, "elite": elite, "rate": round(rate, 2)}
        return [by_label[label] for label in NATION_LABELS if by_label[label]["owned"] or by_label[label]["elite"]]

    def _lb_metric(self, label: str, item: Dict[str, Any], monthly: bool = False, percent: bool = False) -> Dict[str, Any]:
        val_key, rank_key = ("1", "2") if monthly else ("3", "4")
        value = _as_double(item.get(val_key))
        rank = item.get(rank_key)
        if value in (None, "", -1, -2) or rank in (-1, -2):
            return {"label": label, "value": "[无数据]", "extra": f"({_fmt_rank(rank)})", "color": "black"}
        if percent and isinstance(value, float):
            value_text = f"{value * 100:.2f}%"
            color = "yellow"
        else:
            value_text = _fmt_score(value)
            color = "black"
        return {"label": label, "value": value_text, "extra": f"({_fmt_rank(rank)})", "color": color}

    def _build_leaderboards(self, common: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        boards = []
        # 参考 AXBot 图：排行榜区块展示通用街机、街机空战、街机陆战。
        # App CommonStatistic 中实测索引：0=通用街机，5=空战街机，3=陆战街机。
        board_specs = [
            (0, "街机模式"),
            (5, "街机模式 · 空战"),
            (3, "街机模式 · 陆战"),
        ]
        for idx, title in board_specs:
            if idx >= len(common):
                continue
            cs = common[idx]
            pvp = cs.get("2", {}) or {}
            lb = cs.get("5", {}) or {}
            if not pvp and not lb:
                continue

            metrics: List[Dict[str, Any]] = []
            rating = _as_double(cs.get("7"))
            if rating not in (None, 0.0):
                rank = (lb.get("15") or {}).get("4") if isinstance(lb.get("15"), dict) else None
                metrics.append({"label": "PVP评分", "value": _fmt_score(rating), "extra": f"({_fmt_rank(rank)})", "color": "black"})
                metrics.append({"label": "近期评分", "value": _fmt_score(rating), "extra": f"({_fmt_rank((lb.get('15') or {}).get('2') if isinstance(lb.get('15'), dict) else None)})", "color": "black"})

            avg_pos = lb.get("12")
            if isinstance(avg_pos, dict):
                for metric in (
                    self._lb_metric("平均相对排名", avg_pos, monthly=False, percent=True),
                    self._lb_metric("近期排名", avg_pos, monthly=True, percent=True),
                ):
                    if metric.get("value") != "[无数据]":
                        metrics.append(metric)

            if metrics:
                boards.append({"title": title, "metrics": metrics})
        return boards

    def _build_detail_columns(self, common: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cols = []
        for idx in range(3):
            if idx >= len(common):
                continue
            cs = common[idx]
            pvp = cs.get("2", {}) or {}
            if not pvp:
                continue
            rows: List[Dict[str, Any]] = []
            missions = pvp.get("12")
            wins = pvp.get("1")
            win_rate = (int(wins or 0) / int(missions or 0) * 100.0) if missions else None
            rows.extend([
                self._row("战斗场次", _fmt_num(missions)),
                self._row("胜利场次", _fmt_num(wins)),
                self._row("场次胜率", f"{win_rate:.2f}%" if win_rate is not None else "[无数据]", self._percent_color(win_rate)),
                self._row("游玩时长", self._sum_time_text(pvp)),
                {"type": "gap"},
                self._row("击毁空军", _fmt_num(pvp.get("10") or 0)),
                self._row("击毁陆军", _fmt_num(pvp.get("9") or 0)),
                self._row("击毁海军", _fmt_num(pvp.get("20") or 0)),
                {"type": "gap"},
                self._vehicle_time_rows(pvp, "战斗机", "6"),
                self._vehicle_time_rows(pvp, "轰炸机", "7"),
                self._vehicle_time_rows(pvp, "攻击机", "8"),
                self._vehicle_time_rows(pvp, "主战坦克", "4"),
                self._vehicle_time_rows(pvp, "重型坦克", "2"),
                self._vehicle_time_rows(pvp, "坦克歼击", "3"),
                self._vehicle_time_rows(pvp, "自行防空", "5"),
                self._vehicle_time_rows(pvp, "驱逐舰", "18"),
                self._vehicle_time_rows(pvp, "鱼雷艇", "14"),
            ])
            clean = []
            for r in rows:
                if isinstance(r, list):
                    clean.extend(r)
                elif r:
                    clean.append(r)
            while clean and clean[-1].get("type") == "gap":
                clean.pop()
            cols.append({"title": MODE_RENDER_TITLES.get(idx, f"模式{idx}数据"), "rows": clean})
        return cols

    def _row(self, label: str, value: Any, color: str = "black") -> Dict[str, Any]:
        return {"label": label, "value": str(value), "color": color}

    def _percent_color(self, percent: Optional[float]) -> str:
        if percent is None:
            return "black"
        if percent >= 75:
            return "purple"
        if percent >= 55:
            return "orange"
        if percent >= 40:
            return "yellow"
        return "red"

    def _sum_time_seconds(self, pvp: Dict[str, Any]) -> int:
        keys = ["2", "3", "4", "5", "6", "7", "8", "14", "18"]
        total = 0
        for key in keys:
            try:
                total += int(pvp.get(key) or 0)
            except Exception:
                continue
        return total

    def _sum_all_mode_time_seconds(self, common: List[Dict[str, Any]]) -> int:
        total = 0
        for idx in range(3):
            if idx >= len(common) or not isinstance(common[idx], dict):
                continue
            pvp = common[idx].get("2", {}) or {}
            if isinstance(pvp, dict):
                total += self._sum_time_seconds(pvp)
        return total

    def _sum_time_text(self, pvp: Dict[str, Any]) -> str:
        return _fmt_time(self._sum_time_seconds(pvp))

    def _vehicle_time_rows(self, pvp: Dict[str, Any], label: str, time_key: str) -> List[Dict[str, Any]]:
        sec = int(pvp.get(time_key) or 0)
        if sec <= 0:
            return []
        # App 当前公共统计只给到兵种时长与总击杀，不给兵种专属击杀；
        # 不再用总击杀/兵种时长硬算 K/R，避免出现几百/几千的错误值。
        return [self._row(f"{label}时长", _fmt_time(sec))]

    def _build_vehicles(self, vehicles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for item in vehicles:
            vid = item.get("11")
            if not vid:
                continue
            row = {
                "id": vid,
                "name": self.loc.unit(str(vid)),
                "image_url": f"https://static.encyclopedia.warthunder.com/images/{vid}.png",
            }
            for key, label in VEHICLE_FIELDS.items():
                if key in item:
                    row[label] = _as_double(item[key]) if key == "4" else item[key]
            result.append(row)
        return result