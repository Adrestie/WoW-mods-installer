# -*- coding: utf-8 -*-
"""The installer's window (MIT licence).

The user picks the module and the folders, reads what is in place, and
installs or removes with one button. The work runs in a background thread;
what the engine says goes to the log at the bottom."""
import json
import os
import queue
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, ttk
from tkinter import font as tkfont

import core

# Dark palette.
BG = "#1e1f22"            # window
CARD = "#2b2d31"          # folder panel, dialogs
FIELD = "#111214"         # entries, lists, log
BORDER = "#3a3d44"
BUTTON = "#3a3d44"
BUTTON_HOVER = "#4a4e57"
TEXT = "#e3e5e8"
MUTED = "#9aa0a8"
ACCENT = "#5b8def"
GOOD = "#5fcf85"
BAD = "#f07070"
SELECTED = "#35507f"
OPTIONAL_CARD = "#24262a"     # optional folder panel, darker than the required one
OPTIONAL_ACCENT = "#c7a24a"   # its left edge, its title and the reasons

# Colour of the state badge, per state.
BADGES = {
    "absent": ("NOT INSTALLED", "#2f8f4e"),
    "present": ("INSTALLED", "#3b6fd4"),
    "conflict": ("CONFLICT", "#c94141"),
    "error": ("ERROR", "#c94141"),
    "unknown": ("NOT CHECKED", "#5a5e66"),
    "busy": ("WORKING", "#5a5e66"),
}
# Action buttons: (label, style name, colour, colour under the mouse).
ACTIONS = {
    "install": ("Install", "Install.TButton", "#2f8f4e", "#37a65b"),
    "remove": ("Remove", "Remove.TButton", "#c94141", "#d95454"),
    "leftovers": ("Remove leftovers", "Leftovers.TButton", "#c7771f", "#d98a2e"),
}

# The fields, in their order on screen: (key in the settings, label, kind of path). The module folder
# is always shown; each other field shows only if the module's manifest declares it (its name there:
# MANIFEST_FIELD), under Required or Optional.
FIELDS = [
    ("module", "Module folder", "folder"),
    ("client", "Game folder", "folder"),
    ("server", "Worldserver folder", "folder"),
    ("sources", "AzerothCore sources", "folder"),
    ("mysql", "MySQL client", "file"),
]
MANIFEST_FIELD = {"client": "game", "server": "worldserver", "sources": "sources", "mysql": "mysql"}
# Fields found by the installer when left empty.
FOUND_BY_ITSELF = ("sources", "mysql")
# What goes in each field, said while it is empty.
EMPTY = {
    "module": "The folder that holds installer.json.",
    "client": "The folder that holds Wow.exe and Data.",
    "server": "The folder that holds worldserver.exe.",
    "sources": "Found by itself when left empty (the folder that holds src and modules).",
    "mysql": "Found by itself when left empty (mysql.exe of MySQL Server).",
}


def field_state(key, path):
    """(ok, text) of a field's content: ok is None while it is empty, True if the path is the one expected
    there, False otherwise; text says so, or what goes there."""
    if not path:
        return None, EMPTY[key]
    if key == "module":
        folder = core.module_folder(path)
        if folder:
            try:
                with open(os.path.join(folder, core.MANIFEST_NAME), encoding="utf-8") as f:
                    m = json.load(f)
                return True, "%s (%s)" % (m.get("title") or m.get("module"), m.get("module"))
            except (OSError, ValueError, AttributeError):
                return False, "installer.json is unreadable."
        if os.path.isdir(path):
            inside = sorted(n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n, core.MANIFEST_NAME)))
            if inside:
                return False, "This folder holds several modules: choose one (%s)." % ", ".join(inside[:4])
        return False, "No installer.json here."
    if key == "server":
        if os.path.isfile(os.path.join(path, "worldserver.exe")):
            return True, "worldserver.exe found."
        return False, "No worldserver.exe here."
    if key == "sources":
        if os.path.isdir(os.path.join(path, "src")) and os.path.isdir(os.path.join(path, "modules")):
            return True, "AzerothCore sources found."
        return False, "Not an AzerothCore source tree (src and modules)."
    if key == "client":
        if os.path.basename(os.path.normpath(path)).lower() == "data" and                 os.path.isfile(os.path.join(os.path.dirname(os.path.normpath(path)), "Wow.exe")):
            return False, "This is the Data folder: choose its parent."
        try:
            core.Client(path)
            return True, "Game found."
        except (core.InstallerError, OSError):
            return False, "No Data folder with .MPQ archives here."
    if os.path.isfile(path) and os.path.basename(path).lower() == "mysql.exe":
        return True, "mysql.exe found."
    return False, "Not mysql.exe."


def module_fields(path):
    """({field key: "required" or "optional"}, {field key: why it is optional}, [optional notes]) from
    the "fields" and "optional-notes" of the module's manifest, by settings key; ({}, {}, []) while it
    cannot be read."""
    try:
        with open(os.path.join(core.module_folder(path) or "", core.MANIFEST_NAME), encoding="utf-8") as f:
            m = json.load(f)
        declared = m.get("fields") or {}
        notes = m.get("optional-notes") or []
    except (OSError, ValueError, AttributeError):
        return {}, {}, []
    needs, reasons = {}, {}
    for key, name in MANIFEST_FIELD.items():
        v = declared.get(name)
        if v == "required":
            needs[key] = "required"
        elif isinstance(v, dict) and isinstance(v.get("optional"), str):
            needs[key], reasons[key] = "optional", v["optional"]
    notes = [notes] if isinstance(notes, str) else [n for n in notes if isinstance(n, str)]
    return needs, reasons, notes


def install_parts(M, server, client):
    """What an install puts in place, in words."""
    parts = ["sources"] + (["configuration"] if M.conf else []) + (["Lua scripts"] if M.lua else []) \
        if M.server_module else []
    if client is not None:
        parts += (["game files"] if M.game_files else []) + (["addons"] if M.addons else [])
    if core.installed_dbc(M, server):
        parts.append("DBC rows")
    if M.server_module and core.has_sql(M):
        parts.append("SQL")
    elif not M.server_module and server is not None and M.databases:
        parts.append("database rows")
    text = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else "".join(parts)
    if M.server_module:
        return text + " (the server is rebuilt afterwards)"
    if server is None and core.has_server_part(M):
        return text + " (no worldserver folder: the server part is left out)"
    return text


def removal_parts(M, dbs):
    """What a removal takes away, in words."""
    if M.server_module:
        return "files, DBC rows and database data, players' data included"
    return "game files, addons and DBC rows" + (", database rows" if dbs else "")


def dark_title_bar(window):
    """Asks Windows for a dark title bar (Windows 10 20H1 and later; ignored elsewhere)."""
    try:
        import ctypes
        window.update_idletasks()
        handle = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):          # DWMWA_USE_IMMERSIVE_DARK_MODE, then its older number
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, attribute, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, OSError):
        pass


class ReadOnlyText(tk.Text):
    """Text shown like a label that the user can select and copy, as tall as the lines it wraps into.
    width: the width it asks for, in characters (it grows with its place when packed to fill)."""

    def __init__(self, master, background, foreground=TEXT, width=1):
        super().__init__(master, wrap="word", width=width, height=1, relief="flat", borderwidth=0,
                         highlightthickness=0, padx=0, pady=0, font=("Segoe UI", 9), background=background,
                         foreground=foreground, selectbackground=SELECTED, inactiveselectbackground=SELECTED,
                         selectforeground=TEXT, cursor="xterm", takefocus=0, state="disabled")
        self.bind("<Configure>", lambda e: self.fit())

    def set(self, text):
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.configure(state="disabled")
        self.fit()

    def fit(self):
        """Height: the lines the text wraps into at its present width, once it has one (before, it
        would wrap one character a line and ask for a huge height)."""
        if self.winfo_width() <= 1:
            return
        breaks = self.tk.call(self._w, "count", "-update", "-displaylines", "1.0", "end-1c")
        self.configure(height=int(breaks or 0) + 1)


class InstallerWindow(object):
    def __init__(self, root, module, settings, load_manifest):
        self.root = root
        self.settings = settings
        self.load_manifest = load_manifest
        self.events = queue.Queue()
        self.context = None           # (M, server, client, dbs, state) of the last check
        self.working = False
        self.filling = False          # fields being filled by the window itself
        self.action = None
        core.output = lambda text: self.events.put(("log", text))

        root.withdraw()
        root.title("WoW-mods installer")
        root.configure(bg=BG)
        scale = root.winfo_fpixels("1i") / 96.0
        width = min(int(1000 * scale), root.winfo_screenwidth() - 40)
        height = min(int(860 * scale), root.winfo_screenheight() - 80)
        root.geometry("%dx%d" % (width, height))
        root.minsize(int(800 * scale), int(640 * scale))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.styles()

        outer = ttk.Frame(root, padding=(20, 16, 20, 12))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="WoW-mods installer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Installs a module, or removes it when it is present.",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 12))

        # Folders: the module folder, then the fields its manifest declares, required ones in the first
        # panel, optional ones (with the reason) in the second; a field it leaves out is not shown.
        folders = ttk.Frame(outer)
        folders.pack(fill="x")
        folders.columnconfigure(0, weight=1)
        ttk.Label(folders, text="REQUIRED", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        # left padding 17: the optional panel's 14 plus its 3-pixel edge, so both columns line up
        self.required_box = ttk.Frame(folders, style="Card.TFrame", padding=(17, 8, 14, 8))
        self.required_box.grid(row=1, column=0, sticky="ew")
        self.optional_title = ttk.Label(folders, text="OPTIONAL", style="OptionalSection.TLabel")
        self.optional_title.grid(row=2, column=0, sticky="w", pady=(12, 4))
        self.optional_panel = tk.Frame(folders, bg=OPTIONAL_ACCENT)
        self.optional_panel.grid(row=3, column=0, sticky="ew")
        # the accent shows as a 3-pixel edge on the left of the darker panel
        self.optional_box = ttk.Frame(self.optional_panel, style="Optional.TFrame", padding=(14, 8))
        self.optional_box.pack(fill="both", expand=True, padx=(3, 0))
        for box in (self.required_box, self.optional_box):
            box.columnconfigure(1, weight=1)
        self.vars, self.rows = {}, {}
        self.notes_frame = None       # what the optional fields add, at the top of their panel
        for key, _, _ in FIELDS:
            var = tk.StringVar(value=(module if key == "module" and module else settings.get(key) or ""))
            var.trace_add("write", lambda *_, k=key: self.fields_changed(k))
            self.vars[key] = var
        self.place_fields()
        folders.bind("<Configure>", lambda e: self.wrap_notes(e.width))
        bar = ttk.Frame(folders)
        bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.check_button = ttk.Button(bar, text="Check", style="Accent.TButton", command=self.check)
        self.check_button.pack(side="right")
        self.places = ReadOnlyText(bar, BG, foreground=MUTED)
        self.places.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.copyable_text(self.places)

        # Progress, at the bottom of the window.
        status = ttk.Frame(outer)
        status.pack(side="bottom", fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(status, mode="determinate", length=180)
        self.progress.pack(side="left")
        self.progress_text = ttk.Label(status, text="", style="Hint.TLabel")
        self.progress_text.pack(side="left", padx=10)

        # State, then log, in two panes the user can resize.
        panes = ttk.PanedWindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(14, 0))
        top = ttk.Frame(panes)
        panes.add(top, weight=4)
        head = ttk.Frame(top)
        head.pack(fill="x")
        self.badge = tk.Label(head, font=("Segoe UI", 10, "bold"), fg="white", padx=10, pady=3)
        self.badge.pack(side="left")
        self.module_title = ttk.Label(head, text="", style="Module.TLabel")
        self.module_title.pack(side="left", padx=12)
        self.action_button = ttk.Button(head, text="Install", style="Install.TButton", command=self.act)
        self.action_button.pack(side="right")
        self.explanation = ReadOnlyText(top, BG)
        self.explanation.pack(fill="x", pady=(8, 6))
        self.copyable_text(self.explanation)
        tree_box = ttk.Frame(top)
        tree_box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_box, columns=("detail",), show="tree headings", height=7)
        self.tree.heading("#0", text="What", anchor="w")
        self.tree.heading("detail", text="Where", anchor="w")
        self.tree.column("#0", width=230, stretch=False)
        self.tree.column("detail", width=600, stretch=True)
        self.tree.tag_configure("group", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("conflict", foreground=BAD)
        scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # Rows selected (Ctrl+A: all of them) are copied by Ctrl+C or the right-click menu.
        for key in ("<Control-c>", "<Control-C>"):
            self.tree.bind(key, lambda e: self.copy(self.tree_text(self.selected_rows())))
        for key in ("<Control-a>", "<Control-A>"):
            self.tree.bind(key, lambda e: self.tree.selection_set(self.tree_rows()))
        self.tree.bind("<Button-3>", self.tree_menu)

        bottom = ttk.Frame(panes)
        panes.add(bottom, weight=1)
        ttk.Label(bottom, text="LOG", style="Section.TLabel").pack(anchor="w", pady=(8, 4))
        log_box = ttk.Frame(bottom)
        log_box.pack(fill="both", expand=True)
        self.log = tk.Text(log_box, height=6, wrap="none", font=("Consolas", 9), relief="flat", borderwidth=0,
                           background=FIELD, foreground=TEXT, insertbackground=TEXT, selectbackground=SELECTED,
                           inactiveselectbackground=SELECTED, highlightthickness=1, highlightbackground=BORDER,
                           highlightcolor=BORDER, padx=6, pady=4, state="disabled")
        log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self.copyable_text(self.log)
        # Every other text: copied whole from its right-click menu.
        root.bind_class("TLabel", "<Button-3>",
                        lambda e: self.copy_menu(e, [("Copy", str(e.widget.cget("text")))]), add="+")

        self.show_state("unknown", "", "Fill in the folders, then press Check.")
        dark_title_bar(root)
        root.deiconify()
        self.root.after(60, self.poll)
        if self.ready(quiet=True):
            self.root.after(200, self.check)

    # -- look -----------------------------------------------------------------
    def styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        base = ("Segoe UI", 9)
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, focuscolor=ACCENT,
                        selectbackground=SELECTED, selectforeground=TEXT, insertcolor=TEXT, font=base)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("Module.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 8, "bold"), foreground=MUTED)
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("CardField.TLabel", background=CARD, font=("Segoe UI", 9, "bold"))
        style.configure("CardHint.TLabel", background=CARD, foreground=MUTED)
        style.configure("CardGood.TLabel", background=CARD, foreground=GOOD)
        style.configure("CardBad.TLabel", background=CARD, foreground=BAD)
        style.configure("OptionalSection.TLabel", font=("Segoe UI", 8, "bold"), foreground=OPTIONAL_ACCENT)
        style.configure("Optional.TFrame", background=OPTIONAL_CARD)
        style.configure("OptionalField.TLabel", background=OPTIONAL_CARD, foreground=MUTED,
                        font=("Segoe UI", 9, "bold"))
        style.configure("OptionalReason.TLabel", background=OPTIONAL_CARD, foreground=OPTIONAL_ACCENT,
                        font=("Segoe UI", 9, "italic"))
        style.configure("OptionalNote.TLabel", background=OPTIONAL_CARD, foreground=TEXT)
        style.configure("OptionalGood.TLabel", background=OPTIONAL_CARD, foreground=GOOD)
        style.configure("OptionalBad.TLabel", background=OPTIONAL_CARD, foreground=BAD)
        style.configure("OptionalHint.TLabel", background=OPTIONAL_CARD, foreground=MUTED)
        style.configure("Dialog.TFrame", background=CARD)
        style.configure("Dialog.TLabel", background=CARD, foreground=TEXT)
        style.configure("Dialog.TCheckbutton", background=CARD, foreground=TEXT, indicatorbackground=FIELD,
                        indicatorforeground=TEXT, focuscolor=CARD)
        style.map("Dialog.TCheckbutton", background=[("active", CARD)], indicatorbackground=[("active", BUTTON)])
        for name, colour in (("DialogInfo", ACCENT), ("DialogWarning", "#e0a040"), ("DialogError", BAD)):
            style.configure(name + ".TLabel", background=CARD, foreground=colour, font=("Segoe UI", 12, "bold"))
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, padding=(6, 4))
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        style.configure("TButton", background=BUTTON, foreground=TEXT, bordercolor=BUTTON, lightcolor=BUTTON,
                        darkcolor=BUTTON, padding=(14, 5))
        style.map("TButton", background=[("disabled", CARD), ("pressed", BORDER), ("active", BUTTON_HOVER)],
                  foreground=[("disabled", MUTED)], lightcolor=[("active", BUTTON_HOVER)],
                  darkcolor=[("active", BUTTON_HOVER)], bordercolor=[("focus", ACCENT)])
        coloured = [("Accent.TButton", ACCENT, "#6f9cf2", ("Segoe UI", 9, "bold"), (18, 5))]
        coloured += [(s, c, h, ("Segoe UI", 10, "bold"), (24, 7)) for _, s, c, h in ACTIONS.values()]
        for name, colour, hover, font, padding in coloured:
            style.configure(name, background=colour, foreground="white", bordercolor=colour, lightcolor=colour,
                            darkcolor=colour, font=font, padding=padding)
            style.map(name, background=[("disabled", BUTTON), ("active", hover)],
                      foreground=[("disabled", MUTED)], lightcolor=[("active", hover)],
                      darkcolor=[("active", hover)], bordercolor=[("disabled", BUTTON), ("focus", "white")])
        # Tree rows as tall as the text, whatever the screen scaling.
        style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT, bordercolor=BORDER,
                        rowheight=int(tkfont.nametofont("TkDefaultFont").metrics("linespace") * 1.5))
        style.map("Treeview", background=[("selected", SELECTED)], foreground=[("selected", TEXT)])
        style.configure("Treeview.Heading", background=CARD, foreground=MUTED, bordercolor=BORDER,
                        lightcolor=CARD, darkcolor=CARD, relief="flat")
        style.map("Treeview.Heading", background=[("active", BUTTON)])
        style.configure("Vertical.TScrollbar", background=BUTTON, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                        lightcolor=BUTTON, darkcolor=BUTTON)
        style.map("Vertical.TScrollbar", background=[("active", BUTTON_HOVER)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD, bordercolor=BORDER,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Sash", sashthickness=6, background=BG, bordercolor=BG, lightcolor=BG, darkcolor=BG)

    def show_state(self, kind, title, text, action=None):
        label, colour = BADGES[kind]
        self.badge.configure(text=label, bg=colour)
        self.module_title.configure(text=title)
        self.explanation.set(text)
        self.action = action
        # without an action, the button stays greyed out as Install
        self.action_button.configure(text=ACTIONS[action or "install"][0], style=ACTIONS[action or "install"][1])
        self.update_buttons()

    def update_buttons(self):
        self.check_button.configure(state="disabled" if self.working else "normal")
        self.action_button.configure(state="normal" if self.action and not self.working else "disabled")

    def dialog(self, title, text, buttons, kind="info", focus=0, option=None):
        """A modal dialog in the window's colours; buttons: [(label, value, style)], left to right, the one
        at index focus has the focus. Returns the value of the button pressed, None if it is closed; with
        option (the text of a box, ticked at first), returns (value, ticked)."""
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.title(title)
        top.configure(bg=CARD)
        top.transient(self.root)
        top.resizable(False, False)
        chosen = {"value": None}
        body = ttk.Frame(top, style="Dialog.TFrame", padding=(24, 18, 24, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="Dialog%s.TLabel" % kind.capitalize()).pack(anchor="w")
        # the message, selectable, as wide as its longest line up to 560 pixels
        font = tkfont.Font(font=("Segoe UI", 9))
        widest = max(font.measure(line) for line in text.splitlines() or [""])
        message = ReadOnlyText(body, CARD, width=max(1, -(-min(560, widest) // font.measure("0"))))
        message.set(text)
        message.pack(anchor="w", pady=(8, 18))
        self.copyable_text(message)
        # Ctrl+C with the focus on a button copies the whole dialog, as Windows does
        for key in ("<Control-c>", "<Control-C>"):
            top.bind(key, lambda e: self.copy("%s\n\n%s" % (title, text)))
        ticked = tk.BooleanVar(value=True)
        if option:
            ttk.Checkbutton(body, text=option, variable=ticked, style="Dialog.TCheckbutton").pack(anchor="w",
                                                                                                pady=(0, 16))
        row = ttk.Frame(body, style="Dialog.TFrame")
        row.pack(fill="x")
        widgets = []
        for label, value, button_style in reversed(buttons):
            b = ttk.Button(row, text=label, style=button_style,
                           command=lambda v=value: (chosen.update(value=v), top.destroy()))
            b.pack(side="right", padx=(8, 0))
            widgets.insert(0, b)
        top.bind("<Escape>", lambda _: top.destroy())
        top.bind("<Return>", lambda _: top.focus_get().invoke() if hasattr(top.focus_get(), "invoke") else None)
        dark_title_bar(top)
        # shown unseen first: the message wraps at its real width, then the dialog is placed
        top.attributes("-alpha", 0.0)
        top.deiconify()
        top.update_idletasks()
        message.fit()
        top.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - top.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - top.winfo_reqheight()) // 3
        top.geometry("+%d+%d" % (max(0, x), max(0, y)))
        top.attributes("-alpha", 1.0)
        top.grab_set()
        widgets[focus].focus_set()
        self.root.wait_window(top)
        return (chosen["value"], ticked.get()) if option else chosen["value"]

    # -- copying --------------------------------------------------------------
    def copy(self, text):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def copy_menu(self, event, entries):
        """A right-click menu at the mouse; entries: [(label, text it copies)], greyed out when empty."""
        menu = tk.Menu(self.root, tearoff=0, background=CARD, foreground=TEXT, activebackground=SELECTED,
                       activeforeground=TEXT, disabledforeground=MUTED, borderwidth=1, relief="flat")
        for label, text in entries:
            menu.add_command(label=label, command=lambda t=text: self.copy(t), state="normal" if text else "disabled")
        menu.tk_popup(event.x_root, event.y_root)

    def copyable_text(self, widget):
        """Selection with the mouse, Ctrl+C, Ctrl+A and a right-click menu on a read-only Text."""
        def selection():
            return widget.get("sel.first", "sel.last") if widget.tag_ranges("sel") else ""
        widget.bind("<Button-1>", lambda e: widget.focus_set(), add="+")
        for key in ("<Control-c>", "<Control-C>"):
            widget.bind(key, lambda e: (self.copy(selection()), "break")[1])
        for key in ("<Control-a>", "<Control-A>"):
            widget.bind(key, lambda e: (widget.tag_add("sel", "1.0", "end-1c"), "break")[1])
        widget.bind("<Button-3>", lambda e: self.copy_menu(e, [("Copy", selection()),
                                                               ("Copy all", widget.get("1.0", "end-1c"))]))

    def tree_rows(self):
        """Every row of the list, in its order."""
        return [i for group in self.tree.get_children() for i in [group] + list(self.tree.get_children(group))]

    def selected_rows(self):
        selected = set(self.tree.selection())
        return [i for i in self.tree_rows() if i in selected]

    def tree_text(self, rows):
        """The rows as lines: What, then Where after a tab, indented under their group."""
        return "\n".join(("  " if self.tree.parent(i) else "") +
                         "\t".join([self.tree.item(i, "text")] + [v for v in self.tree.item(i, "values") if v])
                         for i in rows)

    def tree_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        self.copy_menu(event, [("Copy", self.tree_text(self.selected_rows())),
                               ("Copy all", self.tree_text(self.tree_rows()))])

    # -- fields ---------------------------------------------------------------
    def place_fields(self):
        """Shows the module folder, then the fields the module's manifest declares: required ones in the
        first panel; in the second, what the optional fields add, then each with its reason."""
        needs, reasons, notes = module_fields(self.values()["module"])
        for key in [k for k in self.rows if k != "module"]:
            for w in self.rows.pop(key)["widgets"]:
                w.destroy()
        if self.notes_frame is not None:
            self.notes_frame.destroy()
            self.notes_frame = None
        if notes:
            # one line per note, a bullet before each when there are several, wrapped text aligned
            self.notes_frame = ttk.Frame(self.optional_box, style="Optional.TFrame")
            self.notes_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
            self.notes_frame.columnconfigure(1, weight=1)
            for i, note in enumerate(notes):
                if len(notes) > 1:
                    ttk.Label(self.notes_frame, text="•", style="OptionalNote.TLabel").grid(
                        row=i, column=0, sticky="nw", padx=(0, 6))
                ttk.Label(self.notes_frame, text=note, style="OptionalNote.TLabel", justify="left",
                          wraplength=700).grid(row=i, column=1, sticky="w")
        rows = {"required": 1, "optional": 1}
        if "module" not in self.rows:
            self.add_row("module", self.required_box, 0, None)
        for key, _, _ in FIELDS[1:]:
            need = needs.get(key)
            if need:
                box = self.required_box if need == "required" else self.optional_box
                self.add_row(key, box, rows[need], reasons.get(key))
                rows[need] += 1
        for w in (self.optional_title, self.optional_panel):
            if rows["optional"] > 1:
                w.grid()
            else:
                w.grid_remove()

    def add_row(self, key, box, index, reason):
        """One field in box, at row index: label, entry and button, then the reason (optional field)
        and a line saying whether the path fits."""
        label, kind = [(l, k) for f, l, k in FIELDS if f == key][0]
        optional = reason is not None
        prefix = "Optional" if optional else "Card"
        widgets = [ttk.Label(box, text=label, style=prefix + "Field.TLabel", width=20),
                   ttk.Entry(box, textvariable=self.vars[key]),
                   ttk.Button(box, text="Browse...", command=lambda: self.browse(key, kind))]
        widgets[1].bind("<Return>", lambda _: self.check())
        widgets[0].grid(row=3 * index, column=0, sticky="w", pady=(6, 0))
        widgets[1].grid(row=3 * index, column=1, sticky="ew", padx=8, pady=(6, 0))
        widgets[2].grid(row=3 * index, column=2, pady=(6, 0))
        if optional:
            text = ttk.Label(box, text=reason, style="OptionalReason.TLabel", justify="left", wraplength=600)
            text.grid(row=3 * index + 1, column=1, columnspan=2, sticky="w", padx=8, pady=(2, 0))
            widgets.append(text)
        note = ttk.Label(box, justify="left", wraplength=600)
        note.grid(row=3 * index + 2, column=1, columnspan=2, sticky="w", padx=8, pady=(2, 2))
        widgets.append(note)
        self.rows[key] = {"widgets": widgets, "note": note, "optional": optional}
        self.show_note(key)

    def wrap_notes(self, width):
        for row in self.rows.values():
            for w in row["widgets"][3:]:
                w.configure(wraplength=max(300, width - 260))
        if self.notes_frame is not None:
            for w in self.notes_frame.grid_slaves(column=1):
                w.configure(wraplength=max(300, width - 80))

    def show_note(self, key):
        row = self.rows.get(key)
        if not row:
            return
        ok, text = field_state(key, self.values()[key])
        prefix = "Optional" if row["optional"] else "Card"
        # An empty optional field says nothing more than its reason.
        if ok is None and row["optional"]:
            row["note"].grid_remove()
            return
        row["note"].grid()
        row["note"].configure(text=("✓ " if ok else "✗ " if ok is False else "") + text,
                              style=prefix + ("Good" if ok else "Bad" if ok is False else "Hint") + ".TLabel")

    def browse(self, key, kind):
        current = self.vars[key].get().strip()
        start = current if os.path.isdir(current) else os.path.dirname(current) if current else ""
        label = dict((k, l) for k, l, _ in FIELDS)[key]
        if kind == "folder":
            path = filedialog.askdirectory(parent=self.root, initialdir=start, mustexist=True,
                                           title="%s: %s" % (label, EMPTY[key].split(" (")[0]))
        else:
            path = filedialog.askopenfilename(parent=self.root, initialdir=start, title="%s: mysql.exe" % label,
                                              filetypes=[("mysql.exe", "mysql.exe"), ("*.exe", "*.exe")])
        if path:
            self.vars[key].set(os.path.normpath(path))
            if self.ready(quiet=True):
                self.check()

    def fields_changed(self, key):
        if not hasattr(self, "required_box"):
            return
        if key == "module":
            self.place_fields()
        self.show_note(key)
        if self.filling or self.working:
            return
        self.context = None
        self.tree.delete(*self.tree.get_children())
        self.places.set("")
        self.show_state("unknown", self.module_title.cget("text"), "The folders changed: press Check.")

    def values(self):
        return {key: var.get().strip().strip('"') for key, var in self.vars.items()}

    def ready(self, quiet=False):
        """True if the fields shown hold the expected paths (a required field filled, unless the installer
        finds it by itself); otherwise says which one is wrong."""
        for key, label, _ in FIELDS:
            row = self.rows.get(key)
            if not row:
                continue
            ok = field_state(key, self.values()[key])[0]
            needed = not row["optional"] and key not in FOUND_BY_ITSELF
            if ok is False or (needed and ok is None):
                if not quiet:
                    self.show_state("unknown", "", "%s: %s" % (label, field_state(key, self.values()[key])[1]))
                return False
        return True

    # -- background work ------------------------------------------------------
    def work(self, text, job, done):
        """Runs job() in a thread, then done(result) here; failures go to failed()."""
        self.working = True
        self.update_buttons()
        self.progress_text.configure(text=text)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def target():
            try:
                self.events.put(("done", done, job()))
            except Exception as e:
                self.events.put(("failed", e, traceback.format_exc()))
        threading.Thread(target=target, daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self.write_log(event[1])
                    continue
                self.working = False
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
                self.progress_text.configure(text="")
                if event[0] == "done":
                    event[1](event[2])
                else:
                    self.failed(event[1], event[2])
                self.update_buttons()
        except queue.Empty:
            pass
        self.root.after(60, self.poll)

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def failed(self, error, trace):
        message, unexpected = core.describe_failure(error)
        self.write_log("")
        self.write_log("FAILED: %s" % message)
        if unexpected:
            self.write_log(trace)
        if self.pending_action:
            self.dialog("Failed", message[0].upper() + message[1:], [("Close", None, "TButton")], kind="error")
            self.pending_action = None
            self.check()
        else:
            self.context = None
            self.show_state("error", self.module_title.cget("text"), message[0].upper() + message[1:])

    # -- check ----------------------------------------------------------------
    pending_action = None

    def check(self):
        if self.working or not self.ready():
            return
        v = self.values()

        def job():
            M = self.load_manifest(core.module_folder(v["module"]))
            server = core.open_server(M, v["server"], v["sources"])
            client = core.Client(v["client"]) if "game" in M.fields else None
            dbs = {}
            if core.uses_databases(M, server):
                dbs = core.open_databases(server, v["mysql"] or core.find_mysql(server, self.settings))
            core.check_package_place(M, server)
            return M, server, client, dbs, core.survey(M, server, client, dbs)

        self.show_state("busy", self.module_title.cget("text"), "Reading the folders...")
        self.work("Checking...", job, self.checked)

    def checked(self, context):
        M, server, client, dbs, state = context
        self.context = context
        self.filling = True
        self.vars["module"].set(M.root)
        if M.server_module:
            self.vars["sources"].set(server.sources)
        if dbs:
            self.vars["mysql"].set(dbs["world"].mysql)
        self.filling = False
        core.remember(self.settings, M, server, client, dbs)
        if server is None:
            self.places.set("")
        else:
            # Where the worldserver keeps its files, relative to its folder when inside it.
            self.places.set("From the worldserver folder:  " + "  \u00b7  ".join(
                "%s: %s" % (label, os.path.relpath(path, server.bin)
                           if label != "databases" and core.is_inside(path, server.bin) else path)
                for label, path in core.folder_lines(M, server, client, dbs)
                if label not in ("worldserver", "sources", "game")))
        self.fill_tree(state, client)
        title = "%s  (%s)" % (M.title, M.name) if M.title != M.name else M.name
        if state.strong():
            self.show_state("present", title, "Installed, in whole or in part. Remove takes away what is left: %s."
                            % removal_parts(M, dbs), "remove")
        elif state.weak() and state.removable():
            self.show_state("conflict", title, "The items below carry the module's identifiers without proof they "
                                               "are its own: no install while they are there. If they are leftovers "
                                               "of this module, Remove leftovers takes away their database and DBC "
                                               "rows (never a game file).", "leftovers")
        elif state.weak():
            # nothing a removal of leftovers could take: no button
            self.show_state("conflict", title, "The items below stand in the way and are not leftovers the "
                                               "installer can remove: another archive of the game provides its own "
                                               "version of a file of the module, or an official archive holds rows "
                                               "with its identifiers. The installer never changes them: no install "
                                               "while they are there.")
        else:
            self.show_state("absent", title, "Not installed. Install puts in place: %s."
                            % install_parts(M, server, client), "install")
        self.write_log("%s: %s" % (M.name, {"remove": "present", "leftovers": "conflict", None: "conflict",
                                            "install": "not installed"}[self.action]))

    def fill_tree(self, state, client):
        self.tree.delete(*self.tree.get_children())
        groups = [("Traces of the module", state.lines(), ""),
                  ("Carry its identifiers without being proven its own", state.conflict_lines(), "conflict"),
                  ("Archives that could not be read",
                   [("archive", "%s (%s)" % u) for u in (client.unreadable if client else [])], "conflict")]
        shown = False
        for name, lines, tag in groups:
            if not lines:
                continue
            shown = True
            parent = self.tree.insert("", "end", text="%s (%d)" % (name, len(lines)), open=True, tags=("group",))
            for area, text in lines:
                self.tree.insert(parent, "end", text=area, values=(text,), tags=(tag,))
        if not shown:
            self.tree.insert("", "end", text="No trace of the module", values=("",))

    # -- install, remove --------------------------------------------------------
    def act(self):
        if self.working or not self.context or not self.action:
            return
        M, server, client, dbs, state = self.context
        action = self.action
        cancel = ("Cancel", False, "TButton")
        refusals = core.wow_exe_refusals(M, client) if action == "install" else []
        if any(s != "original" for _, s in refusals):
            self.dialog("Cannot install %s" % M.title, core.refusal_text(client, refusals)[0].upper()
                        + core.refusal_text(client, refusals)[1:] + ".", [("Close", None, "TButton")], kind="error")
            return
        # The existing archives about to change, offered for a backup.
        changing = core.install_targets(M, client, server) if action == "install" \
            else sorted(core.removal_plan(M, state, action == "leftovers"))
        archives = core.archives_to_back_up(changing)
        option = "Back up first the archive%s about to change: %s" % (
            "s" if len(archives) > 1 else "",
            ", ".join("%s (%s)" % (os.path.basename(p), core.size_text(n)) for p, n in archives)) \
            if archives else None
        if action == "install":
            parts = ["the server sources, its configuration and Lua scripts"] if M.server_module else []
            if client is not None:
                parts.append("the game archives (MPQ)" + (", Interface\\AddOns" if M.addons else ""))
            if server is not None and any(d.server is not None for d in M.dbc):
                parts.append("the server DBC files")
            if dbs and not M.server_module:
                parts.append("the databases")
            where = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
            text = "The installer writes into %s.%s" % (
                where, "\n\nThe server must be rebuilt afterwards." if M.server_module else "")
            if refusals:
                text += "\n\n%s." % core.refusal_text(client, refusals)
            answer = self.dialog("Install %s?" % M.title, text,
                                 [("Patch Wow.exe and install" if refusals else "Install", True,
                                   ACTIONS["install"][1]), cancel], option=option)
        elif action == "remove":
            answer = self.dialog("Remove %s?" % M.title, "Everything that is left of it goes: %s.\n\nThis cannot "
                                 "be undone." % removal_parts(M, dbs),
                                 [("Remove", True, ACTIONS["remove"][1]), cancel], kind="warning", focus=1,
                                 option=option)
        else:
            answer = self.dialog("Remove the leftovers of %s?" % M.title, "The database rows and DBC rows listed "
                                 "as conflicts go; game files never do. Do it only if they are leftovers of this "
                                 "module.", [("Remove leftovers", True, ACTIONS["leftovers"][1]), cancel],
                                 kind="warning", focus=1, option=option)
        ok, backup = answer if option else (answer, False)
        if not ok:
            return

        def job():
            # Read again: the state may have changed since the check.
            fresh = core.survey(M, server, core.Client(client.folder) if client else None, dbs)
            now = "remove" if fresh.strong() else "leftovers" if fresh.weak() else "install"
            if now != action:
                raise core.InstallerError("the state changed since the check: check again")
            core.refuse_while_running(server, client)
            if action == "install":
                core.install_and_check(M, server, client, dbs, patch_exe=bool(refusals), backup=backup)
            else:
                core.remove_and_check(M, server, client, dbs, fresh, leftovers=action == "leftovers",
                                      backup=backup)
            return M, server, action

        self.pending_action = action
        self.write_log("")
        self.work({"install": "Installing...", "remove": "Removing...", "leftovers": "Removing leftovers..."}
                  [action], job, self.acted)

    def acted(self, result):
        M, server, action = result
        self.pending_action = None
        if action == "install":
            title, text = "Installation complete", ""
            if M.server_module:
                text = "\n".join(core.build_steps(server))
                if core.has_sql(M):
                    text += "\n\n" + ("On first start, the core updater applies the module's SQL."
                                      if server.updates_mask & 6 == 6 else
                                      "The installer applied the module's SQL itself (the core updater is off).")
            elif server is not None and core.has_server_part(M):
                text = "The worldserver reads its DBC files and database rows when it starts."
        else:
            title = "Uninstallation complete" if action == "remove" else "Leftovers removed"
            text = "\n".join(core.build_steps(server)) if M.server_module else ""
        self.dialog(title, text or "Nothing else to do.", [("Close", None, "Accent.TButton")])
        self.check()

    def close(self):
        if self.working:
            self.dialog("Please wait", "The installer is working: wait until it has finished.",
                        [("Close", None, "TButton")], kind="warning")
            return
        self.root.destroy()


def run(module, settings, load_manifest):
    """Opens the window; module: the module folder given on the command line (or dropped on the exe)."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # sharp text on scaled screens
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    InstallerWindow(root, module, settings, load_manifest)
    root.mainloop()
