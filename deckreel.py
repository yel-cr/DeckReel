#!/usr/bin/env python3
"""
DeckReel - Steam Deck スクリーンショット マネージャー
ブラウザベースGUI — Python標準ライブラリのみ使用。追加インストール不要。

使い方:
    python3 deckreel.py
    ブラウザで http://localhost:8745 が自動的に開きます。

必要環境:
    - Python 3.8+ (SteamOSにプリインストール済み)
    - rclone (Google Driveリモート設定済み)
    - Webブラウザ (SteamOSにFirefoxがプリインストール済み)
"""

import os
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
import mimetypes
import webbrowser
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs



# ═══════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════

APP_NAME = "DeckReel"
APP_VERSION = "1.5.0"
HOST = "127.0.0.1"
PORT = 8745
CONFIG_DIR = Path.home() / ".config" / "deckreel"
CACHE_FILE = CONFIG_DIR / "game_cache.json"
DB_FILE = CONFIG_DIR / "sync_history.db"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_STEAM_PATH = Path.home() / ".local" / "share" / "Steam" / "userdata"
STEAM_API_URL = "https://store.steampowered.com/api/appdetails?appids={}&l=japanese"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
HEARTBEAT_TIMEOUT = 60  # seconds without browser heartbeat before auto-shutdown

# Well-known non-game App IDs
KNOWN_APP_IDS = {
    "7": "Home",
}


# ═══════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════

class Config:
    DEFAULTS = {
        "steam_userdata_path": str(DEFAULT_STEAM_PATH),
        "steam_user_id": "",
        "rclone_path": "rclone",
        "drive_remote": "gdrive",
        "drive_base_path": "Screenshots",
        "transfers": "4",
    }

    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    self._data.update(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get(self, key):
        return self._data.get(key, self.DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value

    def as_dict(self):
        return dict(self._data)

    def detect_user_ids(self):
        base = Path(self.get("steam_userdata_path"))
        if not base.exists():
            return []
        return sorted(
            d.name for d in base.iterdir()
            if d.is_dir() and d.name.isdigit()
        )


# ═══════════════════════════════════════════════════════════════════
#  Game Name Resolver
# ═══════════════════════════════════════════════════════════════════

class GameResolver:
    def __init__(self):
        self._cache = {}
        self._load_cache()

    def _load_cache(self):
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r") as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._cache = {}

    def _save_cache(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(self._cache, f, indent=2, ensure_ascii=False)

    def get_name(self, app_id):
        return self._cache.get(str(app_id))

    def resolve(self, app_id):
        app_id = str(app_id)
        if app_id in self._cache:
            return self._cache[app_id]
        # Check well-known non-game IDs first
        if app_id in KNOWN_APP_IDS:
            self._cache[app_id] = KNOWN_APP_IDS[app_id]
            self._save_cache()
            return self._cache[app_id]
        # Fix 6: リトライ処理（最大3回、失敗時は1秒待機）
        for attempt in range(3):
            try:
                url = STEAM_API_URL.format(app_id)
                req = urllib.request.Request(url, headers={"User-Agent": "DeckReel/1.3"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get(app_id, {}).get("success"):
                    name = data[app_id]["data"]["name"]
                else:
                    name = f"Unknown ({app_id})"
                self._cache[app_id] = name
                self._save_cache()
                return name
            except Exception:
                if attempt < 2:
                    time.sleep(1.0)
        return None

    def resolve_batch(self, app_ids):
        results = {}
        for i, aid in enumerate(app_ids):
            name = self.resolve(aid)
            results[aid] = name
            time.sleep(0.35)
        return results


# ═══════════════════════════════════════════════════════════════════
#  Sync Tracker (SQLite)
# ═══════════════════════════════════════════════════════════════════

class SyncTracker:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Fix 7: _lockを先に作成し、CREATE TABLEもロック内で実行する
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS uploaded_files (
                    file_path   TEXT PRIMARY KEY,
                    file_size   INTEGER,
                    file_mtime  REAL,
                    drive_path  TEXT,
                    uploaded_at TEXT
                )
            """)
            self._conn.commit()

    def is_uploaded(self, file_path):
        path = str(file_path)
        with self._lock:
            row = self._conn.execute(
                "SELECT file_size, file_mtime FROM uploaded_files WHERE file_path=?",
                (path,),
            ).fetchone()
        if row is None:
            return False
        try:
            st = os.stat(path)
            return row[0] == st.st_size and abs(row[1] - st.st_mtime) < 1
        except OSError:
            return False

    def mark_uploaded(self, file_path, drive_path):
        path = str(file_path)
        st = os.stat(path)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO uploaded_files VALUES (?,?,?,?,?)",
                (path, st.st_size, st.st_mtime, drive_path, datetime.now().isoformat()),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()


# ═══════════════════════════════════════════════════════════════════
#  Steam Screenshot Scanner
# ═══════════════════════════════════════════════════════════════════

class SteamScanner:
    def __init__(self, config, resolver, tracker):
        self.config = config
        self.resolver = resolver
        self.tracker = tracker

    def screenshot_base(self):
        uid = self.config.get("steam_user_id")
        if not uid:
            return None
        return Path(self.config.get("steam_userdata_path")) / uid / "760" / "remote"

    def scan(self):
        base = self.screenshot_base()
        if not base or not base.exists():
            return []
        games = []
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            ss = d / "screenshots"
            if not ss.exists():
                continue
            files = sorted(
                f for f in ss.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTS
            )
            if not files:
                continue
            aid = d.name
            name = self.resolver.get_name(aid) or f"ID: {aid}"
            file_strs = [str(f) for f in files]
            synced = sum(1 for f in files if self.tracker.is_uploaded(f))
            games.append(dict(
                app_id=aid, name=name, path=str(ss),
                count=len(files), synced=synced, files=file_strs,
            ))
        games.sort(key=lambda g: g["name"].lower())
        return games

    def unresolved_ids(self, games):
        return [g["app_id"] for g in games if g["name"].startswith("ID: ")]


# ═══════════════════════════════════════════════════════════════════
#  Sync Engine  (v1.5 — ゲーム単位バッチ転送 + 並列転送)
# ═══════════════════════════════════════════════════════════════════

class SyncEngine:
    def __init__(self, config, tracker, resolver):
        self.config = config
        self.tracker = tracker
        self.resolver = resolver
        self._cancel = False
        self._status = {
            "running": False, "current": 0, "total": 0,
            "filename": "", "uploaded": 0, "errors": 0,
            "error_messages": [],
        }
        self._lock = threading.Lock()

    @property
    def status(self):
        with self._lock:
            return dict(self._status)

    def _update(self, **kw):
        with self._lock:
            self._status.update(kw)

    def cancel(self):
        self._cancel = True

    @staticmethod
    def _safe_name(name):
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        return name.strip(". ")

    def sync(self, file_list):
        """ゲーム単位でまとめて rclone copy し、--files-from と
        --transfers で並列転送することで大幅に高速化する。"""
        # 多重起動防止チェック
        with self._lock:
            if self._status["running"]:
                return
        self._cancel = False

        # ── ファイルを (転送元ディレクトリ, ゲーム名) でグループ化 ──
        groups = {}  # key: (src_dir, safe_name, game_name)  value: [filepath, ...]
        for fpath, game_name in file_list:
            src_dir = str(Path(fpath).parent)
            safe = self._safe_name(game_name)
            key = (src_dir, safe, game_name)
            groups.setdefault(key, []).append(fpath)

        total_files = len(file_list)
        self._update(
            running=True, current=0, total=total_files,
            filename="", uploaded=0, errors=0, error_messages=[],
        )

        rclone = self.config.get("rclone_path")
        remote = self.config.get("drive_remote")
        base = self.config.get("drive_base_path")
        transfers = self.config.get("transfers") or "4"
        done = 0

        for (src_dir, safe, game_name), files in groups.items():
            if self._cancel:
                break

            count = len(files)
            self._update(
                current=done,
                filename=f"{game_name}\uff08{count}\u679a\uff09",
            )

            dest = f"{remote}:{base}/{safe}"
            tf_path = None

            try:
                # ── ファイル名リストを一時ファイルに書き出す ──
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False
                ) as tf:
                    for fp in files:
                        tf.write(Path(fp).name + "\n")
                    tf_path = tf.name

                # ── rclone copy をフォルダ単位で実行 ──
                #   --files-from : 対象ファイルを限定
                #   --transfers  : 並列転送数
                #   --checkers   : 並列チェック数
                timeout = max(180, count * 30)
                r = subprocess.run(
                    [
                        rclone, "copy",
                        src_dir, dest,
                        "--files-from", tf_path,
                        "--transfers", str(transfers),
                        "--checkers", "8",
                    ],
                    capture_output=True, text=True, timeout=timeout,
                )

                if r.returncode == 0:
                    # 成功 → 全ファイルをアップロード済みとして記録
                    for fp in files:
                        fname = Path(fp).name
                        self.tracker.mark_uploaded(fp, f"{dest}/{fname}")
                    with self._lock:
                        self._status["uploaded"] += count
                else:
                    with self._lock:
                        self._status["errors"] += count
                        self._status["error_messages"].append(
                            f"{game_name}: {r.stderr.strip()[:200]}"
                        )

            except Exception as e:
                with self._lock:
                    self._status["errors"] += count
                    self._status["error_messages"].append(
                        f"{game_name}: {str(e)[:200]}"
                    )
            finally:
                # 一時ファイルの後片付け
                if tf_path:
                    try:
                        os.unlink(tf_path)
                    except OSError:
                        pass

            done += count
            self._update(current=done)

        self._update(running=False)

    def collect_files(self, games):
        to_sync = []
        for g in games:
            name = g["name"]
            if name.startswith("ID: "):
                resolved = self.resolver.get_name(g["app_id"])
                if resolved:
                    name = resolved
            for fp in g["files"]:
                if not self.tracker.is_uploaded(fp):
                    to_sync.append((fp, name))
        return to_sync




# ═══════════════════════════════════════════════════════════════════
#  HTML Frontend (embedded)
# ═══════════════════════════════════════════════════════════════════

HTML_PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DeckReel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Syne:wght@400;500;600;700;800&family=Syne+Mono&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
:root{
  --ink:#0A0A0A; --ink-2:#2A2A2A; --ink-3:#555; --ink-4:#888; --ink-5:#BBB;
  --paper:#F5F4F0; --paper-2:#EDECEA; --paper-3:#E0DED9;
  --acid:#CCFF00; --acid-d:#A8D400;
  --font-d:'Bebas Neue',sans-serif;
  --font-b:'Syne',sans-serif;
  --font-m:'Syne Mono',monospace;
}
html,body{height:100%;font-family:var(--font-b);background:var(--paper);color:var(--ink);overflow:hidden;-webkit-font-smoothing:antialiased;}
body{display:flex;flex-direction:column;padding-bottom:26px;}
/* cursor: default */
.cursor{display:none!important;}

/* ── TOP BAR ── */
.topbar{height:52px;flex-shrink:0;border-bottom:2px solid var(--ink);display:flex;align-items:stretch;background:var(--paper);animation:bar-in .5s ease-out both;}
@keyframes bar-in{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
.topbar-logo{display:flex;align-items:center;padding:0 22px;border-right:2px solid var(--ink);gap:10px;flex-shrink:0;}
.logo-mark{width:26px;height:26px;background:var(--ink);border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.logo-mark svg{width:12px;height:12px;fill:var(--acid);}
.logo-wordmark{font-family:var(--font-d);font-size:20px;letter-spacing:3px;color:var(--ink);line-height:1;}
.topbar-nav{display:flex;align-items:stretch;flex:1;}
.nav-btn{display:flex;align-items:center;gap:8px;padding:0 20px;font-family:var(--font-m);font-size:10px;font-weight:400;letter-spacing:2px;text-transform:uppercase;color:var(--ink-3);background:transparent;border:none;border-right:1px solid var(--paper-3);transition:all .15s;white-space:nowrap;position:relative;overflow:hidden;}
.nav-btn::after{content:'';position:absolute;bottom:0;left:0;right:0;height:0;background:var(--acid);transition:height .2s;}
.nav-btn:hover{color:var(--ink);}
.nav-btn:hover::after{height:3px;}
.nav-btn .num{font-family:var(--font-d);font-size:15px;color:var(--ink-5);line-height:1;}
.topbar-right{display:flex;align-items:stretch;margin-left:auto;}
.exit-btn{display:flex;align-items:center;gap:8px;padding:0 22px;font-family:var(--font-m);font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--ink-3);background:transparent;border:none;border-left:1px solid var(--paper-3);border-right:2px solid var(--ink);transition:all .15s;white-space:nowrap;}
.exit-btn:hover{background:var(--paper-2);color:var(--ink);}
.exit-btn svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;}
.sync-btn{display:flex;align-items:center;gap:10px;padding:0 28px;font-family:var(--font-d);font-size:17px;letter-spacing:2px;color:var(--paper);background:var(--ink);border:none;transition:background .15s;white-space:nowrap;}
.sync-game-btn{display:flex;align-items:center;gap:10px;padding:0 28px;font-family:var(--font-d);font-size:17px;letter-spacing:2px;color:var(--paper);background:var(--ink-2);border:none;border-left:1px solid rgba(255,255,255,0.1);transition:background .15s;white-space:nowrap;}
.sync-game-btn:hover{background:var(--ink-3);}
.sync-game-btn:disabled{opacity:0.35;}
.sync-btn:hover{background:var(--ink-2);}
.sync-btn .arr{transition:transform .2s;}
.sync-btn:hover .arr{transform:translateY(-2px);}

/* ── LAYOUT ── */
.main{flex:1;display:flex;overflow:hidden;}

/* ── SIDEBAR ── */
.sidebar{width:260px;flex-shrink:0;border-right:2px solid var(--ink);background:var(--paper);display:flex;flex-direction:column;overflow:hidden;animation:side-in .5s ease-out .08s both;}
@keyframes side-in{from{opacity:0;transform:translateX(-12px)}to{opacity:1;transform:none}}
.sidebar-label{padding:10px 18px;border-bottom:1px solid var(--paper-3);display:flex;align-items:center;justify-content:space-between;}
.label-txt{font-family:var(--font-m);font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--ink-4);}
.label-cnt{font-family:var(--font-d);font-size:18px;color:var(--ink);}
.search-area{padding:12px 18px;border-bottom:1px solid var(--paper-3);}
.search-line{display:flex;align-items:center;gap:10px;border-bottom:1.5px solid var(--ink);padding-bottom:6px;}
.search-line span{font-family:var(--font-m);font-size:10px;color:var(--ink-4);letter-spacing:1px;flex-shrink:0;}
.search-line input{flex:1;background:transparent;border:none;outline:none;font-family:var(--font-m);font-size:12px;color:var(--ink);caret-color:var(--acid-d);}
.search-line input::placeholder{color:var(--ink-5);}
.filter-row{padding:8px 10px;display:flex;gap:6px;border-bottom:1px solid var(--paper-3);}
.fpill{font-family:var(--font-m);font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--ink-4);background:transparent;border:1px solid var(--paper-3);padding:4px 0;flex:1;text-align:center;transition:all .15s;}
.fpill:hover{border-color:var(--ink-4);color:var(--ink-3);}
.fpill.active{background:var(--ink);color:var(--acid);border-color:var(--ink);}
.game-list{flex:1;overflow-y:auto;padding-bottom:2px;}
.game-list::-webkit-scrollbar{width:3px;}
.game-list::-webkit-scrollbar-thumb{background:var(--paper-3);}
.game-item{display:flex;align-items:baseline;gap:10px;padding:11px 18px;border-bottom:1px solid var(--paper-3);transition:background .12s;position:relative;}
.game-item:hover{background:var(--paper-2);}
.game-item.active{background:var(--ink);}
.game-item.active .game-name{color:var(--acid);}
.game-item.active .game-count{color:var(--ink-3);}
.game-item.active .gdot{background:var(--acid);}
.gdot{width:5px;height:5px;border-radius:50%;background:var(--ink-5);flex-shrink:0;align-self:center;}
.gdot.done{background:var(--ink);}
.gdot.partial{background:var(--ink-3);}
.game-name{flex:1;font-family:var(--font-b);font-size:12px;font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:.3px;}
.game-count{font-family:var(--font-m);font-size:10px;color:var(--ink-4);flex-shrink:0;}
.sidebar-stats{border-top:2px solid var(--ink);display:grid;grid-template-columns:repeat(3,1fr);min-height:62px;flex-shrink:0;}
.stat{padding:10px 0 8px;text-align:center;}
.stat+.stat{border-left:1px solid var(--paper-3);}
.stat-v{font-family:var(--font-d);font-size:28px;color:var(--ink);line-height:1;display:block;}
.stat-l{font-family:var(--font-m);font-size:8px;letter-spacing:1.5px;text-transform:uppercase;color:var(--ink-4);margin-top:2px;display:block;}
.stat-v.acid{position:relative;color:var(--ink);}
.stat-v.acid::after{content:attr(data-n);position:absolute;bottom:0;left:50%;transform:translateX(-50%);background:var(--acid);color:var(--ink);font-size:28px;line-height:1;padding:0 2px;clip-path:inset(0 100% 0 0);transition:clip-path .5s cubic-bezier(.77,0,.18,1);}
.stat-v.acid.revealed::after{clip-path:inset(0 0% 0 0);}
.stat.done-col{background:transparent;}

/* ── CONTENT ── */
.content{flex:1;display:flex;flex-direction:column;overflow:hidden;animation:content-in .5s ease-out .16s both;}
@keyframes content-in{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.masthead{flex-shrink:0;border-bottom:2px solid var(--ink);padding:10px 0 10px 32px;display:flex;align-items:center;justify-content:space-between;gap:20px;background:var(--paper);position:relative;}
.masthead::after{content:'';position:absolute;bottom:-5px;left:0;width:100px;height:3px;background:var(--acid);transition:width .4s ease;}
.masthead-left{flex:1;min-width:0;overflow:hidden;}
.masthead-eyebrow{font-family:var(--font-m);font-size:9px;letter-spacing:4px;text-transform:uppercase;color:var(--ink-4);margin-bottom:5px;}
.masthead-title{font-family:var(--font-d);font-size:clamp(32px,3.5vw,52px);color:var(--ink);line-height:1.1;letter-spacing:1px;overflow:hidden;white-space:nowrap;padding-top:2px;}
.masthead-title .title-inner{display:inline-block;white-space:nowrap;}
.masthead-title .title-inner.scrolling{animation:title-scroll var(--scroll-dur,8s) linear infinite;}
@keyframes title-scroll{0%{transform:translateX(0)}40%{transform:translateX(var(--scroll-dist,0px))}60%{transform:translateX(var(--scroll-dist,0px))}100%{transform:translateX(0)}}
.masthead-meta{display:flex;align-items:center;gap:0;flex-shrink:0;margin-left:auto;}
.meta-block{text-align:center;padding:0 16px;border-left:1px solid var(--paper-3);}
.meta-big{font-family:var(--font-d);font-size:clamp(28px,3vw,42px);color:var(--ink);line-height:1;}
.meta-big.acid{position:relative;}
.meta-big.acid::after{content:attr(data-n);position:absolute;bottom:0;left:0;background:var(--acid);color:var(--ink);font-size:38px;line-height:1;clip-path:inset(0 100% 0 0);transition:clip-path .5s cubic-bezier(.77,0,.18,1);}
.meta-big.acid.revealed::after{clip-path:inset(0 0% 0 0);}
.meta-label{display:none;}
.masthead-actions{display:flex;gap:0;align-items:stretch;flex-shrink:0;align-self:stretch;}
.act-btn{font-family:var(--font-m);font-size:10px;letter-spacing:2px;text-transform:uppercase;padding:8px 18px;border:1.5px solid;transition:all .15s;}
.act-btn.outline{color:var(--ink-3);border-color:var(--paper-3);background:transparent;}
.act-btn.outline:hover{border-color:var(--ink-3);color:var(--ink);}
.act-btn.solid{background:var(--ink);color:var(--acid);border-color:var(--ink);}
.act-btn.solid:hover{background:var(--ink-2);}

/* progress */
.prog-strip{flex-shrink:0;display:none;align-items:center;gap:0;border-bottom:1px solid var(--paper-3);height:34px;background:var(--paper-2);overflow:hidden;}
.prog-strip.visible{display:flex;}
.prog-info{font-family:var(--font-m);font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--ink-4);padding:0 18px;flex-shrink:0;white-space:nowrap;}
.prog-info strong{color:var(--ink);font-weight:400;}
.prog-track{flex:1;height:100%;background:var(--paper-3);position:relative;overflow:hidden;}
.prog-fill{position:absolute;inset:0;right:auto;width:0%;background:var(--acid);transition:width .4s cubic-bezier(.25,.1,.25,1);}
.prog-pct{font-family:var(--font-d);font-size:20px;color:var(--ink);padding:0 14px;flex-shrink:0;}

/* ── WELCOME ── */
.welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;text-align:center;padding:40px;animation:content-in .5s ease-out .2s both;}
.welcome-title{font-family:var(--font-d);font-size:52px;letter-spacing:2px;color:var(--ink);margin-bottom:10px;}
.welcome-sub{font-family:var(--font-m);font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--ink-4);margin-bottom:40px;}
.welcome-steps{display:flex;flex-direction:column;gap:0;border:1.5px solid var(--ink);width:100%;max-width:420px;}
.w-step{display:flex;align-items:center;gap:20px;padding:14px 22px;border-bottom:1px solid var(--paper-3);font-family:var(--font-b);font-size:13px;font-weight:600;color:var(--ink-3);transition:background .15s;}
.w-step:last-child{border-bottom:none;}
.w-step:hover{background:var(--paper-2);}
.w-step-n{font-family:var(--font-d);font-size:26px;color:var(--ink-5);width:32px;flex-shrink:0;}
.w-step strong{color:var(--ink);}

/* grid */
.grid-area{flex:1;overflow-y:auto;padding:20px 32px 32px;cursor:grab;}
.grid-area.dragging{cursor:grabbing;scroll-behavior:auto;user-select:none;}
.grid-area::-webkit-scrollbar{width:3px;}
.grid-area::-webkit-scrollbar-thumb{background:var(--paper-3);}
.game-list.dragging{cursor:grabbing;scroll-behavior:auto;user-select:none;}
.grid-header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:16px;}
.grid-header-label{font-family:var(--font-m);font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--ink-4);}
.grid-header-sort{font-family:var(--font-m);font-size:9px;letter-spacing:1px;color:var(--ink-4);display:flex;gap:12px;}
.sort-opt{transition:color .15s;padding-bottom:1px;}
.sort-opt:hover{color:var(--ink);}
.sort-opt.active{color:var(--ink);border-bottom:1.5px solid var(--ink);}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:2px;}
.thumb-card{position:relative;overflow:hidden;background:var(--paper-3);animation:card-in .4s ease-out both;}
@keyframes card-in{from{opacity:0}to{opacity:1}}
.thumb-img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;filter:grayscale(1) contrast(1.05) brightness(.9);transition:filter .45s,transform .45s;}
.thumb-color{position:absolute;inset:0;width:100%;aspect-ratio:16/9;object-fit:cover;display:block;clip-path:inset(0 100% 0 0);transition:clip-path .55s cubic-bezier(.77,0,.18,1);}
.thumb-card:hover .thumb-color{clip-path:inset(0 0% 0 0);}
.thumb-card:hover .thumb-img{transform:scale(1.04);}
.thumb-bar{position:absolute;bottom:0;left:0;right:0;padding:7px 9px 6px;background:var(--paper);border-top:1px solid var(--paper-3);display:flex;align-items:center;justify-content:space-between;transform:translateY(100%);transition:transform .22s cubic-bezier(.25,.1,.25,1);}
.thumb-card:hover .thumb-bar{transform:none;}
.thumb-name{font-family:var(--font-m);font-size:9px;color:var(--ink-3);letter-spacing:.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:68%;}
.thumb-status{font-family:var(--font-m);font-size:8px;letter-spacing:1.5px;text-transform:uppercase;flex-shrink:0;}
.thumb-status.up{color:var(--ink);}
.thumb-status.lo{color:var(--ink-5);}
.thumb-corner{position:absolute;top:0;left:0;width:0;height:0;border-style:solid;border-width:20px 20px 0 0;border-color:var(--acid) transparent transparent transparent;opacity:0;transition:opacity .2s;}
.thumb-card:hover .thumb-corner.show{opacity:1;}

/* toast */
.toast-wrap{position:fixed;top:62px;right:16px;z-index:500;display:flex;flex-direction:column;gap:6px;}
.toast{padding:12px 18px;font-family:var(--font-m);font-size:11px;letter-spacing:1px;border:1.5px solid var(--ink);background:var(--paper);color:var(--ink);display:flex;align-items:center;gap:10px;animation:toast-in .3s ease-out;max-width:380px;}
@keyframes toast-in{from{opacity:0;transform:translateX(16px)}to{opacity:1;transform:none}}
.toast.success .t-dot{background:var(--acid);}
.toast.error .t-dot{background:#ff4444;}
.t-dot{width:7px;height:7px;border-radius:50%;background:var(--ink-4);flex-shrink:0;}

/* settings modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(10,10,10,.6);z-index:100;align-items:center;justify-content:center;backdrop-filter:blur(4px);}
.modal-bg.open{display:flex;}
.modal{background:var(--paper);border:2px solid var(--ink);padding:32px;width:500px;max-width:92vw;max-height:88vh;overflow-y:auto;animation:modal-in .25s ease-out;}
@keyframes modal-in{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:none}}
.modal-title{font-family:var(--font-d);font-size:26px;letter-spacing:2px;color:var(--ink);margin-bottom:26px;display:flex;align-items:center;justify-content:space-between;}
.field{margin-bottom:18px;}
.field label{display:block;font-family:var(--font-m);font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--ink-4);margin-bottom:7px;}
.field input,.field select{width:100%;padding:10px 14px;background:var(--paper-2);border:1.5px solid var(--paper-3);font-family:var(--font-m);font-size:12px;color:var(--ink);outline:none;transition:border-color .15s;}
.field input:focus,.field select:focus{border-color:var(--ink);}
.field .hint{font-family:var(--font-m);font-size:10px;color:var(--ink-4);margin-top:5px;letter-spacing:.5px;line-height:1.6;}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:28px;}

/* exit confirm modal */
.exit-modal-inner{text-align:center;padding:8px 0;}
.exit-modal-inner .ex-title{font-family:var(--font-d);font-size:32px;letter-spacing:2px;margin-bottom:8px;}
.exit-modal-inner .ex-sub{font-family:var(--font-m);font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--ink-4);margin-bottom:28px;}
.exit-modal-inner .ex-btns{display:flex;gap:10px;justify-content:center;}

/* viewer */
.viewer-bg{display:none;position:fixed;inset:0;background:rgba(10,10,10,.95);z-index:200;flex-direction:column;align-items:center;justify-content:center;}
.viewer-bg.open{display:flex;}
.viewer-bg img{max-width:90vw;max-height:76vh;object-fit:contain;animation:modal-in .25s ease-out;}
.viewer-ctrl{display:flex;align-items:center;gap:16px;margin-top:18px;}
.vbtn{font-family:var(--font-m);font-size:10px;letter-spacing:2px;text-transform:uppercase;padding:9px 20px;border:1.5px solid rgba(255,255,255,.2);background:transparent;color:rgba(255,255,255,.7);transition:all .15s;}
.vbtn:hover{border-color:rgba(255,255,255,.6);color:#fff;}
.v-counter{font-family:var(--font-d);font-size:22px;color:rgba(255,255,255,.4);min-width:80px;text-align:center;}

/* ticker */
.ticker{position:fixed;bottom:0;left:0;right:0;height:26px;background:var(--ink);z-index:20;display:flex;align-items:center;overflow:hidden;}
.ticker-inner{display:flex;white-space:nowrap;animation:tick-scroll 28s linear infinite;}
.tick-item{font-family:var(--font-m);font-size:8px;letter-spacing:2px;text-transform:uppercase;color:var(--ink-4);padding:0 26px;border-right:1px solid var(--ink-2);}
.tick-item.hi{color:var(--acid);}
@keyframes tick-scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
</style>
</head>
<body>

<div class="cursor" id="cursor"></div>
<div class="toast-wrap" id="toasts"></div>

<!-- ── TOP BAR ── -->
<div class="topbar">
  <div class="topbar-logo">
    <div class="logo-mark">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/></svg>
    </div>
    <div class="logo-wordmark">DECKREEL</div>
  </div>
  <nav class="topbar-nav">
    <button class="nav-btn" onclick="doRefresh()" id="btnRefresh"><span class="num">01</span> Scan</button>
    <button class="nav-btn" onclick="resolveNames()" id="btnResolve"><span class="num">02</span> Resolve</button>
    <button class="nav-btn" onclick="openSettings()"><span class="num">03</span> Settings</button>
    <button class="nav-btn" onclick="cancelSync()" id="btnCancel" style="display:none"><span class="num">—</span> Cancel</button>
  </nav>
  <div class="topbar-right">
    <button class="exit-btn" onclick="openExitModal()">
      <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      EXIT
    </button>
    <button class="sync-btn" onclick="syncAll()" id="btnSyncAll">
      <span class="arr">&#x2191;</span> SYNC ALL
    </button>
  </div>
</div>

<!-- ── MAIN ── -->
<div class="main">
  <div class="sidebar">
    <div class="sidebar-label">
      <span class="label-txt">Library</span>
      <span class="label-cnt" id="statGames">0</span>
    </div>
    <div class="search-area">
      <div class="search-line">
        <span>SRH /</span>
        <input type="text" placeholder="game title…" id="searchInput" oninput="filterGames()">
      </div>
    </div>
    <div class="filter-row">
      <button class="fpill active" data-filter="all"  onclick="setFilter('all')">All</button>
      <button class="fpill"        data-filter="done" onclick="setFilter('done')">Done</button>
      <button class="fpill"        data-filter="none" onclick="setFilter('none')">Pending</button>
    </div>
    <div class="game-list" id="gameList"></div>
    <div class="sidebar-stats">
      <div class="stat"><span class="stat-v" id="statTotal">0</span><span class="stat-l">All</span></div>
      <div class="stat done-col"><span class="stat-v acid" id="statUploaded" data-n="0">0</span><span class="stat-l" style="color:var(--acid-d)">Done</span></div>
      <div class="stat"><span class="stat-v" id="statPending">0</span><span class="stat-l">Pending</span></div>
    </div>
  </div>

  <div class="content" id="contentArea">
    <!-- masthead (hidden until game selected) -->
    <div class="masthead" id="masthead" style="display:none">
      <div class="masthead-left">
        <div class="masthead-eyebrow" id="mastheadEye">—</div>
        <div class="masthead-title" id="mastheadTitle">—</div>
      </div>
      <div class="masthead-meta">
        <div class="meta-block">
          <div class="meta-big" id="metaTotal">0</div>
          <div class="meta-label">Total</div>
        </div>
        <div class="meta-block">
          <div class="meta-big acid" id="metaSynced" data-n="0">0</div>
          <div class="meta-label">Synced</div>
        </div>
      </div>
      <div class="masthead-actions">
        <button class="sync-game-btn" onclick="syncSelected()" id="btnSyncSel">&#x2191; SYNC GAME</button>
      </div>
    </div>

    <!-- progress strip -->
    <div class="prog-strip" id="progStrip">
      <div class="prog-info">Uploading &nbsp;<strong id="progLabel">—</strong></div>
      <div class="prog-track"><div class="prog-fill" id="progFill"></div></div>
      <div class="prog-pct" id="progPct">0%</div>
    </div>

    <!-- welcome / grid area -->
    <div class="grid-area" id="gridArea">
      <div class="welcome" id="welcomePanel">
        <div class="welcome-title">DECKREEL</div>
        <div class="welcome-sub">Steam Deck Screenshot Manager</div>
        <div class="welcome-steps">
          <div class="w-step"><span class="w-step-n">01</span><span>Open <strong>Settings</strong> to configure</span></div>
          <div class="w-step"><span class="w-step-n">02</span><span>Click <strong>Scan</strong> to discover screenshots</span></div>
          <div class="w-step"><span class="w-step-n">03</span><span>Click <strong>Resolve</strong> to fetch game names</span></div>
          <div class="w-step"><span class="w-step-n">04</span><span>Click <strong>Sync All</strong> to upload</span></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div class="modal-bg" id="settingsModal" onclick="if(event.target===this)closeSettings()">
  <div class="modal">
    <div class="modal-title">
      SETTINGS
      <button class="act-btn outline" onclick="closeSettings()" style="font-size:9px;padding:5px 12px;">CLOSE</button>
    </div>
    <div class="field">
      <label>Steam userdataパス</label>
      <input id="cfgSteamPath">
      <div class="hint">Steamのスクリーンショットが保存されているディレクトリ</div>
    </div>
    <div class="field">
      <label>SteamユーザーID</label>
      <select id="cfgUserId"></select>
      <div class="hint">自動検出　複数ある場合は選択してください</div>
    </div>
    <div class="field">
      <label>rcloneパス</label>
      <input id="cfgRclonePath">
      <div class="hint">rcloneバイナリのパス</div>
    </div>
    <div class="field">
      <label>リモート名</label>
      <input id="cfgRemote">
      <div class="hint">rclone configで設定したリモート名</div>
    </div>
    <div class="field">
      <label>保存先フォルダ</label>
      <input id="cfgBasePath">
      <div class="hint">アップロード先 → リモート名:保存先フォルダ/ゲーム名/</div>
    </div>
    <div class="field">
      <label>同時転送数</label>
      <select id="cfgTransfers">
        <option value="2">2</option>
        <option value="4" selected>4</option>
        <option value="8">8</option>
        <option value="16">16</option>
      </select>
      <div class="hint">rcloneの並列転送数</div>
    </div>
    <div class="modal-actions">
      <button class="act-btn outline" onclick="closeSettings()">キャンセル</button>
      <button class="act-btn solid"   onclick="saveSettings()">保存</button>
    </div>
  </div>
</div>

<!-- Exit Confirm Modal -->
<div class="modal-bg" id="exitModal" onclick="if(event.target===this)closeExitModal()">
  <div class="modal" style="width:360px;padding:36px;">
    <div class="exit-modal-inner">
      <div class="ex-title">EXIT?</div>
      <div class="ex-sub">DeckReelを終了します</div>
      <div class="ex-btns">
        <button class="act-btn outline" onclick="closeExitModal()">キャンセル</button>
        <button class="act-btn solid"   onclick="doExit()">終了する</button>
      </div>
    </div>
  </div>
</div>

<!-- Viewer -->
<div class="viewer-bg" id="viewerBg" onclick="if(event.target===this)closeViewer()">
  <img id="viewerImg" src="" alt="" onclick="event.stopPropagation()">
  <div class="viewer-ctrl" onclick="event.stopPropagation()">
    <button class="vbtn" onclick="viewerPrev()">&#x25C0; PREV</button>
    <span class="v-counter" id="viewerInfo">1 / 1</span>
    <button class="vbtn" onclick="viewerNext()">NEXT &#x25B6;</button>
  </div>
</div>

<!-- Ticker -->
<div class="ticker"><div class="ticker-inner" id="tickerInner"></div></div>

<script>
let games=[], selectedAppId=null, viewerFiles=[], viewerIdx=0, uploadedSet=new Set(), appVersion='---';
let curFilter='all';
const API=(p,o)=>fetch('/api'+p,o).then(r=>r.json());
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

// cursor: default (removed custom cursor)

// ── Toast ──
function toast(msg,type='info'){
  const c=document.getElementById('toasts');
  const t=document.createElement('div');
  t.className='toast '+type;
  t.innerHTML=`<div class="t-dot"></div><span>${esc(msg)}</span>`;
  c.appendChild(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(()=>t.remove(),300);},4500);
}

// ── Filter / Sort ──
function setFilter(f){
  curFilter=f;
  document.querySelectorAll('.fpill').forEach(p=>p.classList.toggle('active',p.dataset.filter===f));
  renderGameList();
}
function getSortedFiltered(){
  const ft=document.getElementById('searchInput').value.toLowerCase();
  let list=[...games];
  if(ft) list=list.filter(g=>g.name.toLowerCase().includes(ft));
  if(curFilter==='done')    list=list.filter(g=>g.synced===g.count);
  else if(curFilter==='none')    list=list.filter(g=>g.synced<g.count);  // PARTIALも含む
  list.sort((a,b)=>a.name.localeCompare(b.name,'ja'));
  return list;
}

// ── Refresh ──
async function doRefresh(){
  const b=document.getElementById('btnRefresh');b.disabled=true;
  try{
    const d=await API('/scan');
    games=d.games||[];
    appVersion=d.version||'---';
    uploadedSet=new Set(d.uploaded_paths||[]);
    renderGameList();
    updateStats();
    updateTicker();
    if(games.length>0) document.getElementById('welcomePanel').style.display='none';
    if(selectedAppId){const g=games.find(x=>x.app_id===selectedAppId);if(g)showGame(g);}
  }catch(e){toast('スキャン失敗: '+e.message,'error');}
  b.disabled=false;
}
function filterGames(){renderGameList();}

// ── Game list ──
function renderGameList(){
  const el=document.getElementById('gameList');el.innerHTML='';
  for(const g of getSortedFiltered()){
    const dot=g.synced===g.count?'done':'none';  // partialはnoneに統合
    const div=document.createElement('div');
    div.className='game-item'+(g.app_id===selectedAppId?' active':'');
    div.innerHTML=`<div class="gdot ${dot}"></div><div class="game-name">${esc(g.name.toUpperCase())}</div><div class="game-count">${g.synced}/${g.count}</div>`;
    div.onclick=()=>selectGame(g);
    el.appendChild(div);
  }
}

function selectGame(g){
  selectedAppId=g.app_id;
  renderGameList();
  showGame(g);
}

function updateStats(){
  const t=games.reduce((s,g)=>s+g.count,0);
  const sy=games.reduce((s,g)=>s+g.synced,0);
  document.getElementById('statGames').textContent=games.length;
  document.getElementById('statTotal').textContent=t;
  document.getElementById('statPending').textContent=t-sy;
  // Done: acid アニメーション付きで更新
  const su=document.getElementById('statUploaded');
  su.textContent=sy; su.setAttribute('data-n',sy);
  su.classList.remove('revealed');
  requestAnimationFrame(()=>requestAnimationFrame(()=>su.classList.add('revealed')));
}

// ── Game detail ──
function showGame(g){
  document.getElementById('masthead').style.display='';
  document.getElementById('mastheadEye').textContent='App ID: '+g.app_id;
  // タイトル自動スクロール
  const titleEl=document.getElementById('mastheadTitle');
  titleEl.innerHTML=`<span class="title-inner">${esc(g.name.toUpperCase())}</span>`;
  requestAnimationFrame(()=>{
    const inner=titleEl.querySelector('.title-inner');
    const overflow=inner.scrollWidth-titleEl.clientWidth;
    if(overflow>10){
      const dur=Math.max(8,overflow/20);  // 速度を半分に
      inner.style.setProperty('--scroll-dist','-'+overflow+'px');
      inner.style.setProperty('--scroll-dur',dur+'s');
      inner.classList.add('scrolling');
    }
  });
  document.getElementById('metaTotal').textContent=g.count;
  const ms=document.getElementById('metaSynced');
  ms.textContent=g.synced; ms.setAttribute('data-n',g.synced);
  ms.classList.remove('revealed');
  requestAnimationFrame(()=>requestAnimationFrame(()=>ms.classList.add('revealed')));

  // acid underline width = ratio
  const pct=g.count>0?Math.round(g.synced/g.count*100):0;
  document.querySelector('.masthead').style.setProperty('--uw',(40+pct*0.6)+'px');
  document.querySelector('.masthead::after');
  // just set via style attribute trick
  const mh=document.getElementById('masthead');
  mh.style.setProperty('--acid-w',(40+pct*0.6)+'px');

  document.getElementById('welcomePanel').style.display='none';
  const area=document.getElementById('gridArea');
  let old=area.querySelector('.grid-wrap');if(old)old.remove();

  const wrap=document.createElement('div');wrap.className='grid-wrap';
  wrap.innerHTML=`<div class="grid-header"><span class="grid-header-label">Screenshots — ${g.count} files</span></div><div class="grid" id="thumbGrid"></div>`;
  area.appendChild(wrap);
  area.scrollTop=0;

  viewerFiles=g.files;
  const grid=document.getElementById('thumbGrid');
  g.files.forEach((f,i)=>{
    const fname=f.split('/').pop();
    const up=uploadedSet.has(f);
    const card=document.createElement('div');
    card.className='thumb-card';
    card.style.animationDelay=(i%24)*22+'ms';
    const thumb=`/api/thumb?path=${encodeURIComponent(f)}&v=${appVersion}`;
    card.innerHTML=`<img class="thumb-img" src="${thumb}" loading="lazy" alt=""><img class="thumb-color" src="${thumb}" loading="lazy" alt="">${up?'<div class="thumb-corner show"></div>':''}<div class="thumb-bar"><span class="thumb-name">${esc(fname)}</span><span class="thumb-status ${up?'up':'lo'}">${up?'SYNCED':'LOCAL'}</span></div>`;
    card.onclick=()=>openViewer(i);
    grid.appendChild(card);
  });
}

// ── Viewer ──
function openViewer(i){viewerIdx=i;document.getElementById('viewerBg').classList.add('open');updateViewer();}
function closeViewer(){document.getElementById('viewerBg').classList.remove('open');}
function updateViewer(){
  document.getElementById('viewerImg').src='/api/image?path='+encodeURIComponent(viewerFiles[viewerIdx]);
  document.getElementById('viewerInfo').textContent=`${viewerIdx+1} / ${viewerFiles.length}`;
}
function viewerPrev(){if(viewerIdx>0){viewerIdx--;updateViewer();}}
function viewerNext(){if(viewerIdx<viewerFiles.length-1){viewerIdx++;updateViewer();}}
document.addEventListener('keydown',e=>{
  const v=document.getElementById('viewerBg');
  if(!v.classList.contains('open'))return;
  if(e.key==='ArrowLeft')viewerPrev();
  else if(e.key==='ArrowRight')viewerNext();
  else if(e.key==='Escape')closeViewer();
});

// ── Resolve ──
async function resolveNames(){
  const b=document.getElementById('btnResolve');b.disabled=true;
  showProgress('ゲーム名を取得中...',0,1);
  try{
    const d=await API('/resolve',{method:'POST'});
    if(d.resolved===0&&!d.started){toast('すべてのゲーム名は取得済みです','info');hideProgress();b.disabled=false;return;}
    if(d.already_running){toast('現在取得中です。完了までお待ちください','info');await pollResolve();return;}
    await pollResolve();
  }catch(e){toast('取得失敗: '+e.message,'error');hideProgress();b.disabled=false;}
}
async function pollResolve(){
  const b=document.getElementById('btnResolve');
  try{
    while(true){
      const s=await API('/resolve/status');
      updateProgress(`ゲーム名を取得中... (${s.resolved}/${s.total})`,s.resolved,s.total);
      if(!s.running){hideProgress();if(s.error)toast('エラー: '+s.error,'error');else toast(`${s.resolved}件のゲーム名を取得しました`,'success');await doRefresh();b.disabled=false;return;}
      await new Promise(r=>setTimeout(r,600));
    }
  }catch(e){hideProgress();b.disabled=false;toast('ポーリングエラー: '+e.message,'error');}
}

// ── Sync ──
async function syncAll(){startSync('/sync/all');}
async function syncSelected(){if(!selectedAppId)return;startSync('/sync/game?app_id='+selectedAppId);}
async function startSync(ep){
  try{
    const d=await API(ep,{method:'POST'});
    if(d.count===0){toast('すべて同期済みです！','success');return;}
    document.getElementById('btnSyncAll').disabled=true;
    document.getElementById('btnCancel').style.display='';
    showProgress('同期を開始しています...',0,d.count);
    pollSync();
  }catch(e){toast('同期開始失敗: '+e.message,'error');}
}
async function pollSync(){
  try{
    const s=await API('/sync/status');
    updateProgress(s.filename?`Uploading: ${s.filename}`:'同期中...',s.current,s.total);
    if(s.running){setTimeout(pollSync,500);}
    else{
      hideProgress();
      document.getElementById('btnSyncAll').disabled=false;
      document.getElementById('btnCancel').style.display='none';
      let m=`${s.uploaded}件アップロード完了`;
      if(s.errors>0)m+=`、${s.errors}件エラー`;
      toast(m,s.errors>0?'error':'success');
      await doRefresh();
    }
  }catch(e){setTimeout(pollSync,1000);}
}
async function cancelSync(){await API('/sync/cancel',{method:'POST'});toast('キャンセルしました','info');}

function showProgress(l,c,t){document.getElementById('progStrip').classList.add('visible');updateProgress(l,c,t);}
function updateProgress(l,c,t){
  document.getElementById('progLabel').textContent=l;
  document.getElementById('progFill').style.width=(t>0?c/t*100:0)+'%';
  document.getElementById('progPct').textContent=(t>0?Math.round(c/t*100):0)+'%';
}
function hideProgress(){document.getElementById('progStrip').classList.remove('visible');}

// ── Settings ──
async function openSettings(){
  const d=await API('/config');
  document.getElementById('cfgSteamPath').value=d.steam_userdata_path||'';
  document.getElementById('cfgRclonePath').value=d.rclone_path||'';
  document.getElementById('cfgRemote').value=d.drive_remote||'';
  document.getElementById('cfgBasePath').value=d.drive_base_path||'';
  const tSel=document.getElementById('cfgTransfers');
  const tVal=d.transfers||'4';
  for(let i=0;i<tSel.options.length;i++){tSel.options[i].selected=(tSel.options[i].value===tVal);}
  const sel=document.getElementById('cfgUserId');sel.innerHTML='';
  for(const uid of(d._user_ids||[])){const o=document.createElement('option');o.value=uid;o.textContent=uid;if(uid===d.steam_user_id)o.selected=true;sel.appendChild(o);}
  if(sel.options.length===0&&d.steam_user_id){const o=document.createElement('option');o.value=d.steam_user_id;o.textContent=d.steam_user_id;o.selected=true;sel.appendChild(o);}
  document.getElementById('settingsModal').classList.add('open');
}
function closeSettings(){document.getElementById('settingsModal').classList.remove('open');}
async function saveSettings(){
  await API('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    steam_userdata_path:document.getElementById('cfgSteamPath').value,
    steam_user_id:document.getElementById('cfgUserId').value,
    rclone_path:document.getElementById('cfgRclonePath').value,
    drive_remote:document.getElementById('cfgRemote').value,
    drive_base_path:document.getElementById('cfgBasePath').value,
    transfers:document.getElementById('cfgTransfers').value,
  })});
  closeSettings();
  toast('設定を保存しました','success');
  doRefresh();
}

// ── EXIT ──
function openExitModal(){document.getElementById('exitModal').classList.add('open');}
function closeExitModal(){document.getElementById('exitModal').classList.remove('open');}
async function doExit(){
  // まずウィンドウを閉じ、その後サーバーをシャットダウンする
  // window.close() はスクリプトで開いたタブのみ有効なため、
  // フォールバックとして空ページへ遷移して再アクセスできないようにする
  try{ fetch('/api/exit',{method:'POST',keepalive:true}).catch(()=>{}); }catch(e){}
  setTimeout(()=>{
    try{ window.close(); }catch(e){}
    // ブラウザが window.close() を拒否した場合はブランクページへ
    setTimeout(()=>{ window.location.href='about:blank'; },400);
  },150);
}

// ── Ticker ──
function updateTicker(){
  const t=games.reduce((s,g)=>s+g.count,0);
  const sy=games.reduce((s,g)=>s+g.synced,0);
  const items=[
    {t:'DeckReel v'+appVersion,hi:false},
    {t:`${sy} files synced`,hi:true},
    {t:`${t-sy} files pending`,hi:false},
    {t:`${games.length} games detected`,hi:false},
    {t:'Hover screenshots to reveal color',hi:true},
    {t:'rclone connected',hi:false},
  ];
  const inner=document.getElementById('tickerInner');inner.innerHTML='';
  [...items,...items].forEach(d=>{const el=document.createElement('span');el.className='tick-item'+(d.hi?' hi':'');el.textContent=d.t;inner.appendChild(el);});
}
updateTicker();

// ── Heartbeat ──
setInterval(()=>fetch('/api/heartbeat').catch(()=>{}),3000);

// ── Drag-to-Scroll (Steam Deck ゲームモード対応) ──
function enableDragScroll(el){
  let isDown=false, startY=0, scrollTop=0, moved=false;
  const onDown=e=>{
    if(e.type==='touchstart') return;
    isDown=true; moved=false;
    startY=e.pageY-el.getBoundingClientRect().top-window.scrollY;
    scrollTop=el.scrollTop;
    el.classList.add('dragging');
  };
  const onMove=e=>{
    if(!isDown) return;
    const y=e.pageY-el.getBoundingClientRect().top-window.scrollY;
    const walk=y-startY;
    if(Math.abs(walk)>5) moved=true;
    el.scrollTop=scrollTop-walk;
  };
  const onUp=()=>{isDown=false;el.classList.remove('dragging');};
  el.addEventListener('mousedown',onDown);
  el.addEventListener('mousemove',onMove);
  el.addEventListener('mouseup',onUp);
  el.addEventListener('mouseleave',onUp);
  // ドラッグ中のクリックを無効化（画像ビューアーが誤って開くのを防ぐ）
  el.addEventListener('click',e=>{if(moved){e.stopPropagation();e.preventDefault();moved=false;}},true);
}
enableDragScroll(document.getElementById('gridArea'));
enableDragScroll(document.getElementById('gameList'));

doRefresh();
</script>
</body></html>
"""


# ===================================================================
#  HTTP Server / API
# ===================================================================

class DeckReelHandler(BaseHTTPRequestHandler):
    config = None
    resolver = None
    tracker = None
    scanner = None
    sync_engine = None
    _games = []
    _games_lock = threading.Lock()
    _last_heartbeat = 0.0
    _heartbeat_lock = threading.Lock()
    _server_ref = None
    # Fix 5: Resolve処理の非同期ステータス管理
    _resolve_status = {"running": False, "resolved": 0, "total": 0, "error": ""}
    _resolve_lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    @classmethod
    def touch_heartbeat(cls):
        with cls._heartbeat_lock:
            cls._last_heartbeat = time.time()

    @classmethod
    def seconds_since_heartbeat(cls):
        with cls._heartbeat_lock:
            return time.time() - cls._last_heartbeat

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path in ("/", ""):
            self._html(HTML_PAGE)
        elif path == "/api/scan":
            self._handle_scan()
        elif path == "/api/config":
            self._handle_get_config()
        elif path == "/api/thumb":
            self._serve_thumb_file(qs.get("path", [""])[0])
        elif path == "/api/image":
            self._serve_image_file(qs.get("path", [""])[0])
        elif path == "/api/sync/status":
            self._json(self.sync_engine.status)
        elif path == "/api/resolve/status":
            # Fix 5: Resolveの進捗ステータスを返す
            with type(self)._resolve_lock:
                self._json(dict(type(self)._resolve_status))
            return
        elif path == "/api/heartbeat":
            type(self).touch_heartbeat()
            self._json({"ok": True})
        else:
            self._error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/api/config":
            self._handle_save_config()
        elif path == "/api/resolve":
            self._handle_resolve()
        elif path == "/api/sync/all":
            self._handle_sync_all()
        elif path == "/api/sync/game":
            self._handle_sync_game(qs.get("app_id", [""])[0])
        elif path == "/api/sync/cancel":
            self.sync_engine.cancel()
            self._json({"ok": True})
        elif path == "/api/exit":
            self._handle_exit()
        else:
            self._error(404, "Not found")

    def _handle_exit(self):
        """EXITボタン: レスポンスを返してからサーバーをシャットダウンする"""
        self._json({"ok": True})
        def _shutdown():
            time.sleep(0.3)
            print()
            print("  ブラウザから終了リクエストを受信しました。シャットダウンします...")
            type(self)._server_ref.shutdown()
        threading.Thread(target=_shutdown, daemon=True).start()

    def _handle_scan(self):
        cls = type(self)
        new_games = self.scanner.scan()
        with cls._games_lock:
            cls._games = new_games
        uploaded = []
        for g in new_games:
            for fp in g["files"]:
                if self.tracker.is_uploaded(fp):
                    uploaded.append(fp)
        self._json({"games": new_games, "uploaded_paths": uploaded, "version": APP_VERSION})

    def _handle_get_config(self):
        data = self.config.as_dict()
        data["_user_ids"] = self.config.detect_user_ids()
        self._json(data)

    def _handle_save_config(self):
        body = self._read_json()
        for k, v in body.items():
            if not k.startswith("_"):
                self.config.set(k, v)
        self.config.save()
        self._json({"ok": True})

    def _handle_resolve(self):
        cls = type(self)
        # Fix 5: すでにResolve中ならスキップ
        with cls._resolve_lock:
            if cls._resolve_status["running"]:
                self._json({"started": False, "already_running": True, "total": cls._resolve_status["total"]})
                return
        with cls._games_lock:
            if not cls._games:
                cls._games = self.scanner.scan()
            unresolved = self.scanner.unresolved_ids(cls._games)
        if not unresolved:
            self._json({"resolved": 0})
            return
        # Fix 5: バックグラウンドスレッドで非同期実行
        with cls._resolve_lock:
            cls._resolve_status = {"running": True, "resolved": 0, "total": len(unresolved), "error": ""}
        def _run():
            try:
                for i, aid in enumerate(unresolved):
                    self.resolver.resolve(aid)
                    with cls._resolve_lock:
                        cls._resolve_status["resolved"] = i + 1
                    time.sleep(0.35)
            except Exception as e:
                with cls._resolve_lock:
                    cls._resolve_status["error"] = str(e)[:200]
            finally:
                with cls._resolve_lock:
                    cls._resolve_status["running"] = False
        threading.Thread(target=_run, daemon=True).start()
        self._json({"started": True, "total": len(unresolved)})

    def _handle_sync_all(self):
        cls = type(self)
        with cls._games_lock:
            if not cls._games:
                cls._games = self.scanner.scan()
            games_snapshot = list(cls._games)
        files = self.sync_engine.collect_files(games_snapshot)
        if not files:
            self._json({"count": 0})
            return
        t = threading.Thread(target=self.sync_engine.sync, args=(files,), daemon=True)
        t.start()
        self._json({"count": len(files)})

    def _handle_sync_game(self, app_id):
        cls = type(self)
        with cls._games_lock:
            if not cls._games:
                cls._games = self.scanner.scan()
            game = next((g for g in cls._games if g["app_id"] == app_id), None)
        if not game:
            self._json({"count": 0})
            return
        files = self.sync_engine.collect_files([game])
        if not files:
            self._json({"count": 0})
            return
        t = threading.Thread(target=self.sync_engine.sync, args=(files,), daemon=True)
        t.start()
        self._json({"count": len(files)})

    def _check_image_allowed(self, filepath):
        """Fix 3: パストラバーサル防止つきのパス検証"""
        real = os.path.realpath(filepath)
        cls = type(self)
        with cls._games_lock:
            for g in cls._games:
                if real.startswith(os.path.realpath(g["path"]) + os.sep):
                    return True
        return False

    def _serve_image_file(self, filepath):
        if not filepath or not os.path.isfile(filepath):
            self._error(404, "File not found")
            return
        if not self._check_image_allowed(filepath):
            self._error(403, "Forbidden")
            return
        mt, _ = mimetypes.guess_type(filepath)
        if not mt:
            mt = "image/jpeg"
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mt)
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._error(500, "Read error")

    def _serve_thumb_file(self, filepath):
        """サムネイル表示用。localhost完結のため元画像をそのまま返し、
        ブラウザ側のCSS縮小で高品質に表示する。"""
        if not filepath or not os.path.isfile(filepath):
            self._error(404, "File not found")
            return
        if not self._check_image_allowed(filepath):
            self._error(403, "Forbidden")
            return
        self._serve_image_file(filepath)

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, content):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        return json.loads(body.decode("utf-8"))


# ===================================================================
#  Entry Point
# ===================================================================

def main():
    config = Config()
    resolver = GameResolver()
    tracker = SyncTracker()
    scanner = SteamScanner(config, resolver, tracker)
    sync_engine = SyncEngine(config, tracker, resolver)

    if not config.get("steam_user_id"):
        ids = config.detect_user_ids()
        if len(ids) == 1:
            config.set("steam_user_id", ids[0])
            config.save()

    DeckReelHandler.config = config
    DeckReelHandler.resolver = resolver
    DeckReelHandler.tracker = tracker
    DeckReelHandler.scanner = scanner
    DeckReelHandler.sync_engine = sync_engine

    # Fix 2: 起動時にrcloneの存在を確認し、見つからなければ警告を表示
    def _check_rclone(rclone_path):
        try:
            r = subprocess.run([rclone_path, "version"], capture_output=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    rclone_ok = _check_rclone(config.get("rclone_path"))
    if not rclone_ok:
        print()
        print(f"  ⚠️  警告: rclone が見つかりません: '{config.get('rclone_path')}'")
        print("  Settings でパスを正しく設定してください。")
        print("  ダウンロード: https://rclone.org/downloads/")

    server = HTTPServer((HOST, PORT), DeckReelHandler)
    DeckReelHandler._server_ref = server
    DeckReelHandler.touch_heartbeat()

    url = "http://{}:{}".format(HOST, PORT)
    print()
    print("  {} v{}".format(APP_NAME, APP_VERSION))
    print("  Running at {}".format(url))
    print("  Press Ctrl+C to stop")
    print()

    def heartbeat_watchdog():
        while True:
            time.sleep(3)
            elapsed = DeckReelHandler.seconds_since_heartbeat()
            if elapsed > HEARTBEAT_TIMEOUT:
                print()
                print("  Browser disconnected. Auto-shutting down...")
                server.shutdown()
                return

    wd = threading.Thread(target=heartbeat_watchdog, daemon=True)
    wd.start()
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        tracker.close()
        server.server_close()


if __name__ == "__main__":
    main()
