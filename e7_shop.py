"""
Epic Seven - Secret Shop auto refresh (PC client).

Clicks are sent straight to the game window (PostMessage) and the screen is
captured with BitBlt, so your mouse stays free and the game can sit behind
other windows (it just can't be minimized).

The Covenant/Mystic icons and the button positions come from
github.com/Solunium/Epic-Seven-E7-Secret-Shop-Refresh (GPL-3.0).

GUI: run E7ShopRefresh.exe (or e7_shop_gui.pyw)

Command line (run the terminal as administrator):
  python e7_shop.py screenshot       save a screenshot of the game window (screenshot.png)
  python e7_shop.py test             look for the items on the current screen, no clicking
  python e7_shop.py click X Y        send one test click at X Y
  python e7_shop.py scroll           test scrolling the shop list down
  python e7_shop.py run --max 50     buy and refresh up to 50 times
"""
import argparse
import ctypes
import os
import random
import sys
import threading
import time

try:
    import cv2
    import numpy as np
    import win32api
    import win32con
    import win32gui
    import win32ui
except ImportError:
    print("Missing libraries. Run:  pip install -r requirements.txt")
    sys.exit(1)

# in the .exe, screenshots go next to the executable and the icons are bundled inside it
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    ICON_DIR = os.path.join(sys._MEIPASS, "icons")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    ICON_DIR = os.path.join(BASE_DIR, "icons")

# ---------------- settings ----------------
WINDOW_TITLE = "Epic Seven"
THRESHOLD = 0.75         # how close the icon must match the screen (0 to 1)
ORIGINAL_WIDTH = 890     # game area width when the icons were cropped
SCALES = (0.9, 0.95, 1.0, 1.05, 1.1)  # size variations tried around the expected scale

# Positions in the game area, as a fraction of width/height
BUY_X = 0.907                # Buy button on the item's row
BUY_DY = 0.0916              # distance from the top of the icon to its Buy button
CONFIRM_BUY = (0.551, 0.693)
REFRESH = (0.19, 0.915)
CONFIRM_REFRESH = (0.581, 0.639)
SCROLL_FROM = (0.581, 0.639)  # where the drag to scroll the list starts
SCROLL_DISTANCE = 0.40        # how far to drag (fraction of height)

# Random delays in seconds (min, max): feel like a person clicking without being slow
DELAY_BEFORE_CLICK = (0.15, 0.45)
CLICK_HOLD = (0.05, 0.13)
DELAY_POPUP = (0.6, 1.0)          # waiting for the confirmation popup to open
DELAY_AFTER_BUY = (0.9, 1.6)
DELAY_AFTER_REFRESH = (1.3, 2.2)
DRAG_DURATION = (0.35, 0.7)
LONG_BREAK_CHANCE = 0.05  # every now and then pause a bit, like someone got distracted
LONG_BREAK = (1.5, 4.0)
CLICK_JITTER = 4          # pixels: never click the exact same spot twice

REFRESH_COST = 3          # skystones
GOLD_COVENANT = 184_000
GOLD_MYSTIC = 280_000

PRICES = {"covenant": GOLD_COVENANT, "mystic": GOLD_MYSTIC}

ICONS = {"covenant": "covenant.png", "mystic": "mystic.png"}
# ------------------------------------------

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()


class Stop(Exception):
    pass


class Interrupted(Exception):
    pass


# ---------- image files (works with non-ASCII paths) ----------

def read_image(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def save_image(path, img):
    cv2.imencode(".png", img)[1].tofile(path)


def icon_path(name):
    return os.path.join(ICON_DIR, ICONS[name])


def load_icons():
    """Returns (icons, missing_files)."""
    icons, missing = {}, []
    for name, file in ICONS.items():
        path = icon_path(name)
        if os.path.exists(path):
            icons[name] = read_image(path)
        else:
            missing.append(file)
    return icons, missing


# ---------- window ----------

def is_admin():
    # the game runs as administrator; without it Windows blocks the clicks we send to it
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def find_window(title=WINDOW_TITLE):
    """Returns (hwnd, title). Raises Stop if not found."""
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            text = win32gui.GetWindowText(hwnd)
            if title.lower() in text.lower():
                found.append((hwnd, text))

    win32gui.EnumWindows(cb, None)
    if not found:
        raise Stop(f'No window with "{title}" in its title. Is the game open?')
    # prefer an exact title match (avoids picking a browser tab about the game)
    exact = [f for f in found if f[1].strip().lower() == title.lower()]
    return (exact or found)[0]


def client_size(hwnd):
    _, _, w, h = win32gui.GetClientRect(hwnd)
    return w, h


def capture(hwnd):
    if win32gui.IsIconic(hwnd):
        raise Stop("The game window is minimized. It can be behind other windows, but not minimized.")
    w, h = client_size(hwnd)
    hdc = win32gui.GetDC(hwnd)
    dc = win32ui.CreateDCFromHandle(hdc)
    mem = dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(dc, w, h)
    mem.SelectObject(bmp)
    # the game window is OpenGL (GLFW): PrintWindow returns black, BitBlt works
    mem.BitBlt((0, 0), (w, h), dc, (0, 0), win32con.SRCCOPY)
    img = np.frombuffer(bmp.GetBitmapBits(True), dtype=np.uint8).reshape(h, w, 4)
    win32gui.DeleteObject(bmp.GetHandle())
    mem.DeleteDC()
    dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


# ---------- virtual mouse ----------

def _target(hwnd, x, y):
    """Some game windows have a child window that actually receives the mouse."""
    child = win32gui.ChildWindowFromPoint(hwnd, (x, y))
    if child and child != hwnd:
        sx, sy = win32gui.ClientToScreen(hwnd, (x, y))
        cx, cy = win32gui.ScreenToClient(child, (sx, sy))
        return child, cx, cy
    return hwnd, x, y


def click(hwnd, x, y):
    x += random.randint(-CLICK_JITTER, CLICK_JITTER)
    y += random.randint(-CLICK_JITTER, CLICK_JITTER)
    target, cx, cy = _target(hwnd, int(x), int(y))
    lp = win32api.MAKELONG(cx, cy)
    win32gui.PostMessage(target, win32con.WM_MOUSEMOVE, 0, lp)
    time.sleep(random.uniform(0.02, 0.06))
    win32gui.PostMessage(target, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(random.uniform(*CLICK_HOLD))
    win32gui.PostMessage(target, win32con.WM_LBUTTONUP, 0, lp)


def drag(hwnd, x1, y1, x2, y2, steps=20, duration=0.5):
    target, tx, ty = _target(hwnd, int(x1), int(y1))
    dx, dy = tx - int(x1), ty - int(y1)

    def lp(x, y):
        return win32api.MAKELONG(int(x) + dx, int(y) + dy)

    win32gui.PostMessage(target, win32con.WM_MOUSEMOVE, 0, lp(x1, y1))
    win32gui.PostMessage(target, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp(x1, y1))
    for i in range(1, steps + 1):
        x = x1 + (x2 - x1) * i / steps
        y = y1 + (y2 - y1) * i / steps
        win32gui.PostMessage(target, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lp(x, y))
        time.sleep(duration / steps)
    time.sleep(0.15)  # hold at the end so the list doesn't keep sliding
    win32gui.PostMessage(target, win32con.WM_LBUTTONUP, 0, lp(x2, y2))


# ---------- image matching ----------

def find(screen, template, threshold=THRESHOLD, limit=10):
    """Returns [(x, y, score)] with the top-left corner of each place the template shows up."""
    h, w = template.shape[:2]
    if screen.shape[0] < h or screen.shape[1] < w:
        return []
    res = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    hits = []
    while len(hits) < limit:
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score < threshold:
            break
        hits.append((x, y, score))
        # blank out the area so the same spot isn't found again
        res[max(0, y - h // 2):y + h // 2 + 1, max(0, x - w // 2):x + w // 2 + 1] = -1
    return hits


def find_item(screen, icon, threshold=THRESHOLD):
    """Looks for the icon at a few sizes (the window may be bigger or smaller than the original).
    Returns (hits, best_score, scale); each hit is (x, y_top, score)."""
    base = screen.shape[1] / ORIGINAL_WIDTH
    best = ([], -1.0, base)
    for variation in SCALES:
        scale = base * variation
        template = cv2.resize(icon, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        h, w = template.shape[:2]
        if screen.shape[0] < h or screen.shape[1] < w:
            continue
        score = cv2.minMaxLoc(cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED))[1]
        if score > best[1]:
            best = (find(screen, template, threshold), score, scale)
    return best


# ---------- the bot ----------

def seconds_per_refresh():
    """Average time of one refresh cycle with the delays above (used for the time estimate)."""
    def avg(span):
        return sum(span) / 2

    tap = avg(DELAY_BEFORE_CLICK) + 0.04 + avg(CLICK_HOLD)
    scroll = avg(DRAG_DURATION) + 0.15 + 0.75
    refresh = 2 * tap + avg(DELAY_POPUP) + avg(DELAY_AFTER_REFRESH) + LONG_BREAK_CHANCE * avg(LONG_BREAK)
    return scroll + refresh + 0.3  # + screenshots and image matching


def gold_spent(totals):
    return sum(totals[item] * price for item, price in PRICES.items())


def summary(totals):
    gold = gold_spent(totals)
    return (f"Refreshes: {totals['refresh']} ({totals['refresh'] * REFRESH_COST} skystones)"
            f" | Covenant: {totals['covenant']} ({totals['covenant'] * 5} bookmarks)"
            f" | Mystic: {totals['mystic']} ({totals['mystic'] * 50} medals)"
            f" | Gold spent: {gold:,}")


class Bot:
    def __init__(self, hwnd, icons, log=print, on_change=None, stop=None):
        self.hwnd = hwnd
        self.icons = icons
        self.log = log
        self.on_change = on_change or (lambda totals: None)
        self.stop = stop or threading.Event()
        self.totals = {"covenant": 0, "mystic": 0, "refresh": 0}
        self.gold_limit = None

    def sleep(self, span):
        if self.stop.wait(random.uniform(*span)):
            raise Interrupted()

    def tap(self, fx, fy):
        """Clicks a spot given as a fraction of the game area."""
        w, h = client_size(self.hwnd)
        self.sleep(DELAY_BEFORE_CLICK)
        click(self.hwnd, fx * w, fy * h)

    def scroll(self, down=True):
        w, h = client_size(self.hwnd)
        x = SCROLL_FROM[0] * w + random.randint(-15, 15)
        y_low = SCROLL_FROM[1] * h
        y_high = y_low - SCROLL_DISTANCE * h
        y_start, y_end = (y_low, y_high) if down else (y_high, y_low)
        drag(self.hwnd, x, y_start, x, y_end, duration=random.uniform(*DRAG_DURATION))
        self.sleep((0.6, 0.9))

    def buy_visible(self, bought):
        screen = capture(self.hwnd)
        h = screen.shape[0]
        for item in ("covenant", "mystic"):
            if item in bought:
                continue
            hits, _, _ = find_item(screen, self.icons[item])
            if not hits:
                continue
            if self.gold_limit is not None and gold_spent(self.totals) + PRICES[item] > self.gold_limit:
                self.log(f"  {item.capitalize()} found, but buying it would go over the gold limit.")
                continue
            _, y_top, score = hits[0]
            self.log(f"  {item.capitalize()} found (score {score:.2f}), buying...")
            self.tap(BUY_X, (y_top + BUY_DY * h) / h)
            self.sleep(DELAY_POPUP)
            self.tap(*CONFIRM_BUY)
            self.sleep(DELAY_AFTER_BUY)
            bought.add(item)
            self.totals[item] += 1
            self.on_change(dict(self.totals))

    def refresh(self):
        self.tap(*REFRESH)
        self.sleep(DELAY_POPUP)
        self.tap(*CONFIRM_REFRESH)
        self.totals["refresh"] += 1
        self.on_change(dict(self.totals))
        self.sleep(DELAY_AFTER_REFRESH)
        if random.random() < LONG_BREAK_CHANCE:
            self.sleep(LONG_BREAK)

    def run(self, max_refreshes=None, gold_limit=None):
        """Runs until max_refreshes refreshes are done or gold_limit gold is spent
        (whichever is given). Returns why it stopped."""
        self.gold_limit = gold_limit
        cheapest = min(PRICES.values())
        try:
            # the list goes back to the top by itself after each refresh,
            # so it only needs scrolling up once, in case it starts scrolled down
            self.scroll(down=False)
            while True:
                if gold_limit is not None:
                    self.log(f"[{gold_spent(self.totals):,}/{gold_limit:,} gold] checking the shop...")
                else:
                    self.log(f"[{self.totals['refresh']}/{max_refreshes}] checking the shop...")
                bought = set()
                self.buy_visible(bought)
                if len(bought) < 2:
                    self.scroll(down=True)
                    self.buy_visible(bought)
                if max_refreshes is not None and self.totals["refresh"] >= max_refreshes:
                    reason = "Done: all the skystones you chose were used."
                    break
                if gold_limit is not None and gold_limit - gold_spent(self.totals) < cheapest:
                    reason = "Done: the gold limit was reached."
                    break
                self.refresh()
        except Stop as e:
            reason = f"STOPPED: {e}"
        except Interrupted:
            reason = "Stopped by you."
        self.log(reason)
        self.log(summary(self.totals))
        return reason


# ---------- commands ----------

def cmd_screenshot(hwnd, _args):
    img = capture(hwnd)
    if img.max() < 10:
        print("Warning: the screenshot is black. This client may not allow background capture.")
    path = os.path.join(BASE_DIR, "screenshot.png")
    save_image(path, img)
    print(f"Saved to {path} ({img.shape[1]}x{img.shape[0]})")


def cmd_test(hwnd, _args):
    icons, _ = load_icons()
    screen = capture(hwnd)
    marked = screen.copy()
    for name, icon in icons.items():
        hits, score, scale = find_item(screen, icon)
        status = f"{len(hits)} found" if hits else "not found"
        print(f"  {name:10s} best score {score:.2f} (scale {scale:.2f})  -> {status}")
        ih, iw = int(icon.shape[0] * scale), int(icon.shape[1] * scale)
        for x, y, _ in hits:
            cv2.rectangle(marked, (x, y), (x + iw, y + ih), (0, 255, 0), 2)
            cv2.putText(marked, name, (x, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    path = os.path.join(BASE_DIR, "test.png")
    save_image(path, marked)
    print(f"Marked image saved to {path} (minimum score: {THRESHOLD})")


def cmd_click(hwnd, args):
    click(hwnd, args.x, args.y)
    print(f"Click sent near ({args.x}, {args.y}). Check if the game reacted.")


def cmd_scroll(hwnd, _args):
    Bot(hwnd, {}).scroll(down=True)
    print("Drag sent. The shop list should have scrolled down.")


def cmd_run(hwnd, args):
    icons, missing = load_icons()
    if missing:
        raise Stop("Missing icons in the 'icons' folder: " + ", ".join(missing))
    bot = Bot(hwnd, icons)
    if args.gold:
        print(f"Running until {args.gold:,} gold is spent (no skystone limit). Ctrl+C to stop.")
    else:
        print(f"Running up to {args.max} refreshes (up to {args.max * REFRESH_COST} skystones). Ctrl+C to stop.")
    try:
        if args.gold:
            bot.run(gold_limit=args.gold)
        else:
            bot.run(max_refreshes=args.max)
    except KeyboardInterrupt:
        print("Interrupted.")
        print(summary(bot.totals))


def main():
    p = argparse.ArgumentParser(description="Epic Seven Secret Shop auto refresh")
    p.add_argument("--window", default=WINDOW_TITLE, help="part of the game window title")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("screenshot").set_defaults(func=cmd_screenshot)
    sub.add_parser("test").set_defaults(func=cmd_test)
    c = sub.add_parser("click")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.set_defaults(func=cmd_click)
    sub.add_parser("scroll").set_defaults(func=cmd_scroll)
    r = sub.add_parser("run")
    r.add_argument("--max", type=int, default=20, help="maximum number of refreshes (default 20)")
    r.add_argument("--gold", type=int, help="stop at this much gold spent instead of a refresh count")
    r.set_defaults(func=cmd_run)
    args = p.parse_args()

    try:
        hwnd, title = find_window(args.window)
        print(f'Using window: "{title}"')
        if args.command in ("click", "scroll", "run") and not is_admin():
            raise Stop("The game runs as administrator, so Windows blocks the clicks. "
                       "Open the terminal as administrator and try again.")
        args.func(hwnd, args)
    except Stop as e:
        print("ERROR:", e)


if __name__ == "__main__":
    main()
