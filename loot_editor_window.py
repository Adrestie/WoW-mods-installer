# -*- coding: utf-8 -*-
"""The loot editor's window (MIT licence).

The user connects to the world database (through the worldserver folder) and
to the game (for the icons), searches items, drags them into the families of
DynamicLoot and into lists, sets each draw's chance and quantity, and
generates the <Mod>.Loot.* lines to paste into the mod's .conf.

The window shows a loot_model.Project and changes it; every change redraws
what it touched and the preview at the bottom."""
import os
import queue
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, ttk
from tkinter import font as tkfont

import core
import loot_data
import loot_model
import loot_strings
from window import (ACCENT, BAD, BG, BORDER, BUTTON, BUTTON_HOVER, CARD, FIELD, GOOD, MUTED, SELECTED, TEXT,
                    ReadOnlyText, dark_title_bar)

ICON = 36
SMALL_ICON = 28
DRAG_THRESHOLD = 5
CHAIN_BG = "#24262a"
LIST_COLOUR = "#c7a24a"


class DragDrop(object):
    """Drag and drop between the window's widgets. A source gives its payload when the drag starts; a
    target is a widget (or one of its children) whose callback takes (payload, copy) on a drop. A press
    and release without moving is a click."""

    def __init__(self, root):
        self.root = root
        self.targets = {}
        self.start = None
        self.payload = None
        self.ghost = None
        self.click = None

    def source(self, widget, payload, image=None, click=None):
        for w in [widget] + list(widget.winfo_children()):
            w.bind("<ButtonPress-1>", lambda e: self._press(e, payload, image, click))
            w.bind("<B1-Motion>", self._motion)
            w.bind("<ButtonRelease-1>", self._release)

    def target(self, widget, callback):
        self.targets[str(widget)] = callback

    def forget(self, widget):
        self.targets.pop(str(widget), None)

    def _press(self, event, payload, image, click):
        self.start = (event.x_root, event.y_root)
        self.payload = (payload, image)
        self.click = click

    def _motion(self, event):
        if not self.start:
            return
        if not self.ghost:
            if abs(event.x_root - self.start[0]) + abs(event.y_root - self.start[1]) < DRAG_THRESHOLD:
                return
            self.ghost = tk.Toplevel(self.root)
            self.ghost.overrideredirect(True)
            self.ghost.attributes("-alpha", 0.8)
            self.ghost.attributes("-topmost", True)
            image = self.payload[1]
            tk.Label(self.ghost, image=image, bg=ACCENT, bd=0, padx=1, pady=1).pack() if image else \
                tk.Label(self.ghost, text="•", bg=ACCENT, fg="white", padx=6).pack()
        self.ghost.geometry("+%d+%d" % (event.x_root + 8, event.y_root + 8))

    def _release(self, event):
        start, payload, click = self.start, self.payload, self.click
        self.start = self.payload = self.click = None
        if not self.ghost:
            if start and click:
                click()
            return
        self.ghost.destroy()
        self.ghost = None
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        copy = bool(event.state & 0x0004)           # Ctrl held
        while widget is not None:
            callback = self.targets.get(str(widget))
            if callback:
                callback(payload[0], copy)
                return
            widget = widget.master


class Scrolled(ttk.Frame):
    """A frame that scrolls vertically: its content goes in .inner."""

    def __init__(self, master, background=BG):
        super().__init__(master)
        self.canvas = tk.Canvas(self, background=background, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        self.inner = tk.Frame(self.canvas, background=background)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._wheel))
            w.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"))

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class Tooltip(object):
    """A small text that follows the mouse over a widget."""

    def __init__(self, root):
        self.root = root
        self.top = None

    def attach(self, widget, text):
        for w in [widget] + list(widget.winfo_children()):
            w.bind("<Enter>", lambda e: self.show(e, text), add="+")
            w.bind("<Leave>", lambda e: self.hide(), add="+")

    def show(self, event, text):
        self.hide()
        self.top = tk.Toplevel(self.root)
        self.top.overrideredirect(True)
        tk.Label(self.top, text=text() if callable(text) else text, bg=CARD, fg=TEXT, justify="left",
                 font=("Segoe UI", 9), padx=8, pady=4, highlightthickness=1, highlightbackground=BORDER).pack()
        self.top.geometry("+%d+%d" % (event.x_root + 14, event.y_root + 14))

    def hide(self):
        if self.top:
            self.top.destroy()
            self.top = None


class LootEditor(object):
    def __init__(self, root):
        self.root = root
        self.settings = loot_data.load_settings()
        self.events = queue.Queue()
        self.world = None
        self.icons = None
        self.families = list(loot_model.BUILT_IN_FAMILIES)
        self.items = {}               # {entry: loot_data.Item}
        self.photos = {}              # {(display, size): PhotoImage}, kept alive
        self.project = loot_model.Project(self.settings.get("prefix") or "")
        self.results = []
        self.prefix_values = []
        self.moving = None
        self.working = False
        self.preview_pending = False
        self.language = self.settings.get("language") or loot_strings.language_of(self.settings.get("locale"))
        self.t = loot_strings.Texts(self.language)

        root.withdraw()
        root.configure(bg=BG)
        scale = root.winfo_fpixels("1i") / 96.0
        root.geometry("%dx%d" % (min(int(1380 * scale), root.winfo_screenwidth() - 40),
                                 min(int(900 * scale), root.winfo_screenheight() - 80)))
        root.minsize(int(1000 * scale), int(680 * scale))
        self.styles()
        self.dnd = DragDrop(root)
        self.tip = Tooltip(root)
        self.main = None
        self.build()
        dark_title_bar(root)
        root.deiconify()
        root.after(80, self.poll)
        if self.settings.get("server"):
            root.after(200, self.connect)

    # -- look -----------------------------------------------------------------
    def styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, focuscolor=ACCENT,
                        selectbackground=SELECTED, selectforeground=TEXT, insertcolor=TEXT, font=("Segoe UI", 9))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("CardHint.TLabel", background=CARD, foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 8, "bold"), foreground=MUTED)
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("Good.TLabel", foreground=GOOD)
        style.configure("Bad.TLabel", foreground=BAD)
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, padding=(6, 3))
        style.configure("TSpinbox", fieldbackground=FIELD, foreground=TEXT, arrowcolor=MUTED, padding=(4, 2))
        style.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT, arrowcolor=MUTED, padding=(4, 2))
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SELECTED)
        style.configure("TButton", background=BUTTON, foreground=TEXT, bordercolor=BUTTON, lightcolor=BUTTON,
                        darkcolor=BUTTON, padding=(12, 4))
        style.map("TButton", background=[("disabled", CARD), ("active", BUTTON_HOVER)],
                  foreground=[("disabled", MUTED)])
        style.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT, font=("Segoe UI", 9, "bold"), padding=(16, 5))
        style.map("Accent.TButton", background=[("disabled", BUTTON), ("active", "#6f9cf2")])
        style.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=(12, 4), bordercolor=BORDER,
                        font=("Segoe UI", 9))
        style.map("TNotebook.Tab", background=[("selected", BUTTON)], foreground=[("selected", TEXT)])
        style.configure("Vertical.TScrollbar", background=BUTTON, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
        style.configure("Sash", sashthickness=6, background=BG)

    # -- building -------------------------------------------------------------
    def build(self):
        """(Re)builds the whole window in the current language; the project stays."""
        t = self.t
        if self.main is not None:
            self.main.destroy()
            self.dnd.targets.clear()
        self.root.title(t("title"))
        self.main = ttk.Frame(self.root, padding=(16, 12, 16, 10))
        self.main.pack(fill="both", expand=True)

        head = ttk.Frame(self.main)
        head.pack(fill="x")
        ttk.Label(head, text=t("title"), style="Title.TLabel").pack(side="left")
        lang = ttk.Combobox(head, values=["Français", "English"], state="readonly", width=9)
        lang.current(0 if self.language == "fr" else 1)
        lang.bind("<<ComboboxSelected>>", lambda e: self.set_language("fr" if lang.current() == 0 else "en"))
        lang.pack(side="right")
        ttk.Label(head, text=t("language")).pack(side="right", padx=6)
        ttk.Label(self.main, text=t("subtitle"), style="Hint.TLabel").pack(anchor="w", pady=(0, 8))

        # folders
        box = ttk.Frame(self.main, style="Card.TFrame", padding=(12, 8))
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        self.vars = {}
        for row, (key, label, hint) in enumerate((("client", "game", "game_hint"), ("server", "server", "server_hint"))):
            ttk.Label(box, text=t(label), style="Card.TLabel", width=22).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=self.settings.get(key) or "")
            self.vars[key] = var
            entry = ttk.Entry(box, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", padx=6, pady=2)
            ttk.Button(box, text=t("browse"), command=lambda k=key: self.browse(k)).grid(row=row, column=2, pady=2)
            self.tip.attach(entry, t(hint))
        bar = ttk.Frame(box, style="Card.TFrame")
        bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.connect_button = ttk.Button(bar, text=t("connect"), style="Accent.TButton", command=self.connect)
        self.connect_button.pack(side="right")
        self.status = ttk.Label(bar, text=self.status_text if hasattr(self, "status_text") else t("not_connected"),
                                style="CardHint.TLabel")
        self.status.pack(side="left")

        # the mod
        mod = ttk.Frame(self.main)
        mod.pack(fill="x", pady=(10, 6))
        ttk.Label(mod, text=t("prefix")).pack(side="left")
        self.prefix = ttk.Combobox(mod, width=22, values=self.prefix_values)
        self.prefix.set(self.project.prefix)
        self.prefix.pack(side="left", padx=(6, 18))
        self.prefix.bind("<<ComboboxSelected>>", lambda e: self.prefix_changed())
        self.prefix.bind("<KeyRelease>", lambda e: self.prefix_changed())
        ttk.Label(mod, text=t("rate")).pack(side="left")
        self.rate = tk.StringVar(value=loot_model.number(self.project.rate))
        rate = ttk.Spinbox(mod, from_=0, to=10000, increment=10, width=8, textvariable=self.rate)
        rate.pack(side="left", padx=6)
        self.rate.trace_add("write", lambda *_: self.rate_changed())
        ttk.Button(mod, text=t("new"), command=self.clear).pack(side="right")
        ttk.Button(mod, text=t("import"), command=self.import_conf).pack(side="right", padx=6)

        # bottom: preview and Generate
        bottom = ttk.Frame(self.main)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        top_line = ttk.Frame(bottom)
        top_line.pack(fill="x")
        ttk.Label(top_line, text=t("preview"), style="Section.TLabel").pack(side="left")
        ttk.Button(top_line, text=t("generate"), style="Accent.TButton", command=self.generate).pack(side="right")
        self.preview = tk.Text(bottom, height=7, wrap="none", font=("Consolas", 9), relief="flat", background=FIELD,
                               foreground=TEXT, insertbackground=TEXT, selectbackground=SELECTED,
                               highlightthickness=1, highlightbackground=BORDER, padx=6, pady=4)
        self.preview.pack(fill="x", pady=(4, 0))

        # the three panes
        panes = ttk.PanedWindow(self.main, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, width=330)
        centre = ttk.Frame(panes)
        right = ttk.Frame(panes, width=300)
        panes.add(left, weight=0)
        panes.add(centre, weight=1)
        panes.add(right, weight=0)
        self.build_search(left)
        self.build_families(centre)
        self.build_side(right)
        self.show_results()
        self.refresh_all()

    def build_search(self, parent):
        t = self.t
        ttk.Label(parent, text=t("search"), style="Section.TLabel").pack(anchor="w")
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=(4, 2))
        self.query = tk.StringVar(value=self.settings.get("query") or "")
        entry = ttk.Entry(line, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.search())
        self.tip.attach(entry, t("search_hint"))
        ttk.Button(line, text=t("search_button"), command=self.search).pack(side="left", padx=(6, 0))
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=2)
        ttk.Label(line, text=t("quality")).pack(side="left")
        self.quality = ttk.Combobox(line, values=[t("any_quality")] + t("qualities"), state="readonly", width=12)
        self.quality.current(0)
        self.quality.pack(side="left", padx=6)
        self.result_text = ttk.Label(parent, text="", style="Hint.TLabel")
        self.result_text.pack(anchor="w", pady=(2, 2))
        self.results_box = Scrolled(parent, background=FIELD)
        self.results_box.pack(fill="both", expand=True)
        self.dnd.target(self.results_box.inner, self.drop_delete)
        self.dnd.target(self.results_box.canvas, self.drop_delete)
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=(4, 0))
        ttk.Button(line, text=t("add_all"), command=self.add_all).pack(side="left")
        self.add_all_list = ttk.Combobox(line, state="readonly", width=14)
        self.add_all_list.pack(side="left", padx=6)
        ttk.Label(parent, text=t("drop_delete"), style="Hint.TLabel").pack(anchor="w", pady=(2, 0))

    def build_families(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True, padx=8)
        self.family_rows = {}           # {family: frame of its draws}
        for tab, groups in loot_model.layout_for(self.families):
            page = Scrolled(self.notebook)
            self.notebook.add(page, text=self.t("tabs").get(tab, tab))
            for group, names in groups:
                tk.Label(page.inner, text=self.t("groups").get(group, group).upper(), bg=BG, fg=MUTED,
                         font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", padx=6, pady=(10, 2))
                for name in names:
                    row = tk.Frame(page.inner, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
                    row.pack(fill="x", padx=6, pady=2)
                    label = tk.Frame(row, bg=CARD)
                    label.pack(side="left", fill="y")
                    tk.Frame(label, bg=CARD, width=230, height=0).pack()     # the column's width
                    tk.Label(label, text=name, bg=CARD, fg=TEXT, font=("Segoe UI", 9, "bold"),
                             anchor="w").pack(fill="x", padx=8, pady=(6, 0))
                    tk.Label(label, text=self.t.family(name), bg=CARD, fg=MUTED, font=("Segoe UI", 8),
                             anchor="w", justify="left", wraplength=215).pack(fill="x", padx=8, pady=(0, 6))
                    draws = tk.Frame(row, bg=CARD)
                    draws.pack(side="left", fill="both", expand=True, padx=4, pady=4)
                    self.family_rows[name] = draws
                    for w in (row, label, draws):
                        self.dnd.target(w, lambda p, c, n=name: self.drop_family(n, p, c))

    def build_side(self, parent):
        t = self.t
        head = ttk.Frame(parent)
        head.pack(fill="x")
        ttk.Label(head, text=t("lists"), style="Section.TLabel").pack(side="left")
        ttk.Button(head, text=t("new_list"), command=self.new_list).pack(side="right")
        ttk.Label(parent, text=t("list_hint"), style="Hint.TLabel", wraplength=280).pack(anchor="w", pady=(2, 4))
        self.lists_box = Scrolled(parent)
        self.lists_box.pack(fill="both", expand=True)
        ttk.Label(parent, text=t("no_pity"), style="Section.TLabel").pack(anchor="w", pady=(10, 0))
        ttk.Label(parent, text=t("no_pity_hint"), style="Hint.TLabel", wraplength=280).pack(anchor="w")
        self.no_pity_box = tk.Frame(parent, bg=FIELD, height=ICON + 14, highlightthickness=1,
                                    highlightbackground=BORDER)
        self.no_pity_box.pack(fill="x", pady=(4, 0))
        self.dnd.target(self.no_pity_box, self.drop_no_pity)

    # -- images ---------------------------------------------------------------
    def photo(self, entry, size=ICON):
        """The Tk image of an item's icon (a grey square until the game is known)."""
        item = self.items.get(entry)
        display = item.display if item else -1
        key = (display, size)
        if key not in self.photos:
            from PIL import Image, ImageTk
            image = self.icons.image(display, size) if self.icons else Image.new("RGBA", (size, size), (70, 70, 70, 255))
            self.photos[key] = ImageTk.PhotoImage(image)
        return self.photos[key]

    def item_text(self, entry):
        item = self.items.get(entry)
        if not item:
            return "%d" % entry
        return "%s\n%d · ilvl %d%s" % (item.name, entry, item.item_level,
                                        (" · lvl %d" % item.required_level) if item.required_level else "")

    def icon_widget(self, parent, entry, size=ICON, background=None):
        item = self.items.get(entry)
        colour = item.colour if item else MUTED
        frame = tk.Frame(parent, bg=colour, padx=2, pady=2)
        tk.Label(frame, image=self.photo(entry, size), bd=0, bg=background or FIELD).pack()
        self.tip.attach(frame, lambda: self.item_text(entry))
        return frame

    def list_widget(self, parent, name, size=ICON):
        """A list, as an item: its first item's icon on the list's colour, its name and size below."""
        items = self.project.lists.get(name) or []
        frame = tk.Frame(parent, bg=LIST_COLOUR, padx=2, pady=2)
        if items:
            tk.Label(frame, image=self.photo(items[0], size), bd=0).pack()
        tk.Label(frame, text="%s\n%d" % (name, len(items)), bg=LIST_COLOUR, fg="black",
                 font=("Segoe UI", 7, "bold")).pack(fill="x")
        self.tip.attach(frame, lambda: "%s: %d" % (name, len(self.project.lists.get(name) or [])))
        return frame

    # -- redrawing ------------------------------------------------------------
    def refresh_all(self):
        self.refresh_lists()
        self.refresh_no_pity()
        for family in self.family_rows:
            self.refresh_family(family)
        self.refresh_preview()

    def refresh_family(self, family):
        box = self.family_rows.get(family)
        if box is None:
            return
        for w in box.winfo_children():
            self.dnd.forget(w)
            w.destroy()
        chains = self.project.lines.get(family) or []
        if not chains:
            tk.Label(box, text=self.t("drop_family"), bg=CARD, fg=MUTED, font=("Segoe UI", 8, "italic")).pack(
                side="left", padx=6, pady=10)
        for ci, chain in enumerate(chains):
            die = tk.Frame(box, bg=CHAIN_BG, highlightthickness=1, highlightbackground=BORDER)
            die.pack(side="left", padx=3, pady=2, fill="y")
            self.tip.attach(die, self.t("draw_hint"))
            self.dnd.target(die, lambda p, c, f=family, i=ci: self.drop_chain(f, i, p, c))
            for di, draw in enumerate(chain):
                cell = tk.Frame(die, bg=CHAIN_BG)
                cell.pack(side="left", padx=3, pady=3)
                widget = self.list_widget(cell, draw.target, SMALL_ICON) if draw.is_list else \
                    self.icon_widget(cell, draw.target, SMALL_ICON)
                widget.pack()
                tk.Label(cell, text="%s%% ×%d" % (loot_model.number(draw.chance), draw.quantity), bg=CHAIN_BG,
                         fg=TEXT, font=("Segoe UI", 8)).pack()
                if di < len(chain) - 1:
                    tk.Label(die, text=",", bg=CHAIN_BG, fg=MUTED).pack(side="left")
                image = self.photo(self.project.lists[draw.target][0], SMALL_ICON) \
                    if draw.is_list and self.project.lists.get(draw.target) else \
                    (None if draw.is_list else self.photo(draw.target, SMALL_ICON))
                self.dnd.source(widget, ("draw", family, ci, di), image,
                                click=lambda f=family, i=ci, j=di: self.edit_draw(f, i, j))
                self.dnd.target(cell, lambda p, c, f=family, i=ci: self.drop_chain(f, i, p, c))
                self.dnd.target(widget, lambda p, c, f=family, i=ci: self.drop_chain(f, i, p, c))
            if ci < len(chains) - 1:
                tk.Label(box, text=";", bg=CARD, fg=MUTED, font=("Segoe UI", 11, "bold")).pack(side="left")

    def refresh_lists(self):
        inner = self.lists_box.inner
        for w in inner.winfo_children():
            w.destroy()
        names = list(self.project.lists)
        self.add_all_list.configure(values=names)
        if names and self.add_all_list.get() not in names:
            self.add_all_list.set(names[-1])
        elif not names:
            self.add_all_list.set("")
        for name in names:
            card = tk.Frame(inner, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            card.pack(fill="x", padx=2, pady=3)
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x", padx=6, pady=(4, 2))
            token = self.list_widget(head, name, SMALL_ICON)
            token.pack(side="left")
            items = self.project.lists[name]
            self.dnd.source(token, ("list", name), self.photo(items[0], SMALL_ICON) if items else None)
            tk.Label(head, text=name, bg=CARD, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(side="left", padx=6)
            ttk.Button(head, text="✕", width=3, command=lambda n=name: self.delete_list(n)).pack(side="right")
            ttk.Button(head, text=self.t("rename"), command=lambda n=name: self.rename_list(n)).pack(side="right",
                                                                                                   padx=4)
            grid = tk.Frame(card, bg=FIELD, highlightthickness=0)
            grid.pack(fill="x", padx=6, pady=(0, 6))
            self.dnd.target(card, lambda p, c, n=name: self.drop_list(n, p, c))
            self.dnd.target(grid, lambda p, c, n=name: self.drop_list(n, p, c))
            if not items:
                tk.Label(grid, text=self.t("drop_items"), bg=FIELD, fg=MUTED, font=("Segoe UI", 8, "italic")).pack(
                    pady=8)
            per_row = 7
            for i, entry in enumerate(items[:140]):
                w = self.icon_widget(grid, entry, SMALL_ICON)
                w.grid(row=i // per_row, column=i % per_row, padx=1, pady=1)
                self.dnd.source(w, ("listitem", name, i), self.photo(entry, SMALL_ICON))
            if len(items) > 140:
                tk.Label(grid, text="+%d" % (len(items) - 140), bg=FIELD, fg=MUTED).grid(
                    row=140 // per_row + 1, column=0, columnspan=per_row)

    def refresh_no_pity(self):
        for w in self.no_pity_box.winfo_children():
            w.destroy()
        for i, entry in enumerate(self.project.no_pity):
            w = self.icon_widget(self.no_pity_box, entry, SMALL_ICON)
            w.pack(side="left", padx=2, pady=4)
            self.dnd.source(w, ("nopity", i), self.photo(entry, SMALL_ICON))

    def refresh_preview(self):
        if self.preview_pending:
            return
        self.preview_pending = True

        def draw():
            self.preview_pending = False
            self.project.prune()
            text = loot_model.generate(self.project, self.families) if self.project.prefix else ""
            self.preview.configure(state="normal")
            self.preview.delete("1.0", "end")
            self.preview.insert("1.0", text)
            self.preview.configure(state="disabled")
        self.root.after(150, draw)

    def show_results(self):
        inner = self.results_box.inner
        for w in inner.winfo_children():
            w.destroy()
        per_row = 7
        for i, item in enumerate(self.results):
            w = self.icon_widget(inner, item.entry)
            w.grid(row=i // per_row, column=i % per_row, padx=2, pady=2)
            self.dnd.source(w, ("item", item.entry), self.photo(item.entry, SMALL_ICON))

    # -- drops ----------------------------------------------------------------
    def take(self, payload, copy):
        """The draw a payload stands for (taken out of its place unless copying), or None."""
        kind = payload[0]
        if kind == "item":
            return loot_model.Draw(payload[1])
        if kind == "list":
            return loot_model.Draw(payload[1])
        if kind == "listitem":
            return loot_model.Draw(self.project.lists[payload[1]][payload[2]])
        if kind == "nopity":
            return loot_model.Draw(self.project.no_pity[payload[1]])
        if kind == "draw":
            _, family, ci, di = payload
            draw = self.project.lines[family][ci][di]
            if copy:
                return draw.copy()
            # left in place, marked: removed once the drop is placed (indices stay valid until then)
            self.moving = (family, ci, di)
            return draw
        return None

    def finish_move(self):
        moving = getattr(self, "moving", None)
        self.moving = None
        if moving:
            family, ci, di = moving
            self.project.lines[family][ci][di] = None
            self.project.lines[family] = [[d for d in c if d is not None] for c in self.project.lines[family]]
            self.project.prune()
            return family
        return None

    def drop_family(self, family, payload, copy):
        draw = self.take(payload, copy)
        if draw is None:
            return
        self.project.chains(family).append([draw])
        moved = self.finish_move()
        self.changed(family, moved)

    def drop_chain(self, family, ci, payload, copy):
        draw = self.take(payload, copy)
        if draw is None:
            return
        chain = self.project.chains(family)[ci]
        if payload[0] == "draw" and not copy and payload[1] == family and payload[2] == ci:
            # within its own die: to the end
            chain.append(draw)
        else:
            chain.append(draw)
        moved = self.finish_move()
        self.changed(family, moved)

    def drop_list(self, name, payload, copy):
        kind = payload[0]
        entries = []
        if kind == "item":
            entries = [payload[1]]
        elif kind == "listitem":
            entries = [self.project.lists[payload[1]][payload[2]]]
            if payload[1] == name:
                return
        elif kind == "draw":
            draw = self.project.lines[payload[1]][payload[2]][payload[3]]
            entries = list(self.project.lists[draw.target]) if draw.is_list else [draw.target]
        elif kind == "list":
            if payload[1] == name:
                return
            entries = list(self.project.lists[payload[1]])
        elif kind == "nopity":
            entries = [self.project.no_pity[payload[1]]]
        items = self.project.lists[name]
        items += [e for e in entries if e not in items]
        self.refresh_lists()
        self.refresh_families_using(name)
        self.refresh_preview()

    def drop_no_pity(self, payload, copy):
        kind = payload[0]
        entries = []
        if kind == "item":
            entries = [payload[1]]
        elif kind == "listitem":
            entries = [self.project.lists[payload[1]][payload[2]]]
        elif kind == "list":
            entries = list(self.project.lists[payload[1]])
        elif kind == "draw":
            draw = self.project.lines[payload[1]][payload[2]][payload[3]]
            entries = list(self.project.lists[draw.target]) if draw.is_list else [draw.target]
        self.project.no_pity += [e for e in entries if e not in self.project.no_pity]
        self.refresh_no_pity()
        self.refresh_preview()

    def drop_delete(self, payload, copy):
        """A placed item dropped back on the search: removed from where it was."""
        kind = payload[0]
        if kind == "draw":
            _, family, ci, di = payload
            del self.project.lines[family][ci][di]
            self.project.prune()
            self.changed(family)
        elif kind == "listitem":
            del self.project.lists[payload[1]][payload[2]]
            self.refresh_lists()
            self.refresh_families_using(payload[1])
            self.refresh_preview()
        elif kind == "nopity":
            del self.project.no_pity[payload[1]]
            self.refresh_no_pity()
            self.refresh_preview()

    def changed(self, *families):
        for f in set(f for f in families if f):
            self.refresh_family(f)
        self.refresh_preview()

    def refresh_families_using(self, list_name):
        for family, chains in self.project.lines.items():
            if any(d.target == list_name for c in chains for d in c):
                self.refresh_family(family)

    # -- editing --------------------------------------------------------------
    def edit_draw(self, family, ci, di):
        t = self.t
        chain = self.project.lines[family][ci]
        draw = chain[di]
        top, body = self.modal(t("edit_title"))
        name = draw.target if draw.is_list else (self.items[draw.target].name if draw.target in self.items
                                                 else str(draw.target))
        ttk.Label(body, text="%s — %s" % (family, name), style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        ttk.Label(body, text=t("chance"), style="Card.TLabel").grid(row=1, column=0, sticky="w")
        chance = tk.StringVar(value=loot_model.number(draw.chance))
        e = ttk.Entry(body, textvariable=chance, width=10)
        e.grid(row=1, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(body, text=t("quantity"), style="Card.TLabel").grid(row=2, column=0, sticky="w")
        quantity = tk.StringVar(value=str(draw.quantity))
        ttk.Spinbox(body, from_=1, to=loot_model.MAX_QUANTITY, textvariable=quantity, width=8).grid(
            row=2, column=1, sticky="w", padx=6, pady=3)
        error = ttk.Label(body, text="", style="Card.TLabel", foreground=BAD)
        error.grid(row=3, column=0, columnspan=4, sticky="w")
        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(12, 0))

        def apply():
            try:
                c = float(chance.get().replace(",", "."))
                q = int(quantity.get())
            except ValueError:
                c, q = -1, 0
            if not 0 <= c <= 100 or not 1 <= q <= loot_model.MAX_QUANTITY:
                error.configure(text=t("problem")["chance"] if not 0 <= c <= 100 else t("problem")["quantity"])
                return False
            draw.chance, draw.quantity = c, q
            return True

        def ok():
            if apply():
                top.destroy()
                self.changed(family)

        def move(step):
            if not apply():
                return
            j = di + step
            if 0 <= j < len(chain):
                chain[di], chain[j] = chain[j], chain[di]
            top.destroy()
            self.changed(family)

        def delete():
            del chain[di]
            self.project.prune()
            top.destroy()
            self.changed(family)

        ttk.Button(buttons, text=t("move_left"), command=lambda: move(-1),
                   state="normal" if di > 0 else "disabled").pack(side="left")
        ttk.Button(buttons, text=t("move_right"), command=lambda: move(1),
                   state="normal" if di < len(chain) - 1 else "disabled").pack(side="left", padx=4)
        ttk.Button(buttons, text=t("delete"), command=delete).pack(side="left", padx=(12, 0))
        ttk.Button(buttons, text=t("ok"), style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(buttons, text=t("cancel"), command=top.destroy).pack(side="right", padx=6)
        top.bind("<Return>", lambda _: ok())
        self.show_modal(top, e)

    def new_list(self):
        name = self.project.new_list_name()
        self.project.lists[name] = []
        self.refresh_lists()
        self.add_all_list.set(name)
        self.refresh_preview()

    def rename_list(self, old):
        new = self.ask(self.t("rename"), self.t("rename_prompt"), old)
        if not new or new == old:
            return
        if not loot_model.LIST_NAME.match(new) or new in self.project.lists:
            self.message(self.t("error"), self.t("problem")["list_name"] + ": " + new, BAD)
            return
        self.project.rename_list(old, new)
        self.refresh_all()

    def delete_list(self, name):
        self.project.remove_list(name)
        self.refresh_all()

    def add_all(self):
        name = self.add_all_list.get()
        if not name:
            self.new_list()
            name = self.add_all_list.get()
        items = self.project.lists[name]
        items += [i.entry for i in self.results if i.entry not in items]
        self.refresh_lists()
        self.refresh_families_using(name)
        self.refresh_preview()

    def prefix_changed(self):
        self.project.prefix = self.prefix.get().strip()
        self.settings["prefix"] = self.project.prefix
        loot_data.save_settings(self.settings)
        self.refresh_preview()

    def rate_changed(self):
        try:
            rate = float(self.rate.get().replace(",", "."))
        except ValueError:
            return
        if rate >= 0:
            self.project.rate = rate
            self.refresh_preview()

    def clear(self):
        if not self.confirm(self.t("new"), self.t("clear_confirm")):
            return
        prefix = self.project.prefix
        self.project = loot_model.Project(prefix)
        self.rate.set("100")
        self.refresh_all()

    def set_language(self, language):
        if language == self.language:
            return
        self.language = language
        self.settings["language"] = language
        loot_data.save_settings(self.settings)
        self.t = loot_strings.Texts(language)
        self.status_text = self.t("not_connected") if not self.world else self.connected_text()
        self.build()

    # -- connecting, searching ------------------------------------------------
    def browse(self, key):
        path = filedialog.askdirectory(initialdir=self.vars[key].get() or None, parent=self.root)
        if path:
            self.vars[key].set(os.path.normpath(path))

    def connect(self):
        if self.working:
            return
        server, client = self.vars["server"].get().strip(), self.vars["client"].get().strip()
        self.settings.update(server=server, client=client)
        loot_data.save_settings(self.settings)
        self.status.configure(text=self.t("connecting"), style="CardHint.TLabel")

        def job():
            icons = loot_data.Icons(client) if client else None
            world = loot_data.World(server, self.settings.get("mysql"), icons.locale if icons else None)
            families, from_sources = world.families()
            return world, icons, families, from_sources, world.prefixes()

        self.run(job, self.connected)

    def connected(self, result):
        self.world, self.icons, self.families, self.from_sources, prefixes = result
        self.photos.clear()
        self.settings["mysql"] = self.world.mysql
        if self.icons and self.icons.locale:
            self.settings["locale"] = self.icons.locale
            if "language" not in self.settings and loot_strings.language_of(self.icons.locale) != self.language:
                self.language = loot_strings.language_of(self.icons.locale)
                self.t = loot_strings.Texts(self.language)
        loot_data.save_settings(self.settings)
        self.status_text = self.connected_text()
        known = [e for e in self.project.items() if e not in self.items]
        if known:
            self.items.update(self.world.items(known))
        self.prefix_values = prefixes
        self.build()

    def connected_text(self):
        return self.t("connected", self.world.db.name, len(self.families),
                      "" if getattr(self, "from_sources", True) else self.t("families_builtin"),
                      (self.icons.locale or "?") if self.icons else self.t("no_icons"))

    def search(self):
        if not self.world or self.working:
            return
        text = self.query.get()
        self.settings["query"] = text
        loot_data.save_settings(self.settings)
        quality = self.quality.current() - 1
        self.result_text.configure(text=self.t("searching"))
        self.run(lambda: self.world.search(text, quality if quality >= 0 else None), self.searched)

    def searched(self, result):
        items, more = result
        for item in items:
            self.items[item.entry] = item
        self.results = items
        self.result_text.configure(text=self.t("results_more", len(items)) if more else self.t("results", len(items)))
        self.show_results()

    # -- import, generate -----------------------------------------------------
    def import_conf(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[(".conf", "*.conf *.dist"), ("*", "*")],
                                          initialdir=self.world.server.module_confs if self.world else None)
        if not path:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        prefixes = loot_model.prefixes_in(text)
        if not prefixes:
            self.message(self.t("import_title"), self.t("import_none"), BAD)
            return
        prefix = prefixes[0] if len(prefixes) == 1 else self.choose(self.t("import_title"),
                                                                    self.t("import_choose"), prefixes)
        if not prefix:
            return
        project, problems = loot_model.parse(text, prefix)
        self.project = project
        if self.world:
            missing = [e for e in project.items() if e not in self.items]
            if missing:
                self.items.update(self.world.items(missing))
        self.prefix.set(prefix)
        self.rate.set(loot_model.number(project.rate))
        self.prefix_changed()
        self.refresh_all()
        text = self.t("import_done", prefix, len(project.lists), len(project.lines))
        if problems:
            text += "\n\n" + self.t("import_problems") + "\n" + "\n".join(problems)
        self.message(self.t("import_title"), text, BAD if problems else GOOD)

    def generate(self):
        t = self.t
        self.project.prune()
        existing = set(self.items) if self.world else None
        if self.world:
            missing = [e for e in self.project.items() if e not in self.items]
            if missing:
                self.items.update(self.world.items(missing))
                existing = set(self.items)
        errors, warnings = loot_model.check(self.project, self.families, existing)
        words = t("problem")
        if errors:
            self.message(t("errors_title"), t("errors_text") + "\n\n" +
                         "\n".join("• %s : %s" % (words.get(k, k), v) for k, v in errors), BAD)
            return
        text = loot_model.generate(self.project, self.families)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        top, body = self.modal(t("generated"))
        ttk.Label(body, text=t("generated_text"), style="Card.TLabel", wraplength=640).pack(anchor="w")
        if warnings:
            ttk.Label(body, text=t("warnings") + "\n" + "\n".join("• %s : %s" % (words.get(k, k), v)
                                                                 for k, v in warnings),
                      style="Card.TLabel", foreground="#e0a040", wraplength=640, justify="left").pack(
                anchor="w", pady=(8, 0))
        box = tk.Text(body, width=100, height=22, wrap="none", font=("Consolas", 9), background=FIELD,
                      foreground=TEXT, insertbackground=TEXT, relief="flat", highlightthickness=1,
                      highlightbackground=BORDER)
        box.insert("1.0", text)
        box.pack(fill="both", expand=True, pady=8)
        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.pack(fill="x")

        def save():
            path = filedialog.asksaveasfilename(parent=top, defaultextension=".conf",
                                                initialfile="%s-loot.conf" % self.project.prefix)
            if path:
                with open(path, "w", encoding="utf-8", newline="\r\n") as f:
                    f.write(text)

        ttk.Button(buttons, text=t("close"), style="Accent.TButton", command=top.destroy).pack(side="right")
        ttk.Button(buttons, text=t("save"), command=save).pack(side="right", padx=6)
        ttk.Button(buttons, text=t("copy"), command=lambda: (self.root.clipboard_clear(),
                                                             self.root.clipboard_append(text))).pack(side="right")
        self.show_modal(top, box)

    # -- dialogs --------------------------------------------------------------
    def modal(self, title):
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.title(title)
        top.configure(bg=CARD)
        top.transient(self.root)
        body = ttk.Frame(top, style="Card.TFrame", padding=(20, 16))
        body.pack(fill="both", expand=True)
        return top, body

    def show_modal(self, top, focus=None):
        top.bind("<Escape>", lambda _: top.destroy())
        dark_title_bar(top)
        top.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - top.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - top.winfo_reqheight()) // 3
        top.geometry("+%d+%d" % (max(0, x), max(0, y)))
        top.deiconify()
        top.grab_set()
        (focus or top).focus_set()
        self.root.wait_window(top)

    def message(self, title, text, colour=ACCENT):
        top, body = self.modal(title)
        ttk.Label(body, text=title, style="Card.TLabel", foreground=colour,
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        msg = ReadOnlyText(body, CARD, width=80)
        msg.set(text)
        msg.pack(anchor="w", fill="x", pady=(8, 14))
        b = ttk.Button(body, text=self.t("ok"), style="Accent.TButton", command=top.destroy)
        b.pack(side="right")
        top.after(30, msg.fit)
        self.show_modal(top, b)

    def confirm(self, title, text):
        answer = {"ok": False}
        top, body = self.modal(title)
        ttk.Label(body, text=text, style="Card.TLabel", wraplength=480).pack(anchor="w", pady=(0, 14))
        ttk.Button(body, text=self.t("ok"), style="Accent.TButton",
                   command=lambda: (answer.update(ok=True), top.destroy())).pack(side="right")
        b = ttk.Button(body, text=self.t("cancel"), command=top.destroy)
        b.pack(side="right", padx=6)
        self.show_modal(top, b)
        return answer["ok"]

    def ask(self, title, text, value):
        answer = {"value": None}
        top, body = self.modal(title)
        ttk.Label(body, text=text, style="Card.TLabel").pack(anchor="w")
        var = tk.StringVar(value=value)
        e = ttk.Entry(body, textvariable=var, width=30)
        e.pack(fill="x", pady=(6, 12))

        def ok():
            answer["value"] = var.get().strip()
            top.destroy()
        ttk.Button(body, text=self.t("ok"), style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(body, text=self.t("cancel"), command=top.destroy).pack(side="right", padx=6)
        top.bind("<Return>", lambda _: ok())
        self.show_modal(top, e)
        return answer["value"]

    def choose(self, title, text, values):
        answer = {"value": None}
        top, body = self.modal(title)
        ttk.Label(body, text=text, style="Card.TLabel").pack(anchor="w")
        combo = ttk.Combobox(body, values=values, state="readonly")
        combo.current(0)
        combo.pack(fill="x", pady=(6, 12))
        ttk.Button(body, text=self.t("ok"), style="Accent.TButton",
                   command=lambda: (answer.update(value=combo.get()), top.destroy())).pack(side="right")
        ttk.Button(body, text=self.t("cancel"), command=top.destroy).pack(side="right", padx=6)
        self.show_modal(top, combo)
        return answer["value"]

    # -- background work ------------------------------------------------------
    def run(self, job, done):
        """Runs job() in a thread; done(result) runs in the window once it returns."""
        self.working = True
        self.connect_button.configure(state="disabled")

        def target():
            try:
                self.events.put(("done", done, job()))
            except Exception as e:
                message, unexpected = core.describe_failure(e)
                self.events.put(("failed", message, traceback.format_exc() if unexpected else ""))
        threading.Thread(target=target, daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                self.working = False
                try:
                    self.connect_button.configure(state="normal")
                except tk.TclError:
                    pass
                if event[0] == "done":
                    event[1](event[2])
                else:
                    self.status.configure(text=event[1], style="Bad.TLabel")
                    self.result_text.configure(text="")
                    if event[2]:
                        self.message(self.t("error"), event[1] + "\n\n" + event[2], BAD)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)


def run():
    root = tk.Tk()
    LootEditor(root)
    root.mainloop()
