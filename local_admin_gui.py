"""High-DPI local administration console for the offline Soul Tide server."""

import argparse
import ctypes
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SOULTIDE_DB_PATH", ROOT / "soultide.db"))
AUDIT_PATH = ROOT / "local_admin_audit.jsonl"
GAME_DATA_PATH = ROOT / "analysis" / "game_data.json"


def load_game_data():
    defaults = {
        "items": {},
        "souls": {},
        "dresses": {},
        "maze_chapters": {},
        "maze_instances": {},
        "major_activities": {},
        "activities": {},
        "quests": {},
        "soul_stories": {},
        "item_types": {},
    }
    try:
        loaded = json.loads(GAME_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    return {**defaults, **loaded}


game_data = load_game_data()
ITEM_NAMES = game_data["items"]
ITEM_TYPE_NAMES = {
    str(key): value for key, value in game_data.get("item_types", {}).items()
}
SOUL_NAMES = game_data["souls"]
DRESS_NAMES = game_data["dresses"]
MAZE_CHAPTERS = game_data["maze_chapters"]
MAJOR_ACTIVITIES = game_data.get("major_activities", {})
ACTIVITY_NAMES = game_data["activities"]
QUEST_NAMES = game_data["quests"]
STORY_NAMES = game_data["soul_stories"]
ACTIVITY_RUN_STATUSES = {
    "pending": "待测试",
    "running": "进行中",
    "passed": "通过",
    "failed": "失败",
    "blocked": "阻塞",
}
ACTIVITY_RUN_STATUS_CODES = {
    label: code for code, label in ACTIVITY_RUN_STATUSES.items()
}


def enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def now():
    return int(time.time())


def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def backup_db():
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    millis = (time.time_ns() // 1_000_000) % 1000
    target = ROOT / f"{DB_PATH.name}.bak-admin-{timestamp}-{millis:03d}"
    with connect() as source, sqlite3.connect(str(target)) as destination:
        source.backup(destination)
    return target


def audit(action, detail):
    record = {"at": now(), "action": action, "detail": detail}
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_name(records, cid):
    info = records.get(str(cid), {})
    if isinstance(info, dict):
        return info.get("name", "")
    return str(info) if info else ""


def item_name(cid):
    return record_name(ITEM_NAMES, cid)


def item_type_name(info):
    if not isinstance(info, dict):
        return "未分类"
    return info.get("type_name") or ITEM_TYPE_NAMES.get(
        str(info.get("type")), f"其他({info.get('type', '-')})"
    )


def soul_name(cid):
    return record_name(SOUL_NAMES, cid)


def dress_name(cid):
    return record_name(DRESS_NAMES, cid)


def activity_name(cid):
    return record_name(ACTIVITY_NAMES, cid)


def parse_record_id(value):
    text = str(value).strip()
    if not text:
        raise ValueError("请选择记录")
    token = text.split(maxsplit=1)[0]
    number = int(token)
    if number <= 0:
        raise ValueError("CID 必须大于 0")
    return number


def parse_integer(value, label, minimum=0, maximum=None):
    text = str(value).strip()
    if not text:
        raise ValueError(f"请输入{label}")
    number = int(text)
    if number < minimum:
        raise ValueError(f"{label}不能小于 {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label}不能大于 {maximum}")
    return number


def format_record(records, cid, suffix=None):
    name = record_name(records, cid)
    text = f"{cid}  {name}" if name else str(cid)
    return f"{text}  {suffix}" if suffix else text


def filter_record_options(records, query="", extra=None, limit=None):
    needle = query.strip().casefold()
    options = []
    for cid in sorted(records, key=lambda value: int(value)):
        suffix = extra(cid, records[cid]) if extra else None
        text = format_record(records, cid, suffix)
        if needle and needle not in text.casefold():
            continue
        options.append(text)
        if limit and len(options) >= limit:
            break
    return options


def create_local_account(conn, channel_uid, username):
    if not channel_uid.startswith("local-test-"):
        raise ValueError("本地账号 ID 必须以 local-test- 开头")
    if not username:
        raise ValueError("角色名不能为空")
    created_at = now()
    uid = hashlib.md5(channel_uid.encode("utf-8")).hexdigest()
    stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "soultide:" + channel_uid))
    conn.execute(
        """
        INSERT INTO accounts(
            channel_uid,uid,uuid,username,channel_id,created_at,
            last_http_login_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (channel_uid, uid, stable_uuid, username, "46", created_at, created_at, created_at),
    )
    conn.execute(
        """
        INSERT INTO players(uid,role_id,role_name,level,snapshot_mode,updated_at)
        VALUES(?,?,?,?,?,?)
        """,
        (uid, str(int(uid[:16], 16)), username, 1, "local", created_at),
    )
    conn.execute(
        "INSERT INTO currencies(uid,gold,souls,updated_at) VALUES(?,?,?,?)",
        (uid, 1000, 0, created_at),
    )
    conn.execute(
        "INSERT INTO souls(uid,soul_id,level,affection,created_at) VALUES(?,?,?,?,?)",
        (uid, 20010001, 1, 0, created_at),
    )
    return uid


def delete_local_account(conn, uid):
    tables = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'accounts'"
    ).fetchall()
    for row in tables:
        table = row[0]
        columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if any(column[1] == "uid" for column in columns):
            escaped = table.replace('"', '""')
            conn.execute(f'DELETE FROM "{escaped}" WHERE uid=?', (uid,))
    conn.execute("DELETE FROM accounts WHERE uid=?", (uid,))


def ensure_item_unique_key(conn):
    conn.execute(
        """
        UPDATE items
        SET quantity=(
            SELECT SUM(other.quantity)
            FROM items AS other
            WHERE other.uid=items.uid AND other.template_id=items.template_id
        )
        WHERE id=(
            SELECT MIN(other.id)
            FROM items AS other
            WHERE other.uid=items.uid AND other.template_id=items.template_id
        )
        """
    )
    conn.execute(
        "DELETE FROM items WHERE id NOT IN ("
        "SELECT MIN(id) FROM items GROUP BY uid,template_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_uid_template "
        "ON items(uid,template_id)"
    )


def upsert_item(conn, uid, cid, quantity):
    conn.execute(
        """
        INSERT INTO items(uid,template_id,quantity,created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(uid,template_id) DO UPDATE SET
            quantity=items.quantity+excluded.quantity
        """,
        (uid, cid, quantity, now()),
    )


def remove_item_quantity(conn, uid, cid, quantity):
    conn.execute(
        "UPDATE items SET quantity=MAX(0,quantity-?) "
        "WHERE uid=? AND template_id=?",
        (quantity, uid, cid),
    )
    conn.execute(
        "DELETE FROM items WHERE uid=? AND template_id=? AND quantity<=0",
        (uid, cid),
    )


def set_currency(conn, uid, field, value):
    if field not in {"gold", "souls"}:
        raise ValueError("未知货币字段")
    other = "souls" if field == "gold" else "gold"
    conn.execute(
        f"""
        INSERT INTO currencies(uid,{field},{other},updated_at)
        VALUES(?,?,0,?)
        ON CONFLICT(uid) DO UPDATE SET
            {field}=excluded.{field},
            updated_at=excluded.updated_at
        """,
        (uid, value, now()),
    )


def update_player_profile(conn, uid, role_name, level):
    cursor = conn.execute(
        "UPDATE players SET role_name=?,level=?,updated_at=? WHERE uid=?",
        (role_name, level, now(), uid),
    )
    conn.execute("UPDATE accounts SET username=? WHERE uid=?", (role_name, uid))
    if cursor.rowcount != 1:
        raise ValueError("玩家记录不存在")


def upsert_soul(conn, uid, soul_id, level, favor):
    conn.execute(
        """
        INSERT INTO souls(uid,soul_id,level,favor,affection,favor_level,created_at)
        VALUES(?,?,?,?,?,1,?)
        ON CONFLICT(uid,soul_id) DO UPDATE SET
            level=excluded.level,
            favor=excluded.favor,
            affection=excluded.affection
        """,
        (uid, soul_id, level, favor, favor, now()),
    )


def set_current_show_soul(conn, uid, soul_id):
    owned = conn.execute(
        "SELECT 1 FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)
    ).fetchone()
    if not owned:
        raise ValueError("该账号尚未解锁此人偶")
    conn.execute(
        "UPDATE players SET current_show_soul_cid=?,updated_at=? WHERE uid=?",
        (soul_id, now(), uid),
    )


def set_current_dress(conn, uid, dress_cid):
    cursor = conn.execute(
        "UPDATE players SET current_dress_cid=?,updated_at=? WHERE uid=?",
        (dress_cid, now(), uid),
    )
    if cursor.rowcount != 1:
        raise ValueError("玩家记录不存在")


def complete_quest(conn, uid, quest_cid):
    timestamp = now()
    conn.execute(
        "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) "
        "VALUES(?,?,?)",
        (uid, "finish", quest_cid),
    )
    conn.execute(
        """
        INSERT INTO quest_progress(uid,quest_cid,fin_num,tgt_num,create_time)
        VALUES(?,?,1,1,?)
        ON CONFLICT(uid,quest_cid) DO UPDATE SET
            fin_num=MAX(quest_progress.fin_num,quest_progress.tgt_num,1),
            tgt_num=MAX(quest_progress.tgt_num,1)
        """,
        (uid, quest_cid, timestamp),
    )


def unlock_chapter(conn, uid, chapter_cid):
    conn.execute(
        "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
        (uid, "unlock", chapter_cid),
    )


def set_story_progress(conn, uid, story_cid, chapter_index):
    conn.execute(
        """
        INSERT INTO soul_story_progress(
            uid,story_cid,highest_chapter_index,updated_at
        ) VALUES(?,?,?,?)
        ON CONFLICT(uid,story_cid) DO UPDATE SET
            highest_chapter_index=MAX(
                soul_story_progress.highest_chapter_index,
                excluded.highest_chapter_index
            ),
            updated_at=excluded.updated_at
        """,
        (uid, story_cid, chapter_index, now()),
    )


def set_event_state(conn, uid, cid, enabled):
    row = conn.execute(
        "SELECT value_json FROM player_state_json "
        "WHERE uid=? AND field_name='opEventsStatus'",
        (uid,),
    ).fetchone()
    state = json.loads(row[0]) if row else {}
    state[str(cid)] = 1 if enabled else 0
    conn.execute(
        """
        INSERT INTO player_state_json(uid,field_name,value_json,updated_at)
        VALUES(?,'opEventsStatus',?,?)
        ON CONFLICT(uid,field_name) DO UPDATE SET
            value_json=excluded.value_json,
            updated_at=excluded.updated_at
        """,
        (uid, json.dumps(state, ensure_ascii=False, separators=(",", ":")), now()),
    )


def ensure_player_state_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS player_state_json(
            uid TEXT NOT NULL,
            field_name TEXT NOT NULL,
            value_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(uid,field_name)
        )
        """
    )


def acquire_dress(conn, uid, dress_cid):
    if str(dress_cid) not in DRESS_NAMES:
        raise ValueError("服装 CID 不存在于当前解包配置")
    ensure_player_state_table(conn)
    row = conn.execute(
        "SELECT value_json FROM player_state_json WHERE uid=? AND field_name='dresses'",
        (uid,),
    ).fetchone()
    dresses = json.loads(row[0]) if row else []
    if not isinstance(dresses, list):
        dresses = []
    found = False
    for dress in dresses:
        if isinstance(dress, dict) and int(dress.get("dressCid", 0)) == dress_cid:
            dress.update({"dressCid": dress_cid, "expireTime": 0, "isNew": True})
            found = True
            break
    if not found:
        dresses.append({"dressCid": dress_cid, "expireTime": 0, "isNew": True})
    conn.execute(
        """
        INSERT INTO player_state_json(uid,field_name,value_json,updated_at)
        VALUES(?,'dresses',?,?)
        ON CONFLICT(uid,field_name) DO UPDATE SET
            value_json=excluded.value_json,updated_at=excluded.updated_at
        """,
        (uid, json.dumps(dresses, ensure_ascii=False, separators=(",", ":")), now()),
    )


def create_major_activity_runs(conn, uid, chapter_cid):
    chapter = MAJOR_ACTIVITIES.get(str(chapter_cid))
    if not chapter:
        raise ValueError("请选择已解包的大型活动章节")
    ensure_activity_run_table(conn)
    created = 0
    chapter_name = chapter.get("name") or f"章节 {chapter_cid}"
    for maze_cid, maze in sorted(
        chapter.get("mazes", {}).items(), key=lambda pair: int(pair[0])
    ):
        test_id = f"local-activity-major-{uid[:8]}-{chapter_cid}-{maze_cid}"
        exists = conn.execute(
            "SELECT 1 FROM local_activity_runs WHERE test_id=?", (test_id,)
        ).fetchone()
        if exists:
            continue
        maze_name = maze.get("name") or f"关卡 {maze_cid}"
        save_activity_run(
            conn,
            None,
            test_id,
            uid,
            int(maze_cid),
            f"{chapter_name} / {maze_name}",
            "pending",
            "大型活动回归关卡",
        )
        created += 1
    return created


def ensure_activity_run_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_activity_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id TEXT NOT NULL UNIQUE,
            uid TEXT NOT NULL,
            activity_cid INTEGER NOT NULL,
            activity_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_activity_runs_uid_updated "
        "ON local_activity_runs(uid,updated_at DESC)"
    )


def save_activity_run(
    conn, run_id, test_id, uid, activity_cid, activity_title, status, notes
):
    if not test_id.startswith("local-activity-"):
        raise ValueError("活动测试 ID 必须以 local-activity- 开头")
    if status not in ACTIVITY_RUN_STATUSES:
        raise ValueError("未知活动测试状态")
    ensure_activity_run_table(conn)
    timestamp = now()
    if run_id is None:
        cursor = conn.execute(
            """
            INSERT INTO local_activity_runs(
                test_id,uid,activity_cid,activity_name,status,notes,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                test_id,
                uid,
                activity_cid,
                activity_title,
                status,
                notes,
                timestamp,
                timestamp,
            ),
        )
        return cursor.lastrowid
    cursor = conn.execute(
        """
        UPDATE local_activity_runs
        SET test_id=?,activity_cid=?,activity_name=?,status=?,notes=?,updated_at=?
        WHERE id=? AND uid=?
        """,
        (
            test_id,
            activity_cid,
            activity_title,
            status,
            notes,
            timestamp,
            run_id,
            uid,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("活动回归记录不存在或不属于当前账号")
    return run_id


def delete_activity_run(conn, run_id, uid):
    ensure_activity_run_table(conn)
    cursor = conn.execute(
        "DELETE FROM local_activity_runs WHERE id=? AND uid=?", (run_id, uid)
    )
    if cursor.rowcount != 1:
        raise ValueError("活动回归记录不存在或不属于当前账号")


class AdminApp:
    def __init__(self, writable=False):
        self.writable = writable
        self.selected_uid = None
        self.account_rows = {}
        self.account_display_to_uid = {}
        self.selected_activity_run_id = None
        self.activity_run_rows = {}

        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("灵魂潮汐本地管理后台")
        self._configure_window()
        self._configure_style()
        self._build_shell()
        self.refresh_all()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _configure_window(self):
        self.root.update_idletasks()
        dpi = float(self.root.winfo_fpixels("1i"))
        self.ui_scale = max(1.0, min(2.5, dpi / 96.0))
        self.root.tk.call("tk", "scaling", dpi / 72.0)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(int(1280 * self.ui_scale), int(screen_width * 0.92))
        height = min(int(820 * self.ui_scale), int(screen_height * 0.90))
        width = max(width, min(int(1040 * self.ui_scale), screen_width))
        height = max(height, min(int(640 * self.ui_scale), screen_height))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(
            min(int(1040 * self.ui_scale), screen_width),
            min(int(620 * self.ui_scale), screen_height),
        )
        if os.name == "nt" and (screen_width >= 1600 or screen_height >= 1000):
            self.root.state("zoomed")

    def _configure_style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family="Microsoft YaHei UI", size=10)
        text_font = tkfont.nametofont("TkTextFont")
        text_font.configure(family="Microsoft YaHei UI", size=10)
        heading_font = tkfont.nametofont("TkHeadingFont")
        heading_font.configure(family="Microsoft YaHei UI", size=10, weight="bold")
        style.configure("TButton", padding=(10, 6))
        style.configure("Danger.TButton", foreground="#9b1c1c")
        style.configure("Treeview", rowheight=max(28, int(28 * self.ui_scale)))
        style.configure("Treeview.Heading", padding=(6, 6))
        style.configure("TNotebook.Tab", padding=(14, 8))
        style.configure("Header.TFrame", background="#f4f6f8")
        style.configure(
            "Title.TLabel",
            background="#f4f6f8",
            foreground="#17212b",
            font=("Microsoft YaHei UI", 14, "bold"),
        )
        style.configure(
            "Header.TLabel", background="#f4f6f8", foreground="#3f4d5a"
        )
        style.configure(
            "Writable.TLabel",
            background="#dff3e4",
            foreground="#176b35",
            padding=(8, 4),
        )
        style.configure(
            "Readonly.TLabel",
            background="#e8ebef",
            foreground="#4d5965",
            padding=(8, 4),
        )
        style.configure("Status.TLabel", foreground="#425466")
        style.configure("Error.TLabel", foreground="#a61b1b")
        style.configure("Metric.TLabel", font=("Microsoft YaHei UI", 12, "bold"))

    def _build_shell(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(16, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="灵魂潮汐本地管理后台", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        mode_style = "Writable.TLabel" if self.writable else "Readonly.TLabel"
        mode_text = "本地写入" if self.writable else "只读"
        ttk.Label(header, text=mode_text, style=mode_style).grid(
            row=0, column=3, sticky="e"
        )
        ttk.Label(header, text="账号", style="Header.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0)
        )
        self.account_var = tk.StringVar()
        self.account_combo = ttk.Combobox(
            header, textvariable=self.account_var, state="readonly", width=42
        )
        self.account_combo.grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=(10, 0)
        )
        self.account_combo.bind("<<ComboboxSelected>>", self._select_account_combo)
        ttk.Button(header, text="刷新全部", command=self.refresh_all).grid(
            row=1, column=3, sticky="e", pady=(10, 0)
        )

        summary = ttk.Frame(header, style="Header.TFrame")
        summary.grid(
            row=2, column=0, columnspan=4, sticky="ew", pady=(9, 0)
        )
        self.summary_name = tk.StringVar(value="未选择账号")
        self.summary_level = tk.StringVar(value="等级 -")
        self.summary_gold = tk.StringVar(value="金币 -")
        self.summary_souls = tk.StringVar(value="魂石 -")
        self.summary_dress = tk.StringVar(value="服装 -")
        for column, variable in enumerate(
            (
                self.summary_name,
                self.summary_level,
                self.summary_gold,
                self.summary_souls,
                self.summary_dress,
            )
        ):
            ttk.Label(
                summary, textvariable=variable, style="Header.TLabel"
            ).grid(
                row=0, column=column, padx=(0, 18), sticky="e"
            )

        self.note = ttk.Notebook(self.root)
        self.note.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 6))
        self.frames = {}
        for name in ("账号", "资源", "人偶", "进度", "背包", "服装", "活动", "审计"):
            frame = ttk.Frame(self.note, padding=10)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(1, weight=1)
            self.frames[name] = frame
            self.note.add(frame, text=name)

        self._build_accounts()
        self._build_resources()
        self._build_souls()
        self._build_progress()
        self._build_inventory()
        self._build_dresses()
        self._build_events()
        self._build_audit()

        footer = ttk.Frame(self.root, padding=(14, 5))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(
            footer, textvariable=self.status_var, style="Status.TLabel"
        )
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(
            footer,
            text=f"{DB_PATH}  |  数据 {len(ITEM_NAMES):,} 道具 / "
            f"{len(SOUL_NAMES):,} 人偶 / {len(DRESS_NAMES):,} 服装",
            style="Status.TLabel",
        ).grid(row=0, column=1, sticky="e")

    def _write_button(self, parent, text, command, danger=False):
        button = ttk.Button(
            parent,
            text=text,
            command=command,
            style="Danger.TButton" if danger else "TButton",
        )
        if not self.writable:
            button.state(["disabled"])
        return button

    def _make_tree(self, parent, columns):
        holder = ttk.Frame(parent)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            holder, columns=tuple(column[0] for column in columns), show="headings"
        )
        vertical = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        for key, title, width, stretch in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=max(60, width // 2), stretch=stretch)
        return holder, tree

    def _set_status(self, text, error=False):
        self.status_var.set(text)
        self.status_label.configure(style="Error.TLabel" if error else "Status.TLabel")

    def _input_error(self, error):
        self._set_status(str(error), error=True)
        messagebox.showwarning("输入有误", str(error), parent=self.root)

    def _uid(self):
        if not self.selected_uid:
            self._input_error(ValueError("请先选择账号"))
            return None
        return self.selected_uid

    def _mutate(self, action, detail, callback):
        if not self.writable:
            self._input_error(ValueError("当前为只读模式"))
            return False
        account = self.account_rows.get(self.selected_uid, {})
        account_name = account.get("role_name") or account.get("username") or ""
        target_name = detail.get("channel_uid") if action == "创建账号" else account_name
        prompt = f"{action}\n\n账号: {target_name or '新账号'}"
        if not messagebox.askyesno("确认操作", prompt, parent=self.root):
            return False
        backup = backup_db()
        try:
            with connect() as conn:
                callback(conn)
            audit(action, {**detail, "backup": backup.name})
        except Exception as exc:
            self._set_status(f"{action}失败: {exc}", error=True)
            messagebox.showerror("操作失败", str(exc), parent=self.root)
            return False
        self.refresh_all()
        self._set_status(f"{action}完成，备份 {backup.name}")
        return True

    def _bind_combo_filter(self, combo, records, extra=None):
        all_options = filter_record_options(records, extra=extra)
        combo.configure(values=all_options)

        def update(_event=None):
            combo.configure(
                values=filter_record_options(records, combo.get(), extra=extra, limit=500)
            )

        combo.bind("<KeyRelease>", update)

    def _select_account_combo(self, _event=None):
        uid = self.account_display_to_uid.get(self.account_var.get())
        if uid:
            self.select_account(uid)

    def select_account(self, uid):
        if uid not in self.account_rows:
            return
        self.selected_uid = uid
        display = next(
            (text for text, value in self.account_display_to_uid.items() if value == uid),
            "",
        )
        if display:
            self.account_var.set(display)
        self._update_account_summary()
        self._populate_account_details()
        self._refresh_selected_tabs()
        selected_items = self.account_tree.selection()
        selected_uid = (
            self.account_tree.item(selected_items[0], "values")[0]
            if selected_items
            else None
        )
        if selected_uid == uid:
            return
        for item in self.account_tree.get_children():
            if self.account_tree.item(item, "values")[0] == uid:
                self.account_tree.selection_set(item)
                self.account_tree.see(item)
                break

    def refresh_all(self):
        self.refresh_accounts()
        self._refresh_selected_tabs()
        self.refresh_audit()
        self._set_status("数据已刷新")

    def _refresh_selected_tabs(self):
        self.refresh_resources()
        self.refresh_souls()
        self.refresh_progress()
        self.refresh_inventory()
        self.refresh_dresses()
        self.refresh_events()

    def _update_account_summary(self):
        row = self.account_rows.get(self.selected_uid)
        if not row:
            self.summary_name.set("未选择账号")
            self.summary_level.set("等级 -")
            self.summary_gold.set("金币 -")
            self.summary_souls.set("魂石 -")
            self.summary_dress.set("服装 -")
            return
        self.summary_name.set(row.get("role_name") or row.get("username") or "-")
        self.summary_level.set(f"等级 {row.get('level') or '-'}")
        gold = row.get("gold")
        souls = row.get("souls")
        self.summary_gold.set(f"金币 {gold:,}" if gold is not None else "金币 -")
        self.summary_souls.set(f"魂石 {souls:,}" if souls is not None else "魂石 -")
        dress_cid = row.get("current_dress_cid")
        self.summary_dress.set(
            f"服装 {dress_name(dress_cid) or dress_cid}" if dress_cid else "服装 -"
        )

    def _build_accounts(self):
        frame = self.frames["账号"]
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="筛选").grid(row=0, column=0, padx=(0, 6))
        self.account_search = tk.StringVar()
        account_search = ttk.Entry(toolbar, textvariable=self.account_search)
        account_search.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.account_search.trace_add("write", lambda *_: self.refresh_accounts())
        self._write_button(toolbar, "创建账号", self.create_account).grid(
            row=0, column=2, padx=(0, 6)
        )
        self._write_button(
            toolbar, "删除账号", self.delete_account, danger=True
        ).grid(row=0, column=3)

        body = ttk.Panedwindow(frame, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")
        tree_holder, self.account_tree = self._make_tree(
            body,
            (
                ("uid", "UID", 230, False),
                ("name", "角色名", 150, True),
                ("channel", "本地账号 ID", 180, False),
                ("level", "等级", 70, False),
                ("last", "最近活动", 150, False),
            ),
        )
        body.add(tree_holder, weight=3)
        self.account_tree.bind("<<TreeviewSelect>>", self._on_account_tree_select)

        details = ttk.Frame(body, padding=(4, 10))
        details.columnconfigure(1, weight=1)
        self.account_detail_vars = {
            key: tk.StringVar(value="-")
            for key in (
                "channel",
                "uid",
                "uuid",
                "role_id",
                "created",
                "last_seen",
                "gift_remaining",
                "gift_daily_max",
                "fondle_num",
                "fondle_recovery",
                "companion_day",
            )
        }
        labels = (
            ("本地账号 ID", "channel"),
            ("UID", "uid"),
            ("UUID", "uuid"),
            ("角色 ID", "role_id"),
            ("创建时间", "created"),
            ("最近活动", "last_seen"),
            ("赠礼剩余次数", "gift_remaining"),
            ("赠礼每日上限", "gift_daily_max"),
            ("今日抚摸次数", "fondle_num"),
            ("抚摸恢复时间", "fondle_recovery"),
            ("相伴计数日期", "companion_day"),
        )
        for row, (title, key) in enumerate(labels):
            ttk.Label(details, text=title).grid(
                row=row, column=0, sticky="nw", padx=(0, 8), pady=3
            )
            ttk.Label(
                details, textvariable=self.account_detail_vars[key], wraplength=520
            ).grid(row=row, column=1, sticky="nw", pady=3)

        editor = ttk.Frame(details)
        editor.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        editor.columnconfigure(1, weight=1)
        self.profile_name_var = tk.StringVar()
        self.profile_level_var = tk.StringVar()
        ttk.Label(editor, text="角色名").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(editor, textvariable=self.profile_name_var).grid(
            row=0, column=1, columnspan=3, sticky="ew"
        )
        ttk.Label(editor, text="等级").grid(
            row=1, column=0, padx=(0, 6), pady=(8, 0)
        )
        ttk.Entry(editor, textvariable=self.profile_level_var, width=8).grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        self._write_button(editor, "保存角色", self.save_profile).grid(
            row=1, column=3, sticky="e", pady=(8, 0)
        )
        body.add(details, weight=1)

    def refresh_accounts(self):
        if not hasattr(self, "account_tree"):
            return
        query = self.account_search.get().strip().casefold()
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*,p.role_id,p.role_name,p.level,p.current_dress_cid,
                       p.current_show_soul_cid,p.remainder_give_gift_num,
                       p.give_gift_daily_max,p.fondle_num,
                       p.next_recovery_fondle_time,p.companion_reset_day,
                       c.gold,c.souls
                FROM accounts AS a
                LEFT JOIN players AS p ON p.uid=a.uid
                LEFT JOIN currencies AS c ON c.uid=a.uid
                ORDER BY COALESCE(a.last_seen_at,a.created_at) DESC
                """
            ).fetchall()
        self.account_rows = {row["uid"]: dict(row) for row in rows}
        self.account_tree.delete(*self.account_tree.get_children())
        combo_values = []
        self.account_display_to_uid = {}
        for row in rows:
            data = dict(row)
            haystack = " ".join(
                str(data.get(key) or "")
                for key in ("uid", "channel_uid", "username", "role_name", "uuid")
            ).casefold()
            display = (
                f"{data.get('role_name') or data.get('username') or '-'}  "
                f"[{data['channel_uid']}]"
            )
            combo_values.append(display)
            self.account_display_to_uid[display] = data["uid"]
            if query and query not in haystack:
                continue
            last = data.get("last_seen_at")
            last_text = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "-"
            )
            self.account_tree.insert(
                "",
                "end",
                values=(
                    data["uid"],
                    data.get("role_name") or data.get("username") or "",
                    data["channel_uid"],
                    data.get("level") or "",
                    last_text,
                ),
            )
        self.account_combo.configure(values=combo_values)
        if self.selected_uid not in self.account_rows:
            self.selected_uid = rows[0]["uid"] if rows else None
        if self.selected_uid:
            self.select_account(self.selected_uid)
        else:
            self.account_var.set("")
            self._update_account_summary()

    def _on_account_tree_select(self, _event=None):
        selection = self.account_tree.selection()
        if selection:
            uid = self.account_tree.item(selection[0], "values")[0]
            if uid != self.selected_uid:
                self.select_account(uid)

    def _populate_account_details(self):
        row = self.account_rows.get(self.selected_uid, {})

        def timestamp(value):
            return (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
                if value
                else "-"
            )

        values = {
            "channel": row.get("channel_uid") or "-",
            "uid": row.get("uid") or "-",
            "uuid": row.get("uuid") or "-",
            "role_id": row.get("role_id") or "-",
            "created": timestamp(row.get("created_at")),
            "last_seen": timestamp(row.get("last_seen_at")),
            "gift_remaining": (
                str(row["remainder_give_gift_num"])
                if row.get("remainder_give_gift_num") is not None
                else "未记录"
            ),
            "gift_daily_max": (
                str(row["give_gift_daily_max"])
                if row.get("give_gift_daily_max") is not None
                else "未记录"
            ),
            "fondle_num": (
                str(row["fondle_num"])
                if row.get("fondle_num") is not None
                else "未记录"
            ),
            "fondle_recovery": (
                timestamp(row["next_recovery_fondle_time"])
                if row.get("next_recovery_fondle_time")
                else "未记录"
            ),
            "companion_day": row.get("companion_reset_day") or "未记录",
        }
        for key, value in values.items():
            self.account_detail_vars[key].set(value)
        self.profile_name_var.set(row.get("role_name") or row.get("username") or "")
        self.profile_level_var.set(str(row.get("level") or ""))

    def create_account(self):
        if not self.writable:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("创建本地账号")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        content = ttk.Frame(dialog, padding=18)
        content.grid(sticky="nsew")
        channel_var = tk.StringVar()
        name_var = tk.StringVar()
        ttk.Label(content, text="本地账号 ID").grid(
            row=0, column=0, sticky="w", pady=5
        )
        ttk.Entry(content, textvariable=channel_var, width=38).grid(
            row=0, column=1, sticky="ew", padx=(8, 6), pady=5
        )
        ttk.Button(
            content,
            text="生成",
            command=lambda: channel_var.set(f"local-test-{now()}"),
        ).grid(row=0, column=2, pady=5)
        ttk.Label(content, text="角色名").grid(row=1, column=0, sticky="w", pady=5)
        name_entry = ttk.Entry(content, textvariable=name_var, width=38)
        name_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=5)

        def submit():
            channel_uid = channel_var.get().strip()
            username = name_var.get().strip()
            try:
                if not channel_uid:
                    raise ValueError("请输入或生成本地账号 ID")
                if not username:
                    raise ValueError("请输入角色名")
            except ValueError as exc:
                messagebox.showwarning("输入有误", str(exc), parent=dialog)
                return
            uid = hashlib.md5(channel_uid.encode("utf-8")).hexdigest()
            if self._mutate(
                "创建账号",
                {"channel_uid": channel_uid, "uid": uid},
                lambda conn: create_local_account(conn, channel_uid, username),
            ):
                self.selected_uid = uid
                self.refresh_accounts()
                dialog.destroy()

        buttons = ttk.Frame(content)
        buttons.grid(row=2, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).grid(
            row=0, column=0, padx=(0, 6)
        )
        ttk.Button(buttons, text="创建", command=submit).grid(row=0, column=1)
        name_entry.focus_set()

    def delete_account(self):
        uid = self._uid()
        if uid and self._mutate(
            "删除账号", {"uid": uid}, lambda conn: delete_local_account(conn, uid)
        ):
            self.selected_uid = None
            self.refresh_accounts()

    def save_profile(self):
        uid = self._uid()
        if not uid:
            return
        try:
            role_name = self.profile_name_var.get().strip()
            if not role_name:
                raise ValueError("角色名不能为空")
            level = parse_integer(self.profile_level_var.get(), "等级", 1, 999)
        except ValueError as exc:
            self._input_error(exc)
            return
        self._mutate(
            "保存角色",
            {"uid": uid, "role_name": role_name, "level": level},
            lambda conn: update_player_profile(conn, uid, role_name, level),
        )

    def _build_resources(self):
        frame = self.frames["资源"]
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(0, weight=1)

        currency = ttk.Labelframe(toolbar, text="货币", padding=10)
        currency.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        currency.columnconfigure(1, weight=1)
        currency.columnconfigure(4, weight=1)
        self.gold_current = tk.StringVar(value="-")
        self.souls_current = tk.StringVar(value="-")
        self.gold_var = tk.StringVar()
        self.souls_var = tk.StringVar()
        ttk.Label(currency, text="金币", style="Metric.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(currency, textvariable=self.gold_current).grid(
            row=0, column=1, sticky="w", padx=(8, 16)
        )
        ttk.Entry(currency, textvariable=self.gold_var, width=16).grid(
            row=0, column=2, padx=(0, 6)
        )
        self._write_button(currency, "设置", self.set_gold).grid(row=0, column=3)
        ttk.Label(currency, text="魂石", style="Metric.TLabel").grid(
            row=0, column=4, sticky="e", padx=(22, 0)
        )
        ttk.Label(currency, textvariable=self.souls_current).grid(
            row=0, column=5, sticky="w", padx=(8, 16)
        )
        ttk.Entry(currency, textvariable=self.souls_var, width=16).grid(
            row=0, column=6, padx=(0, 6)
        )
        self._write_button(currency, "设置", self.set_souls).grid(row=0, column=7)

        item_tool = ttk.Labelframe(frame, text="选中道具调整", padding=10)
        item_tool.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        item_tool.columnconfigure(1, weight=1)
        self.item_var = tk.StringVar()
        self.item_qty_var = tk.StringVar()
        ttk.Label(item_tool, text="道具").grid(row=0, column=0, padx=(0, 6))
        self.item_combo = ttk.Combobox(item_tool, textvariable=self.item_var)
        self.item_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self._bind_combo_filter(self.item_combo, ITEM_NAMES)
        ttk.Label(item_tool, text="数量").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(item_tool, textvariable=self.item_qty_var, width=12).grid(
            row=0, column=3, padx=(0, 8)
        )
        self._write_button(item_tool, "添加", self.add_item).grid(
            row=0, column=4, padx=(0, 6)
        )
        self._write_button(
            item_tool, "移除", self.remove_item, danger=True
        ).grid(row=0, column=5)

        filters = ttk.Frame(frame)
        filters.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        filters.columnconfigure(3, weight=1)
        ttk.Label(filters, text="类型").grid(row=0, column=0, padx=(0, 6))
        self.resource_type_var = tk.StringVar(value="全部类型")
        type_values = ["全部类型"] + sorted(set(ITEM_TYPE_NAMES.values()))
        ttk.Combobox(
            filters,
            textvariable=self.resource_type_var,
            values=type_values,
            state="readonly",
            width=16,
        ).grid(row=0, column=1, padx=(0, 16))
        ttk.Label(filters, text="搜索").grid(row=0, column=2, padx=(0, 6))
        self.resource_search = tk.StringVar()
        ttk.Entry(filters, textvariable=self.resource_search).grid(
            row=0, column=3, sticky="ew", padx=(0, 12)
        )
        self.resource_count_var = tk.StringVar()
        ttk.Label(filters, textvariable=self.resource_count_var).grid(
            row=0, column=4, sticky="e"
        )
        self.resource_search.trace_add("write", lambda *_: self.refresh_resources())
        self.resource_type_var.trace_add("write", lambda *_: self.refresh_resources())

        holder, self.resource_tree = self._make_tree(
            frame,
            (
                ("cid", "道具 CID", 150, False),
                ("name", "名称", 360, True),
                ("type", "类型", 180, False),
                ("quality", "品质", 90, False),
                ("quantity", "当前持有", 150, False),
            ),
        )
        holder.grid(row=3, column=0, sticky="nsew")
        frame.rowconfigure(3, weight=1)
        self.resource_tree.bind("<<TreeviewSelect>>", self._on_resource_select)

    def refresh_resources(self):
        if not hasattr(self, "resource_tree"):
            return
        self.resource_tree.delete(*self.resource_tree.get_children())
        self.gold_current.set("-")
        self.souls_current.set("-")
        uid = self.selected_uid
        if not uid:
            return
        with connect() as conn:
            currency = conn.execute(
                "SELECT gold,souls FROM currencies WHERE uid=?", (uid,)
            ).fetchone()
            attrs = conn.execute(
                "SELECT cid,quantity FROM player_num_attrs WHERE uid=? ORDER BY cid",
                (uid,),
            ).fetchall()
            items = conn.execute(
                "SELECT template_id,quantity FROM items WHERE uid=?", (uid,)
            ).fetchall()
        if currency:
            self.gold_current.set(f"{currency['gold']:,}")
            self.souls_current.set(f"{currency['souls']:,}")
        quantities = {int(row["template_id"]): int(row["quantity"]) for row in items}
        for row in attrs:
            quantities.setdefault(int(row["cid"]), int(row["quantity"]))
        needle = self.resource_search.get().strip().casefold()
        selected_type = self.resource_type_var.get()
        shown = 0
        for cid in sorted(ITEM_NAMES, key=int):
            info = ITEM_NAMES[cid]
            name = item_name(cid)
            type_name = item_type_name(info)
            if selected_type != "全部类型" and type_name != selected_type:
                continue
            if needle and needle not in f"{cid} {name} {type_name}".casefold():
                continue
            self.resource_tree.insert(
                "",
                "end",
                values=(
                    cid,
                    name or "（配置未命名）",
                    type_name,
                    info.get("quality", "") if isinstance(info, dict) else "",
                    f"{quantities.get(int(cid), 0):,}",
                ),
            )
            shown += 1
        self.resource_count_var.set(f"显示 {shown:,} / {len(ITEM_NAMES):,}")

    def _on_resource_select(self, _event=None):
        selection = self.resource_tree.selection()
        if not selection:
            return
        cid = self.resource_tree.item(selection[0], "values")[0]
        self.item_var.set(format_record(ITEM_NAMES, cid))

    def set_gold(self):
        self._set_currency("gold", self.gold_var, "金币")

    def set_souls(self):
        self._set_currency("souls", self.souls_var, "魂石")

    def _set_currency(self, field, variable, label):
        uid = self._uid()
        if not uid:
            return
        try:
            value = parse_integer(variable.get(), label, 0)
        except ValueError as exc:
            self._input_error(exc)
            return
        if self._mutate(
            f"设置{label}",
            {"uid": uid, field: value},
            lambda conn: set_currency(conn, uid, field, value),
        ):
            variable.set("")

    def add_item(self):
        self._change_item(add=True)

    def remove_item(self):
        self._change_item(add=False)

    def _change_item(self, add):
        uid = self._uid()
        if not uid:
            return
        try:
            cid = parse_record_id(self.item_var.get())
            quantity = parse_integer(self.item_qty_var.get(), "数量", 1)
        except ValueError as exc:
            self._input_error(exc)
            return

        def mutate(conn):
            ensure_item_unique_key(conn)
            if add:
                upsert_item(conn, uid, cid, quantity)
            else:
                remove_item_quantity(conn, uid, cid, quantity)

        action = "添加道具" if add else "移除道具"
        if self._mutate(
            action, {"uid": uid, "cid": cid, "quantity": quantity}, mutate
        ):
            self.item_qty_var.set("")

    def _build_souls(self):
        frame = self.frames["人偶"]
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="筛选").grid(row=0, column=0, padx=(0, 6))
        self.soul_search = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.soul_search).grid(
            row=0, column=1, sticky="ew", padx=(0, 12)
        )
        self.soul_search.trace_add("write", lambda *_: self.refresh_souls())

        editor = ttk.Labelframe(frame, text="人偶编辑", padding=10)
        editor.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        editor.columnconfigure(1, weight=1)
        self.soul_var = tk.StringVar()
        self.soul_level_var = tk.StringVar()
        self.soul_favor_var = tk.StringVar()
        ttk.Label(editor, text="人偶").grid(row=0, column=0, padx=(0, 6))
        self.soul_combo = ttk.Combobox(editor, textvariable=self.soul_var)
        self.soul_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self._bind_combo_filter(self.soul_combo, SOUL_NAMES)
        ttk.Label(editor, text="等级").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(editor, textvariable=self.soul_level_var, width=10).grid(
            row=0, column=3, padx=(0, 12)
        )
        ttk.Label(editor, text="好感").grid(row=0, column=4, padx=(0, 6))
        ttk.Entry(editor, textvariable=self.soul_favor_var, width=12).grid(
            row=0, column=5, padx=(0, 12)
        )
        self._write_button(editor, "保存", self.save_soul).grid(
            row=0, column=6, padx=(0, 6)
        )
        self._write_button(editor, "设为展示", self.set_show_soul).grid(
            row=0, column=7
        )

        holder, self.soul_tree = self._make_tree(
            frame,
            (
                ("cid", "人偶 CID", 130, False),
                ("name", "名称", 180, True),
                ("level", "等级", 80, False),
                ("favor", "好感", 110, False),
                ("favor_level", "好感等级", 110, False),
                ("oath", "誓约", 80, False),
                ("show", "展示", 80, False),
            ),
        )
        holder.grid(row=2, column=0, sticky="nsew")
        frame.rowconfigure(2, weight=1)
        self.soul_tree.bind("<<TreeviewSelect>>", self._on_soul_select)

    def refresh_souls(self):
        if not hasattr(self, "soul_tree"):
            return
        self.soul_tree.delete(*self.soul_tree.get_children())
        uid = self.selected_uid
        if not uid:
            return
        needle = self.soul_search.get().strip().casefold()
        current = self.account_rows.get(uid, {}).get("current_show_soul_cid")
        with connect() as conn:
            rows = conn.execute(
                "SELECT soul_id,level,favor,favor_level,oath_activation "
                "FROM souls WHERE uid=? ORDER BY soul_id",
                (uid,),
            ).fetchall()
        for row in rows:
            name = soul_name(row["soul_id"])
            if needle and needle not in f"{row['soul_id']} {name}".casefold():
                continue
            self.soul_tree.insert(
                "",
                "end",
                values=(
                    row["soul_id"],
                    name,
                    row["level"],
                    row["favor"],
                    row["favor_level"],
                    "已激活" if row["oath_activation"] else "-",
                    "当前" if row["soul_id"] == current else "",
                ),
            )

    def _on_soul_select(self, _event=None):
        selection = self.soul_tree.selection()
        if not selection:
            return
        values = self.soul_tree.item(selection[0], "values")
        self.soul_var.set(format_record(SOUL_NAMES, values[0]))
        self.soul_level_var.set(str(values[2]))
        self.soul_favor_var.set(str(values[3]))

    def save_soul(self):
        uid = self._uid()
        if not uid:
            return
        try:
            soul_id = parse_record_id(self.soul_var.get())
            level = parse_integer(self.soul_level_var.get(), "等级", 1, 999)
            favor = parse_integer(self.soul_favor_var.get(), "好感", 0)
        except ValueError as exc:
            self._input_error(exc)
            return
        self._mutate(
            "保存人偶",
            {"uid": uid, "soul_id": soul_id, "level": level, "favor": favor},
            lambda conn: upsert_soul(conn, uid, soul_id, level, favor),
        )

    def set_show_soul(self):
        uid = self._uid()
        if not uid:
            return
        try:
            soul_id = parse_record_id(self.soul_var.get())
        except ValueError as exc:
            self._input_error(exc)
            return
        self._mutate(
            "设置展示人偶",
            {"uid": uid, "soul_id": soul_id},
            lambda conn: set_current_show_soul(conn, uid, soul_id),
        )

    def _build_progress(self):
        frame = self.frames["进度"]
        tools = ttk.Frame(frame)
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for column in range(3):
            tools.columnconfigure(column, weight=1)

        chapter_box = ttk.Labelframe(tools, text="章节", padding=10)
        chapter_box.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        chapter_box.columnconfigure(0, weight=1)
        self.chapter_var = tk.StringVar()
        chapter_combo = ttk.Combobox(chapter_box, textvariable=self.chapter_var)
        chapter_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._bind_combo_filter(chapter_combo, MAZE_CHAPTERS)
        self._write_button(chapter_box, "解锁", self.unlock_chapter_action).grid(
            row=0, column=1
        )

        quest_box = ttk.Labelframe(tools, text="任务", padding=10)
        quest_box.grid(row=0, column=1, sticky="ew", padx=6)
        quest_box.columnconfigure(0, weight=1)
        self.quest_var = tk.StringVar()
        quest_combo = ttk.Combobox(quest_box, textvariable=self.quest_var)
        quest_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._bind_combo_filter(quest_combo, QUEST_NAMES)
        self._write_button(quest_box, "完成", self.complete_quest_action).grid(
            row=0, column=1
        )

        story_box = ttk.Labelframe(tools, text="人偶剧情", padding=10)
        story_box.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        story_box.columnconfigure(0, weight=1)
        self.story_var = tk.StringVar()
        self.story_chapter_var = tk.StringVar()

        def story_extra(_cid, info):
            soul = soul_name(info.get("soul_id"))
            return f"[{soul}]" if soul else None

        story_combo = ttk.Combobox(story_box, textvariable=self.story_var)
        story_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._bind_combo_filter(story_combo, STORY_NAMES, extra=story_extra)
        ttk.Entry(
            story_box, textvariable=self.story_chapter_var, width=7
        ).grid(row=0, column=1, padx=(0, 6))
        self._write_button(story_box, "设置", self.set_story_action).grid(
            row=0, column=2
        )

        holder, self.progress_tree = self._make_tree(
            frame,
            (
                ("type", "类型", 110, False),
                ("cid", "CID", 150, False),
                ("name", "名称", 320, True),
                ("status", "状态", 180, True),
            ),
        )
        holder.grid(row=1, column=0, sticky="nsew")

    def refresh_progress(self):
        if not hasattr(self, "progress_tree"):
            return
        self.progress_tree.delete(*self.progress_tree.get_children())
        uid = self.selected_uid
        if not uid:
            return
        with connect() as conn:
            quests = conn.execute(
                "SELECT quest_cid,fin_num,tgt_num FROM quest_progress "
                "WHERE uid=? ORDER BY quest_cid",
                (uid,),
            ).fetchall()
            lists = conn.execute(
                "SELECT list_name,quest_cid FROM quest_lists "
                "WHERE uid=? ORDER BY list_name,quest_cid",
                (uid,),
            ).fetchall()
            stories = conn.execute(
                "SELECT story_cid,highest_chapter_index FROM soul_story_progress "
                "WHERE uid=? ORDER BY story_cid",
                (uid,),
            ).fetchall()
        for row in quests:
            self.progress_tree.insert(
                "",
                "end",
                values=(
                    "任务",
                    row["quest_cid"],
                    record_name(QUEST_NAMES, row["quest_cid"]),
                    f"{row['fin_num']}/{row['tgt_num']}",
                ),
            )
        for row in lists:
            name = (
                record_name(MAZE_CHAPTERS, row["quest_cid"])
                or record_name(QUEST_NAMES, row["quest_cid"])
            )
            self.progress_tree.insert(
                "",
                "end",
                values=(
                    "章节" if row["list_name"] == "unlock" else "任务列表",
                    row["quest_cid"],
                    name,
                    "已解锁" if row["list_name"] == "unlock" else "已完成",
                ),
            )
        for row in stories:
            self.progress_tree.insert(
                "",
                "end",
                values=(
                    "人偶剧情",
                    row["story_cid"],
                    record_name(STORY_NAMES, row["story_cid"]),
                    f"章节 {row['highest_chapter_index']}",
                ),
            )

    def unlock_chapter_action(self):
        uid = self._uid()
        if not uid:
            return
        try:
            cid = parse_record_id(self.chapter_var.get())
        except ValueError as exc:
            self._input_error(exc)
            return
        self._mutate(
            "解锁章节",
            {"uid": uid, "chapter_cid": cid},
            lambda conn: unlock_chapter(conn, uid, cid),
        )

    def complete_quest_action(self):
        uid = self._uid()
        if not uid:
            return
        try:
            cid = parse_record_id(self.quest_var.get())
        except ValueError as exc:
            self._input_error(exc)
            return
        self._mutate(
            "完成任务",
            {"uid": uid, "quest_cid": cid},
            lambda conn: complete_quest(conn, uid, cid),
        )

    def set_story_action(self):
        uid = self._uid()
        if not uid:
            return
        try:
            story_cid = parse_record_id(self.story_var.get())
            chapter = parse_integer(self.story_chapter_var.get(), "章节序号", 1)
        except ValueError as exc:
            self._input_error(exc)
            return
        self._mutate(
            "设置剧情进度",
            {"uid": uid, "story_cid": story_cid, "chapter": chapter},
            lambda conn: set_story_progress(conn, uid, story_cid, chapter),
        )

    def _build_inventory(self):
        frame = self.frames["背包"]
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(3, weight=1)
        ttk.Label(toolbar, text="类型").grid(row=0, column=0, padx=(0, 6))
        self.inventory_type_var = tk.StringVar(value="全部类型")
        ttk.Combobox(
            toolbar,
            textvariable=self.inventory_type_var,
            values=["全部类型"] + sorted(set(ITEM_TYPE_NAMES.values())),
            state="readonly",
            width=16,
        ).grid(row=0, column=1, padx=(0, 16))
        ttk.Label(toolbar, text="搜索").grid(row=0, column=2, padx=(0, 6))
        self.inventory_search = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.inventory_search).grid(
            row=0, column=3, sticky="ew", padx=(0, 12)
        )
        self.inventory_owned_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            toolbar, text="仅显示已持有", variable=self.inventory_owned_only
        ).grid(row=0, column=4)
        self.inventory_search.trace_add("write", lambda *_: self.refresh_inventory())
        self.inventory_type_var.trace_add("write", lambda *_: self.refresh_inventory())
        self.inventory_owned_only.trace_add("write", lambda *_: self.refresh_inventory())
        holder, self.inventory_tree = self._make_tree(
            frame,
            (
                ("cid", "道具 CID", 140, False),
                ("name", "名称", 360, True),
                ("type", "类型", 100, False),
                ("quantity", "数量", 140, False),
            ),
        )
        holder.grid(row=1, column=0, sticky="nsew")
        self.inventory_tree.bind("<Double-1>", self._inventory_to_resource)

    def refresh_inventory(self):
        if not hasattr(self, "inventory_tree"):
            return
        self.inventory_tree.delete(*self.inventory_tree.get_children())
        uid = self.selected_uid
        if not uid:
            return
        needle = self.inventory_search.get().strip().casefold()
        with connect() as conn:
            rows = conn.execute(
                "SELECT template_id,quantity FROM items "
                "WHERE uid=? ORDER BY template_id",
                (uid,),
            ).fetchall()
        quantities = {int(row["template_id"]): int(row["quantity"]) for row in rows}
        selected_type = self.inventory_type_var.get()
        for cid in sorted(ITEM_NAMES, key=int):
            info = ITEM_NAMES[cid]
            name = item_name(cid)
            type_name = item_type_name(info)
            quantity = quantities.get(int(cid), 0)
            if self.inventory_owned_only.get() and quantity <= 0:
                continue
            if selected_type != "全部类型" and type_name != selected_type:
                continue
            haystack = f"{cid} {name} {type_name}".casefold()
            if needle and needle not in haystack:
                continue
            self.inventory_tree.insert(
                "",
                "end",
                values=(
                    cid,
                    name or "（配置未命名）",
                    type_name,
                    f"{quantity:,}",
                ),
            )

    def _inventory_to_resource(self, _event=None):
        selection = self.inventory_tree.selection()
        if not selection:
            return
        cid = self.inventory_tree.item(selection[0], "values")[0]
        self.item_var.set(format_record(ITEM_NAMES, cid))
        self.item_qty_var.set("")
        self.note.select(self.frames["资源"])

    def _build_dresses(self):
        frame = self.frames["服装"]
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="筛选").grid(row=0, column=0, padx=(0, 6))
        self.dress_search = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.dress_search).grid(
            row=0, column=1, sticky="ew", padx=(0, 12)
        )
        self.dress_search.trace_add("write", lambda *_: self.refresh_dresses())
        self._write_button(toolbar, "直接获得", self.acquire_dress_action).grid(
            row=0, column=2, padx=(0, 6)
        )
        self._write_button(toolbar, "设为当前服装", self.set_dress_action).grid(
            row=0, column=3
        )
        holder, self.dress_tree = self._make_tree(
            frame,
            (
                ("cid", "服装 CID", 140, False),
                ("name", "名称", 260, True),
                ("soul", "所属人偶", 220, True),
                ("type", "类型", 110, False),
                ("gain", "获取方式", 280, True),
                ("owned", "持有", 90, False),
                ("current", "当前", 80, False),
            ),
        )
        holder.grid(row=1, column=0, sticky="nsew")

    def refresh_dresses(self):
        if not hasattr(self, "dress_tree"):
            return
        self.dress_tree.delete(*self.dress_tree.get_children())
        needle = self.dress_search.get().strip().casefold()
        current = self.account_rows.get(self.selected_uid, {}).get("current_dress_cid")
        owned = set()
        if self.selected_uid:
            with connect() as conn:
                row = conn.execute(
                    "SELECT value_json FROM player_state_json "
                    "WHERE uid=? AND field_name='dresses'",
                    (self.selected_uid,),
                ).fetchone()
            if row:
                try:
                    owned = {
                        int(item.get("dressCid", 0))
                        for item in json.loads(row[0])
                        if isinstance(item, dict)
                    }
                except (TypeError, ValueError):
                    owned = set()
        for cid in sorted(DRESS_NAMES, key=int):
            info = DRESS_NAMES[cid]
            soul = soul_name(info.get("soul_id")) if isinstance(info, dict) else ""
            name = dress_name(cid)
            type_name = info.get("type_name", info.get("type", ""))
            gain_way = info.get("gain_way", "")
            haystack = f"{cid} {name} {soul} {type_name} {gain_way}".casefold()
            if needle and needle not in haystack:
                continue
            self.dress_tree.insert(
                "",
                "end",
                values=(
                    cid,
                    name,
                    soul,
                    type_name,
                    gain_way or ("初始服装" if info.get("initial") else "未记录"),
                    "已拥有" if int(cid) in owned else "-",
                    "当前" if int(cid) == current else "",
                ),
            )

    def acquire_dress_action(self):
        uid = self._uid()
        selection = self.dress_tree.selection()
        if not uid or not selection:
            if uid:
                self._input_error(ValueError("请选择服装"))
            return
        cid = int(self.dress_tree.item(selection[0], "values")[0])
        self._mutate(
            "直接获得服装",
            {"uid": uid, "dress_cid": cid},
            lambda conn: acquire_dress(conn, uid, cid),
        )

    def set_dress_action(self):
        uid = self._uid()
        selection = self.dress_tree.selection()
        if not uid or not selection:
            if uid:
                self._input_error(ValueError("请选择服装"))
            return
        cid = int(self.dress_tree.item(selection[0], "values")[0])
        self._mutate(
            "设置当前服装",
            {"uid": uid, "dress_cid": cid},
            lambda conn: set_current_dress(conn, uid, cid),
        )

    def _build_events(self):
        frame = self.frames["活动"]
        body = ttk.Panedwindow(frame, orient="vertical")
        body.grid(row=0, column=0, rowspan=2, sticky="nsew")

        event_panel = ttk.Frame(body)
        event_panel.columnconfigure(0, weight=1)
        event_panel.rowconfigure(2, weight=1)
        major_bar = ttk.Labelframe(event_panel, text="大型活动关卡回归", padding=8)
        major_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        major_bar.columnconfigure(1, weight=1)
        ttk.Label(major_bar, text="活动章节").grid(row=0, column=0, padx=(0, 6))
        self.major_activity_var = tk.StringVar()
        self.major_activity_combo = ttk.Combobox(
            major_bar, textvariable=self.major_activity_var
        )
        self.major_activity_combo.grid(
            row=0, column=1, sticky="ew", padx=(0, 12)
        )

        def major_extra(_cid, info):
            return f"{len(info.get('mazes', {}))} 关 / {info.get('type_name', '活动')}"

        self._bind_combo_filter(
            self.major_activity_combo, MAJOR_ACTIVITIES, extra=major_extra
        )
        self._write_button(
            major_bar, "生成关卡回归清单", self.create_major_activity_runs_action
        ).grid(row=0, column=2)
        toolbar = ttk.Frame(event_panel)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="筛选").grid(row=0, column=0, padx=(0, 6))
        self.event_search = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.event_search).grid(
            row=0, column=1, sticky="ew", padx=(0, 12)
        )
        self.event_search.trace_add("write", lambda *_: self.refresh_events())
        self._write_button(toolbar, "开启", lambda: self.set_event_action(True)).grid(
            row=0, column=2, padx=(0, 6)
        )
        self._write_button(
            toolbar, "关闭", lambda: self.set_event_action(False), danger=True
        ).grid(row=0, column=3)
        holder, self.event_tree = self._make_tree(
            event_panel,
            (
                ("cid", "活动 CID", 150, False),
                ("name", "名称", 360, True),
                ("type", "类型", 90, False),
                ("status", "本地状态", 120, False),
            ),
        )
        holder.grid(row=2, column=0, sticky="nsew")
        self.event_tree.bind("<<TreeviewSelect>>", self._on_event_select)
        body.add(event_panel, weight=2)

        run_panel = ttk.Frame(body, padding=(0, 10, 0, 0))
        run_panel.columnconfigure(0, weight=1)
        run_panel.rowconfigure(1, weight=1)
        editor = ttk.Frame(run_panel)
        editor.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        editor.columnconfigure(1, weight=2)
        editor.columnconfigure(4, weight=2)
        self.activity_test_id_var = tk.StringVar()
        self.activity_run_activity_var = tk.StringVar()
        self.activity_run_status_var = tk.StringVar(value="待测试")
        self.activity_run_notes_var = tk.StringVar()
        ttk.Label(editor, text="测试 ID").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(editor, textvariable=self.activity_test_id_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 6)
        )
        ttk.Button(editor, text="生成", command=self.new_activity_run).grid(
            row=0, column=2, padx=(0, 12)
        )
        ttk.Label(editor, text="活动").grid(row=0, column=3, padx=(0, 6))
        self.activity_run_combo = ttk.Combobox(
            editor, textvariable=self.activity_run_activity_var
        )
        self.activity_run_combo.grid(
            row=0, column=4, sticky="ew", padx=(0, 12)
        )
        self._bind_combo_filter(self.activity_run_combo, ACTIVITY_NAMES)
        ttk.Label(editor, text="状态").grid(row=0, column=5, padx=(0, 6))
        ttk.Combobox(
            editor,
            textvariable=self.activity_run_status_var,
            values=tuple(ACTIVITY_RUN_STATUSES.values()),
            state="readonly",
            width=9,
        ).grid(row=0, column=6, padx=(0, 12))
        ttk.Label(editor, text="备注").grid(
            row=1, column=0, padx=(0, 6), pady=(8, 0)
        )
        ttk.Entry(editor, textvariable=self.activity_run_notes_var).grid(
            row=1,
            column=1,
            columnspan=4,
            sticky="ew",
            padx=(0, 12),
            pady=(8, 0),
        )
        self._write_button(editor, "保存记录", self.save_activity_run_action).grid(
            row=1, column=5, padx=(0, 6), pady=(8, 0)
        )
        self._write_button(
            editor, "删除记录", self.delete_activity_run_action, danger=True
        ).grid(row=1, column=6, pady=(8, 0))

        run_holder, self.activity_run_tree = self._make_tree(
            run_panel,
            (
                ("test_id", "本地测试 ID", 260, False),
                ("activity", "活动", 260, True),
                ("status", "状态", 100, False),
                ("updated", "更新时间", 170, False),
                ("notes", "备注", 360, True),
            ),
        )
        run_holder.grid(row=1, column=0, sticky="nsew")
        self.activity_run_tree.bind(
            "<<TreeviewSelect>>", self._on_activity_run_select
        )
        body.add(run_panel, weight=2)

    def refresh_events(self):
        if not hasattr(self, "event_tree"):
            return
        self.event_tree.delete(*self.event_tree.get_children())
        state = {}
        if self.selected_uid:
            with connect() as conn:
                row = conn.execute(
                    "SELECT value_json FROM player_state_json "
                    "WHERE uid=? AND field_name='opEventsStatus'",
                    (self.selected_uid,),
                ).fetchone()
            state = json.loads(row[0]) if row else {}
        needle = self.event_search.get().strip().casefold()
        for cid in sorted(ACTIVITY_NAMES, key=int):
            info = ACTIVITY_NAMES[cid]
            name = activity_name(cid)
            if needle and needle not in f"{cid} {name}".casefold():
                continue
            value = state.get(str(cid))
            status = "开启" if value == 1 else "关闭" if value == 0 else "未设置"
            self.event_tree.insert(
                "",
                "end",
                values=(
                    cid,
                    name,
                    info.get("type_name", info.get("type", ""))
                    if isinstance(info, dict)
                    else "",
                    status,
                ),
            )
        self.refresh_activity_runs()

    def _on_event_select(self, _event=None):
        selection = self.event_tree.selection()
        if not selection:
            return
        cid = self.event_tree.item(selection[0], "values")[0]
        self.activity_run_activity_var.set(format_record(ACTIVITY_NAMES, cid))

    def new_activity_run(self):
        self.selected_activity_run_id = None
        self.activity_test_id_var.set(
            f"local-activity-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        selection = self.event_tree.selection()
        if selection:
            cid = self.event_tree.item(selection[0], "values")[0]
            self.activity_run_activity_var.set(format_record(ACTIVITY_NAMES, cid))
        else:
            self.activity_run_activity_var.set("")
        self.activity_run_status_var.set("待测试")
        self.activity_run_notes_var.set("")
        self.activity_run_tree.selection_remove(
            *self.activity_run_tree.selection()
        )

    def refresh_activity_runs(self):
        if not hasattr(self, "activity_run_tree"):
            return
        self.activity_run_tree.delete(*self.activity_run_tree.get_children())
        self.activity_run_rows = {}
        uid = self.selected_uid
        if not uid:
            return
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='local_activity_runs'"
            ).fetchone()
            rows = (
                conn.execute(
                    """
                    SELECT id,test_id,activity_cid,activity_name,status,notes,
                           created_at,updated_at
                    FROM local_activity_runs
                    WHERE uid=?
                    ORDER BY updated_at DESC,id DESC
                    """,
                    (uid,),
                ).fetchall()
                if exists
                else ()
            )
        for row in rows:
            data = dict(row)
            self.activity_run_rows[data["id"]] = data
            name = data["activity_name"] or activity_name(data["activity_cid"])
            updated = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(data["updated_at"])
            )
            self.activity_run_tree.insert(
                "",
                "end",
                iid=f"activity-run-{data['id']}",
                values=(
                    data["test_id"],
                    format_record(
                        ACTIVITY_NAMES, data["activity_cid"]
                    ) if activity_name(data["activity_cid"]) else (
                        f"{data['activity_cid']}  {name}"
                    ),
                    ACTIVITY_RUN_STATUSES.get(data["status"], data["status"]),
                    updated,
                    data["notes"],
                ),
            )
        if self.selected_activity_run_id not in self.activity_run_rows:
            self.selected_activity_run_id = None
            self.activity_test_id_var.set("")
            self.activity_run_notes_var.set("")

    def _on_activity_run_select(self, _event=None):
        selection = self.activity_run_tree.selection()
        if not selection:
            return
        run_id = int(selection[0].removeprefix("activity-run-"))
        row = self.activity_run_rows.get(run_id)
        if not row:
            return
        self.selected_activity_run_id = run_id
        self.activity_test_id_var.set(row["test_id"])
        title = activity_name(row["activity_cid"])
        self.activity_run_activity_var.set(
            format_record(ACTIVITY_NAMES, row["activity_cid"])
            if title
            else f"{row['activity_cid']}  {row['activity_name']}"
        )
        self.activity_run_status_var.set(
            ACTIVITY_RUN_STATUSES.get(row["status"], row["status"])
        )
        self.activity_run_notes_var.set(row["notes"])

    def save_activity_run_action(self):
        uid = self._uid()
        if not uid:
            return
        try:
            test_id = self.activity_test_id_var.get().strip()
            if not test_id:
                raise ValueError("请生成活动测试 ID")
            cid = parse_record_id(self.activity_run_activity_var.get())
            status_label = self.activity_run_status_var.get()
            status = ACTIVITY_RUN_STATUS_CODES.get(status_label)
            if not status:
                raise ValueError("请选择活动测试状态")
            notes = self.activity_run_notes_var.get().strip()
        except ValueError as exc:
            self._input_error(exc)
            return
        run_id = self.selected_activity_run_id
        activity_title = activity_name(cid)
        if not activity_title and run_id is not None:
            activity_title = self.activity_run_rows.get(run_id, {}).get(
                "activity_name", ""
            )
        action = "更新活动回归记录" if run_id is not None else "新增活动回归记录"
        if self._mutate(
            action,
            {
                "uid": uid,
                "test_id": test_id,
                "cid": cid,
                "status": status,
            },
            lambda conn: save_activity_run(
                conn,
                run_id,
                test_id,
                uid,
                cid,
                activity_title,
                status,
                notes,
            ),
        ):
            self.selected_activity_run_id = None
            self.activity_test_id_var.set("")
            self.activity_run_notes_var.set("")

    def create_major_activity_runs_action(self):
        uid = self._uid()
        if not uid:
            return
        try:
            chapter_cid = parse_record_id(self.major_activity_var.get())
            chapter = MAJOR_ACTIVITIES.get(str(chapter_cid))
            if not chapter:
                raise ValueError("请选择大型活动章节")
        except ValueError as exc:
            self._input_error(exc)
            return
        self._mutate(
            "生成大型活动回归清单",
            {
                "uid": uid,
                "chapter_cid": chapter_cid,
                "chapter_name": chapter.get("name", ""),
                "stage_count": len(chapter.get("mazes", {})),
            },
            lambda conn: create_major_activity_runs(conn, uid, chapter_cid),
        )

    def delete_activity_run_action(self):
        uid = self._uid()
        run_id = self.selected_activity_run_id
        if not uid or run_id is None:
            if uid:
                self._input_error(ValueError("请选择活动回归记录"))
            return
        row = self.activity_run_rows.get(run_id, {})
        if self._mutate(
            "删除活动回归记录",
            {
                "uid": uid,
                "test_id": row.get("test_id", ""),
                "cid": row.get("activity_cid"),
            },
            lambda conn: delete_activity_run(conn, run_id, uid),
        ):
            self.selected_activity_run_id = None
            self.activity_test_id_var.set("")
            self.activity_run_notes_var.set("")

    def set_event_action(self, enabled):
        uid = self._uid()
        selection = self.event_tree.selection()
        if not uid or not selection:
            if uid:
                self._input_error(ValueError("请选择活动"))
            return
        cid = int(self.event_tree.item(selection[0], "values")[0])
        action = "开启活动" if enabled else "关闭活动"
        self._mutate(
            action,
            {"uid": uid, "cid": cid},
            lambda conn: set_event_state(conn, uid, cid, enabled),
        )

    def _build_audit(self):
        frame = self.frames["审计"]
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(0, weight=1)
        ttk.Button(toolbar, text="刷新", command=self.refresh_audit).grid(
            row=0, column=1
        )
        holder, self.audit_tree = self._make_tree(
            frame,
            (
                ("time", "时间", 170, False),
                ("action", "操作", 180, False),
                ("account", "账号 / UID", 260, True),
                ("backup", "备份", 260, True),
                ("detail", "详情", 420, True),
            ),
        )
        holder.grid(row=1, column=0, sticky="nsew")

    def refresh_audit(self):
        if not hasattr(self, "audit_tree"):
            return
        self.audit_tree.delete(*self.audit_tree.get_children())
        try:
            lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-500:]
        except OSError:
            return
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            detail = record.get("detail", {})
            timestamp = record.get("at")
            time_text = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
                if timestamp
                else "-"
            )
            account = detail.get("uid") or detail.get("channel_uid") or ""
            backup = detail.get("backup") or ""
            compact = {
                key: value
                for key, value in detail.items()
                if key not in {"uid", "channel_uid", "backup"}
            }
            self.audit_tree.insert(
                "",
                "end",
                values=(
                    time_text,
                    record.get("action", ""),
                    account,
                    backup,
                    json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-local-only", action="store_true")
    args = parser.parse_args()
    AdminApp(writable=args.write_local_only).run()


if __name__ == "__main__":
    main()
