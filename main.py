"""astrbot_plugin_auto_diary —— 把对话自动整理成日记。

每次对话结束后，插件先把这一轮内容攒在内存里；等对话安静下来
（默认 10 分钟没有新消息）之后，再调用当前使用的模型，把攒下的
对话写成一篇第一人称的日记，存进本地 SQLite。

设计要点
--------
* 不打扰对话：全部在后台完成，出错只记日志，绝不影响正常回复。
* 一天一篇：同一天的日记会被整合更新，而不是堆成流水账。
* 数据自持：默认存在插件自己的数据目录，卸载或分享插件都不会带走别人的日记。
* 可配置：人设、日记标题、标签、记录范围、安静时长都能在配置页调整。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # Web 页面 API（AstrBot 用 Quart 提供）
    from quart import jsonify, request
except Exception:  # noqa: BLE001 - 没有 Web 环境时只跳过页面
    jsonify = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_auto_diary"

DEFAULT_TAG = "auto-diary"
SELF_LABEL = "我"             # 对话记录里机器人自己那一侧，固定第一人称
OTHER_LABEL = "对方"          # 另一侧只做记号，真正的称呼由人设决定
DEFAULT_TRASH_DAYS = 30      # 回收站保留天数，0 = 永久保留
PERSONA_TAG_PREFIX = "persona:"
PLATFORM_TAG_PREFIX = "plat:"
SESSION_TAG_PREFIX = "sess:"
DEFAULT_TITLE_TEMPLATE = "{persona}的日记本 · {date}"
DIARY_HINT = "现在你在写只给自己看的日记，不需要说给别人听，可以写得随意、坦率一些。"

DEFAULT_PERSONA = "你是一个 AI 助手。" + DIARY_HINT

MAX_PENDING_ROUNDS = 40      # 内存里最多攒多少轮对话
MAX_CONVO_CHARS = 1800       # 单次喂给模型的对话字符上限
MAX_DIARY_CHARS = 1800       # 整合时已有日记的字符上限
IDLE_CHECK_INTERVAL = 20     # 后台检查间隔（秒）
MAX_CONTENT_CHARS = 2_000_000  # 手动编辑正文的长度上限
MAX_REPLY_CHARS = 1500       # 单条回复最多带多少字日记正文
LIST_LIMIT = 10              # /读日记 最近 列出的条数

FIRST_PROMPT = """以下是刚刚发生的一段对话。标着「{other_label}」的是正在和你说话的那个人，标着「{self_label}」的是你自己——
这两个词只是记录用的记号。写到日记里时，请按你自己的身份和你一贯的习惯去称呼他：
用你平时叫他的那个称呼，别照抄记号，别用「对方」「那人」这类生分说法，也尽量别用「他」「她」「它」这种没温度的代词（实在没有合适的称呼时，「他」也比「它」好）。
称呼要求：提到他时，用你平时叫他的那个称呼（例如「老师」「主人」「前辈」这类），整篇保持一致，
不要用「他」「她」「它」来代替。也别用你自己的名字去称呼他——{self_name_hint}那是你，不是他。
{address_hint}

{convo}

请以你自己的第一人称视角（「我」），把这段对话写成今天的日记开头。

要求：
- 2~4 句话，口语化，像写给自己看的
- 不要列表、不要标题、不要日期、不要时间戳
- 只输出日记正文，不要任何解释
"""

MERGE_PROMPT = """今天早些时候，你在日记里已经写过这些：

{existing}

后来，又发生了这样一段对话（「{other_label}」是和你说话的人，「{self_label}」是你自己）——称呼他时用你平时叫他的那个称呼（例如「老师」这类），整篇保持一致，别用「他」「它」代替，也别用你自己的名字叫他（{self_name_hint}那是你）。
{address_hint}

{convo}

请把两部分整合成一篇连贯的日记（第一人称）。

要求：
- 保留之前写过的要点，自然地接着往下写
- 整体 3~7 句话，不要时间戳、不要小标题
- 只输出整合后的完整正文，不要任何解释
"""

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    mood       TEXT    NOT NULL DEFAULT '',
    weather    TEXT    NOT NULL DEFAULT '',
    location   TEXT    NOT NULL DEFAULT '',
    tags       TEXT    NOT NULL DEFAULT '',
    deleted    INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""

# 兼容早期版本的库结构
EXTRA_COLUMNS = {
    "mood": "TEXT NOT NULL DEFAULT ''",
    "weather": "TEXT NOT NULL DEFAULT ''",
    "location": "TEXT NOT NULL DEFAULT ''",
    "tags": "TEXT NOT NULL DEFAULT ''",
    "deleted": "INTEGER NOT NULL DEFAULT 0",
    "deleted_at": "TEXT",
}


def plugin_data_dir() -> Path:
    """插件专属的数据目录（AstrBot 的 plugin_data 下）。"""
    try:
        data_dir = StarTools.get_data_dir()
        if data_dir:
            return Path(str(data_dir))
    except Exception:  # noqa: BLE001 - 拿不到就退回标准位置
        pass
    return Path(__file__).resolve().parent.parent.parent / "plugin_data" / PLUGIN_NAME


class DiaryDatabase:
    """日记的本地存储：一个 SQLite 文件，entries 表里放正文。

    多条日记靠 `tags` 区分来源，例如：
        auto-diary                      —— 本插件写的
        auto-diary,persona:某角色        —— 某个人设写的
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute(CREATE_TABLE_SQL)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(entries)")}
        for name, decl in EXTRA_COLUMNS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE entries ADD COLUMN {name} {decl}")
        return conn

    @staticmethod
    def _tag_clause(tags: list[str] | tuple[str, ...]) -> tuple[str, list[str]]:
        """要求记录同时带有这些标签。

        用逗号定界做精确匹配：短标签不会误伤以它开头的长标签
        （比如两个前缀相同的会话标识），互相之间也不会串。标签本身是逗号分隔存的，无需迁移。
        """
        wanted = [tag for tag in (tags or []) if tag]
        if not wanted:
            return "1 = 1", []
        clause = " AND ".join("instr(',' || tags || ',', ?) > 0" for _ in wanted)
        return clause, [f",{tag}," for tag in wanted]

    @staticmethod
    def _line_prefix(tags) -> str:
        """取会话／平台标签的公共前缀。

        旧版本把会话标识硬截断到 48 字符，新版本改成「前缀＋指纹」，
        同一个会话前后算出来的标签不一样，会被当成两条线。这个前缀能把它们认成同一条。
        """
        for tag in tags or []:
            for head in ("sess:", "plat:"):
                if tag.startswith(head):
                    value = tag[len(head) :]
                    if "_" in value:
                        return f"{head}{value.rsplit('_', 1)[0]}"
                    return tag
        return ""

    def _find_id(self, conn, day: str, tags) -> int | None:
        """找当天这一条：先精确匹配，找不到再按前缀兼容旧标签。"""
        clause, params = self._tag_clause(tags)
        row = conn.execute(
            "SELECT id FROM entries WHERE date = ? AND deleted = 0 "
            f"AND {clause} ORDER BY id LIMIT 1",
            [day, *params],
        ).fetchone()
        if row:
            return row["id"]

        prefix = self._line_prefix(tags)
        if not prefix:
            return None
        fixed = [x for x in (tags or []) if not x.startswith(("sess:", "plat:"))]
        clause2, params2 = self._tag_clause(fixed)
        rows = conn.execute(
            "SELECT id FROM entries WHERE date = ? AND deleted = 0 "
            f"AND {clause2} AND instr(tags, ?) > 0 ORDER BY id",
            [day, *params2, prefix],
        ).fetchall()
        # 同平台的不同会话前缀是一样的，候选不唯一时宁可不认，别张冠李戴
        return rows[0]["id"] if len(rows) == 1 else None

    def read_today(self, tags) -> str:
        conn = self.connect()
        try:
            entry_id = self._find_id(conn, date.today().isoformat(), tags)
            if entry_id is None:
                return ""
            row = conn.execute(
                "SELECT content FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
            return row["content"] if row else ""
        finally:
            conn.close()

    def entries_by_date(self, day: str, tags) -> list[dict]:
        """某一天的全部日记。

        分组打开时同一天会有好几条（每条会话线各一条），
        只看第一条会让人以为「日记没更新」。
        """
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, date, title, content, tags, updated_at FROM entries "
                "WHERE date = ? AND deleted = 0 "
                f"AND {clause} ORDER BY id",
                [day, *params],
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def read_by_date(self, day: str, tags) -> str:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT content FROM entries WHERE date = ? AND deleted = 0 "
                f"AND {clause} ORDER BY id LIMIT 1",
                [day, *params],
            ).fetchone()
            return row["content"] if row else ""
        finally:
            conn.close()

    def list_recent(self, tags, limit: int = LIST_LIMIT) -> list[tuple[str, str]]:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT date, title FROM entries WHERE deleted = 0 "
                f"AND {clause} ORDER BY date DESC, id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            return [(row["date"], row["title"]) for row in rows]
        finally:
            conn.close()

    def search(self, keyword: str, tags, limit: int = 5) -> list[tuple[str, str, str]]:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT date, title, content FROM entries WHERE deleted = 0 "
                f"AND {clause} AND content LIKE ? ORDER BY date DESC LIMIT ?",
                [*params, f"%{keyword}%", limit],
            ).fetchall()
        finally:
            conn.close()
        results = []
        for row in rows:
            content = row["content"] or ""
            index = content.find(keyword)
            start = max(0, index - 15)
            snippet = content[start : start + 60].replace("\n", " ").strip()
            results.append((row["date"], row["title"], snippet))
        return results

    def dates_map(self, tags, start: str, end: str) -> dict[str, int]:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT date, COUNT(*) AS n FROM entries WHERE deleted = 0 "
                f"AND {clause} AND date BETWEEN ? AND ? GROUP BY date",
                [*params, start, end],
            ).fetchall()
            return {row["date"]: int(row["n"]) for row in rows}
        finally:
            conn.close()

    def get_entry(self, day: str, tags) -> dict | None:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id, date, title, content, mood, tags, created_at, updated_at "
                "FROM entries WHERE date = ? AND deleted = 0 "
                f"AND {clause} ORDER BY id LIMIT 1",
                [day, *params],
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_entries(self, tags, limit: int, offset: int = 0) -> list[dict]:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, date, title, mood, tags, length(content) AS size, updated_at "
                "FROM entries WHERE deleted = 0 "
                f"AND {clause} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def search_entries(self, keyword: str, tags, limit: int = 20) -> list[dict]:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, date, title, content FROM entries WHERE deleted = 0 "
                f"AND {clause} AND (content LIKE ? OR title LIKE ?) "
                "ORDER BY date DESC LIMIT ?",
                [*params, f"%{keyword}%", f"%{keyword}%", limit],
            ).fetchall()
        finally:
            conn.close()
        results = []
        for row in rows:
            content = row["content"] or ""
            index = content.find(keyword)
            start = max(0, index - 20) if index >= 0 else 0
            snippet = content[start : start + 80].replace("\n", " ").strip()
            results.append(
                {
                    "id": row["id"],
                    "date": row["date"],
                    "title": row["title"],
                    "snippet": snippet,
                }
            )
        return results

    def stats(self, tags) -> dict:
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last, "
                "SUM(length(content)) AS chars FROM entries WHERE deleted = 0 "
                f"AND {clause}",
                params,
            ).fetchone()
            return {
                "count": int(row["n"] or 0),
                "first": row["first"] or "",
                "last": row["last"] or "",
                "chars": int(row["chars"] or 0),
            }
        finally:
            conn.close()

    def all_entries(self, tags) -> list[dict]:
        """全部日记，按时间正序（导出用）。"""
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT date, title, content FROM entries WHERE deleted = 0 "
                f"AND {clause} ORDER BY date ASC, id ASC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_entry(self, entry_id: int, content: str, tags=None) -> int:
        """按 id 改某篇日记的正文。带了 tags 就只改本插件范围内的记录。"""
        conn = self.connect()
        try:
            sql = (
                "UPDATE entries SET content = ?, "
                "updated_at = datetime('now', 'localtime') WHERE id = ?"
            )
            params = [content, entry_id]
            if tags:
                clause, extra = self._tag_clause(tags)
                sql += f" AND {clause}"
                params.extend(extra)
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def on_this_day(self, tags, month_day: str, this_year: str) -> list[dict]:
        """往年同月同日的日记。month_day 形如 10-10。"""
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, date, title, content FROM entries WHERE deleted = 0 "
                f"AND {clause} AND date LIKE ? AND date NOT LIKE ? "
                "ORDER BY date DESC",
                [*params, f"%-{month_day}", f"{this_year}-%"],
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _soft_delete(self, where: str, params: list) -> list[int]:
        """按条件软删除，返回被删的 id 列表（留着好反悔）。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                f"SELECT id FROM entries WHERE deleted = 0 AND {where}", params
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.executemany(
                    "UPDATE entries SET deleted = 1, "
                    "deleted_at = datetime('now', 'localtime') WHERE id = ?",
                    [(one,) for one in ids],
                )
                conn.commit()
            return ids
        finally:
            conn.close()

    def delete_ids(self, ids: list[int], tags) -> list[int]:
        """按 id 软删除（只删本插件范围内的）。"""
        wanted = [int(i) for i in (ids or [])]
        if not wanted:
            return []
        clause, params = self._tag_clause(tags)
        placeholders = ",".join("?" for _ in wanted)
        return self._soft_delete(
            f"id IN ({placeholders}) AND {clause}", [*wanted, *params]
        )

    def delete_day(self, day: str, tags) -> list[int]:
        """软删除某一天的日记，返回被删的 id 列表。"""
        clause, params = self._tag_clause(tags)
        return self._soft_delete(f"date = ? AND {clause}", [day, *params])

    def delete_all(self, tags) -> list[int]:
        """软删除本插件写过的所有日记，返回被删的 id 列表。"""
        clause, params = self._tag_clause(tags)
        return self._soft_delete(clause, params)

    def restore(self, ids: list[int], tags=None) -> int:
        """把软删除的日记恢复回来（带了 tags 就只恢复本插件范围内的）。"""
        wanted = [int(i) for i in (ids or [])]
        if not wanted:
            return 0
        conn = self.connect()
        try:
            placeholders = ",".join("?" for _ in wanted)
            sql = f"UPDATE entries SET deleted = 0, deleted_at = NULL WHERE id IN ({placeholders})"
            params = list(wanted)
            if tags:
                clause, extra = self._tag_clause(tags)
                sql += f" AND {clause}"
                params.extend(extra)
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def purge_trash(self, tags, before: str) -> int:
        """彻底删掉回收站里早于 before 的记录。"""
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            cur = conn.execute(
                "DELETE FROM entries WHERE deleted = 1 AND deleted_at IS NOT NULL "
                f"AND deleted_at < ? AND {clause}",
                [before, *params],
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def empty_trash(self, tags) -> int:
        """清空回收站：彻底删除，不可恢复。"""
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            cur = conn.execute(
                f"DELETE FROM entries WHERE deleted = 1 AND {clause}", params
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def deleted_entries(self, tags) -> list[dict]:
        """回收站：看看有哪些被删掉的。"""
        clause, params = self._tag_clause(tags)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, date, title, deleted_at FROM entries "
                f"WHERE deleted = 1 AND {clause} ORDER BY deleted_at DESC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def save_today(self, text: str, title: str, tags) -> None:
        """当天（同一组标签下）有则更新，没有则新建——一天一人设一篇。"""
        tag_string = ",".join(t for t in (tags or []) if t) or DEFAULT_TAG
        conn = self.connect()
        try:
            entry_id = self._find_id(conn, date.today().isoformat(), tags)
            if entry_id is not None:
                # 顺手把标签规范成当前格式，免得这次认出来、下次又认不出
                conn.execute(
                    "UPDATE entries SET content = ?, title = ?, tags = ?, "
                    "updated_at = datetime('now', 'localtime') WHERE id = ?",
                    (text, title, tag_string, entry_id),
                )
            else:
                conn.execute(
                    "INSERT INTO entries (date, title, content, mood, tags) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (date.today().isoformat(), title, text, "", tag_string),
                )
            conn.commit()
        finally:
            conn.close()


class Main(Star):
    """对话自动整理成日记。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = config or {}

        self.enabled: bool = bool(cfg.get("enabled", True))
        raw_db = str(cfg.get("db_path") or "").strip()
        self.db_path: str = raw_db or str(plugin_data_dir() / "diary.db")
        self.scope: str = str(cfg.get("scope") or "private").strip().lower()
        self.mode: str = str(cfg.get("mode") or "append").strip().lower()
        self.idle_minutes: int = max(1, int(cfg.get("idle_minutes") or 10))
        self.persona: str = str(cfg.get("persona") or DEFAULT_PERSONA).strip()
        # 日记里怎么称呼对方（留空交给模型按人设判断）
        self.other_address: str = str(cfg.get("other_address") or "").strip()
        self.base_tags: list[str] = [DEFAULT_TAG]  # 查看“全部日记”时的过滤标签
        self.title_template: str = (
            str(cfg.get("title_template") or DEFAULT_TITLE_TEMPLATE).strip()
            or DEFAULT_TITLE_TEMPLATE
        )

        self.db = DiaryDatabase(self.db_path)
        # 待写队列按「来源」分开：每个会话/平台各排各的，互不干扰
        self._queues: dict[str, list[str]] = {}
        self._sources: dict[str, tuple[str, str]] = {}   # key -> (umo, platform)
        self._last_activity: dict[str, float] = {}
        self._writing: set[str] = set()
        self._last_umo: str = ""
        self._last_platform: str = ""
        self._busy: bool = False
        self._paused: bool = False  # /暂停日记 用
        self.trash_days: int = self._parse_trash_days(cfg.get("trash_days"))
        self.group_by: str = str(cfg.get("group_by") or "none").strip().lower()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        self._register_web_api()
        self._load_pending()
        self._sync_titles()
        self._purge_trash()
        logger.info(
            f"[AutoDiary] 已加载 | 数据库：{self.db_path} | "
            f"记录范围：{self.scope} | 安静 {self.idle_minutes} 分钟后整理"
        )
        self._debug(
            f"===== 插件已加载 ===== 分组={self.group_by} | 写法={self.mode} | "
            f"范围={self.scope} | 安静={self.idle_minutes}分钟 | 回收站={self.trash_days}天"
        )
        if any(self._queues.values()):
            self._ensure_worker()

    # ------------------------------------------------------------------ 生命周期
    async def terminate(self) -> None:
        """插件卸载时收尾。"""
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------ 事件
    @filter.on_agent_done()
    async def on_agent_done(self, event, run_context, response) -> None:
        """每轮回复结束后，把这一轮对话攒起来。"""
        if not self.enabled:
            self._debug("收到一轮回复，但插件已禁用")
            return
        try:
            self._collect(event, response)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[AutoDiary] 收集对话失败：{exc}")
            self._debug(f"收集对话失败：{exc!r}")
            return
        self._debug(
            f"收到一轮对话 | 平台={self._last_platform} | 会话={self._last_umo} | "
            f"总待写={sum(len(v) for v in self._queues.values())} | 暂停={self._paused}"
        )
        self._ensure_worker()

    @staticmethod
    def _source_of(event) -> tuple[str, str]:
        """尽量问出 (会话标识, 平台名)。

        不同 AstrBot 版本给的字段不一样，只认 unified_msg_origin 的话，
        一旦取空就会退成同一条“default”线，不同会话就串在一起了。
        """
        umo = ""
        for attr in ("unified_msg_origin", "umo", "session_id"):
            value = getattr(event, attr, None)
            if value:
                umo = str(value)
                break

        platform = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            pass

        if not umo:
            parts = [platform]
            for getter in ("get_group_id", "get_sender_id", "get_session_id"):
                try:
                    value = getattr(event, getter, lambda: "")()
                except Exception:  # noqa: BLE001
                    value = ""
                if value:
                    parts.append(str(value))
                    break
            umo = ":".join(part for part in parts if part)

        if not platform and umo:
            platform = umo.split(":")[0]
        return umo, platform

    def _collect(self, event, response) -> None:
        if self._paused:
            self._debug("已暂停记录，跳过")
            return
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception as exc:  # noqa: BLE001
            self._debug(f"取群号失败：{exc!r}")
        if self.scope == "private" and group_id:
            self._debug(f"群聊 + scope=private，跳过（group_id={group_id}）")
            return
        self._last_umo, self._last_platform = self._source_of(event)
        user_text = (getattr(event, "message_str", "") or "").strip()
        bot_text = (getattr(response, "completion_text", "") or "").strip()
        if not user_text and not bot_text:
            self._debug("这一轮没有文本，跳过")
            return
        key = self._group_key()
        if key == "default" and self.group_by != "none":
            self._debug(
                f"警告：分组方式={self.group_by} 但没拿到会话标识，"
                f"这条线会和其它会话混在一起（umo={self._last_umo!r} "
                f"platform={self._last_platform!r}）"
            )
        self._sources[key] = (self._last_umo, self._last_platform)
        queue = self._queues.setdefault(key, [])
        queue.append(f"{OTHER_LABEL}：{user_text}\n{SELF_LABEL}：{bot_text}")
        if len(queue) > MAX_PENDING_ROUNDS:
            self._queues[key] = queue[-MAX_PENDING_ROUNDS:]
        self._last_activity[key] = time.time()
        self._save_pending()

    def _ensure_worker(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 不在事件循环里（极少见）
            return
        self._task = loop.create_task(self._idle_worker())

    async def _idle_worker(self) -> None:
        """盯着每个来源：谁安静够了就先写谁。"""
        idle = self.idle_minutes * 60
        while True:
            if not self._queues:
                break
            now = time.time()
            due = [
                key
                for key, queue in list(self._queues.items())
                if queue
                and key not in self._writing
                and now - self._last_activity.get(key, 0) >= idle
            ]
            for key in due:
                self._debug(f"「{key}」安静满 {self.idle_minutes} 分钟，开始整理")
                await self._flush(key)
            if not any(self._queues.values()):
                break
            await asyncio.sleep(IDLE_CHECK_INTERVAL)

    # ------------------------------------------------------------------ 生成与写入
    async def _flush(self, key: str = "default") -> None:
        async with self._lock:
            queue = self._queues.get(key) or []
            if not queue or key in self._writing:
                return
            umo, platform = self._sources.get(key, ("", ""))
            self._purge_trash()
            convo = "\n\n".join(queue)
            self._queues[key] = []
            self._writing.add(key)
            try:
                persona_name, system_prompt = await self._resolve_persona(umo)
                tags = self._build_tags(persona_name, key)
                self._debug(f"落笔 | 线={key} | 人设={persona_name!r} | 标签={tags}")
                title = self._safe_format(
                    self.title_template,
                    {"date": date.today().strftime("%m-%d"), "persona": persona_name or "AI"},
                )
                existing = self.db.read_today(tags)
                # append：每次都追加，带时间戳，一条不丢
                # merge ：把旧内容和新对话交给模型，重写成一篇连贯日记
                self._debug(
                    f"本次对话 {len(convo)} 字，开头：{convo[:40]!r}"
                )
                if self.mode == "append":
                    piece = await self._compose(
                        convo, "", system_prompt, umo, persona_name
                    )
                    if not piece:
                        self._debug("模型没返回内容，这次不写")
                        return
                    # 同一段内容已经记过就别再写一遍，免得日记里出现两段一模一样的
                    if piece.strip() and piece.strip() in existing:
                        self._debug("这段内容和日记里已有的重复，跳过")
                        return
                    stamp = time.strftime("%H:%M")
                    merged = (
                        f"{existing.rstrip()}\n\n〔{stamp}〕\n{piece}".strip()
                        if existing
                        else f"〔{stamp}〕\n{piece}"
                    )
                    self.db.save_today(merged, title, tags)
                    self._debug(f"写入完成 | 追加 {len(piece)} 字 | 全文 {len(merged)} 字")
                    logger.info(
                        f"[AutoDiary] {persona_name or '默认人设'} 追加了一段："
                        f"{piece[:30]}…"
                    )
                else:
                    text = await self._compose(
                        convo, existing, system_prompt, umo, persona_name
                    )
                    self._debug(f"模型返回 {len(text or '')} 字")
                    if text:
                        self.db.save_today(text, title, tags)
                        logger.info(
                            f"[AutoDiary] {persona_name or '默认人设'} 今天这篇写好了："
                            f"{text[:30]}…"
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[AutoDiary] 生成日记失败：{exc}")
                self._debug(f"生成日记失败：{exc!r}")
            finally:
                self._writing.discard(key)
                self._save_pending()

    @staticmethod
    def _pick_persona_fields(persona) -> tuple[str, str]:
        """从人设对象里取出 (名字, 提示词)，兼容 dict / 对象两种形态。"""
        if persona is None:
            return "", ""
        if isinstance(persona, dict):
            name = persona.get("name") or persona.get("persona_id") or ""
            prompt = persona.get("prompt") or persona.get("system_prompt") or ""
        else:
            name = getattr(persona, "name", "") or getattr(persona, "persona_id", "")
            prompt = getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "")
        return str(name or "").strip(), str(prompt or "").strip()

    @staticmethod
    def _clean_persona_name(value) -> str:
        """把「没有名字」的几种情况统一成空串。"""
        name = str(value or "").strip()
        if not name or name.lower() in {"default", "default_personality", "none", "null"}:
            return ""
        return name

    def _persona_id_from_config(self, manager, umo: str) -> str:
        """读出当前会话用的人设 id。

        AstrBot 的存放位置随版本变过：
        - 4.29+ ：`agent_runner.config.persona.persona_id`（local runner）
        - 更早  ：`agent_runner.config.persona_id`
        - 旧版  ：`provider_settings.default_personality`
        全都试一遍，最后兜底用 PersonaManager 自己算好的默认值。
        """
        conf = None
        acm = getattr(manager, "acm", None)
        if acm is not None:
            if umo:
                try:
                    conf = acm.get_conf(umo)
                except Exception:  # noqa: BLE001
                    conf = None
            if not isinstance(conf, dict):
                try:
                    conf = acm.get_conf()
                except Exception:  # noqa: BLE001
                    conf = None

        if isinstance(conf, dict):
            runner = conf.get("agent_runner") or {}
            if isinstance(runner, dict):
                rcfg = runner.get("config") or {}
                if isinstance(rcfg, dict):
                    persona = rcfg.get("persona") or {}
                    if isinstance(persona, dict):
                        pid = str(persona.get("persona_id") or "").strip()
                        if pid:
                            return pid
                    pid = str(rcfg.get("persona_id") or "").strip()
                    if pid:
                        return pid
            provider = conf.get("provider_settings") or {}
            if isinstance(provider, dict):
                pid = str(provider.get("default_personality") or "").strip()
                if pid:
                    return pid

        return str(getattr(manager, "default_persona", "") or "").strip()


    async def _resolve_persona(self, umo: str = "") -> tuple[str, str]:
        """返回 (人设名, 系统提示词)。

        名字和提示词分开取：
        - 名字：优先用配置里的 `default_personality`（那是人设的真实名称），
          其次才用人设对象自带的 name。
        - 提示词：按名字查人设，查不到再走官方接口、最后遍历人设列表。

        这样即使某个版本的人设对象里没有 name 字段，标题里的 {persona} 也不会退化成「AI」。
        """
        if self.persona:
            # 口吻固定用自己的，但标题里的 {persona} 还是尽量取真实人设名
            manager = getattr(self.context, "persona_manager", None)
            name = ""
            if manager is not None:
                pid = self._persona_id_from_config(manager, umo or self._last_umo)
                if pid:
                    try:
                        obj = manager.get_persona_v3_by_id(pid)
                        p_name, _ = self._pick_persona_fields(obj)
                        name = self._clean_persona_name(p_name) or self._clean_persona_name(pid)
                    except Exception:  # noqa: BLE001
                        name = self._clean_persona_name(pid)
            return name, self.persona

        manager = getattr(self.context, "persona_manager", None)
        target_umo = umo or self._last_umo
        if manager is None:
            self._debug("没有 persona_manager，无法读取人设")
            return "", DEFAULT_PERSONA

        name = ""
        base = ""
        tried = []

        # 1) 配置里的 default_personality 是【人设 id】，显示名在对象的 name 上
        config_id = self._persona_id_from_config(manager, target_umo)
        if config_id:
            tried.append(f"配置里的人设 id={config_id!r}")
            try:
                persona = manager.get_persona_v3_by_id(config_id)
                p_name, base = self._pick_persona_fields(persona)
                name = self._clean_persona_name(p_name) or self._clean_persona_name(config_id)
                tried.append(f"按 id 查 -> name={p_name!r} len={len(base)}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"按 id 查失败：{exc!r}")
        else:
            tried.append("配置里没有 default_personality")

        # 2) 官方接口（带会话）
        if not base and target_umo:
            try:
                persona = await manager.get_default_persona_v3(target_umo)
                api_name, base = self._pick_persona_fields(persona)
                if not name:
                    name = self._clean_persona_name(api_name)
                tried.append(f"带会话 -> name={api_name!r} len={len(base)}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"带会话失败：{exc!r}")

        # 3) 官方接口（不带会话）
        if not base:
            try:
                persona = await manager.get_default_persona_v3()
                api_name, base = self._pick_persona_fields(persona)
                if not name:
                    name = self._clean_persona_name(api_name)
                tried.append(f"不带会话 -> name={api_name!r} len={len(base)}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"不带会话失败：{exc!r}")

        # 4) 遍历人设列表
        if not base:
            try:
                pool = list(getattr(manager, "personas_v3", []) or [])
                tried.append(f"人设列表 {len(pool)} 条")
                for item in pool:
                    item_name, item_prompt = self._pick_persona_fields(item)
                    if item_prompt:
                        base = item_prompt
                        if not name:
                            name = self._clean_persona_name(item_name)
                        break
            except Exception as exc:  # noqa: BLE001
                tried.append(f"遍历人设失败：{exc!r}")

        self._debug(f"人设解析 | 名字={name!r} | " + " | ".join(tried))

        if base:
            return name, f"{base}\n\n{DIARY_HINT}"
        return name, DEFAULT_PERSONA

    @staticmethod
    def _safe_persona(name: str) -> str:
        """人设名要放进标签，逗号和百分号会把它拆坏，换成下划线。"""
        text = str(name or "").replace(",", "_").replace("%", "_").replace("\n", " ")
        return text.strip()

    @staticmethod
    def _safe_format(template: str, values: dict) -> str:
        """安全套模板：不认识的占位符原样保留，别因为一个 {foo} 整篇写不出来。"""
        class _Soft(dict):
            def __missing__(self, key):
                return "{" + key + "}"

        try:
            return str(template).format_map(_Soft(values))
        except Exception:  # noqa: BLE001
            return str(template)

    @staticmethod
    def _safe_tag(value: str) -> str:
        """把会话标识压成能安全放进标签的样子。

        太长时保留可读前缀再补一段指纹——直接硬截断会把不同会话
        截成同一个标识，那就会串线。
        """
        raw = str(value or "")
        text = re.sub(r"[^0-9A-Za-z_\-]", "_", raw)
        if len(text) > 60:
            digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
            text = f"{text[:40]}_{digest}"
        return text

    def _group_key(self) -> str:
        """当前这轮对话属于哪一条日记线。"""
        if self.group_by == "platform" and self._last_platform:
            return f"{PLATFORM_TAG_PREFIX}{self._safe_tag(self._last_platform)}"
        if self.group_by == "session" and self._last_umo:
            return f"{SESSION_TAG_PREFIX}{self._safe_tag(self._last_umo)}"
        return "default"

    def _build_tags(self, persona_name: str, key: str = "default") -> list[str]:
        """人设 / 平台 / 会话不同，标签就不同 → 同一天可以各写各的。"""
        tags = [DEFAULT_TAG]
        safe_name = self._safe_persona(persona_name)
        if safe_name:
            tags.append(f"{PERSONA_TAG_PREFIX}{safe_name}")
        if key and key != "default":
            tags.append(key)
        return tags

    def _address_hint(self) -> str:
        """用户指定的「日记里怎么称呼对方」。留空就交给模型按人设判断。"""
        name = str(getattr(self, "other_address", "") or "").strip()
        if not name:
            return ""
        return f"（重要：写到他时，一律称呼「{name}」，不要用「他」「她」「它」代替。）"

    @staticmethod
    def _self_name_hint(persona_name: str) -> str:
        """告诉模型「你自己叫什么」，免得它拿自己的名字去称呼对方。"""
        name = str(persona_name or "").strip()
        if not name:
            return ""
        return f"你自己叫「{name}」，"

    async def _compose(
        self,
        convo: str,
        existing: str,
        system_prompt: str,
        umo: str = "",
        persona_name: str = "",
    ) -> str:
        provider = None
        try:
            if umo:
                provider = self.context.get_using_provider(umo)
        except Exception as exc:  # noqa: BLE001
            self._debug(f"按会话取模型失败：{exc!r}")
        if provider is None:
            try:
                provider = self.context.get_using_provider()
            except Exception as exc:  # noqa: BLE001
                self._debug(f"取模型失败：{exc!r}")
        if provider is None:
            logger.warning("[AutoDiary] 没有可用的模型提供商，跳过本次整理")
            self._debug("没有可用的模型提供商，跳过")
            return ""
        convo = convo[:MAX_CONVO_CHARS]
        if existing.strip():
            prompt = MERGE_PROMPT.format(
                existing=existing[:MAX_DIARY_CHARS],
                convo=convo,
                other_label=OTHER_LABEL,
                self_label=SELF_LABEL,
                self_name_hint=self._self_name_hint(persona_name),
                address_hint=self._address_hint(),
            )
        else:
            prompt = FIRST_PROMPT.format(
                convo=convo,
                other_label=OTHER_LABEL,
                self_label=SELF_LABEL,
                self_name_hint=self._self_name_hint(persona_name),
                address_hint=self._address_hint(),
            )
        resp = await provider.text_chat(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=f"auto_diary_{date.today().isoformat()}",
            persist=False,
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    @staticmethod
    def _parse_trash_days(value) -> int:
        """回收站保留天数，0/空 = 永久保留。"""
        try:
            days = int(str(value).strip() or DEFAULT_TRASH_DAYS)
        except (TypeError, ValueError):
            return DEFAULT_TRASH_DAYS
        return max(0, days)

    def _purge_trash(self) -> int:
        """把回收站里超过保留期的记录彻底删掉。"""
        if self.trash_days <= 0:
            return 0
        try:
            cutoff = (
                datetime.now() - timedelta(days=self.trash_days)
            ).strftime("%Y-%m-%d %H:%M:%S")
            removed = self.db.purge_trash(self.base_tags, cutoff)
            if removed:
                logger.info(
                    f"[AutoDiary] 回收站里超过 {self.trash_days} 天的 "
                    f"{removed} 篇日记已彻底清除"
                )
            return removed
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[AutoDiary] 清理回收站失败：{exc}")
            return 0

    def _pending_path(self) -> Path:
        return Path(self.db_path).parent / "pending.json"

    def _save_pending(self) -> None:
        """把还没落笔的对话存到磁盘，免得重载/重启把它吃掉。"""
        try:
            data = {
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "queues": {k: v for k, v in self._queues.items() if v},
                "sources": {k: list(v) for k, v in self._sources.items()},
                "activity": self._last_activity,
            }
            self._pending_path().write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[AutoDiary] 暂存待写内容失败：{exc}")

    def _load_pending(self) -> None:
        """插件重新加载时，把上次没写完的接回来。"""
        try:
            path = self._pending_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8")) or {}
            queues = data.get("queues") or {}
            if isinstance(queues, dict):
                self._queues = {
                    str(k): [str(x) for x in v] for k, v in queues.items() if v
                }
            sources = data.get("sources") or {}
            if isinstance(sources, dict):
                self._sources = {
                    str(k): (str(v[0]), str(v[1]))
                    for k, v in sources.items()
                    if isinstance(v, (list, tuple)) and len(v) >= 2
                }
            activity = data.get("activity") or {}
            if isinstance(activity, dict):
                self._last_activity = {
                    str(k): float(v) for k, v in activity.items()
                }
            total = sum(len(v) for v in self._queues.values())
            if total:
                logger.info(f"[AutoDiary] 接回上次没写完的 {total} 轮对话")
                self._debug(f"接回上次没写完的 {total} 轮对话（来自 {data.get('saved_at')}）")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[AutoDiary] 读取暂存内容失败：{exc}")

    def _debug(self, message: str) -> None:
        """往插件目录写一行调试日志，出问题时好查。"""
        try:
            path = Path(self.db_path).parent / "debug.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                # 太大了就留后半段，免得无限长
                kept = path.read_text(encoding="utf-8", errors="ignore")[-500_000:]
                path.write_text(kept, encoding="utf-8")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {message}\n")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 标题同步
    def _sync_titles(self) -> int:
        """按当前模板重刷所有日记的标题。

        标题本来就由模板生成，所以改了模板之后，已写好的日记也能跟上。
        """
        try:
            conn = self.db.connect()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[AutoDiary] 跳过标题同步：{exc}")
            return 0
        try:
            rows = conn.execute(
                "SELECT id, date, tags FROM entries WHERE deleted = 0 "
                "AND instr(',' || tags || ',', ?) > 0",
                (f",{DEFAULT_TAG},",),
            ).fetchall()

            # 旧记录的标签里可能根本没存人设名（早期版本没写进去），
            # 那就用当前人设名兜底，否则刷多少次都还是「AI」。
            current_name = ""
            manager = getattr(self.context, "persona_manager", None)
            if manager is not None:
                pid = self._persona_id_from_config(manager, "")
                if pid:
                    try:
                        obj = manager.get_persona_v3_by_id(pid)
                        p_name, _ = self._pick_persona_fields(obj)
                        current_name = (
                            self._clean_persona_name(p_name) or self._clean_persona_name(pid)
                        )
                    except Exception:  # noqa: BLE001
                        current_name = self._clean_persona_name(pid)

            changed = 0
            for row in rows:
                tags = row["tags"] or ""
                match = re.search(f"{PERSONA_TAG_PREFIX}([^,]+)", tags)

                if match:
                    # 已经记下人设名的，那是历史，切换人设也不能改
                    persona_name = match.group(1)
                elif current_name:
                    # 早期记录没把名字写进标签：用当前人设补一次，
                    # 同时把名字固化到标签里，以后切换人设就不会再被改动
                    safe = self._safe_persona(current_name) or current_name
                    persona_name = safe
                    new_tags = (
                        f"{tags},{PERSONA_TAG_PREFIX}{safe}"
                        if tags
                        else f"{PERSONA_TAG_PREFIX}{safe}"
                    )
                    conn.execute(
                        "UPDATE entries SET tags = ? WHERE id = ?", (new_tags, row["id"])
                    )
                else:
                    # 什么线索都没有，保持原样
                    continue

                title = self._safe_format(
                    self.title_template,
                    {"date": (row["date"] or "")[5:], "persona": persona_name or "AI"},
                )
                if title:
                    conn.execute(
                        "UPDATE entries SET title = ? WHERE id = ?", (title, row["id"])
                    )
                    changed += 1
            conn.commit()
            if changed:
                logger.info(f"[AutoDiary] 已按当前模板为 {changed} 篇日记更新标题")
            return changed
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[AutoDiary] 同步标题失败：{exc}")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------ Web 页面 API
    def _register_web_api(self) -> None:
        if jsonify is None or request is None:
            logger.info("[AutoDiary] 当前环境没有 Quart，跳过日记页面 API")
            return
        routes = [
            ("/stats", self._api_stats, ["GET"]),
            ("/dates", self._api_dates, ["GET"]),
            ("/entry", self._api_entry, ["GET"]),
            ("/list", self._api_list, ["GET"]),
            ("/search", self._api_search, ["GET"]),
            ("/delete", self._api_delete, ["POST"]),
            ("/update", self._api_update, ["POST"]),
            ("/restore", self._api_restore, ["POST"]),
            ("/trash", self._api_trash, ["GET"]),
            ("/trash/empty", self._api_empty_trash, ["POST"]),
            ("/export", self._api_export, ["GET"]),
            ("/onthisday", self._api_onthisday, ["GET"]),
            ("/prefs", self._api_get_prefs, ["GET"]),
            ("/prefs", self._api_set_prefs, ["POST"]),
        ]
        for path, handler, methods in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}{path}", handler, methods, f"AutoDiary {path}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[AutoDiary] 注册接口 {path} 失败：{exc}")

    @staticmethod
    def _arg(name: str, default: str = "") -> str:
        try:
            value = request.args.get(name)
        except Exception:  # noqa: BLE001
            return default
        return str(value) if value is not None else default

    async def _api_stats(self):
        try:
            return jsonify({"ok": True, "data": self.db.stats(self.base_tags)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] stats 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_dates(self):
        """GET /dates?start=YYYY-MM-DD&end=YYYY-MM-DD"""
        try:
            start = self._arg("start")
            end = self._arg("end")
            if not start or not end:
                today = date.today()
                start = today.replace(day=1).isoformat()
                if today.month == 12:
                    end = today.replace(month=12, day=31).isoformat()
                else:
                    end = today.replace(
                        month=today.month + 1, day=1
                    ).replace(day=1) - timedelta(days=1)
                    end = end.isoformat()
            data = self.db.dates_map(self.base_tags, start, end)
            return jsonify({"ok": True, "data": data})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] dates 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_entry(self):
        """GET /entry?date=YYYY-MM-DD"""
        try:
            day = self._arg("date") or date.today().isoformat()
            entries = self.db.entries_by_date(day, self.base_tags)
            return jsonify(
                {
                    "ok": True,
                    "data": {
                        "date": day,
                        "count": len(entries),
                        "entry": entries[0] if entries else None,  # 兼容旧行为
                        "entries": entries,
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] entry 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_list(self):
        """GET /list?limit=30&offset=0"""
        try:
            limit = max(1, min(200, int(self._arg("limit", "30") or 30)))
            offset = max(0, int(self._arg("offset", "0") or 0))
            rows = self.db.list_entries(self.base_tags, limit, offset)
            return jsonify({"ok": True, "data": rows})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] list 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_search(self):
        """GET /search?q=关键词&limit=20"""
        try:
            keyword = self._arg("q").strip()
            if not keyword:
                return jsonify({"ok": True, "data": []})
            limit = max(1, min(100, int(self._arg("limit", "20") or 20)))
            return jsonify({"ok": True, "data": self.db.search_entries(keyword, self.base_tags, limit)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] search 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_delete(self):
        """POST /delete   body: {"id": 12} / {"date": "YYYY-MM-DD"} / {"all": true}"""
        try:
            payload = await self._payload()
            if payload.get("id") is not None:
                ids = self.db.delete_ids([payload["id"]], self.base_tags)
                logger.info(f"[AutoDiary] 已删除 1 篇日记（id={payload['id']}）")
                return jsonify({"ok": True, "data": {"deleted": len(ids), "ids": ids}})
            if payload.get("all"):
                ids = self.db.delete_all(self.base_tags)
                logger.info(f"[AutoDiary] 已清空 {len(ids)} 篇日记（可撤回）")
                return jsonify({"ok": True, "data": {"deleted": len(ids), "ids": ids}})
            day = str(payload.get("date") or "").strip()
            if not day:
                return jsonify(
                    {"ok": False, "error": "请求里没带 date 或 all，无法确定要删什么"}
                ), 400
            ids = self.db.delete_day(day, self.base_tags)
            logger.info(f"[AutoDiary] 已删除 {day} 的 {len(ids)} 篇日记（可撤回）")
            return jsonify({"ok": True, "data": {"deleted": len(ids), "ids": ids}})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] delete 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_update(self):
        """POST /update   body: {"id": 12, "content": "..."}"""
        try:
            payload = await self._payload()
            entry_id = payload.get("id")
            content = payload.get("content")
            if entry_id is None or not isinstance(content, str):
                return jsonify({"ok": False, "error": "需要 id 和 content"}), 400
            if len(content) > MAX_CONTENT_CHARS:
                return jsonify(
                    {"ok": False, "error": f"正文太长了（上限 {MAX_CONTENT_CHARS} 字）"}
                ), 400
            rows = self.db.update_entry(int(entry_id), content, self.base_tags)
            return jsonify({"ok": True, "data": {"updated": rows}})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] update 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_restore(self):
        """POST /restore   body: {"ids": [1,2]} 或 {"all": true}"""
        try:
            payload = await self._payload()
            if payload.get("all"):
                ids = [row["id"] for row in self.db.deleted_entries(self.base_tags)]
            else:
                raw = payload.get("ids") or []
                if isinstance(raw, (str, int)):
                    raw = [raw]
                try:
                    ids = [int(i) for i in raw]
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "ids 里有不是数字的值"}), 400
            count = self.db.restore(ids, self.base_tags)
            logger.info(f"[AutoDiary] 已恢复 {count} 篇日记")
            return jsonify({"ok": True, "data": {"restored": count}})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] restore 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_trash(self):
        """GET /trash  —— 回收站列表"""
        try:
            return jsonify(
                {
                    "ok": True,
                    "data": {
                        "entries": self.db.deleted_entries(self.base_tags),
                        "keep_days": self.trash_days,
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] trash 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_empty_trash(self):
        """POST /trash/empty  —— 彻底清空回收站（不可恢复）"""
        try:
            count = self.db.empty_trash(self.base_tags)
            logger.info(f"[AutoDiary] 回收站已清空，彻底删除 {count} 篇")
            return jsonify({"ok": True, "data": {"purged": count}})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] 清空回收站出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_export(self):
        """GET /export?format=md|txt|json"""
        try:
            fmt = (self._arg("format", "md") or "md").lower()
            rows = self.db.all_entries(self.base_tags)
            if fmt == "json":
                content = json.dumps(rows, ensure_ascii=False, indent=2)
                ext, mime = "json", "application/json"
            elif fmt == "txt":
                blocks = [
                    f"{row['title']}\n{row['date']}\n\n{row['content']}"
                    for row in rows
                ]
                content = "\n\n".join(blocks)
                ext, mime = "txt", "text/plain"
            else:
                blocks = [
                    f"## {row['title']}\n\n> {row['date']}\n\n{row['content']}"
                    for row in rows
                ]
                content = "# 自动日记本\n\n" + "\n\n---\n\n".join(blocks)
                ext, mime = "md", "text/markdown"
            return jsonify(
                {
                    "ok": True,
                    "data": {
                        "filename": f"diary-{date.today().isoformat()}.{ext}",
                        "mime": mime,
                        "count": len(rows),
                        "content": content,
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] export 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_onthisday(self):
        """GET /onthisday  —— 往年今天的日记"""
        try:
            today = date.today()
            rows = self.db.on_this_day(
                self.base_tags, today.strftime("%m-%d"), str(today.year)
            )
            return jsonify({"ok": True, "data": rows})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] onthisday 接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    @staticmethod
    async def _payload() -> dict:
        """尽量从请求里取出参数：JSON 优先，其次是表单、查询串。"""
        try:
            data = await request.get_json(silent=True)
            if isinstance(data, dict) and data:
                return data
        except Exception:  # noqa: BLE001
            pass
        try:
            form = await request.form
            if form:
                return dict(form)
        except Exception:  # noqa: BLE001
            pass
        try:
            args = request.args
            if args:
                return {key: args.get(key) for key in args}
        except Exception:  # noqa: BLE001
            pass
        return {}

    # ------------------------------------------------------------------ 页面偏好
    def _prefs_path(self) -> Path:
        return Path(self.db_path).parent / "prefs.json"

    def _load_prefs(self) -> dict:
        try:
            path = self._prefs_path()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[AutoDiary] 读取页面偏好失败：{exc}")
        return {}

    def _save_prefs(self, data: dict) -> None:
        try:
            path = self._prefs_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[AutoDiary] 保存页面偏好失败：{exc}")

    async def _api_get_prefs(self):
        """GET /prefs  —— 页面当前的字体、字号等偏好"""
        try:
            return jsonify({"ok": True, "data": self._load_prefs()})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] 读取偏好接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def _api_set_prefs(self):
        """POST /prefs   body: {"font": "...", "size": 16}"""
        try:
            payload = await self._payload()
            data = self._load_prefs()
            if isinstance(payload.get("font"), str) and payload["font"].strip():
                font = payload["font"].strip()[:200]
                # 别让分号、花括号之类的东西跑进样式里
                if not re.search(r"[;{}<>]", font):
                    data["font"] = font
            if payload.get("size") is not None:
                try:
                    data["size"] = max(8, min(48, int(payload["size"])))
                except (TypeError, ValueError):
                    pass
            self._save_prefs(data)
            return jsonify({"ok": True, "data": data})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AutoDiary] 保存偏好接口出错")
            return jsonify({"ok": False, "error": str(exc)}), 500

    # ------------------------------------------------------------------ 命令
    @filter.command("写日记")
    async def cmd_write(self, event: AstrMessageEvent):
        """不等空闲，立刻把攒下的对话整理成日记。"""
        if not self.enabled:
            yield event.plain_result("自动日记功能当前已关闭。")
            return
        if self._writing:
            yield event.plain_result("正在整理中，请稍候。")
            return
        if not any(self._queues.values()):
            yield event.plain_result("这段对话还没攒下什么内容。")
            return
        done = 0
        for key in [k for k, v in self._queues.items() if v]:
            try:
                await self._flush(key)
                done += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[AutoDiary] 手动整理失败：{exc}")
        if done:
            yield event.plain_result("已整理并写入今天的日记。")
        else:
            yield event.plain_result("这次没整理成功，详情我记在 debug.log 里了。")

    @staticmethod
    def _command_arg(event, *names: str) -> str:
        """取出命令后面的参数部分。"""
        raw = (getattr(event, "message_str", "") or "").strip()
        for name in names:
            for prefix in (f"/{name}", name):
                if raw.startswith(prefix):
                    return raw[len(prefix) :].strip()
        parts = raw.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @filter.command("暂停日记")
    async def cmd_pause(self, event: AstrMessageEvent):
        """暂停记录（攒着的内容会保留）。"""
        if self._paused:
            yield event.plain_result("本来就没在记呀。")
            return
        self._paused = True
        yield event.plain_result("好，先不记了。想恢复就发 /恢复日记。")

    @filter.command("恢复日记")
    async def cmd_resume(self, event: AstrMessageEvent):
        """恢复记录。"""
        if not self._paused:
            yield event.plain_result("一直开着的哦。")
            return
        self._paused = False
        yield event.plain_result("记着了。……别、别乱说话啊。")

    @filter.command("读日记")
    async def cmd_read(self, event: AstrMessageEvent):
        """看日记：默认今天，也可以翻旧的和搜索。

        用法：
            /读日记              今天的
            /读日记 昨天         昨天的（前天也可以）
            /读日记 2026-10-09   指定日期
            /读日记 最近         列出最近写过的
            /读日记 搜索 关键词   在正文里搜
        """
        arg = self._command_arg(event, "读日记")
        today = date.today()

        # 无参数：今天的
        if not arg:
            text = self.db.read_by_date(today.isoformat(), self.base_tags)
            if not text:
                yield event.plain_result(
                    "今天还没有日记。\n可以发「/读日记 最近」看看以前写了什么。"
                )
                return
            yield event.plain_result(f"今天的日记：\n\n{text[:MAX_REPLY_CHARS]}")
            return

        # 列表
        if arg in ("最近", "列表", "全部", "目录"):
            rows = self.db.list_recent(self.base_tags, LIST_LIMIT)
            if not rows:
                yield event.plain_result("还没有任何日记。")
                return
            lines = [f"· {day}　{title}" for day, title in rows]
            yield event.plain_result(
                "最近写过的日记：\n"
                + "\n".join(lines)
                + "\n\n想看哪一篇，发「/读日记 日期」就行。"
            )
            return

        # 搜索
        for kw in ("搜索", "搜", "查找", "查"):
            if arg.startswith(kw):
                keyword = arg[len(kw) :].strip()
                if not keyword:
                    yield event.plain_result("要搜什么呢？例如「/读日记 搜索 咖啡」。")
                    return
                hits = self.db.search(keyword, self.base_tags, 5)
                if not hits:
                    yield event.plain_result(f"没找到包含「{keyword}」的日记。")
                    return
                lines = [f"· {day}　{title}\n　　{snippet}" for day, title, snippet in hits]
                yield event.plain_result(
                    f"找到 {len(hits)} 篇：\n" + "\n".join(lines)
                )
                return

        # 相对日期
        if arg in ("今天", "昨天", "前天"):
            delta = {"今天": 0, "昨天": 1, "前天": 2}[arg]
            day = (today - timedelta(days=delta)).isoformat()
            text = self.db.read_by_date(day, self.base_tags)
            label = "今天" if delta == 0 else arg
            if not text:
                yield event.plain_result(f"{label}（{day}）没有日记。")
                return
            yield event.plain_result(f"{label}的日记：\n\n{text[:MAX_REPLY_CHARS]}")
            return

        # 完整日期 YYYY-MM-DD
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", arg):
            try:
                day = date.fromisoformat(
                    "-".join(f"{int(p):02d}" if i else p for i, p in enumerate(arg.split("-")))
                ).isoformat()
            except ValueError:
                yield event.plain_result("日期格式不太对哦，例如 2026-10-09。")
                return
            text = self.db.read_by_date(day, self.base_tags)
            if not text:
                yield event.plain_result(f"{day} 没有日记。")
                return
            yield event.plain_result(f"{day} 的日记：\n\n{text[:MAX_REPLY_CHARS]}")
            return

        # 月-日（当年）
        if re.fullmatch(r"\d{1,2}-\d{1,2}", arg):
            month, day_num = arg.split("-")
            day = f"{today.year}-{int(month):02d}-{int(day_num):02d}"
            text = self.db.read_by_date(day, self.base_tags)
            if not text:
                yield event.plain_result(f"{day} 没有日记。")
                return
            yield event.plain_result(f"{day} 的日记：\n\n{text[:MAX_REPLY_CHARS]}")
            return

        # 兜底：当成关键词搜
        hits = self.db.search(arg, self.base_tags, 5)
        if hits:
            lines = [f"· {day}　{title}\n　　{snippet}" for day, title, snippet in hits]
            yield event.plain_result(f"含「{arg}」的日记：\n" + "\n".join(lines))
            return
        yield event.plain_result(
            "没看懂这个参数呢。可以这样用：\n"
            "/读日记　　　　今天的\n"
            "/读日记 昨天　　昨天的\n"
            "/读日记 最近　　列出最近写过的\n"
            "/读日记 搜索 关键词"
        )
