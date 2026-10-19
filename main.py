# -*- coding: utf-8 -*-
# Автоматически адаптировано для Android (Kivy)
import sys
import os

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window

# Исходная логика скрипта пользователя (без Tkinter):
'''
# -*- coding: utf-8 -*-
"""
FIFA 18 + FIFA 22 stats bot with GUI (Requests/BeautifulSoup web-preview + CustomTkinter).
Reads public Telegram web previews (t.me/s/...) without logging in as a user,
then displays stats in a desktop window and handles Bot API commands.

Функционал полностью из FIFA.py:
  - scrape без кэша (всегда свежие данные с 00:00)
  - поддержка ничьих 🅿
  - разворот истории и последовательности П1/П2
  - команды start / /start

Install:
    py -m pip install requests beautifulsoup4 customtkinter pystray pillow
"""
import html
import os
import re
import time
import logging
import threading
import sys
import socket
import ctypes
from datetime import datetime, date
from datetime import timedelta, timezone as dt_timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from urllib.parse import urlparse

import requests
from requests.exceptions import RequestException, ConnectionError as RequestsConnectionError, Timeout
from bs4 import BeautifulSoup

import tkinter as tk
import customtkinter as ctk

# ===================== ЛОГИРОВАНИЕ =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fifa-stats-gui")


def resource_path(relative_path):
    """Корректный путь к ресурсам при сборке в один exe (PyInstaller)."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# ====== ЗАЩИТА ОТ ПОВТОРНОГО ЗАПУСКА ======
SINGLE_INSTANCE_PORT = 47829
instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    instance_socket.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    instance_socket.listen(1)
except socket.error:
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            "Программа «Fifa Stats Bot» уже запущена в системе!",
            "Внимание",
            0x10 | 0x0,
        )
    except Exception:
        pass
    sys.exit(0)

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FifaStatsBot.Desktop.1.0")
except Exception:
    pass

# ===================== SETTINGS (из FIFA.py) =====================
BOT_TOKEN = os.getenv("FIFA_BOT_TOKEN", "6546967401:AAGI9OnOoKn4UVMYcRPiwwb071s5mGGUz4g").strip()

# Uzbekistan UTC+5. Fixed offset - no tzdata needed on Windows.
try:
    TIMEZONE = ZoneInfo("Asia/Tashkent") if ZoneInfo is not None else dt_timezone(timedelta(hours=5))
except Exception:
    TIMEZONE = dt_timezone(timedelta(hours=5))

POLL_SECONDS = 3
WEB_REQUEST_TIMEOUT = 20
MAX_PAGES_PER_CHANNEL = 100
TELEGRAM_TEXT_LIMIT = 3900
CACHE_TTL_DAYS = 7  # больше не используется для отчётов (логика FIFA.py — без кэша)

CHANNELS = {
    "FIFA18 Penalty": {
        "source_url": "https://t.me/s/statistika_fifa_penalty_fast",
        "chat_id": -4805948448,
        "cache_file": "fifa18_cache.json",
    },
    "FIFA22 Penalty": {
        "source_url": "https://t.me/s/statistika_fifa_penalty_fast2022",
        "chat_id": -5475586570,
        "cache_file": "fifa22_cache.json",
    },
}

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )
})

stop_event = threading.Event()

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")


# ===================== TELEGRAM / ПАРСИНГ (логика FIFA.py) =====================
def telegram_api(method, payload=None, timeout=35):
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан FIFA_BOT_TOKEN. В CMD выполните: "
            "set FIFA_BOT_TOKEN=ВАШ_НОВЫЙ_ТОКЕН"
        )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = session.post(url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram Bot API {method}: {data}")
    return data["result"]


def send_text(chat_id, text):
    chunks = split_message(text, TELEGRAM_TEXT_LIMIT)
    for chunk in chunks:
        try:
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except (RequestsConnectionError, ConnectionResetError, Timeout) as e:
            log.warning("Сетевая ошибка при отправке сообщения в Telegram: %s. Повтор через 3 сек...", e)
            time.sleep(3)
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })


def split_message(text, limit):
    if len(text) <= limit:
        return [text]
    blocks = text.split("\n\n")
    parts, current = [], ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts


def clean_team(name):
    name = html.unescape(name or "")
    name = name.replace("**", "").replace("__", "").replace("`", "")
    name = name.strip().strip("*_ :")
    name = re.sub(r"^(?:✅|❌|🏁|⏳|⚽️?|🔹|\s)+", "", name).strip()
    name = re.sub(r"(?:✅|❌|🏁|⏳|⚽️?|🔹|\s)+$", "", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name


def parse_post(text):
    """Парсер постов — точная копия логики FIFA.py."""
    if not text:
        return None

    cleaned = text.replace("\u200b", "").replace("\ufeff", "")
    lines = [re.sub(r"\\+$", "", x).strip() for x in cleaned.splitlines()]
    lines = [x for x in lines if x]

    # 1) Single-line: "Liverpool (3:2) Barcelona"
    for line in lines:
        line = re.sub(r"^(?:✅|❌|🏁|⏳|⚽️?|🔹)\s*", "", line).strip()
        line = re.sub(r"^#?N\d+\s+", "", line, flags=re.I).strip()
        score = re.search(r"\(\s*(\d+)\s*[:\-]\s*(\d+)\s*\)", line)
        if not score:
            continue
        left = clean_team(line[:score.start()])
        right = clean_team(line[score.end():])
        s1, s2 = map(int, score.groups())
        if left and right:
            return left, s1, s2, right

    # 2) Multi-line (current FIFA 18)
    for i, line in enumerate(lines):
        score = re.fullmatch(r"\(\s*(\d+)\s*[:\-]\s*(\d+)\s*\)", line.strip())
        if not score or i < 1 or i + 1 >= len(lines):
            continue
        left = clean_team(lines[i - 1])
        right = clean_team(lines[i + 1])
        if left in ("✅", "❌", "🏁") or left.startswith("#"):
            if i >= 2:
                left = clean_team(lines[i - 2])
        if (
            left
            and right
            and not left.startswith("#")
            and not right.startswith("#")
            and not re.match(r"^П[12]\b", left)
            and not re.match(r"^П[12]\b", right)
        ):
            s1, s2 = map(int, score.groups())
            return left, s1, s2, right

    # 3) Result-only format
    for i, line in enumerate(lines):
        line_without_prefix = re.sub(r"^(?:🏁|⏳)\s*", "", line).strip()
        m = re.match(r"^#?\s*N\d+\s+(.+?)\s*:?\s*(✅|❌)?$", line_without_prefix, re.I)
        if m:
            team, outcome = m.groups()
            if not outcome and i + 1 < len(lines):
                next_line = re.sub(r"[*_ ]", "", lines[i + 1])
                if next_line in ("✅", "❌"):
                    outcome = next_line
            team = clean_team(team)
            if team and outcome in ("✅", "❌"):
                return "RESULT", team, outcome

        m = re.fullmatch(r"(.+?)\s*:\s*(✅|❌)", line)
        if m:
            team, outcome = m.groups()
            team = clean_team(team)
            if team:
                return "RESULT", team, outcome

    return None


def post_datetime(post):
    time_el = post.select_one("time[datetime]")
    if not time_el:
        return None
    raw = time_el.get("datetime", "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_timezone.utc)
        return value.astimezone(TIMEZONE)
    except ValueError:
        return None


def post_text(post):
    text_el = post.select_one(".tgme_widget_message_text")
    if not text_el:
        return ""
    return text_el.get_text("\n", strip=True)


def scrape_today(title, today=None):
    """Всегда считывает свежие данные с канала с 00:00 (без кэша) — логика FIFA.py."""
    today = today or datetime.now(TIMEZONE).date()
    config = CHANNELS[title]
    source_url = config["source_url"]

    parsed_url = urlparse(source_url)
    channel_path = parsed_url.path.rstrip("/")
    if not channel_path.startswith("/s/"):
        raise ValueError(f"Expected public Telegram web link t.me/s/...: {source_url}")

    before_id = None
    seen_ids = set()
    matches_map = {}
    checked = 0
    found_older_day = False

    for _ in range(MAX_PAGES_PER_CHANNEL):
        url = source_url if before_id is None else f"https://t.me{channel_path}?before={before_id}"

        response = None
        for attempt in range(3):
            try:
                response = session.get(url, timeout=WEB_REQUEST_TIMEOUT)
                response.raise_for_status()
                break
            except (RequestException, ConnectionResetError, ConnectionAbortedError) as net_err:
                if attempt == 2:
                    raise RuntimeError(
                        f"Сетевая ошибка при запросе к Telegram ({net_err}). Хост разорвал подключение."
                    )
                time.sleep(2 * (attempt + 1))

        soup = BeautifulSoup(response.text, "html.parser")
        posts = soup.select(".tgme_widget_message_wrap")
        if not posts:
            if checked == 0:
                raise RuntimeError(
                    f"Could not read posts from {source_url}. "
                    "Telegram may have limited access to the public page."
                )
            break

        page_ids = []
        for post in posts:
            data_post = post.select_one("[data-post]")
            post_key = data_post.get("data-post", "") if data_post else ""
            try:
                message_id = int(post_key.rsplit("/", 1)[-1])
            except (ValueError, AttributeError):
                message_id = None
            if message_id is not None:
                page_ids.append(message_id)
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)

            checked += 1
            posted_at = post_datetime(post)
            if posted_at and posted_at.date() < today:
                found_older_day = True
                continue
            if posted_at and posted_at.date() > today:
                continue

            parsed = parse_post(post_text(post))
            if parsed:
                matches_map[message_id or 0] = (message_id or 0, posted_at, parsed)

        if found_older_day:
            break
        if not page_ids:
            break
        oldest_id = min(page_ids)
        if before_id is not None and oldest_id >= before_id:
            break
        before_id = oldest_id
        time.sleep(0.15)

    all_matches = list(matches_map.values())
    # Сортировка по времени (утро → вечер), как в FIFA.py
    all_matches.sort(key=lambda item: (item[1] or datetime.min.replace(tzinfo=TIMEZONE), item[0]))
    return [item[2] for item in all_matches], checked


def build_stats(matches):
    """Статистика с поддержкой ничьих 🅿 — логика FIFA.py."""
    stats = {}
    for match in matches:
        if len(match) == 3 and match[0] == "RESULT":
            _, team, outcome = match
            item = stats.setdefault(team, {"win": 0, "lose": 0, "draw": 0, "history": []})
            item["history"].append(outcome)
            if outcome == "✅":
                item["win"] += 1
            else:
                item["lose"] += 1
            continue

        team1, score1, score2, team2 = match

        # Ничья
        if score1 == score2:
            for team in (team1, team2):
                item = stats.setdefault(team, {"win": 0, "lose": 0, "draw": 0, "history": []})
                item["draw"] += 1
                item["history"].append("🅿")
            continue

        winner, loser = (team1, team2) if score1 > score2 else (team2, team1)
        win_item = stats.setdefault(winner, {"win": 0, "lose": 0, "draw": 0, "history": []})
        lose_item = stats.setdefault(loser, {"win": 0, "lose": 0, "draw": 0, "history": []})
        win_item["win"] += 1
        win_item["history"].append("✅")
        lose_item["lose"] += 1
        lose_item["history"].append("❌")
    return stats


def build_p1p2_sequence(matches):
    """Sequence of P1/P2 based on which side won (left=P1, right=P2).
    Draws (including 0:0) are included as 🅿."""
    sequence = []
    for match in matches:
        if len(match) == 3 and match[0] == "RESULT":
            continue
        team1, score1, score2, team2 = match
        if score1 == score2:
            sequence.append("🅿")
            continue
        sequence.append("П1" if score1 > score2 else "П2")
    return sequence


def format_report(stats, title, checked):
    """Формат отчёта с ничьими и разворотом истории — FIFA.py."""
    header = f"📊 <b>Итоговая статистика матчей {html.escape(title)} (с 00:00)</b>"
    if not stats:
        return header + f"\n\nℹ️ Сегодня не найдено распознанных матчей (проверено публикаций: {checked})."
    sections = []
    for team, item in sorted(stats.items(), key=lambda pair: (pair[1]["win"], pair[1]["lose"]), reverse=True):
        reversed_history = "".join(reversed(item["history"]))
        total = item["win"] + item["lose"] + item["draw"]
        sections.append(
            f"<b>{html.escape(team)}:</b>\n"
            f"{reversed_history}\n"
            f"Итог: ✅ {item['win']} | ❌ {item['lose']} | 🅿 {item['draw']} | 🎮 {total} игр"
        )
    return header + "\n\n" + "\n\n".join(sections)


def format_p1p2_report(sequence, title, checked):
    """Последовательность П1/П2/🅿 с разворотом — FIFA.py."""
    header = f"📋 <b>Последовательность исходов (П1/П2/🅿) — {html.escape(title)} (с 00:00)</b>"
    if not sequence:
        return header + f"\n\nℹ️ Сегодня не найдено матчей (проверено публикаций: {checked})."
    reversed_sequence = list(reversed(sequence))
    body = ", ".join(reversed_sequence)
    p1 = sequence.count("П1")
    p2 = sequence.count("П2")
    draws = sequence.count("🅿")
    return (
        header
        + "\n\n"
        + body
        + f"\n\n🎮 П1: {p1} | П2: {p2} | 🅿: {draws} | Всего: {len(sequence)} матчей"
    )


def get_today_report(title):
    matches, checked = scrape_today(title)
    return format_report(build_stats(matches), title, checked)


def get_today_p1p2_report(title):
    matches, checked = scrape_today(title)
    sequence = build_p1p2_sequence(matches)
    return format_p1p2_report(sequence, title, checked)


def send_report(title, chat_id=None):
    config = CHANNELS[title]
    target = chat_id if chat_id is not None else config["chat_id"]
    report = get_today_report(title)
    send_text(target, report)
    log.info("Отчёт (stats) отправлен: %s -> chat_id=%s", title, target)


def send_p1p2_report(title, chat_id=None):
    config = CHANNELS[title]
    target = chat_id if chat_id is not None else config["chat_id"]
    report = get_today_p1p2_report(title)
    send_text(target, report)
    log.info("Отчёт (П1/П2) отправлен: %s -> chat_id=%s", title, target)


# ===================== ГРАФИЧЕСКИЙ ИНТЕРФЕЙС (GUI) =====================
class AutoFitText(tk.Text):
    def __init__(self, master, font_size=11, **kw):
        super().__init__(
            master,
            font=("Segoe UI", font_size, "bold"),
            bd=0,
            highlightthickness=0,
            relief="flat",
            wrap="char",
            spacing1=1,
            spacing3=1,
            **kw,
        )
        self.bind("<<Modified>>", self._adjust)

    def _adjust(self, event=None):
        if self.edit_modified():
            self.update_idletasks()
            lines = int(self.count("1.0", "end", "displaylines")[0]) if self.get("1.0", "end-1c") else 1
            self.configure(height=lines)
            self.edit_modified(False)


class TeamCard(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, corner_radius=14, fg_color="#edf2f7")

        self.lbl_team = ctk.CTkLabel(
            self, text="", font=("Segoe UI", 13, "bold"), text_color="#0f172a"
        )
        self.lbl_team.pack(anchor="w", padx=12, pady=(6, 2))

        # История на всю ширину карточки, без скроллбара, высота под все строки
        self.txt_hist = AutoFitText(self, font_size=12, bg="#edf2f7")
        self.txt_hist.pack(fill="x", expand=False, padx=10, pady=0)
        self.txt_hist.configure(cursor="arrow")
        self.txt_hist.bind("<Key>", lambda e: "break")  # только чтение
        self.txt_hist.tag_config("win_icon", foreground="#16a34a", font=("Segoe UI", 12, "bold"))
        self.txt_hist.tag_config("lose_icon", foreground="#dc2626", font=("Segoe UI", 12, "bold"))
        self.txt_hist.tag_config("draw_icon", foreground="#ca8a04", font=("Segoe UI", 12, "bold"))

        self.sum_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sum_frame.pack(anchor="w", padx=12, pady=(2, 6))

        self.lbl_intro = ctk.CTkLabel(self.sum_frame, text="Итог: ", font=("Segoe UI", 11), text_color="#334155")
        self.lbl_intro.pack(side="left")

        self.lbl_win = ctk.CTkLabel(self.sum_frame, text="", font=("Segoe UI", 11, "bold"), text_color="#16a34a")
        self.lbl_win.pack(side="left")

        self.lbl_mid = ctk.CTkLabel(self.sum_frame, text=" | ", font=("Segoe UI", 11), text_color="#334155")
        self.lbl_mid.pack(side="left")

        self.lbl_lose = ctk.CTkLabel(self.sum_frame, text="", font=("Segoe UI", 11, "bold"), text_color="#dc2626")
        self.lbl_lose.pack(side="left")

        self.lbl_draw = ctk.CTkLabel(self.sum_frame, text="", font=("Segoe UI", 11, "bold"), text_color="#ca8a04")
        self.lbl_draw.pack(side="left")

        self.lbl_total = ctk.CTkLabel(self.sum_frame, text="", font=("Segoe UI", 11), text_color="#334155")
        self.lbl_total.pack(side="left")

    def update_data(self, team_name, history, win, lose, draw=0):
        self.lbl_team.configure(text=f"• {team_name}:")

        self.txt_hist.configure(state="normal")
        self.txt_hist.delete("1.0", tk.END)
        # Разворот истории (последние матчи справа) — как в FIFA.py
        # Значки идут подряд, перенос по ширине — без пустот
        reversed_history = list(reversed(history))
        for char in reversed_history:
            if char == "✅":
                ui_char, tag = "✔", "win_icon"
            elif char == "❌":
                ui_char, tag = "✖", "lose_icon"
            else:  # 🅿 ничья
                ui_char, tag = "🅿", "draw_icon"
            self.txt_hist.insert(tk.END, f"{ui_char} ", tag)
        self.txt_hist.event_generate("<<Modified>>")
        self.txt_hist.configure(state="disabled")

        self.lbl_win.configure(text=f"✔ {win}")
        self.lbl_lose.configure(text=f"✖ {lose}")
        self.lbl_draw.configure(text=f" | 🅿 {draw}" if draw else "")
        total = win + lose + draw
        self.lbl_total.configure(text=f" | 🎮 {total} игр")


class ChannelColumn:
    def __init__(self, parent, title):
        self.title = title
        self.is_visible = True
        self.seq_visible = True   # Развернут по умолчанию, чтобы размеры корректно инициализировались при старте

        self.scroll = ctk.CTkScrollableFrame(
            parent, corner_radius=18, fg_color="#d0d7e0",
            scrollbar_button_color="#94a3b8", scrollbar_button_hover_color="#64748b",
        )

        self.lbl_title = ctk.CTkLabel(
            self.scroll, text=title, font=("Segoe UI", 13, "bold"), text_color="#1e293b"
        )
        self.lbl_title.pack(anchor="w", padx=12, pady=(8, 4))

        self.seq_card = ctk.CTkFrame(self.scroll, corner_radius=14, fg_color="#edf2f7")
        self.seq_card.pack(fill="x", padx=2, pady=(0, 8))

        self.btn_toggle_seq = ctk.CTkButton(
            self.seq_card,
            text="👁 Скрыть последовательность",
            font=("Segoe UI", 12, "bold"),
            fg_color="#cbd5e1",
            hover_color="#cbd5e1",
            text_color="#0f172a",
            corner_radius=10,
            height=28,
            anchor="w",
            command=self.toggle_sequence,
        )
        self.btn_toggle_seq.pack(fill="x", padx=8, pady=(8, 4))

        self.txt_seq = AutoFitText(self.seq_card, font_size=10, bg="#edf2f7")
        self.txt_seq.pack(fill="x", padx=12, pady=0)
        self.txt_seq.tag_config("p1", foreground="#16a34a", font=("Segoe UI", 10, "bold"))
        self.txt_seq.tag_config("p2", foreground="#dc2626", font=("Segoe UI", 10, "bold"))
        self.txt_seq.tag_config("draw", foreground="#ca8a04", font=("Segoe UI", 10, "bold"))
        self.txt_seq.tag_config("plain", foreground="#334155", font=("Segoe UI", 10))

        self.lbl_seq_summary = ctk.CTkFrame(self.seq_card, fg_color="transparent")
        self.lbl_seq_summary.pack(anchor="w", padx=12, pady=(4, 8))

        self.lbl_p1_sum = ctk.CTkLabel(
            self.lbl_seq_summary, text="🎮 П1: 0", font=("Segoe UI", 11, "bold"), text_color="#16a34a"
        )
        self.lbl_p1_sum.pack(side="left")

        self.lbl_p2_sum = ctk.CTkLabel(
            self.lbl_seq_summary, text=" | П2: 0", font=("Segoe UI", 11, "bold"), text_color="#dc2626"
        )
        self.lbl_p2_sum.pack(side="left")

        self.lbl_draw_sum = ctk.CTkLabel(
            self.lbl_seq_summary, text=" | 🅿: 0", font=("Segoe UI", 11, "bold"), text_color="#ca8a04"
        )
        self.lbl_draw_sum.pack(side="left")

        self.lbl_total_sum = ctk.CTkLabel(
            self.lbl_seq_summary, text=" | Всего: 0", font=("Segoe UI", 11, "bold"), text_color="#0f172a"
        )
        self.lbl_total_sum.pack(side="left")

        self.lbl_sep = ctk.CTkLabel(
            self.scroll, text="----------------------------------------------------------------",
            font=("Segoe UI", 10), text_color="#94a3b8",
        )
        self.lbl_sep.pack(anchor="w", padx=8, pady=(0, 4))

        self.lbl_stats_header = ctk.CTkLabel(
            self.scroll, text=f"📊 Итоговая статистика {title} (с 00:00)",
            font=("Segoe UI", 12, "bold"), text_color="#0f172a",
        )
        self.lbl_stats_header.pack(anchor="w", padx=8, pady=(0, 6))

        self.teams_container = ctk.CTkFrame(self.scroll, fg_color="transparent")
        self.teams_container.pack(fill="x", expand=True)

        self.team_cards = []

    def toggle_sequence(self):
        """Показать / скрыть блок последовательности П1/П2/🅿."""
        if self.seq_visible:
            self.txt_seq.pack_forget()
            self.lbl_seq_summary.pack_forget()
            self.btn_toggle_seq.configure(
                text="👁 Показать последовательность",
                fg_color="#e2e8f0",
            )
            self.seq_visible = False
        else:
            self.txt_seq.pack(fill="x", padx=12, pady=0)
            self.lbl_seq_summary.pack(anchor="w", padx=12, pady=(4, 8))
            self.btn_toggle_seq.configure(
                text="👁 Скрыть последовательность",
                fg_color="#cbd5e1",
            )
            self.seq_visible = True
            self.txt_seq.event_generate("<<Modified>>")

    def set_data(self, sequence, checked, stats_items):
        self.txt_seq.configure(state="normal")
        self.txt_seq.delete("1.0", tk.END)
        display_seq = list(reversed(sequence))
        p1 = sequence.count("П1")
        p2 = sequence.count("П2")
        draws = sequence.count("🅿")

        if not sequence:
            self.txt_seq.insert(tk.END, f"ℹ️ Сегодня не найдено матчей (проверено: {checked}).")
        else:
            for i, item in enumerate(display_seq):
                if item == "П1":
                    tag = "p1"
                elif item == "П2":
                    tag = "p2"
                else:
                    tag = "draw"
                self.txt_seq.insert(tk.END, item, tag)
                if i < len(display_seq) - 1:
                    self.txt_seq.insert(tk.END, ", ", "plain")

        self.txt_seq.event_generate("<<Modified>>")
        self.txt_seq.configure(state="disabled")

        self.lbl_p1_sum.configure(text=f"🎮 П1: {p1}")
        self.lbl_p2_sum.configure(text=f" | П2: {p2}")
        self.lbl_draw_sum.configure(text=f" | 🅿: {draws}")
        self.lbl_total_sum.configure(text=f" | Всего: {len(sequence)}")

        needed = len(stats_items)
        while len(self.team_cards) < needed:
            card = TeamCard(self.teams_container)
            card.pack(fill="x", padx=2, pady=4)
            self.team_cards.append(card)

        while len(self.team_cards) > needed:
            card = self.team_cards.pop()
            card.destroy()

        for card, (team, res) in zip(self.team_cards, stats_items):
            card.update_data(
                team_name=team,
                history=res["history"],
                win=res["win"],
                lose=res["lose"],
                draw=res.get("draw", 0),
            )

        if self.seq_visible:
            self.txt_seq.event_generate("<<Modified>>")


class StatsApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Fifa Stats Bot (Web Scraper)")
        # Узкое окно — одна колонка, как на скриншоте
        self.geometry("500x900")
        self.minsize(360, 480)
        self.configure(fg_color="#b2bcc8")

        try:
            icon_path = resource_path("ball.ico")
            self.iconbitmap(icon_path)
        except Exception as e:
            print(f"Не удалось установить иконку окна: {e}")

        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        # Одна активная колонка — всегда column 0
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)

        channels_list = list(CHANNELS.items())
        self.channels_keys = [channels_list[0][0], channels_list[1][0]]
        self.columns = {}

        top_panel = ctk.CTkFrame(self, fg_color="transparent")
        top_panel.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 0))

        self.btn_toggle_fifa18 = ctk.CTkButton(
            top_panel, text="● FIFA18", font=("Segoe UI", 11, "bold"),
            fg_color="#86efac", hover_color="#4ade80", text_color="#14532d",
            corner_radius=14, height=28, width=110,
            command=lambda: self.show_channel(self.channels_keys[0]),
        )
        self.btn_toggle_fifa18.pack(side="left", padx=(0, 8))

        self.btn_toggle_fifa22 = ctk.CTkButton(
            top_panel, text="○ FIFA22", font=("Segoe UI", 11, "bold"),
            fg_color="#e2e8f0", hover_color="#cbd5e1", text_color="#1e293b",
            corner_radius=14, height=28, width=110,
            command=lambda: self.show_channel(self.channels_keys[1]),
        )
        self.btn_toggle_fifa22.pack(side="left")

        col_left = ChannelColumn(self, channels_list[0][0])
        col_left.scroll.grid(row=1, column=0, padx=10, pady=(6, 6), sticky="nsew")
        self.columns[channels_list[0][0]] = col_left

        col_right = ChannelColumn(self, channels_list[1][0])
        # FIFA22 скрыта при старте — показываем только FIFA18
        col_right.is_visible = False
        self.columns[channels_list[1][0]] = col_right

        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 8))

        self.btn_start = ctk.CTkButton(
            bottom_frame,
            text="START / Обновить",
            font=("Segoe UI", 11, "bold"),
            fg_color="#dcfce7",
            hover_color="#bbf7d0",
            text_color="#166534",
            corner_radius=18,
            height=30,
            width=135,
            border_width=1,
            border_color="#86efac",
            command=self.on_start_click,
        )
        self.btn_start.pack(side="left")

        self.status_frame = ctk.CTkFrame(bottom_frame, fg_color="transparent")
        self.status_frame.pack(side="left", padx=12)

        self.lbl_status_text = ctk.CTkLabel(
            self.status_frame,
            text="Готов к работе",
            font=("Segoe UI", 11, "italic"),
            text_color="#334155",
        )
        self.lbl_status_text.pack(side="left")

        self.last_update_time = None
        self.after(1000, self.tick_timer)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)

        self.on_start_click()

    def show_channel(self, title):
        """Переключение: видна только одна колонка (FIFA18 или FIFA22)."""
        for key, col in self.columns.items():
            if key == title:
                if not col.is_visible:
                    col.scroll.grid(row=1, column=0, padx=10, pady=(6, 6), sticky="nsew")
                    col.is_visible = True
            else:
                if col.is_visible:
                    col.scroll.grid_remove()
                    col.is_visible = False

        is_18 = title == self.channels_keys[0]
        if is_18:
            self.btn_toggle_fifa18.configure(
                text="● FIFA18", fg_color="#86efac", text_color="#14532d",
            )
            self.btn_toggle_fifa22.configure(
                text="○ FIFA22", fg_color="#e2e8f0", text_color="#1e293b",
            )
        else:
            self.btn_toggle_fifa18.configure(
                text="○ FIFA18", fg_color="#e2e8f0", text_color="#1e293b",
            )
            self.btn_toggle_fifa22.configure(
                text="● FIFA22", fg_color="#86efac", text_color="#14532d",
            )

    def quit_app(self, event=None):
        stop_event.set()
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)

    def tick_timer(self):
        if self.last_update_time is not None:
            delta = int((datetime.now() - self.last_update_time).total_seconds())
            if delta < 60:
                time_str = f"{delta} сек. назад"
            elif delta < 3600:
                mins = delta // 60
                time_str = f"{mins} мин. назад"
            else:
                hours = delta // 3600
                time_str = f"{hours} ч. назад"

            self.lbl_status_text.configure(
                text=f"Обновлено {time_str}",
                font=("Segoe UI", 14, "bold"),
                text_color="#dc2626",
            )
        self.after(1000, self.tick_timer)

    def on_start_click(self):
        self.btn_start.configure(state="disabled", text="Обновление...")
        self.lbl_status_text.configure(
            text="⏳ Сбор статистики...", font=("Segoe UI", 11, "italic"), text_color="#0284c7"
        )
        threading.Thread(target=self.background_fetch_worker, daemon=True).start()

    def background_fetch_worker(self):
        results = {}
        errors = {}

        def fetch_single(title):
            try:
                matches, checked = scrape_today(title)
                if not matches:
                    results[title] = ([], checked, [])
                else:
                    sequence = build_p1p2_sequence(matches)
                    stats = build_stats(matches)
                    sorted_stats = sorted(
                        stats.items(),
                        key=lambda x: (x[1]["win"], x[1]["lose"]),
                        reverse=True,
                    )
                    results[title] = (sequence, checked, sorted_stats)
            except Exception as e:
                log.exception("Ошибка при сборе данных для %s", title)
                errors[title] = str(e)[:100]

        threads = []
        for title in CHANNELS:
            t = threading.Thread(target=fetch_single, args=(title,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        self.after(0, lambda: self.apply_updates(results, errors))

    def apply_updates(self, results, errors):
        for title, data in results.items():
            sequence, checked, sorted_stats = data
            self.columns[title].set_data(sequence, checked, sorted_stats)

        for title, err_msg in errors.items():
            col = self.columns[title]
            col.txt_seq.configure(state="normal")
            col.txt_seq.delete("1.0", tk.END)
            col.txt_seq.insert(tk.END, f"✖ Ошибка: {err_msg}")
            col.txt_seq.event_generate("<<Modified>>")
            col.txt_seq.configure(state="disabled")

        self.update_idletasks()

        self.last_update_time = datetime.now()
        self.lbl_status_text.configure(
            text="Обновлено только что", font=("Segoe UI", 14, "bold"), text_color="#dc2626"
        )
        self.btn_start.configure(state="normal", text="START / Обновить")


# ===================== ПОЛЛИНГ TELEGRAM BOT API =====================
def handle_update(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if not text:
        return

    log.info("Получено из chat_id=%s: %s", chat_id, text[:80])

    title_by_chat = {cfg["chat_id"]: title for title, cfg in CHANNELS.items()}
    title = title_by_chat.get(chat_id)

    cmd = text.lower().strip()

    if cmd in ("start", "старт"):
        if not title:
            send_text(chat_id, "Я работаю только в настроенных группах BOT_FIFA_18 и BOT_FIFA_22.")
            return
        send_text(chat_id, f"⏳ Собираю сегодняшнюю статистику {html.escape(title)}...")
        try:
            send_report(title, chat_id)
            send_text(chat_id, "✔ Готово!")
        except Exception as exc:
            log.exception("Ошибка отчёта %s", title)
            send_text(chat_id, f"❌ Не удалось собрать отчёт: <code>{html.escape(str(exc)[:700])}</code>")
        return

    if cmd in ("/start", "/start@leontimizm_bot", "/start@fifa_stats_bot"):
        if not title:
            send_text(chat_id, "Я работаю только в настроенных группах BOT_FIFA_18 и BOT_FIFA_22.")
            return
        send_text(chat_id, f"⏳ Собираю последовательность П1/П2 {html.escape(title)}...")
        try:
            send_p1p2_report(title, chat_id)
            send_text(chat_id, "✔ Готово!")
        except Exception as exc:
            log.exception("Ошибка П1/П2 отчёта %s", title)
            send_text(chat_id, f"❌ Не удалось собрать отчёт: <code>{html.escape(str(exc)[:700])}</code>")
        return


def run_bot_polling():
    offset = None
    while not stop_event.is_set():
        params = {"timeout": 10, "allowed_updates": ["message", "edited_message"]}
        if offset is not None:
            params["offset"] = offset
        try:
            updates = telegram_api("getUpdates", params, timeout=20)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception:
                    log.exception("Ошибка обработки update_id=%s", update.get("update_id"))
        except KeyboardInterrupt:
            break
        except Exception:
            if stop_event.is_set():
                break
            time.sleep(5)


def make_tray_icon():
    """Half-green half-red ball icon for system tray."""
    try:
        from PIL import Image, ImageDraw
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.pieslice([2, 2, size - 3, size - 3], 90, 270, fill=(0, 180, 0, 255))
        draw.pieslice([2, 2, size - 3, size - 3], 270, 90, fill=(220, 30, 30, 255))
        draw.ellipse([2, 2, size - 3, size - 3], outline=(40, 40, 40, 255), width=2)
        return img
    except Exception:
        return None


def run_tray():
    """System tray icon. Right-click -> Exit fully closes the bot."""
    try:
        import pystray
        from pystray import MenuItem as item
    except ImportError:
        log.warning("pystray не установлен — значок в трее недоступен.")
        return

    icon_img = make_tray_icon()
    if icon_img is None:
        log.warning("Pillow не установлен — значок в трее недоступен.")
        return

    def on_exit(icon, item):
        log.info("Выход из трея — полное завершение бота.")
        stop_event.set()
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(item("Выход", on_exit))
    icon = pystray.Icon("fifa_stats", icon_img, "FIFA Stats Bot", menu)
    icon.run()


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Не найден токен. Задайте его:\n"
            "set FIFA_BOT_TOKEN=ВАШ_НОВЫЙ_ТОКЕН\n"
            "py FIFA_GUI.py"
        )

    try:
        me = telegram_api("getMe")
        log.info("Запущен бот @%s (Web Scraper + GUI режим).", me.get("username"))
    except Exception as e:
        log.warning("Не удалось подключиться к Telegram API при старте: %s", e)

    bot_thread = threading.Thread(target=run_bot_polling, daemon=True)
    bot_thread.start()

    tray_thread = threading.Thread(target=run_tray, daemon=True)
    tray_thread.start()

    app = StatsApp()
    app.mainloop()


if __name__ == "__main__":
    main()

'''

class MainMobileApp(App):
    def build(self):
        self.title = "Mobile App"
        
        layout = BoxLayout(orientation='vertical', padding=15, spacing=10)
        
        header = Label(
            text="Приложение запущено на Android",
            font_size='20sp',
            size_hint_y=None,
            height=50
        )
        layout.add_widget(header)
        
        self.log_input = TextInput(
            text="Готово к работе.\n",
            readonly=True,
            multiline=True,
            font_size='14sp'
        )
        layout.add_widget(self.log_input)
        
        btn = Button(
            text="Запустить логику",
            size_hint_y=None,
            height=60,
            background_color=(0.2, 0.6, 1, 1)
        )
        btn.bind(on_press=self.on_btn_click)
        layout.add_widget(btn)
        
        return layout

    def on_btn_click(self, instance):
        self.log_input.text += "Кнопка нажата! Логика выполнена.\n"

if __name__ == '__main__':
    MainMobileApp().run()
