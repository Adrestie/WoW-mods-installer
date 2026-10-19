# -*- coding: utf-8 -*-
"""What the loot editor reads (MIT licence): items from the world database,
their icons from the game's archives, the families of the server's
DynamicLoot and the loot prefixes of its modules' .conf files.

The database is reached the installer's way: worldserver.conf gives the
connection, mysql.exe runs the queries (core.Database). The icons come the
game's way: item_template.displayid, ItemDisplayInfo.dbc read from the
archive the game reads it from, then Interface\\Icons\\<icon>.blp, decoded
by Pillow."""
import glob
import io
import json
import os
import re
import struct

import core
import loot_model

ICON_DBC = "DBFilesClient\\ItemDisplayInfo.dbc"
ICON_FIELD = 5                      # ItemDisplayInfo.dbc: InventoryIcon[0]
UNKNOWN_ICON = "INV_Misc_QuestionMark"
MAX_RESULTS = 300

QUALITY_COLOURS = ["#9d9d9d", "#ffffff", "#1eff00", "#0070dd", "#a335ee", "#ff8000", "#e6cc80", "#e6cc80"]


def settings_path():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "WoW-mods", "loot-editor-settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=1, ensure_ascii=False)
    except OSError:
        pass


class Item(object):
    def __init__(self, entry, name, quality, display, item_level, required_level):
        self.entry, self.name, self.quality = entry, name, quality
        self.display, self.item_level, self.required_level = display, item_level, required_level

    @property
    def colour(self):
        return QUALITY_COLOURS[self.quality] if 0 <= self.quality < len(QUALITY_COLOURS) else QUALITY_COLOURS[1]


def _quoted(text):
    return "'%s'" % text.replace("\\", "\\\\").replace("'", "''")


def _like(text):
    return _quoted("%" + text.replace("\\", "\\\\\\\\").replace("%", "\\%").replace("_", "\\_") + "%")


class World(object):
    """The world database of a worldserver folder, and what the editor needs from the server."""

    def __init__(self, bin_dir, mysql=None, locale=None):
        self.server = core.Server(bin_dir)
        path = core.find_mysql(self.server, {}, mysql)
        if not path:
            raise core.InstallerError("mysql.exe not found")
        self.mysql = path
        self.db = core.Database(path, self.server.databases["world"])
        self.locale = locale if locale and locale != "enUS" and locale != "enGB" else None
        self.db.run("SELECT 1;")

    def families(self):
        """(families, read from the server's sources)."""
        found = loot_model.families_from_sources(self.server.sources)
        return (found, True) if found else (list(loot_model.BUILT_IN_FAMILIES), False)

    def prefixes(self):
        """The loot prefixes the .conf files of the server's modules declare, sorted."""
        found = set()
        for path in glob.glob(os.path.join(self.server.module_confs, "*.conf")) + \
                glob.glob(os.path.join(self.server.module_confs, "*.conf.dist")):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    found.update(loot_model.prefixes_in(f.read()))
            except OSError:
                pass
        return sorted(found)

    def _select(self, where, limit=MAX_RESULTS + 1):
        name = "COALESCE(NULLIF(l.Name, ''), t.name)" if self.locale else "t.name"
        join = (" LEFT JOIN item_template_locale l ON l.ID = t.entry AND l.locale = %s" % _quoted(self.locale)) \
            if self.locale else ""
        rows = self.db.run("SELECT t.entry, %s, t.Quality, t.displayid, t.ItemLevel, t.RequiredLevel "
                           "FROM item_template t%s WHERE %s ORDER BY t.entry LIMIT %d;" % (name, join, where, limit))
        items = []
        for r in rows:
            try:
                items.append(Item(int(r[0]), r[1], int(r[2]), int(r[3]), int(r[4]), int(r[5])))
            except (ValueError, IndexError):
                pass
        return items

    def search(self, text, quality=None):
        """(items, more): what a search finds, at most MAX_RESULTS. text: a name (or part of one), an
        entry, or a range "first-last"."""
        text = text.strip()
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", text)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            where = "t.entry BETWEEN %d AND %d" % (a, b)
        elif text.isdigit():
            where = "t.entry = %d" % int(text)
        elif text:
            like = _like(text)
            where = "(t.name LIKE %s%s)" % (like, (" OR l.Name LIKE %s" % like) if self.locale else "")
        else:
            return [], False
        if quality is not None:
            where += " AND t.Quality = %d" % quality
        items = self._select(where)
        return items[:MAX_RESULTS], len(items) > MAX_RESULTS

    def items(self, entries):
        """{entry: Item} of these entries, those the database holds."""
        found = {}
        entries = sorted(set(entries))
        for i in range(0, len(entries), 500):
            chunk = entries[i:i + 500]
            for item in self._select("t.entry IN (%s)" % ", ".join(str(e) for e in chunk), limit=len(chunk)):
                found[item.entry] = item
        return found


def _locale_name(text):
    """"frfr", "FRFR" -> "frFR"; None for anything that is not a locale."""
    return text[:2].lower() + text[2:].upper() if text and re.match(r"^[A-Za-z]{4}$", text) else None


def game_locale(client):
    """The language the game runs in: WTF\\Config.wtf (SET locale), else its first language folder."""
    for wtf in glob.glob(os.path.join(client.folder, "[Ww][Tt][Ff]", "[Cc]onfig.wtf")):
        try:
            with open(wtf, encoding="utf-8", errors="replace") as f:
                m = re.search(r'^\s*SET\s+locale\s+"([A-Za-z]{4})"', f.read(), re.M | re.I)
            if m:
                return _locale_name(m.group(1))
        except OSError:
            pass
    return _locale_name(os.path.basename(client.locale_dir)) if client.locale_dir else None


class Icons(object):
    """Item icons, read from the game: {display id: icon name} from ItemDisplayInfo.dbc, then the .blp
    files, decoded by Pillow, kept in memory and in a cache folder."""

    def __init__(self, game_folder):
        from PIL import Image          # noqa: F401  (a clear error now rather than at the first icon)
        self.client = core.Client(game_folder)
        self.locale = game_locale(self.client)
        folder = next((n for n in self.client.languages.values() if self.locale and n.lower() == self.locale.lower()),
                      None)
        if folder:
            self.client = self.client.in_language(folder)
        self.names = {}
        self.images = {}
        self.cache = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "WoW-mods",
                                  "loot-editor-icons")
        raw = self._read(ICON_DBC)
        if raw is None:
            raise core.InstallerError("%s not found in the game's archives" % ICON_DBC)
        fields, size, rows, strings = core.dbc_split(raw, "ItemDisplayInfo.dbc")
        for row in rows:
            display = struct.unpack_from("<I", row, 0)[0]
            offset = struct.unpack_from("<I", row, ICON_FIELD * 4)[0]
            name = core.read_string(strings, offset)
            if name:
                self.names[display] = name

    def _read(self, name):
        path = self.client.winner(name)
        if not path:
            return None
        archive = self.client.open(path)
        return archive.read(name) if archive is not None else None

    def image(self, display, size=36):
        """The icon of a display id as a Pillow image of that size (the question mark when there is none)."""
        name = self.names.get(display) or UNKNOWN_ICON
        key = (name.lower(), size)
        if key in self.images:
            return self.images[key]
        image = self._load(name) or (self._load(UNKNOWN_ICON) if name != UNKNOWN_ICON else None)
        from PIL import Image
        if image is None:
            image = Image.new("RGBA", (64, 64), (60, 60, 60, 255))
        image = image.convert("RGBA").resize((size, size), Image.LANCZOS)
        self.images[key] = image
        return image

    def _load(self, name):
        from PIL import Image
        cached = os.path.join(self.cache, re.sub(r"[^A-Za-z0-9_.-]", "_", name.lower()) + ".png")
        if os.path.isfile(cached):
            try:
                return Image.open(cached).copy()
            except OSError:
                pass
        raw = self._read("Interface\\Icons\\%s.blp" % name)
        if raw is None:
            return None
        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except Exception:
            return None
        try:
            os.makedirs(self.cache, exist_ok=True)
            image.save(cached)
        except OSError:
            pass
        return image
