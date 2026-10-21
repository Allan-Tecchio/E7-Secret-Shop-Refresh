"""Epic Seven Secret Shop auto refresh - GUI. Double-click to open."""
import base64
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import cv2

import e7_shop as shop

COLOR = {
    "bg": "#141414",
    "card": "#1e1e1e",
    "border": "#2c2c2c",
    "field": "#2a2a2a",
    "text": "#e6e6e6",
    "muted": "#8a8a8a",
    "amber": "#FFBF00",
    "amber_light": "#ffd24d",
    "red": "#e5484d",
    "red_light": "#f06a6e",
    "log": "#101010",
    "notice": "#2a2310",
}
FONT = "Segoe UI"
PRESETS = {
    # mode: (button labels, values)
    "sky": (("60", "150", "300", "900"), (60, 150, 300, 900)),
    "gold": (("1M", "5M", "10M", "50M"), (1_000_000, 5_000_000, 10_000_000, 50_000_000)),
}
ITEMS = [
    # key, name, unit, units per purchase, placeholder color
    ("covenant", "Covenant", "bookmarks", 5, "#4c8dff"),
    ("mystic", "Mystic", "medals", 50, "#b06cff"),
]


STATS_FILE = os.path.join(shop.BASE_DIR, "stats.json")


def fmt(n):
    return f"{n:,}"


def fmt_time(seconds):
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"~{minutes} min"
    return f"~{minutes // 60} h {minutes % 60:02d} min"


def empty_totals():
    return {"covenant": 0, "mystic": 0, "refresh": 0}


def add_totals(a, b):
    return {k: a[k] + b[k] for k in a}


def load_stats():
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: int(data.get(k, 0)) for k in empty_totals()}
    except (OSError, ValueError):
        return empty_totals()


def save_stats(totals):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(totals, f)
    except OSError:
        pass


def item_icon(name, size):
    """Loads the item icon (the same one used to recognize it in the shop)."""
    path = shop.icon_path(name)
    if not os.path.exists(path):
        return None
    img = shop.read_image(path)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = size / max(h, w)
    img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    png = cv2.imencode(".png", img)[1].tobytes()
    return tk.PhotoImage(data=base64.b64encode(png))


class FlatButton(tk.Label):
    """Flat button with a hover effect."""

    def __init__(self, parent, text, command, color, hover, fg, **kw):
        super().__init__(parent, text=text, bg=color, fg=fg, cursor="hand2", **kw)
        self.command, self.color, self.hover = command, color, hover
        self.enabled = True
        self.bind("<Button-1>", lambda _: self.enabled and self.command())
        self.bind("<Enter>", lambda _: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda _: self.configure(bg=self.color))

    def restyle(self, text=None, color=None, hover=None, fg=None, command=None):
        if color:
            self.color, self.hover = color, hover
        if command:
            self.command = command
        self.configure(text=text or self.cget("text"), bg=self.color, fg=fg or self.cget("fg"))

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")


class App:
    def __init__(self, root):
        self.root = root
        root.title("E7 Secret Shop Refresh · Mouse Free")
        root.configure(bg=COLOR["bg"])
        root.resizable(False, False)
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.max_refreshes = 0
        self.images = []
        self.saved = load_stats()   # totals from earlier runs (kept until Reset)
        self.run = empty_totals()   # totals of the run in progress
        self.running = False
        self.run_started = 0.0
        self.run_mode = "sky"
        self.run_gold_limit = 0

        body = tk.Frame(root, bg=COLOR["bg"], padx=18, pady=16)
        body.pack(fill="both", expand=True)

        # --- header ---
        tk.Label(body, text="EPIC SEVEN", font=(FONT, 9, "bold"), fg=COLOR["amber"],
                 bg=COLOR["bg"]).pack(anchor="w")
        tk.Label(body, text="Secret Shop Refresh", font=(FONT, 20, "bold"), fg=COLOR["text"],
                 bg=COLOR["bg"]).pack(anchor="w")
        tk.Label(body, text="Auto refresh that leaves your mouse free", font=(FONT, 10),
                 fg=COLOR["muted"], bg=COLOR["bg"]).pack(anchor="w", pady=(0, 10))

        # --- the one rule people need to know ---
        notice = tk.Frame(body, bg=COLOR["notice"], highlightthickness=1, highlightbackground=COLOR["amber"],
                          padx=12, pady=8)
        notice.pack(fill="x", pady=(0, 12))
        tk.Label(notice, text="⚠  Don't minimize the game", font=(FONT, 10, "bold"), fg=COLOR["amber"],
                 bg=COLOR["notice"]).pack(anchor="w")
        tk.Label(notice, text="Other windows on top of it are fine: keep using your PC normally.",
                 font=(FONT, 9), fg=COLOR["text"], bg=COLOR["notice"]).pack(anchor="w")

        # --- limit: skystones or gold ---
        card = self.card(body)
        top = tk.Frame(card, bg=COLOR["card"])
        top.pack(fill="x")
        tk.Label(top, text="Limit by", font=(FONT, 10), fg=COLOR["muted"], bg=COLOR["card"]).pack(side="left")
        self.mode_buttons = {}
        for mode, text in (("gold", "Gold"), ("sky", "Skystones")):  # packed from the right
            b = FlatButton(top, text, lambda m=mode: self.set_mode(m), COLOR["field"], COLOR["border"],
                           COLOR["text"], font=(FONT, 9, "bold"), padx=10, pady=3)
            b.pack(side="right", padx=(4, 0))
            self.mode_buttons[mode] = b
        self.preset_row = tk.Frame(card, bg=COLOR["card"])
        self.preset_row.pack(fill="x", pady=(8, 0))
        self.mode = "sky"
        self.limits = {"sky": "150", "gold": "10000000"}  # value typed in each mode
        self.limit_var = tk.StringVar(value=self.limits["sky"])
        digits_only = (root.register(lambda s: s == "" or (s.isdigit() and len(s) <= 10)), "%P")
        self.limit_entry = tk.Entry(self.preset_row, textvariable=self.limit_var, width=9, font=(FONT, 16, "bold"),
                                    bg=COLOR["field"], fg=COLOR["text"], insertbackground=COLOR["text"],
                                    relief="flat", justify="center", validate="key", validatecommand=digits_only,
                                    disabledbackground=COLOR["field"], disabledforeground=COLOR["muted"])
        self.limit_entry.pack(side="left", ipady=5)
        self.presets = []
        self.preview = tk.Label(card, font=(FONT, 10), fg=COLOR["muted"], bg=COLOR["card"])
        self.preview.pack(anchor="w", pady=(8, 0))
        self.limit_var.trace_add("write", lambda *_: self.on_limit_change())
        self.set_mode("sky")

        # --- items bought ---
        header = tk.Frame(body, bg=COLOR["bg"])
        header.pack(fill="x", pady=(12, 0))
        tk.Label(header, text="Total bought", font=(FONT, 10), fg=COLOR["muted"],
                 bg=COLOR["bg"]).pack(side="left")
        self.reset_button = FlatButton(header, "Reset", self.reset_totals, COLOR["field"], COLOR["border"],
                                       COLOR["text"], font=(FONT, 9, "bold"), padx=10, pady=3)
        self.reset_button.pack(side="right")
        grid = tk.Frame(body, bg=COLOR["bg"])
        grid.pack(fill="x", pady=(6, 0))
        grid.columnconfigure((0, 1), weight=1, uniform="items")
        self.count, self.detail = {}, {}
        for i, (key, name, unit, _, color) in enumerate(ITEMS):
            c = tk.Frame(grid, bg=COLOR["card"], highlightthickness=1, highlightbackground=COLOR["border"],
                         padx=12, pady=12)
            c.grid(row=0, column=i, sticky="nsew", padx=(0, 5) if i == 0 else (5, 0))
            self.draw_icon(c, key, name, color).grid(row=0, column=0, rowspan=3, padx=(0, 10))
            tk.Label(c, text=name, font=(FONT, 10), fg=COLOR["muted"], bg=COLOR["card"]).grid(
                row=0, column=1, sticky="w")
            self.count[key] = tk.Label(c, text="0", font=(FONT, 22, "bold"), fg=COLOR["amber"], bg=COLOR["card"])
            self.count[key].grid(row=1, column=1, sticky="w")
            self.detail[key] = tk.Label(c, text=f"0 {unit}", font=(FONT, 9), fg=COLOR["muted"], bg=COLOR["card"])
            self.detail[key].grid(row=2, column=1, sticky="w")

        # --- run stats ---
        card = self.card(body, pady=(10, 0))
        stats = tk.Frame(card, bg=COLOR["card"])
        stats.pack(fill="x")
        stats.columnconfigure((0, 1, 2), weight=1, uniform="stats")
        self.stats = {}
        for i, (key, text) in enumerate([("refresh", "Refreshes"), ("sky", "Skystones"), ("gold", "Gold")]):
            self.stats[key] = tk.Label(stats, text="0", font=(FONT, 14, "bold"), fg=COLOR["text"], bg=COLOR["card"])
            self.stats[key].grid(row=0, column=i)
            tk.Label(stats, text=text, font=(FONT, 9), fg=COLOR["muted"], bg=COLOR["card"]).grid(row=1, column=i)
        self.bar = tk.Canvas(card, height=6, bg=COLOR["border"], highlightthickness=0)
        self.bar.pack(fill="x", pady=(12, 0))
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, 6, fill=COLOR["amber"], width=0)
        self.progress = 0.0
        self.bar.bind("<Configure>", lambda _: self.draw_bar())
        self.run_label = tk.Label(card, font=(FONT, 9), fg=COLOR["muted"], bg=COLOR["card"])
        self.run_label.pack(anchor="w", pady=(6, 0))

        # --- main button ---
        self.button = FlatButton(body, "START", self.start, COLOR["amber"], COLOR["amber_light"], COLOR["bg"],
                                 font=(FONT, 12, "bold"), pady=12)
        self.button.pack(fill="x", pady=(14, 0))

        self.status = tk.Label(body, text="Open the Secret Shop in the game and press Start.",
                               font=(FONT, 9), fg=COLOR["muted"], bg=COLOR["bg"], wraplength=400, justify="left")
        self.status.pack(anchor="w", pady=(10, 6))

        # --- log ---
        self.log = tk.Text(body, height=6, width=52, state="disabled", font=("Consolas", 9), wrap="word",
                           bg=COLOR["log"], fg=COLOR["muted"], relief="flat", highlightthickness=1,
                           highlightbackground=COLOR["border"], padx=8, pady=6)
        self.log.pack(fill="x")

        if self.images:
            root.iconphoto(True, self.images[0])
        root.update_idletasks()
        try:  # dark title bar (Windows 10/11)
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass
        if not shop.is_admin():
            self.status.configure(text="Not running as administrator: Windows will block the clicks.",
                                  fg=COLOR["red"])
        self.show_totals()
        root.bind("<Escape>", lambda _: self.request_stop())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.process_queue)

    # ---------- building blocks ----------

    def card(self, parent, pady=(0, 0)):
        c = tk.Frame(parent, bg=COLOR["card"], highlightthickness=1, highlightbackground=COLOR["border"],
                     padx=14, pady=12)
        c.pack(fill="x", pady=pady)
        return c

    def draw_icon(self, parent, key, name, color, size=48):
        canvas = tk.Canvas(parent, width=size, height=size, bg=COLOR["card"], highlightthickness=0)
        image = item_icon(key, size)
        if image:
            self.images.append(image)  # tkinter drops the image unless something keeps a reference
            canvas.create_image(size // 2, size // 2, image=image)
        else:
            canvas.create_oval(4, 4, size - 4, size - 4, fill=color, outline="")
            canvas.create_text(size // 2, size // 2, text=name[0], fill="white", font=(FONT, 16, "bold"))
        return canvas

    # ---------- display ----------

    def set_mode(self, mode):
        self.limits[self.mode] = self.limit_var.get()
        self.mode = mode
        for m, b in self.mode_buttons.items():
            if m == mode:
                b.restyle(color=COLOR["amber"], hover=COLOR["amber_light"], fg=COLOR["bg"])
            else:
                b.restyle(color=COLOR["field"], hover=COLOR["border"], fg=COLOR["text"])
        for b in self.presets:
            b.destroy()
        labels, values = PRESETS[mode]
        self.presets = []
        for label, value in zip(labels, values):
            b = FlatButton(self.preset_row, label, lambda v=value: self.limit_var.set(str(v)), COLOR["field"],
                           COLOR["border"], COLOR["text"], font=(FONT, 10, "bold"), padx=9, pady=8)
            b.pack(side="left", padx=(6, 0))
            self.presets.append(b)
        self.limit_var.set(self.limits[mode])  # also refreshes the preview

    def on_limit_change(self):
        self.update_preview()
        if hasattr(self, "run_label"):  # not built yet during startup
            self.show_totals()

    def read_limit(self):
        try:
            return int(self.limit_var.get())
        except ValueError:
            return 0

    def planned_refreshes(self):
        return self.read_limit() // shop.REFRESH_COST

    def update_preview(self):
        if self.mode == "sky":
            refreshes = self.planned_refreshes()
            estimate = fmt_time(refreshes * shop.seconds_per_refresh())
            text = f"= {refreshes} refreshes  ·  {estimate}  ·  {shop.REFRESH_COST} skystones each"
        else:
            text = f"= up to {fmt(self.read_limit())} gold  ·  no skystone limit"
        self.preview.configure(text=text)

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def show_totals(self):
        t = add_totals(self.saved, self.run)
        for key, _, unit, per_purchase, _ in ITEMS:
            self.count[key].configure(text=str(t[key]))
            self.detail[key].configure(text=f"{fmt(t[key] * per_purchase)} {unit}")
        gold = t["covenant"] * shop.GOLD_COVENANT + t["mystic"] * shop.GOLD_MYSTIC
        self.stats["refresh"].configure(text=fmt(t["refresh"]))
        self.stats["sky"].configure(text=fmt(t["refresh"] * shop.REFRESH_COST))
        self.stats["gold"].configure(text=fmt(gold))
        if self.running and self.run_mode == "gold":
            done, total = shop.gold_spent(self.run), self.run_gold_limit
            text = f"This run: {fmt(done)} / {fmt(total)} gold  ·  {self.run['refresh']} refreshes"
        elif self.running:
            done, total = self.run["refresh"], self.max_refreshes
            # after the first refreshes, use the real pace instead of the estimate
            pace = (time.time() - self.run_started) / done if done else shop.seconds_per_refresh()
            text = f"This run: {done} / {total} refreshes  ·  {fmt_time((total - done) * pace)} left"
        elif self.mode == "gold":
            done, total = 0, self.read_limit()
            text = f"Next run: up to {fmt(total)} gold"
        else:
            done, total = 0, self.planned_refreshes()
            text = f"Next run: {total} refreshes  ·  {fmt_time(total * shop.seconds_per_refresh())}"
        self.progress = done / total if total else 0
        self.run_label.configure(text=text)
        self.draw_bar()

    def reset_totals(self):
        if not messagebox.askyesno("Reset", "Reset the Covenant, Mystic, refresh and gold counters to zero?"):
            return
        self.saved = empty_totals()
        self.run = empty_totals()
        self.max_refreshes = 0
        save_stats(self.saved)
        self.show_totals()

    def draw_bar(self):
        self.bar.coords(self.bar_fill, 0, 0, self.bar.winfo_width() * self.progress, 6)

    def set_running(self, running):
        self.running = running
        if running:
            self.button.restyle("STOP", COLOR["red"], COLOR["red_light"], "white", self.request_stop)
            self.root.title("Running... E7 Secret Shop Refresh · Mouse Free")
        else:
            self.button.restyle("START", COLOR["amber"], COLOR["amber_light"], COLOR["bg"], self.start)
            self.root.title("E7 Secret Shop Refresh · Mouse Free")
        self.button.set_enabled(True)
        self.limit_entry.configure(state="disabled" if running else "normal")
        for b in self.presets + list(self.mode_buttons.values()) + [self.reset_button]:
            b.set_enabled(not running)

    # ---------- actions ----------

    def start(self):
        if self.mode == "sky":
            refreshes = self.planned_refreshes()
            if refreshes < 1:
                messagebox.showwarning("Skystones", f"Enter at least {shop.REFRESH_COST} skystones.")
                return
            run_args = {"max_refreshes": refreshes}
            start_text = f"--- Starting: {refreshes} refreshes ({refreshes * shop.REFRESH_COST} skystones) ---"
        else:
            refreshes, limit = 0, self.read_limit()
            cheapest = min(shop.PRICES.values())
            if limit < cheapest:
                messagebox.showwarning("Gold", f"Enter at least {fmt(cheapest)} gold (the price of a Covenant).")
                return
            run_args = {"gold_limit": limit}
            start_text = f"--- Starting: up to {fmt(limit)} gold (no skystone limit) ---"
            self.run_gold_limit = limit
        icons, missing = shop.load_icons()
        if missing:
            messagebox.showerror("Missing icons", "These files are missing from the 'icons' folder:\n\n"
                                 + "\n".join(missing))
            return
        try:
            hwnd, _ = shop.find_window()
        except shop.Stop as err:
            messagebox.showerror("Game not found", str(err))
            return

        self.max_refreshes = refreshes
        self.run_mode = self.mode
        self.stop_event.clear()
        bot = shop.Bot(hwnd, icons,
                       log=lambda m: self.queue.put(("log", m)),
                       on_change=lambda t: self.queue.put(("totals", t)),
                       stop=self.stop_event)
        self.run = empty_totals()
        self.run_started = time.time()
        self.write(start_text)
        self.status.configure(text="Running... use your PC normally, just don't minimize the game.",
                              fg=COLOR["muted"])
        self.set_running(True)
        self.show_totals()

        def work():
            try:
                reason = bot.run(**run_args)
            except Exception as err:  # unexpected error: show it instead of freezing the window
                reason = f"Unexpected error: {err}"
                self.queue.put(("log", reason))
            self.queue.put(("done", reason))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def request_stop(self):
        if not (self.worker and self.worker.is_alive()):
            return
        self.stop_event.set()
        self.status.configure(text="Stopping...")
        self.button.set_enabled(False)

    def process_queue(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == "log":
                    self.write(data)
                elif kind == "totals":
                    self.run = data
                    save_stats(add_totals(self.saved, self.run))  # saved as it goes, in case it closes mid-run
                    self.show_totals()
                elif kind == "done":
                    self.saved = add_totals(self.saved, self.run)
                    self.run = empty_totals()
                    save_stats(self.saved)
                    self.set_running(False)
                    self.show_totals()
                    self.status.configure(text=data, fg=COLOR["muted"])
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)

    def close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Close", "The bot is running. Stop it and close?"):
                return
            self.stop_event.set()
        self.root.destroy()


def relaunch_as_admin():
    """Reopens this window as administrator (Windows shows the permission prompt)."""
    script = os.path.abspath(__file__)
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}"', None, 1)
    return result > 32


if __name__ == "__main__":
    # the .exe already asks for administrator by itself; only the .pyw needs to relaunch
    if not shop.is_admin() and not getattr(sys, "frozen", False):
        if not relaunch_as_admin():
            tk.Tk().withdraw()
            messagebox.showerror("Administrator required",
                                 "Epic Seven runs as administrator, so Windows only lets this program "
                                 "click it if it runs as administrator too.\n\n"
                                 "Open it again and click Yes on the Windows prompt.")
        sys.exit()
    root = tk.Tk()
    App(root)
    root.mainloop()
