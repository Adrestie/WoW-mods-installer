# -*- coding: utf-8 -*-
r"""WoW-mods installer engine (MIT licence).

One program installs and removes any module that carries a manifest
(installer.json at its root, format described in MANIFEST.md).

No trace of the module: it installs. It copies the module into the server
sources (modules/), puts its configuration and Lua scripts in place, adds its
rows to the server DBC files and writes them, with its game files, directly
into the game's MPQ archives, and copies its addons into Interface\AddOns.
The server is then rebuilt; on first start the core updater applies the
module's SQL. A package without server module (server_module false) writes
the game files, the addons and the DBC rows, and applies its own SQL: nothing
goes into the server's sources, configuration or scripts. When its manifest
makes the worldserver folder optional and it is left empty (a player), only
the game part goes in: no DBC row that the server also needs, no SQL.

Any trace: it removes everything that is left, wherever it is (sources,
configuration, Lua scripts anywhere in the scripts folder, server DBC rows,
DBC rows and files of every game archive, database data read and deleted
through the mysql client). An uninstall started by hand is therefore
finished by running the program again. Or, when asked, it updates: the same
removal, minus the players' data (kept_on_update tables) and the module's
.conf, then the install of the package.

Items that carry the module's identifiers with nothing proving they are its
own (a DBC row with the same identifier and other content, a game file
provided in another version by another archive, database rows without the
rest of the module) are a conflict: no install, and they are removed only
when the user confirms they are leftovers of the module (never a game file).

Other modules' rows, files and tables are never touched. In each game archive
it writes to, the installer leaves a receipt (WoW-mods\<module>.receipt)
listing what it put there; removal takes what its receipts name, the DBC rows
identical to its own and the files stored under the module's own folders. An
identical file provided by another archive is never its own.

Compaction (--compact, or the window's Compact button) rewrites the custom
patch archives chosen (patch-Z and patch-xxXX-Z by default) without the space
their removed and replaced files left, the game reading the same files.
"""
import argparse
import array
import copy
import glob
import json
import os
import re
import shutil
import stat
import string
import struct
import subprocess
import sys
import tempfile
import time
import traceback

import mpq_archive
import wow_exe

# Package entries never copied into modules/.
NEVER_COPIED = {".git", "__pycache__"}

NEW_ARCHIVE_NAME = "patch-Z.MPQ"
# The archives Wow.exe 12340 loads: in Data, and in the language folder (%s: its name);
# "." stands for the "?" of patch-?.MPQ, one character.
DATA_ARCHIVES = re.compile(r"^(common|common-2|expansion|lichking|patch|patch-.)\.mpq$", re.I)
LOCALE_ARCHIVES = r"^((locale|speech|expansion-locale|lichking-locale|expansion-speech|lichking-speech)-%s" \
                  r"|patch-%s(-.)?)\.mpq$"
# Interface files Wow.exe checks against Blizzard's signature: (check, folder, what they are).
INTERFACE_CHECKS = (("GlueXML", "interface\\gluexml\\", "login screen files (Interface\\GlueXML)"),
                    ("FrameXML", "interface\\framexml\\", "in-game interface files (Interface\\FrameXML)"))
MANIFEST_NAME = "installer.json"
RECEIPT_FOLDER = "WoW-mods"
CREATE_NO_WINDOW = 0x08000000


class InstallerError(Exception):
    pass


def sql_list(values):
    """Values as a SQL list: strings quoted, numbers as integers."""
    return ", ".join(("'%s'" % v.replace("'", "''")) if isinstance(v, str) else str(int(v)) for v in values)


# ------------------------------------------------------------------ output

def print_line(text):
    print(text, flush=True)


# Where say() sends each line: the console, or the window's log.
output = print_line


def say(text=""):
    output(text)


def heading(text):
    say()
    say(text)
    say("-" * len(text))


# ------------------------------------------------------------------ remembered paths

def settings_path():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "WoW-mods", "installer-settings.json")


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


# ------------------------------------------------------------------ configuration files

def read_conf(path):
    """{key: value} of an AzerothCore .conf file (quotes removed)."""
    values = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.lstrip().startswith("#"):
                continue
            m = re.match(r"\s*([A-Za-z0-9_.]+)\s*=\s*(.*?)\s*$", line)
            if m:
                v = m.group(2)
                if len(v) >= 2 and v[0] == v[-1] == '"':
                    v = v[1:-1]
                values[m.group(1)] = v
    return values


def read_version(folder, name):
    """The whole number written in folder/name, or None."""
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def is_inside(path, folder):
    path = os.path.normcase(os.path.abspath(path))
    folder = os.path.normcase(os.path.abspath(folder))
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


# ------------------------------------------------------------------ the server

class Server(object):
    """Everything the installer derives from the worldserver folder: configuration
    files, DBC folder, Lua scripts folder, databases, build folder and sources.

    bin_dir: folder of worldserver.exe; sources: AzerothCore sources, or None
    to read them from the build folder's CMakeCache.txt."""

    def __init__(self, bin_dir, sources=None):
        self.bin = os.path.normpath(os.path.abspath(bin_dir))
        if not os.path.isfile(os.path.join(self.bin, "worldserver.exe")):
            raise InstallerError("no worldserver.exe in %s" % self.bin)
        candidates = [os.path.join(self.bin, "configs", "worldserver.conf"),
                      os.path.join(self.bin, "worldserver.conf"),
                      os.path.join(self.bin, "..", "etc", "worldserver.conf")]
        self.conf = next((os.path.normpath(c) for c in candidates if os.path.isfile(c)), None)
        if not self.conf:
            raise InstallerError("worldserver.conf not found (searched in configs\\ and next to worldserver.exe)")
        self.module_confs = os.path.join(os.path.dirname(self.conf), "modules")
        c = read_conf(self.conf)
        self.data = self._resolve(c.get("DataDir") or ".")
        self.dbc = os.path.join(self.data, "dbc")
        self.lua = self._resolve(self._lua_folder())
        self.databases = {"world": self._db_info(c, "WorldDatabaseInfo"),
                          "characters": self._db_info(c, "CharacterDatabaseInfo")}
        try:
            self.updates_mask = int(c.get("Updates.EnableDatabases") or 0)
        except ValueError:
            self.updates_mask = 0
        self.mysql_conf = c.get("MySQLExecutable") or ""
        self.build_dir, self.build_config = self._find_build()
        self.sources = os.path.normpath(sources) if sources else self._sources_from_cache()

    def _resolve(self, p):
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(self.bin, p))

    def _lua_folder(self):
        # The core reads only the .conf file: without it, ALE uses its default.
        for name, key in (("mod_ale.conf", "ALE.ScriptPath"), ("mod_eluna.conf", "Eluna.ScriptPath")):
            p = os.path.join(self.module_confs, name)
            if os.path.isfile(p):
                v = read_conf(p).get(key)
                if v:
                    return v
        return "lua_scripts"

    @staticmethod
    def _db_info(c, key):
        parts = (c.get(key) or "").split(";")
        if len(parts) != 5:
            raise InstallerError("%s unreadable in worldserver.conf" % key)
        host, port, user, password, name = parts
        return {"host": host, "port": port, "user": user, "password": password, "name": name}

    def _find_build(self):
        """(build folder holding CMakeCache.txt, configuration name) or (None, None)."""
        d = self.bin
        for _ in range(6):
            if os.path.isfile(os.path.join(d, "CMakeCache.txt")):
                config = os.path.basename(self.bin)
                if config not in ("Debug", "Release", "RelWithDebInfo", "MinSizeRel"):
                    config = None
                return d, config
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return None, None

    def _sources_from_cache(self):
        if not self.build_dir:
            return None
        with open(os.path.join(self.build_dir, "CMakeCache.txt"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("CMAKE_HOME_DIRECTORY:"):
                    return os.path.normpath(line.split("=", 1)[1].strip())
        return None

    def has_valid_sources(self):
        return bool(self.sources) and os.path.isdir(os.path.join(self.sources, "modules")) and \
            os.path.isdir(os.path.join(self.sources, "src"))

    @property
    def modules(self):
        return os.path.join(self.sources, "modules")


# ------------------------------------------------------------------ the game and its archives

ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def archive_rank(name, locale=None):
    """(load rank, is official) of an archive, from its file name; locale: the name of the language
    folder, for an archive that lies there.

    As Wow.exe 12340 does it: base archives first (group 0), then patch-xxXX.MPQ and patch.MPQ
    (group 1, in that order), then every patch-?.MPQ of Data and patch-xxXX-?.MPQ of the language
    folder together (group 2), ranked by their path, "Data\\patch-z.mpq" or
    "Data\\xxXX\\patch-xxXX-z.mpq", compared without letter case. The last one read wins: Data
    comes after the language folder when the language sorts before "patch" (deDE, enGB, enUS, esES,
    esMX, frFR, koKR), before it otherwise (ruRU, zhCN, zhTW)."""
    stem = name.lower().rsplit(".", 1)[0]
    if not stem.startswith("patch"):
        return (0, 0, stem), True
    parts = [p for p in stem[5:].split("-") if p]
    in_locale = bool(parts) and len(parts[0]) > 1 and not parts[0].isdigit()
    suffix = parts[-1] if parts and not (in_locale and len(parts) == 1) else ""
    official = suffix in ("", "2", "3")
    if not suffix:
        return (1, 0 if in_locale else 1, ""), official
    path = "data\\" + (locale + "\\" if locale else "") + name
    return (2, 0, path.translate(ASCII_LOWER)), official


class Client(object):
    """The game folder: its archives, in the order the client reads them.

    The game reads Data and the one language folder its settings name. The installer does without
    knowing which: a DBC file it copies serves every language (game_copy), and it reads the game as
    each language does (views). Its own reading takes the first language folder the game reads
    before Data (any but ruRU, zhCN and zhTW): Data\\patch-Z.MPQ then serves every such language."""

    def __init__(self, folder):
        self.folder = os.path.normpath(os.path.abspath(folder))
        self.data = next((os.path.join(self.folder, n) for n in os.listdir(self.folder)
                          if n.lower() == "data" and os.path.isdir(os.path.join(self.folder, n))), None) \
            if os.path.isdir(self.folder) else None
        if not self.data or not any(n.lower().endswith(".mpq") for n in os.listdir(self.data)):
            raise InstallerError("no Data folder with .MPQ archives in %s" % self.folder)
        # {name in lower case: name on disk} of the folders of Data that hold .MPQ archives
        self.languages = {n.lower(): n for n in os.listdir(self.data) if os.path.isdir(os.path.join(self.data, n))
                          and glob.glob(os.path.join(self.data, n, "*.mpq"))}
        name = min(self.languages.values(), key=lambda n: (n.lower() > "patch", n.lower()), default=None)
        self.locale_dir = os.path.join(self.data, name) if name else None
        self.wow_exe = wow_exe.find(self.folder)
        self._open = {}
        self.unreadable = []

    def in_language(self, name):
        """The same game read in another of its languages (name: a language folder of Data)."""
        view = copy.copy(self)
        view.locale_dir = os.path.join(self.data, name)
        return view

    def views(self):
        """The game read in each of its languages (the game itself, without language folder)."""
        return [self.in_language(n) for n in sorted(self.languages.values(), key=str.lower)] or [self]

    def rank(self, path):
        """The load rank of an archive of the game (archive_rank), existing or not."""
        in_locale = self.locale_dir is not None and \
            os.path.normcase(os.path.dirname(path)) == os.path.normcase(self.locale_dir)
        return archive_rank(os.path.basename(path), os.path.basename(self.locale_dir) if in_locale else None)[0]

    def archives(self):
        """[(rank, path)] of every archive the game loads, from weakest to strongest."""
        found = []
        patterns = [(self.data, DATA_ARCHIVES)]
        if self.locale_dir:
            locale = re.escape(os.path.basename(self.locale_dir))
            patterns.append((self.locale_dir, re.compile(LOCALE_ARCHIVES % (locale, locale), re.I)))
        for folder, pattern in patterns:
            for n in os.listdir(folder):
                p = os.path.join(folder, n)
                if pattern.match(n) and os.path.isfile(p):
                    found.append((self.rank(p), p))
        return sorted(found)

    def stray_archives(self):
        """The .mpq files of Data and of its subfolders that the game does not load in any language (a
        name it does not look for): never written into, only searched for what a module left."""
        loaded = {os.path.normcase(p) for view in self.views() for r, p in view.archives()}
        folders = [self.data] + [os.path.join(self.data, n) for n in sorted(os.listdir(self.data))
                                 if os.path.isdir(os.path.join(self.data, n))]
        return [p for folder in folders for p in (os.path.join(folder, n) for n in sorted(os.listdir(folder)))
                if p.lower().endswith(".mpq") and os.path.isfile(p) and os.path.normcase(p) not in loaded]

    def custom_archives(self):
        """The custom archives the game loads, in any of its languages."""
        found = []
        for view in self.views():
            found += [p for r, p in view.archives() if not archive_rank(os.path.basename(p))[1] and p not in found]
        return found

    def open(self, path):
        """The archive, or None if it cannot be read (recorded in unreadable)."""
        if path not in self._open:
            try:
                self._open[path] = mpq_archive.Archive(path)
            except (mpq_archive.MpqError, OSError, struct.error) as e:
                self._open[path] = None
                self.unreadable.append((path, str(e)))
        return self._open[path]

    def forget(self, path):
        self._open.pop(path, None)

    def winner(self, name, below=None, skip=()):
        """The archive the game reads this file from; below=path: the winner
        among the archives read before that one (all of them, for an archive the game does not load);
        skip=[paths]: the winner without those archives."""
        skipped = {os.path.normcase(p) for p in skip}
        paths = [p for r, p in self.archives() if os.path.normcase(p) not in skipped]
        if below is not None and below in paths:
            paths = paths[:paths.index(below)]
        for path in reversed(paths):
            a = self.open(path)
            if a is not None and a.contains(name):
                return path
        return None

    def write_targets(self):
        """[(archive, the game read as it reads that archive)]: the archives installs write into, each
        the patch read last by the languages it serves; the one there (any letter case), or the path of
        the one to create. Data\\patch-Z.MPQ serves a game without language folder and every language
        whose folder Wow.exe reads before Data (deDE, enGB, enUS, esES, esMX, frFR, koKR); a language
        whose folder it reads after Data (ruRU, zhCN, zhTW) overrides Data with its own patches, and
        gets patch-xxXX-Z.MPQ in its folder."""
        names = sorted(self.languages.values(), key=str.lower)
        before = [n for n in names if n.lower() < "patch"]
        targets = [(os.path.join(self.data, NEW_ARCHIVE_NAME), self.in_language(before[0]) if before else self)] \
            if before or not names else []
        targets += [(os.path.join(self.data, n, "patch-%s-Z.MPQ" % n), self.in_language(n))
                    for n in names if n.lower() > "patch"]
        return [(next((p for r, p in view.archives() if os.path.normcase(p) == os.path.normcase(t)), t), view)
                for t, view in targets]

    def view_of(self, path):
        """The game read as it reads this archive: in its folder's language, or as itself for Data."""
        folder = os.path.normcase(os.path.dirname(path))
        name = next((n for n in self.languages.values()
                     if os.path.normcase(os.path.join(self.data, n)) == folder), None)
        return self.in_language(name) if name else self


# ------------------------------------------------------------------ DBC

def dbc_split(raw, name):
    """(field count, record size, [row bytes], string block) of a DBC file.

    Most files hold 4-byte fields only (record size = fields x 4); a few hold
    byte fields too (SpellChainEffects.dbc: 48 fields in 177 bytes). A row is
    then read as 4-byte words, followed by the bytes left over."""
    if raw[:4] != b"WDBC":
        raise InstallerError("%s is not a DBC file (no WDBC signature)" % name)
    count, fields, size, string_size = struct.unpack_from("<4I", raw, 4)
    if size < 4 or size > fields * 4 or len(raw) < 20 + count * size + string_size:
        raise InstallerError("%s: inconsistent DBC header" % name)
    rows = [raw[20 + i * size:20 + (i + 1) * size] for i in range(count)]
    start = 20 + count * size
    return fields, size, rows, bytearray(raw[start:start + string_size])


def dbc_join(fields, size, rows, strings):
    return b"WDBC" + struct.pack("<4I", len(rows), fields, size, len(strings)) + \
        b"".join(rows) + bytes(strings)


def _row_id(row):
    return struct.unpack_from("<I", row)[0]


def read_string(strings, offset):
    """The string starting at this offset of the string block (empty for 0)."""
    if not offset or offset >= len(strings):
        return ""
    end = strings.find(b"\0", offset)
    return bytes(strings[offset:end if end >= 0 else len(strings)]).decode("utf-8", "surrogateescape")


def row_values(row, strings, text):
    """The values of a DBC row, in the form of the module's rows: text fields
    (indices in text) read from the string block, the others as unsigned
    integers; the bytes left over after the last 4-byte word, if any, as a
    last value (bytes)."""
    words = len(row) // 4
    values = [read_string(strings, v) if i in text else v
              for i, v in enumerate(struct.unpack_from("<%dI" % words, row))]
    if len(row) % 4:
        values.append(bytes(row[words * 4:]))
    return values


def row_bytes(values, strings, text, size, name):
    """A DBC row from its values (row_values' form); its strings are appended to the string block."""
    out = bytearray()
    for i, v in enumerate(values):
        if isinstance(v, bytes):
            out += v
        elif i in text:
            if not v:
                out += struct.pack("<I", 0)
                continue
            if not strings:
                strings.extend(b"\0")          # offset 0 is the empty string
            out += struct.pack("<I", len(strings))
            strings.extend(v.encode("utf-8", "surrogateescape") + b"\0")
        else:
            out += struct.pack("<I", int(v) & 0xFFFFFFFF)
    if len(out) != size:
        raise InstallerError("%s: a module row is %d bytes long, the file's rows %d" % (name, len(out), size))
    return bytes(out)


def dbc_survey(raw, name, d, rows, given=None):
    """(our ids, conflicting ids): rows of this DBC that carry one of the module's
    identifiers, identical to the module's rows or not.

    d: the manifest's DBC entry; rows: the module's rows for this side; given: the same rows as the
    manifest declares them, also the module's (written so by installers that did not fill every
    language)."""
    fields, size, recs, strings = dbc_split(raw, name)
    if fields != d.fields:
        raise InstallerError("%s has %d fields, %d expected: unexpected client version" % (name, fields, d.fields))
    expected = {r[0]: r for r in rows}
    declared = {r[0]: r for r in given or []}
    ours, others = [], []
    for r in recs:
        i = _row_id(r)
        if i in expected:
            values = row_values(r, strings, d.text)
            (ours if values == expected[i] or values == declared.get(i) else others).append(i)
    return sorted(ours), sorted(others)


def dbc_layout_error(client, name, path, fields, d):
    """The refusal when the DBC the game reads (from the archive path) has another field count
    than the manifest's (d); the archives read after it that could not be opened are named, since
    the game may read the file from one of them."""
    loaded = [p for r, p in client.archives()]
    after = set(loaded[loaded.index(path) + 1:]) if path in loaded else set()
    unread = [p for p, _ in client.unreadable if p in after]
    return InstallerError("%s, read by the game from %s, has %d fields, %d expected: unexpected client version%s"
                          % (name, path, fields, d.fields,
                             "; or the game reads it from an archive the installer cannot open: %s"
                             % ", ".join(unread) if unread else ""))


def dbc_add(raw, name, d, rows):
    """The DBC with the module's rows appended (rows already carrying their
    identifiers removed first); their strings appended to the string block."""
    fields, size, recs, strings = dbc_split(raw, name)
    if fields != d.fields:
        raise InstallerError("%s has %d fields, %d expected: unexpected client version" % (name, fields, d.fields))
    ids = set(r[0] for r in rows)
    recs = [r for r in recs if _row_id(r) not in ids]
    recs += [row_bytes(values, strings, d.text, size, name) for values in rows]
    return dbc_join(fields, size, recs, strings)


def dbc_remove(raw, name, ids, text):
    """(DBC without the rows of these identifiers, rows removed).

    The string block is cut after the last string a remaining row still
    reads: through a declared text field (text), and, to be safe, through any
    other field whose value points at a string start further on (a text field
    the manifest would not declare). Strings the install appended at the end
    go; nothing still in use does. With nothing removed, the original bytes
    are returned."""
    fields, size, recs, strings = dbc_split(raw, name)
    words = size // 4
    ids = set(ids)
    kept = [r for r in recs if _row_id(r) not in ids]
    n = len(recs) - len(kept)
    if not n:
        return raw, 0
    if strings:
        length = len(strings)

        def end_of(offset):
            zero = strings.find(b"\0", offset)
            return (zero if zero >= 0 else length - 1) + 1

        end = 1
        for r in kept:
            values = struct.unpack_from("<%dI" % words, r)
            for c in text:
                if c < words and 0 < values[c] < length:
                    end = max(end, end_of(values[c]))
        all_values = array.array("I")
        all_values.frombytes(b"".join(r[:words * 4] for r in kept))
        for v in {x for x in all_values if end <= x < length}:
            if strings[v - 1] == 0:
                end = max(end, end_of(v))
        strings = strings[:min(end, length)]
    return dbc_join(fields, size, kept, strings), n


def localized_strings(text):
    """The first field of each localized string among these text fields: 16 text fields in a row, one
    per language of the client (0 enUS, 1 koKR, 2 frFR, 3 deDE, 4 zhCN, 5 zhTW, 6 esES, 7 esMX,
    8 ruRU), then a field of flags."""
    return sorted(i for i in text if i - 1 not in text and i + 16 not in text and all(i + k in text for k in range(16)))


def in_every_language(row, localized):
    """The row with each of its localized strings (first fields: localized) given in every language: a
    language the module does not translate takes the English text, or the first text given without one."""
    row = list(row)
    for a in localized:
        texts = row[a:a + 16]
        fallback = texts[0] or next((t for t in texts if t), "")
        row[a:a + 16] = [t or fallback for t in texts]
    return row


def _dbc_values(raw, name):
    """(fields, row size in words, every field of every row, string block) of a DBC file whose fields
    are all 4 bytes; None for another one (byte fields)."""
    fields, size, recs, strings = dbc_split(raw, name)
    if size % 4:
        return None
    values = array.array("I")
    values.frombytes(raw[20:20 + len(recs) * size])
    return fields, size // 4, values, strings


def _dbc_content(parts, d, localized):
    """What a DBC file holds besides its localized strings (parts: _dbc_values): its layout, every field
    with the localized strings and the other text fields set to 0, the other text fields' strings."""
    fields, words, values, strings = parts
    count = len(values) // words
    others = sorted(i for i in d.text if not any(a <= i < a + 16 for a in localized))
    texts = [read_string(strings, values[r * words + i]) for r in range(count) for i in others]
    masked = array.array("I", values)
    for i in [a + k for a in localized for k in range(16)] + others:
        masked[i::words] = array.array("I", [0]) * count
    return fields, words, masked, texts


def game_copy(client, name, d, skip):
    """The DBC file an install copies whole into an archive, made to serve every language of the game
    folder: the file as the game (client: read as that archive serves it) reads it without the archives
    skip (all those installs write into, so that removal finds the same copy whatever order it goes in),
    its localized strings filled from each language's own file (a language's file fills only its own
    slot of each string). The languages taken are those whose file holds the same rows and values; the
    first by name gives the rest."""
    current = client.open(client.winner(name, skip=skip)).read(name)
    localized = d.localized
    parts = _dbc_values(current, name)
    if not localized or parts is None or len(client.languages) < 2:
        return current
    reference = _dbc_content(parts, d, localized)
    group = []                  # (the game read in a language, the archive it reads the file from)
    for language in sorted(client.languages.values(), key=str.lower):
        view = client.in_language(language)
        w = view.winner(name, skip=skip)
        theirs = _dbc_values(view.open(w).read(name), name) if w else None
        if theirs is not None and _dbc_content(theirs, d, localized) == reference:
            group.append((view, w))
    if len(group) < 2:
        return current
    fields, words, values, strings = _dbc_values(group[0][0].open(group[0][1]).read(name), name)
    appended = {}
    for view, w in group[1:]:
        their_values, their_strings = _dbc_values(view.open(w).read(name), name)[2:]
        for a in localized:
            for k in range(16):
                column = their_values[a + k::words]
                if not any(column):
                    continue
                ours = values[a + k::words]
                for row, offset in enumerate(column):
                    if offset and not ours[row]:
                        end = their_strings.find(b"\0", offset)
                        text = bytes(their_strings[offset:end if end >= 0 else len(their_strings)])
                        if text not in appended:
                            appended[text] = len(strings)
                            strings.extend(text + b"\0")
                        values[row * words + a + k] = appended[text]
    return b"WDBC" + struct.pack("<4I", len(values) // words, fields, words * 4, len(strings)) + \
        values.tobytes() + bytes(strings)


def write_file_atomic(path, content):
    """Writes a sibling file, then replaces the target in one step."""
    temporary = path + ".installer-tmp"
    try:
        with open(temporary, "wb") as f:
            f.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


# ------------------------------------------------------------------ running processes

def running_processes():
    """[(name, full path or None)] of the running processes."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    result = []
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return result
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k.Process32FirstW(snapshot, ctypes.byref(e))
    while ok:
        path = None
        h = k.OpenProcess(0x1000, False, e.th32ProcessID)      # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(32768)
            if k.QueryFullProcessImageNameW(h, 0, buffer, ctypes.byref(size)):
                path = buffer.value
            k.CloseHandle(h)
        result.append((e.szExeFile, path))
        ok = k.Process32NextW(snapshot, ctypes.byref(e))
    k.CloseHandle(snapshot)
    return result


def running_worldservers(server):
    return [(n, p) for n, p in running_processes()
            if n.lower() == "worldserver.exe" and (p is None or is_inside(p, server.bin))]


def running_game(client):
    me = os.path.normcase(os.path.abspath(sys.executable))
    return [(n, p) for n, p in running_processes()
            if p and is_inside(p, client.folder) and os.path.normcase(p) != me]


# ------------------------------------------------------------------ the database, through the mysql client

class Database(object):
    """One database, queried through mysql.exe. info: connection fields read
    from worldserver.conf (host, port, user, password, name)."""

    def __init__(self, mysql, info):
        self.mysql = mysql
        self.info = info
        self.name = info["name"]

    def _options_text(self):
        i = self.info
        lines = ["[client]", "user=%s" % i["user"],
                 'password="%s"' % i["password"].replace("\\", "\\\\").replace('"', '\\"')]
        if i["host"] == ".":
            lines += ["protocol=PIPE", "socket=%s" % i["port"]]
        else:
            lines += ["host=%s" % i["host"], "port=%s" % i["port"]]
        return "\n".join(lines) + "\n"

    def run(self, sql):
        """Runs the statements; returns the result rows, split into columns."""
        fd, options = tempfile.mkstemp(suffix=".cnf")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self._options_text())
            r = subprocess.run([self.mysql, "--defaults-extra-file=" + options, "--default-character-set=utf8mb4",
                                "--batch", "--skip-column-names", self.name],
                               input=sql.encode("utf-8"), capture_output=True, creationflags=CREATE_NO_WINDOW)
        finally:
            os.remove(options)
        if r.returncode:
            raise InstallerError("MySQL, database %s: %s" % (self.name, r.stderr.decode("utf-8", "replace").strip()))
        return [line.split("\t") for line in r.stdout.decode("utf-8", "replace").splitlines() if line]

    def existing_tables(self, names):
        """The given table names that exist, in lower case."""
        if not names:
            return set()
        r = self.run("SELECT LOWER(table_name) FROM information_schema.tables WHERE table_schema = "
                     "DATABASE() AND LOWER(table_name) IN (%s);" % sql_list([n.lower() for n in names]))
        return {row[0] for row in r}

    def has_column(self, table, column):
        r = self.run("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
                     "AND LOWER(table_name) = '%s' AND LOWER(column_name) = '%s';" % (table.lower(), column.lower()))
        return r[0][0] != "0"


def find_mysql(server, settings, forced=None):
    """Path of mysql.exe: forced, then worldserver.conf, the remembered one, the PATH, usual folders."""
    candidates = [forced, server.mysql_conf, settings.get("mysql"), shutil.which("mysql")]
    for pattern in (r"C:\Program Files\MySQL\MySQL Server *\bin\mysql.exe",
                    r"C:\Program Files\MariaDB *\bin\mysql.exe",
                    r"C:\Program Files (x86)\MySQL\MySQL Server *\bin\mysql.exe"):
        candidates += sorted(glob.glob(pattern), reverse=True)
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.normpath(c)
    return None


# ------------------------------------------------------------------ receipts

def receipt_name(M):
    return RECEIPT_FOLDER + "\\" + M.name + ".receipt"


def receipt_text(M, files, dbc, added=()):
    """The receipt of one archive: files ([names]) and dbc ({file: [ids]}) the installer put there, and
    added: the DBC files it copied whole into this archive, which had none of them."""
    lines = ["# WoW-mods installer: what %s wrote into this archive" % M.name]
    lines += ["file %s" % n for n in sorted(files)]
    lines += ["dbc %s %s" % (f, ",".join(str(i) for i in sorted(ids))) for f, ids in sorted(dbc.items())]
    lines += ["added %s" % f for f in sorted(added)]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def read_receipt(M, a):
    """{"files": [...], "dbc": {file: [ids]}, "added": [...]} if archive a carries a receipt of the
    module, else None."""
    name = receipt_name(M)
    if not a.contains(name):
        return None
    receipt = {"files": [], "dbc": {}, "added": []}
    for line in a.read(name).decode("utf-8", "replace").splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) >= 2 and parts[0] == "file":
            receipt["files"].append(line.strip()[5:])
        elif len(parts) == 3 and parts[0] == "dbc":
            receipt["dbc"][parts[1]] = [int(x) for x in parts[2].split(",") if x.strip()]
        elif len(parts) == 2 and parts[0] == "added":
            receipt["added"].append(parts[1])
    return receipt


# ------------------------------------------------------------------ current state

class State(object):
    """What was found of the module: strong traces prove it is there, weak items
    only carry its identifiers."""

    def __init__(self):
        # strong traces
        self.sources = []            # module folders in modules/
        self.confs = []              # configuration files
        self.lua = []                # the module's Lua scripts, wherever they are
        self.lua_dir = None          # the module's folder in the scripts folder
        self.server_dbc = []         # (path, file, ids identical to the module's)
        self.receipts = []           # (archive, receipt)
        self.client_dbc = []         # (archive, file, ids to remove: identical, or named by the receipt)
        self.files = []              # (archive, name): named by the receipt, or under an owned folder
        self.backups = []            # backups left by older tools
        self.addons = []             # the module's addon folders in Interface\AddOns
        self.server_data = None      # (receipt path, receipt): the module's files in the Data folder
        self.db_strong = {}          # {database: [(text, count)]}: module tables, updater rows
        # weak items
        self.server_conflicts = []   # (path, file, ids with other content)
        self.client_conflicts = []   # (archive, file, ids with other content)
        self.file_conflicts = []     # (archive, name): the game reads another version, from a custom archive
        self.db_weak = {}            # {database: [(text, count)]}: rows of shared tables

    def strong(self):
        return bool(self.sources or self.confs or self.lua or self.lua_dir or self.server_dbc or
                    self.receipts or self.client_dbc or self.files or self.backups or self.addons or
                    self.server_data or self.db_strong)

    def weak(self):
        return bool(self.server_conflicts or self.client_conflicts or self.file_conflicts or self.db_weak)

    def removable(self):
        """True when a removal of leftovers has something to take: DBC rows outside the official
        archives, database rows; never a game file of another archive."""
        return bool(self.server_conflicts or self.db_weak or
                    any(not archive_rank(os.path.basename(a))[1] for a, f, ids in self.client_conflicts))

    def lines(self):
        """[(area, text)] of the module's traces."""
        out = [("sources", d) for d in self.sources]
        out += [("configuration", c) for c in self.confs]
        out += [("Lua script", c) for c in self.lua]
        if self.lua_dir:
            out.append(("Lua folder", self.lua_dir))
        out += [("server DBC", "%s: %d module row(s)" % (p, len(ids))) for p, f, ids in self.server_dbc]
        out += [("receipt", "%s: installation receipt" % a) for a, r in self.receipts]
        out += [("client", "%s, %s: %d module row(s)" % (a, f, len(ids))) for a, f, ids in self.client_dbc]
        by_archive = {}
        for a, n in self.files:
            by_archive.setdefault(a, []).append(n)
        for a, names in sorted(by_archive.items()):
            detail = names[0] if len(names) == 1 else "%d files (%s, ...)" % (len(names), names[0])
            out.append(("game files", "%s: %s" % (a, detail)))
        out += [("backup", s) for s in self.backups]
        out += [("addon", d) for d in self.addons]
        if self.server_data:
            path, receipt = self.server_data
            out.append(("server data", "%s: %d file(s) in the Data folder, %d original(s) kept"
                        % (path, len(receipt.get("files", [])), len(receipt.get("originals", [])))))
        for db, traces in sorted(self.db_strong.items()):
            out += [("database %s" % db, "%s: %s" % (t, n)) for t, n in traces]
        # the module is there: the database rows in its name go with it
        if self.strong():
            for db, traces in sorted(self.db_weak.items()):
                out += [("database %s" % db, "%s: %s" % (t, n)) for t, n in traces]
        return out

    def conflict_lines(self):
        """[(area, text)] of what carries the module's identifiers without being its own."""
        out = [("server DBC", "%s: %s, same identifier, other content" % (p, _id_summary(ids)))
               for p, f, ids in self.server_conflicts]
        out += [("client", "%s, %s: %s, same identifier, other content" % (a, f, _id_summary(ids)))
                for a, f, ids in self.client_conflicts]
        out += [("game file", "%s: %s, provided in another version" % (a, n)) for a, n in self.file_conflicts]
        if not self.strong():
            for db, traces in sorted(self.db_weak.items()):
                out += [("database %s" % db, "%s: %s" % (t, n)) for t, n in traces]
        return out

    def has_conflicts(self):
        """True if something stays in place because it is not the module's (module present or not)."""
        return bool(self.conflict_lines())


def _id_summary(ids):
    ids = list(ids)
    return ", ".join(str(i) for i in ids[:8]) + (" (+%d)" % (len(ids) - 8) if len(ids) > 8 else "")


def module_source_dirs(M, server):
    """Folders of modules/ named after the module or holding all its signature files."""
    found = []
    if not os.path.isdir(server.modules):
        return found
    for n in sorted(os.listdir(server.modules)):
        d = os.path.join(server.modules, n)
        if not os.path.isdir(d):
            continue
        if n.lower() == M.name.lower() or all(os.path.isfile(os.path.join(d, s)) for s in M.signature):
            found.append(d)
    return found


def installed_modules(server):
    """{module name: dependencies} of the modules in the server sources: each keeps its installer.json
    there (a folder without one counts by its name, without dependencies)."""
    found = {}
    if server is None or not server.has_valid_sources():
        return found
    for n in sorted(os.listdir(server.modules)):
        d = os.path.join(server.modules, n)
        if not os.path.isdir(d):
            continue
        name, needs = n, []
        try:
            with open(os.path.join(d, MANIFEST_NAME), encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict):
                name = m.get("module") if isinstance(m.get("module"), str) else n
                needs = [x for x in m.get("dependencies") or [] if isinstance(x, str)]
        except (OSError, ValueError):
            pass
        found[name.lower()] = (name, needs)
    return found


def missing_dependencies(M, server):
    """The modules M needs that the server's sources do not hold."""
    if server is None or not server.has_valid_sources():
        return []
    installed = installed_modules(server)
    return [d for d in M.dependencies if d.lower() not in installed]


def dependent_modules(M, server):
    """The other installed modules whose manifest lists M among their dependencies."""
    return [name for key, (name, needs) in sorted(installed_modules(server).items())
            if key != M.name.lower() and M.name.lower() in (x.lower() for x in needs)]


def missing_dependencies_text(M, server):
    """The warning of an install whose dependencies are missing ("" when none is)."""
    missing = missing_dependencies(M, server)
    if not missing:
        return ""
    return ("%s needs %s, not installed on this server: the installation goes on, but the module goes without "
            "what %s brings until %s installed" % (M.title, ", ".join(missing), "they" if len(missing) > 1 else "it",
                                                   "they are" if len(missing) > 1 else "it is"))


def dependent_modules_text(M, server):
    """The warning of a removal that other installed modules need ("" when none does)."""
    dependents = dependent_modules(M, server)
    if not dependents:
        return ""
    return ("%s need%s %s and stay%s installed: the removal goes on, they lose what it brings"
            % (", ".join(dependents), "" if len(dependents) > 1 else "s", M.title,
               "" if len(dependents) > 1 else "s"))


def addon_folder(client, name):
    """Where the game reads the addon of this name."""
    return os.path.join(client.folder, "Interface", "AddOns", name)


def module_lua_files(M, server):
    """The module's Lua files present: each at its place in the module's folder, and the ones the
    manifest lists in `files` wherever they are under the scripts folder."""
    if not M.lua:
        return []
    found = {}
    base = os.path.join(server.lua, M.lua["folder"])
    for rel in M.lua["files"]:
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            found.setdefault(os.path.normcase(p), p)
    anywhere = {os.path.basename(p).lower() for p in M.lua["anywhere"]}
    if anywhere and os.path.isdir(server.lua):
        for d, _, files in os.walk(server.lua):
            for f in files:
                if f.lower() in anywhere:
                    found.setdefault(os.path.normcase(os.path.join(d, f)), os.path.join(d, f))
    return sorted(found.values())


def files_under(folder):
    """Paths, relative to folder, of every file under it."""
    return [os.path.relpath(os.path.join(d, n), folder) for d, _, names in os.walk(folder) for n in names]


def shared_version(component, folder):
    """The version of a copy of the shared component (None: unreadable)."""
    return read_version(folder, component["version_file"])


def shared_providers(component, server):
    """Scripts, outside the component's own folder, of the modules that use it (relative paths)."""
    own = os.path.normcase(os.path.join(server.lua, component["folder"]))
    found = []
    for d, folders, names in os.walk(server.lua):
        if is_inside(d, own):
            folders[:] = []
            continue
        for n in names:
            if not n.lower().endswith((".lua", ".ext")):
                continue
            try:
                with open(os.path.join(d, n), encoding="utf-8", errors="replace") as f:
                    if component["provider_mark"] in f.read():
                        found.append(os.path.relpath(os.path.join(d, n), server.lua))
            except OSError:
                continue
    return sorted(found)


def module_backups(M, server, client):
    found = []
    for suffix in M.backups:
        for folder in (server.dbc if server else None, client.data if client else None,
                       client.locale_dir if client else None):
            if folder and os.path.isdir(folder):
                found += [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                          if n.lower().endswith(suffix.lower())]
    return found


def survey_database(db, desc):
    """([(text, count)] strong, [(text, count)] weak): what the module left in
    this database. Its tables and the updater's rows (`updates`) prove it is
    there; rows of shared tables do not. desc: the manifest's entry for this database."""
    all_tables = list(desc.get("tables", [])) + [t for t, _ in desc.get("rows", [])]
    existing = db.existing_tables(all_tables)
    queries, labels = [], []
    for t in desc.get("tables", []):
        if t.lower() in existing:
            queries.append("SELECT COUNT(*) FROM `%s`" % t)
            labels.append(("table", t))
    for t, condition in desc.get("rows", []):
        if t.lower() in existing:
            queries.append("SELECT COUNT(*) FROM `%s` WHERE %s" % (t, condition))
            labels.append(("rows", t))
    if not queries:
        return [], []
    r = db.run(" UNION ALL ".join(queries) + ";")
    strong, weak = [], []
    for (kind, t), row in zip(labels, r):
        n = int(row[0])
        if kind == "table":
            strong.append(("table %s" % t, "present, %d row(s)" % n))
        elif n:
            (strong if t.lower() == "updates" else weak).append(("rows of %s" % t, "%d" % n))
    return strong, weak


def in_owned_folder(M, name):
    n = name.replace("/", "\\").lower()
    return any(n.startswith(d) for d in M.owned_folders)


def module_files_in(M, a, receipt):
    """The module's files present in archive a: those its receipt names, and
    everything stored under a folder owned by the module."""
    found = {}
    for n in (receipt or {}).get("files", []):
        if a.contains(n):
            found.setdefault(n.lower(), n)
    if M.owned_folders:
        if a.contains("(listfile)"):
            for n in a.read("(listfile)").decode("latin-1").replace(";", "\n").splitlines():
                n = n.strip().replace("/", "\\")
                if n and in_owned_folder(M, n) and a.contains(n):
                    found.setdefault(n.lower(), n)
        for n in M.game_files:
            if in_owned_folder(M, n) and a.contains(n):
                found.setdefault(n.lower(), n)
    return sorted(found.values())


def has_server_part(M):
    """True when the package needs the server for some rows: DBC rows written on the server side
    too, or database rows."""
    return any(d.server is not None for d in M.dbc) or bool(M.databases)


def installed_dbc(M, server):
    """The DBC entries an install writes: all of them with a worldserver folder; without one (the
    folder left empty, the manifest allowing it), only those that leave the server alone."""
    return [d for d in M.dbc if server is not None or d.server is None]


def uses_databases(M, server):
    """True when the run reads and writes the databases: the manifest declares the MySQL client, and
    the worldserver folder (which names the databases) is given."""
    return "mysql" in M.fields and server is not None


def has_sql(M):
    """True when the module carries SQL files (data/sql) for a database."""
    return any(module_sql_files(M, key) for key in ("world", "characters"))


def install_plan(M, client, server):
    """[(archive, the game read as it reads it)]: the archives an install writes the module's DBC rows
    and game files into, each of them all (Client.write_targets); none for a module without game part.
    Refused before anything is written when one has no room left (hash table full, a v1 archive past
    4 GB), or when the game reads one of the module's DBC files from an archive read after it."""
    dbc = [d for d in installed_dbc(M, server) if d.client is not None]
    if not dbc and not M.game_files:
        return []
    plan = client.write_targets()
    for target, view in plan:
        sizes = {}
        for d in dbc:
            name = "DBFilesClient\\" + d.file
            w = view.winner(name)
            if not w:
                continue
            if view.rank(w) > view.rank(target):
                raise InstallerError("the game reads %s from %s, read after %s: the module's rows would stay "
                                     "hidden" % (name, w, target))
            # a copy made for every language holds at most each language's whole file
            copies = len(client.languages) if w != target and d.localized else 1
            sizes[name] = view.open(w).size(name) * max(1, copies) + sum(
                4 * d.fields + sum(len(v.encode("utf-8")) + 1 if isinstance(v, str) else
                                   len(v) if isinstance(v, bytes) else 0 for v in r) for r in d.client)
        for name, source in M.game_files.items():
            sizes[name] = os.path.getsize(source)
        sizes[receipt_name(M)] = sum(len(n) + 8 for n in sizes) + 1024
        if os.path.exists(target) and not mpq_archive.has_room(target, sizes):
            raise InstallerError("%s has no room left for this module (hash table full, or a v1 archive "
                                 "past 4 GB)" % target)
    return plan


def install_targets(M, client, server):
    """The archives an install writes into, existing or to be created."""
    return sorted(target for target, view in install_plan(M, client, server)) if client else []


def survey(M, server, client, dbs):
    """The State of the module on this server and game (None: no such folder) and databases."""
    s = State()
    # Only what an install would write can stand in its way.
    installed = installed_dbc(M, server)
    if M.server_module:
        s.sources = module_source_dirs(M, server)
    s.addons = [d for d in (addon_folder(client, n) for n in M.addons) if os.path.isdir(d)] if client else []
    if M.conf:
        s.confs = [p for p in (os.path.join(server.module_confs, M.conf["file"]),
                               os.path.join(server.module_confs, M.conf["file"] + ".dist"))
                   if os.path.isfile(p)]
    s.lua = module_lua_files(M, server)
    if M.lua:
        # The module's folder is a trace only if it holds nothing but its scripts:
        # a file of the user's stored there makes it theirs.
        d = os.path.join(server.lua, M.lua["folder"])
        names = {rel.lower() for rel in M.lua["files"]}
        if os.path.isdir(d) and all(rel.lower() in names for rel in files_under(d)):
            s.lua_dir = d

    # Server DBC: rows identical to the module's, or same identifier and other content.
    for d in M.dbc:
        if d.server is None or server is None:
            continue
        p = os.path.join(server.dbc, d.file)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                ours, others = dbc_survey(f.read(), p, d, d.server, d.server_given)
            if ours:
                s.server_dbc.append((p, d.file, ours))
            if others:
                s.server_conflicts.append((p, d.file, others))

    if client is not None:
        survey_game(M, client, s, installed)

    s.backups = module_backups(M, server, client)
    if server is not None:
        p = server_data_receipt(M, server)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                s.server_data = (p, json.load(f))
    for key, db in dbs.items():
        strong, weak = survey_database(db, M.databases.get(key, {}))
        if strong:
            s.db_strong[key] = strong
        if weak:
            s.db_weak[key] = weak
    return s


def survey_game(M, client, s, installed):
    """Adds to State s what the game holds of the module. installed: the DBC entries an install
    would write, the only ones whose other rows stand in its way."""
    # Custom game archives: receipts, rows, files. In an archive the game does not load,
    # other rows with the module's identifiers are no conflict.
    strays = client.stray_archives()
    for path in client.custom_archives() + strays:
        a = client.open(path)
        if a is None:
            continue
        receipt = read_receipt(M, a)
        if receipt is not None:
            s.receipts.append((path, receipt))
        for d in M.dbc:
            if d.client is None:
                continue
            name = "DBFilesClient\\" + d.file
            if not a.contains(name):
                continue
            raw = a.read(name)
            fields = dbc_split(raw, name)[0]
            if fields != d.fields:
                # Another client version's file (an older one in a language archive the patches
                # override, another language's): it holds none of the module's rows. Only the
                # file the game reads must have the module's layout.
                if path == client.winner(name):
                    raise dbc_layout_error(client, name, path, fields, d)
                continue
            ours, others = dbc_survey(raw, name, d, d.client, d.client_given)
            named = set((receipt or {}).get("dbc", {}).get(d.file, []))
            mine = sorted(set(ours) | (set(others) & named))
            others = [i for i in others if i not in named]
            if mine:
                s.client_dbc.append((path, d.file, mine))
            if others and path not in strays and d in installed:
                s.client_conflicts.append((path, d.file, others))
        s.files += [(path, n) for n in module_files_in(M, a, receipt)]

    # What the game reads, in each of its languages: rows of an official archive with the same
    # identifier (Blizzard's rows) and files provided in another version.
    views = client.views()
    for d in installed:
        if d.client is None:
            continue
        name = "DBFilesClient\\" + d.file
        for w in sorted({view.winner(name) for view in views} - {None}):
            if not archive_rank(os.path.basename(w))[1]:
                continue
            raw = client.open(w).read(name)
            fields = dbc_split(raw, name)[0]
            if fields != d.fields:
                # another language's file of another client version is left aside, as game_copy does
                if w != client.winner(name):
                    continue
                raise dbc_layout_error(client, name, w, fields, d)
            ours, others = dbc_survey(raw, name, d, d.client, d.client_given)
            if others:
                s.client_conflicts.append((w, d.file, others))
    mine = {(c.lower(), n.lower()) for c, n in s.files}
    targets = {os.path.normcase(t) for t, view in client.write_targets()}
    for name, source in M.game_files.items():
        for w in sorted({view.winner(name) for view in views} - {None}):
            if archive_rank(os.path.basename(w))[1] or (w.lower(), name.lower()) in mine:
                continue
            # Replaced on purpose: the other archive's version is shadowed, never overwritten --
            # unless it sits in an archive the module writes into.
            if name.lower() in M.replaced and os.path.normcase(w) not in targets:
                continue
            with open(source, "rb") as f:
                if client.open(w).read(name) != f.read():
                    s.file_conflicts.append((w, name))


def conflicting_mods(state, client):
    """The archives of other mods that hold items in conflict with the module: neither Blizzard's nor
    one installs write into (other WoW-mods modules live there)."""
    targets = {os.path.normcase(t) for t, view in client.write_targets()}
    found = {a for a, n in state.file_conflicts} | {a for a, f, ids in state.client_conflicts}
    return sorted(p for p in found if not archive_rank(os.path.basename(p))[1] and os.path.normcase(p) not in targets)


def disable_archives(paths):
    """Disables mod archives: each is renamed <name>.disabled, which the game does not load. This does
    not uninstall the mod: nothing of it is removed, and giving the archive back its name turns it on."""
    for p in paths:
        if os.path.exists(p + ".disabled"):
            raise InstallerError("%s.disabled already exists: %s was not disabled" % (p, p))
    for p in paths:
        os.rename(p, p + ".disabled")
        say("  disabled: %s (now %s.disabled)" % (p, os.path.basename(p)))


# ------------------------------------------------------------------ Wow.exe and backups

def interface_checks(M):
    """The Wow.exe checks the module's game files go through (see INTERFACE_CHECKS)."""
    names = [n.replace("/", "\\").lower() for n in M.game_files]
    return [check for check, folder, _ in INTERFACE_CHECKS if any(n.startswith(folder) for n in names)]


def wow_exe_refusals(M, client):
    """[(check, state)] of the checks Wow.exe would fail on the module's files: state "original"
    (the installer can turn the check off) or "unknown" (it cannot: no Wow.exe, or not the code of
    build 12340 it knows). Empty if Wow.exe accepts them (patched, or WarcraftXL)."""
    checks = interface_checks(M)
    if not checks:
        return []
    exe = client.wow_exe
    if exe and wow_exe.runs_warcraftxl(exe):
        return []
    states = [(check, wow_exe.check_state(exe, check) if exe else "unknown") for check in checks]
    return [(check, state) for check, state in states if state != "patched"]


def refusal_text(client, refusals):
    """Why Wow.exe would stop the game with these files, for the user."""
    what = {check: text for check, _, text in INTERFACE_CHECKS}
    files = " and ".join(what[check] for check, _ in refusals)
    unknown = [check for check, state in refusals if state != "original"]
    if not client.wow_exe:
        return "no Wow.exe in %s: the installer cannot tell whether the game accepts this module's changed %s" \
               % (client.folder, files)
    if unknown:
        return ("%s is not the 3.3.5a build 12340 the installer knows where it checks the %s: the installer "
                "can neither tell whether the game accepts this module's changed files nor patch it; installing "
                "could make the game quit at start, saying its interface files are corrupt"
                % (client.wow_exe, " and ".join(what[c] for c in unknown)))
    return ("Wow.exe checks the %s against Blizzard's signature and quits at start, saying they are corrupt, when "
            "they are changed; this module changes them. The installer can patch Wow.exe so it accepts them "
            "(2 bytes per check, a copy kept as Wow.exe%s; removing the module puts them back)"
            % (files, wow_exe.BACKUP_SUFFIX))


def interface_files_loaded(client, check):
    """The custom archives the game loads that hold files of this check's folder (or no listfile,
    so that nothing proves they hold none)."""
    folder = dict((c, f) for c, f, _ in INTERFACE_CHECKS)[check]
    found = []
    for path in client.custom_archives():
        a = client.open(path)
        if a is None:
            continue
        if not a.contains("(listfile)"):
            found.append(path)
            continue
        names = a.read("(listfile)").decode("latin-1").replace(";", "\n").splitlines()
        if any(n.strip().replace("/", "\\").lower().startswith(folder) for n in names):
            found.append(path)
    return found


def size_text(n):
    return "%.1f GB" % (n / 1e9) if n >= 1e9 else "%.0f MB" % max(1, n / 1e6)


def archives_to_back_up(paths):
    """[(path, size)] of the archives among paths that exist."""
    return [(p, os.path.getsize(p)) for p in paths if os.path.isfile(p)]


def back_up_archives(paths, written=0):
    """Copies each existing archive of paths beside itself (<name>.backup-<date>-<time>) before it
    changes, once each drive is known to hold the copies and the data about to be written
    (written: bytes, counted on every drive that holds one of the archives). Returns the copies made."""
    archives = archives_to_back_up(paths)
    if not archives:
        return []
    need = {}
    for p, size in archives:
        drive = os.path.splitdrive(os.path.abspath(p))[0].lower()
        need[drive] = need.get(drive, written) + size
    for drive, n in sorted(need.items()):
        folder = os.path.dirname(os.path.abspath(next(p for p, _ in archives
                                                      if os.path.splitdrive(os.path.abspath(p))[0].lower() == drive)))
        free = shutil.disk_usage(folder).free
        if free < n:
            raise InstallerError("not enough free space on %s to back up the archives about to change: %s needed "
                                 "(the copies and the data to write), %s free. Free some space, or go on without "
                                 "the backup" % (drive or folder, size_text(n), size_text(free)))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copies = []
    for p, size in archives:
        copy = "%s.backup-%s" % (p, stamp)
        k = 2
        while os.path.exists(copy):
            copy = "%s.backup-%s-%d" % (p, stamp, k)
            k += 1
        say("  backup: %s (%s)..." % (copy, size_text(size)))
        try:
            shutil.copyfile(p, copy)
        except BaseException:
            if os.path.exists(copy):
                os.remove(copy)
            raise
        copies.append(copy)
    return copies


# ------------------------------------------------------------------ compaction

# Bytes of kept files a compaction copies and compares per second, for the time it announces.
COMPACTION_SPEED = 40e6
# The patch compacted unless another is chosen: the one installs write into (NEW_ARCHIVE_NAME).
DEFAULT_COMPACTED = "Z"


def patch_letter(path):
    """The letter of a patch-?.MPQ or patch-xxXX-?.MPQ archive (upper case), None for another name."""
    m = re.match(r"^patch-(?:[a-z]{4}-)?([^-.])\.mpq$", os.path.basename(path), re.I)
    return m.group(1).upper() if m else None


def letter_of_choice(text):
    """The letter given to --compact: "Y", "patch-Y", "patch-Y.MPQ" or "patch-xxXX-Y.MPQ"."""
    text = (text or "").strip()
    letter = text.upper() if len(text) == 1 else patch_letter(text if text.lower().endswith(".mpq")
                                                              else text + ".MPQ")
    if not letter:
        raise InstallerError("%s is not a patch archive name (patch-Z, or its letter Z)" % text)
    return letter


def compactable_archives(client):
    """The custom patch archives the game loads, in any of its languages (patch-?.MPQ of Data,
    patch-xxXX-?.MPQ of a language folder, not Blizzard's): those a compaction may rewrite."""
    return sorted((p for p in client.custom_archives() if patch_letter(p)), key=lambda p: p.lower())


def compaction_survey(client):
    """[(archive, size now, size once compacted or None, bytes of the files it keeps, why it cannot be
    compacted or None)] of the archives a compaction may rewrite."""
    found = []
    for p in compactable_archives(client):
        try:
            found.append((p,) + mpq_archive.compacted_size(p) + (None,))
        except (mpq_archive.MpqError, OSError, struct.error) as e:
            found.append((p, os.path.getsize(p), None, 0, str(e)))
    return found


def compaction_space(plan, backup):
    """{drive: bytes}: the compacted archives, each written beside the old one before it takes its place,
    and with backup the copies of the archives as they are."""
    need = {}
    for p, size, after, kept in plan:
        drive = os.path.splitdrive(os.path.abspath(p))[0].lower()
        need[drive] = need.get(drive, 0) + after + (size if backup else 0)
    return need


def compaction_time(plan):
    """The time a compaction takes, in words: it copies and compares the files the archives keep."""
    seconds = sum(kept for p, size, after, kept in plan) / COMPACTION_SPEED
    if seconds < 60:
        return "under a minute"
    return "about %d minute%s" % (round(seconds / 60), "s" if round(seconds / 60) > 1 else "")


def freed_text(n):
    return "%s freed" % size_text(n) if n > 0 else "nothing freed"


def compaction_line(client, entry):
    """One archive and the space compacting it recovers, in words (entry: from compaction_survey)."""
    p, size, after, kept, error = entry
    if error:
        return "%s — cannot be compacted (%s)" % (os.path.relpath(p, client.folder), error)
    return "%s — %s" % (os.path.relpath(p, client.folder),
                             "%s recoverable" % size_text(size - after) if size > after else "nothing to recover")


def compact(client, paths, backup=True):
    """Rewrites each archive of paths, among those a compaction may rewrite, with only the files it holds
    (mpq_archive.compact_archive): the game reads the same files, the space left by the files removed or
    replaced is freed. With backup, each is first copied beside itself (back_up_archives). Returns (bytes
    freed, [backup copies made])."""
    allowed = {os.path.normcase(p) for p in compactable_archives(client)}
    for p in paths:
        if os.path.normcase(p) not in allowed:
            raise InstallerError("%s is not a custom patch archive the game loads: it is not compacted" % p)
    plan = [(p,) + mpq_archive.compacted_size(p) for p in paths]
    copies = []
    if backup:
        copies = back_up_archives([p for p, _, _, _ in plan], written=sum(after for _, _, after, _ in plan))
    else:
        for drive, n in sorted(compaction_space(plan, False).items()):
            folder = next(os.path.dirname(os.path.abspath(p)) for p, _, _, _ in plan
                          if os.path.splitdrive(os.path.abspath(p))[0].lower() == drive)
            free = shutil.disk_usage(folder).free
            if free < n:
                raise InstallerError("not enough free space on %s to compact the archives: %s needed, %s free"
                                     % (drive or folder, size_text(n), size_text(free)))
    freed = 0
    for p, size, after, kept in plan:
        say("  %s: rewriting %s of files..." % (p, size_text(kept)))
        client.forget(p)
        before, now = mpq_archive.compact_archive(p)
        say("  %s: %s -> %s, %s" % (p, size_text(before), size_text(now), freed_text(before - now)))
        freed += before - now
    if len(plan) > 1:
        say("  Total: %s" % freed_text(freed))
    return freed, copies


def backups_text(copies):
    """What to do with the backup copies of a compaction, in words."""
    sizes = [os.path.getsize(c) if os.path.isfile(c) else 0 for c in copies]
    return ("Start the game and check that everything works as before. Then delete by hand the backup cop%s "
            "made, which the installer does not remove (%s in all):\n%s"
            % ("y" if len(copies) == 1 else "ies", size_text(sum(sizes)),
               "\n".join("%s (%s)" % (c, size_text(n)) for c, n in zip(copies, sizes))))


# ------------------------------------------------------------------ removal

def remove_tree(d):
    def force(function, path, _):
        os.chmod(path, stat.S_IWRITE)           # read-only files of a git checkout
        function(path)
    shutil.rmtree(d, onexc=force)


def archive_is_redundant(client, path, defs):
    """True if the archive holds nothing, or only DBC files each identical to the
    one the game would read without it: it no longer changes anything (the
    archive an install created on a client that had none). Only an archive
    whose listfile names everything it stores is judged."""
    a = client.open(path)
    if a is None or not a.contains("(listfile)"):
        return False
    names = [n.strip() for n in a.read("(listfile)").decode("latin-1").replace(";", "\n").splitlines()
             if n.strip()]
    present = sorted({n for n in names if a.contains(n) and n.lower() not in ("(listfile)", "(attributes)")},
                     key=str.lower)
    special = sum(1 for n in ("(listfile)", "(attributes)") if a.contains(n))
    stored = sum(1 for b in a.block_table if b[3] & mpq_archive.FILE_EXISTS)
    if stored != len({n.lower() for n in present}) + special:
        return False
    if len(present) > 50 or \
            any(not n.replace("/", "\\").lower().startswith("dbfilesclient\\") for n in present):
        return False
    for n in present:
        if not copy_unchanged(client, n, defs.get(n.replace("/", "\\").split("\\")[-1].lower()), path, a.read(n)):
            return False
    return True


def copy_unchanged(client, name, d, path, content):
    """True if content, a DBC file of the archive path, says again what the game reads below it: the
    file of one of its languages (copies made before game_copy), or, with the module's DBC entry d,
    the copy an install makes for every language."""
    readings = {view.winner(name, below=path) for view in client.views()} - {None}
    if not readings:
        return False
    if any(client.open(w).read(name) == content for w in readings):
        return True
    targets = [path] + [t for t, v in client.write_targets()]
    return d is not None and game_copy(client.view_of(path), name, d, targets) == content


def removal_plan(M, state, leftovers=False):
    """{archive: ({DBC file: ids}, {file names})}: what a removal takes out of each game archive."""
    plan = {}
    for path, f, ids in state.client_dbc + [c for c in state.client_conflicts
                                            if leftovers and not archive_rank(os.path.basename(c[0]))[1]]:
        plan.setdefault(path, ({}, set()))[0].setdefault(f, set()).update(ids)
    for path, name in state.files:
        plan.setdefault(path, ({}, set()))[1].add(name)
    for path, r in state.receipts:
        plan.setdefault(path, ({}, set()))[1].add(receipt_name(M))
    return plan


# ------------------------------------------------------------------ the worldserver's Data folder

def server_data_folder(server):
    """Where the installer keeps its records in the Data folder."""
    return os.path.join(server.data, "wow-mods")


def server_data_receipt(M, server):
    """The record of what the module put in the Data folder: its files, and those it replaced."""
    return os.path.join(server_data_folder(server), M.name + ".json")


def server_data_originals(M, server):
    """The files the module's replaced, kept to be put back on removal."""
    return os.path.join(server_data_folder(server), M.name)


def server_data_claimed(M, server):
    """[(other module, file)]: the module's Data files another module's record already lists."""
    mine = {rel.lower() for rel in M.server_data}
    out = []
    for p in sorted(glob.glob(os.path.join(server_data_folder(server), "*.json"))):
        other = os.path.splitext(os.path.basename(p))[0]
        if other.lower() == M.name.lower():
            continue
        with open(p, encoding="utf-8") as f:
            files = json.load(f).get("files", [])
        out += [(other, rel) for rel in files if rel.lower() in mine]
    return out


def install_server_data(M, server):
    """Copies the module's Data files; a file already there is kept aside first (even identical: it
    was there before the module, removal puts it back)."""
    keep = server_data_originals(M, server)
    originals = []
    for rel, source in sorted(M.server_data.items()):
        target = os.path.join(server.data, *rel.split("/"))
        if os.path.isfile(target):
            kept = os.path.join(keep, *rel.split("/"))
            os.makedirs(os.path.dirname(kept), exist_ok=True)
            shutil.copyfile(target, kept)
            originals.append(rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(source, target)
    os.makedirs(server_data_folder(server), exist_ok=True)
    with open(server_data_receipt(M, server), "w", encoding="utf-8") as f:
        json.dump({"files": sorted(M.server_data), "originals": originals}, f, indent=1)
    say("  copied: %d file(s) into %s%s" % (len(M.server_data), server.data,
                                             ", %d original(s) kept in %s" % (len(originals), keep)
                                             if originals else ""))


def remove_server_data(M, server, receipt):
    """Takes the module's Data files away and puts back the ones they had replaced."""
    keep = server_data_originals(M, server)
    restored = deleted = 0
    for rel in receipt.get("files", []):
        target = os.path.join(server.data, *rel.split("/"))
        kept = os.path.join(keep, *rel.split("/"))
        if os.path.isfile(kept):
            shutil.copyfile(kept, target)
            restored += 1
        elif os.path.isfile(target):
            os.remove(target)
            deleted += 1
    if os.path.isdir(keep):
        remove_tree(keep)
    os.remove(server_data_receipt(M, server))
    if os.path.isdir(server_data_folder(server)) and not os.listdir(server_data_folder(server)):
        os.rmdir(server_data_folder(server))
    say("  %s: %d file(s) deleted, %d original(s) put back" % (server.data, deleted, restored))


def remove(M, server, client, dbs, state, leftovers=False, backup=True, update=False):
    """Removes everything that is the module's.

    state: the survey; leftovers: the user said the conflicting items are
    leftovers of the module, so DBC rows with its identifiers go too (never a
    game file); backup: copy the game archives about to change first; update:
    the removal that comes before installing the new version (update), which
    keeps the players' data (kept_on_update tables and rows, and the `before`
    statements are not run), Wow.exe as it is and the shared components."""
    heading("Removal of the installed version" if update else "Removal")
    by_archive = removal_plan(M, state, leftovers)
    if backup:
        # What is written back: the DBC files that lose rows, at most their present size.
        written = 0
        for path, (rows, _) in by_archive.items():
            a = client.open(path)
            written += sum(a.size("DBFilesClient\\" + f) for f in rows if a.contains("DBFilesClient\\" + f))
        back_up_archives(sorted(by_archive), written)
    # Database: characters first (what is taken back, such as talent points, is
    # taken back before the table that counts them is dropped), then world.
    for key in ("characters", "world"):
        desc = M.databases.get(key)
        if not desc or key not in dbs or (key not in state.db_strong and key not in state.db_weak):
            continue
        db = dbs[key]
        existing = db.existing_tables(list(desc.get("tables", [])) + [t for t, _ in desc.get("rows", [])] +
                                      [table for (table, _), _ in desc.get("before", [])])
        kept = {t.lower() for t in desc.get("kept_on_update", [])} if update else set()
        script = []
        # what is taken back before the tables go (talent points...) stays with the players on an update
        for (table, column), sql in [] if update else desc.get("before", []):
            if table.lower() in existing and db.has_column(table, column):
                script.append(sql)
        kept_rows = desc.get("rows_kept_on_update", []) if update else []
        for t, condition in desc.get("rows", []):
            if t.lower() in existing and (t, condition) not in kept_rows:
                script.append("DELETE FROM `%s` WHERE %s" % (t, condition))
        for t in desc.get("tables", []):
            if t.lower() in existing and t.lower() not in kept:
                script.append("DROP TABLE IF EXISTS `%s`" % t)
        if script:
            db.run(";\n".join(script) + ";\n")
            say("  database %s: %d statement(s) run" % (db.name, len(script)))

    # Server DBC: the module's rows (and, for leftovers, those with its identifiers).
    defs = {d.file: d for d in M.dbc}
    to_remove = {}
    for p, f, ids in state.server_dbc + (state.server_conflicts if leftovers else []):
        to_remove.setdefault((p, f), set()).update(ids)
    for (p, f), ids in sorted(to_remove.items()):
        with open(p, "rb") as fh:
            new, n = dbc_remove(fh.read(), p, ids, defs[f].text)
        if n:
            write_file_atomic(p, new)
            say("  %s: %d row(s) removed" % (p, n))

    # Game archives: the module's rows, files and receipt, in each archive.
    added_by = {path: {f.lower() for f in r.get("added", [])} for path, r in state.receipts}
    for path, (rows, names) in sorted(by_archive.items()):
        a = client.open(path)
        written, copies = {}, set()
        for f, ids in sorted(rows.items()):
            name = "DBFilesClient\\" + f
            new, n = dbc_remove(a.read(name), name, ids, defs[f].text)
            if not n:
                continue
            # A file the install copied whole goes, once it says again what the game reads below.
            if f.lower() in added_by.get(path, ()) and copy_unchanged(client, name, defs[f], path, new):
                copies.add(name)
                say("  %s, %s: %d row(s) removed, and the copy the install added" % (path, f, n))
            else:
                written[name] = new
                say("  %s, %s: %d row(s) removed" % (path, f, n))
        leaving = sorted(n for n in names | copies if a.contains(n))
        files = [n for n in leaving if n not in copies and n != receipt_name(M)]
        if len(files) > 5:
            say("  %s: %d game file(s) removed" % (path, len(files)))
        for name in files if len(files) <= 5 else []:
            say("  %s: %s removed" % (path, name))
        if written or leaving:
            mpq_archive.write_into_archive(path, written, remove=leaving)
            client.forget(path)
            if archive_is_redundant(client, path, {f.lower(): d for f, d in defs.items()}):
                client.forget(path)
                os.remove(path)
                say("  %s held nothing but unchanged copies: archive deleted" % path)
    if client is not None and not update:
        restore_wow_exe(M, client)

    # Server files; an update keeps the .conf, whose settings the new version takes over.
    for d in state.sources:
        remove_tree(d)
        say("  deleted: %s" % d)
    confs = [p for p in state.confs if not (update and is_settings_file(M, server, p))]
    for p in confs + state.lua + state.backups:
        os.remove(p)
    for p in confs + state.backups + (state.lua if len(state.lua) <= 5 else []):
        say("  deleted: %s" % p)
    if len(state.lua) > 5:
        say("  deleted: %d Lua script(s) of the module" % len(state.lua))
    for d in state.addons:
        remove_tree(d)
        say("  deleted: %s" % d)
    if state.server_data:
        remove_server_data(M, server, state.server_data[1])
    if M.lua:
        d = os.path.join(server.lua, M.lua["folder"])
        if os.path.isdir(d):
            for folder, _, _ in sorted(os.walk(d), key=lambda w: -len(w[0])):
                if folder != d and not os.listdir(folder):
                    os.rmdir(folder)
            left = files_under(d)
            if left:
                say("  kept: %s, which holds files foreign to the module:" % d)
                for n in sorted(left):
                    say("      %s" % n)
            else:
                remove_tree(d)
                say("  deleted: %s" % d)

    # Shared components go with the last module that uses them (never on a removal of leftovers, nor
    # on an update, whose install keeps the newer copy).
    if not leftovers and not update:
        for c in M.shared:
            remove_shared(c, server, dbs)

    # Without a worldserver folder, the server side was not looked at; game rows that the server
    # also needs show the module was installed with one.
    if server is None and any(defs[f].server is not None for _, f, _ in state.client_dbc):
        say("  The game held rows the server also needs: if the module was installed with a worldserver")
        say("  folder, run the installer again with it, to remove the server DBC rows and database rows.")


def restore_wow_exe(M, client):
    """Turns back on the Wow.exe checks the module's files needed turned off, once no archive the
    game loads changes those files any more; then deletes the installer's copy of Wow.exe if
    Wow.exe is the same again."""
    exe = client.wow_exe
    if not exe:
        return
    fresh = Client(client.folder)
    for check in interface_checks(M):
        if wow_exe.check_state(exe, check) != "patched":
            continue
        users = interface_files_loaded(fresh, check)
        if users:
            say("  Wow.exe: %s check left off, %s still changes those files" % (check, users[0]))
            continue
        wow_exe.restore(exe, [check])
        say("  Wow.exe: %s check turned back on" % check)
    copy = exe + wow_exe.BACKUP_SUFFIX
    if os.path.isfile(copy):
        with open(exe, "rb") as a, open(copy, "rb") as b:
            same = a.read() == b.read()
        if same:
            os.remove(copy)
            say("  deleted: %s (Wow.exe is the same again)" % copy)


def remove_shared(component, server, dbs):
    """Deletes a shared component, folder and database rows, if no module uses it any more."""
    d = os.path.join(server.lua, component["folder"])
    users = shared_providers(component, server)
    if users:
        if os.path.isdir(d):
            say("  kept: %s, still used by %s" % (d, ", ".join(users)))
        return
    if os.path.isdir(d):
        remove_tree(d)
        say("  deleted: %s (no module uses it any more)" % d)
    for key, desc in sorted(component["databases"].items()):
        if key not in dbs:
            continue
        existing = dbs[key].existing_tables([t for t, _ in desc["rows"]])
        script = ["DELETE FROM `%s` WHERE %s" % (t, w) for t, w in desc["rows"] if t.lower() in existing]
        if script:
            dbs[key].run(";\n".join(script) + ";\n")
            say("  database %s: rows of %s deleted (%d statement(s))" % (dbs[key].name, component["folder"],
                                                                       len(script)))


# ------------------------------------------------------------------ installation

def configured_conf(M, previous=None):
    """The .conf text: the template with the manifest's values; previous: {setting: value} of the .conf
    an update replaces, whose settings still in the template keep their value."""
    with open(os.path.join(M.root, M.conf["template"]), encoding="utf-8") as f:
        text = f.read()
    for key, value in M.conf["values"].items():
        text, n = re.subn(r"(?m)^(%s\s*=\s*).*$" % re.escape(key), lambda m: m.group(1) + value, text)
        if not n:
            raise InstallerError("%s: setting %s missing from the template" % (M.conf["template"], key))
    for key, value in (previous or {}).items():
        # quoted in the template, quoted again (read_conf took the quotes away)
        text = re.sub(r"(?m)^(%s\s*=\s*)(.*?)\s*$" % re.escape(key),
                      lambda m: m.group(1) + ('"%s"' % value if m.group(2).startswith('"') else value), text)
    return text


def previous_conf(M, server):
    """{setting: value} of the module's .conf on the server, {} without one."""
    p = os.path.join(server.module_confs, M.conf["file"]) if M.conf and server is not None else None
    return read_conf(p) if p and os.path.isfile(p) else {}


def install(M, server, client, dbs, patch_exe=False, backup=True, started=None, conf=None):
    """patch_exe: Wow.exe may be patched to accept the module's interface files; backup: copy the
    existing game archives about to change first; started: a dict, given "changed" before the
    first change to the game or the server; conf: the settings of the .conf an update replaced."""
    started = {} if started is None else started
    heading("Installation")
    installed = installed_dbc(M, server)
    # What must exist before the first write.
    for d in installed:
        if d.server is not None and not os.path.isfile(os.path.join(server.dbc, d.file)):
            raise InstallerError("%s not found (DataDir of worldserver.conf)" % os.path.join(server.dbc, d.file))
        if d.client is not None and client.winner("DBFilesClient\\" + d.file) is None:
            raise InstallerError("no game archive contains DBFilesClient\\%s" % d.file)
    if M.server_data:
        claimed = server_data_claimed(M, server)
        if claimed:
            raise InstallerError("%s already installed %s in the Data folder: remove it first"
                                 % (claimed[0][0], claimed[0][1]))
    refusals = wow_exe_refusals(M, client)
    if any(state != "original" for _, state in refusals):
        raise InstallerError(refusal_text(client, refusals))
    if refusals and not patch_exe:
        raise InstallerError(refusal_text(client, refusals) + ": allow it (--patch-wow-exe in the console)")

    # 1. Game: each DBC, as the game reads it, and each game file into every archive installs write
    #    into (one for the languages Data serves, one for each language read after Data), with a receipt.
    writes, created, receipts = {}, set(), {}
    plan = install_plan(M, client, server) if client else []
    for target, view in plan:
        for d in installed:
            if d.client is None:
                continue
            name = "DBFilesClient\\" + d.file
            w = view.winner(name)
            if w is None:
                raise InstallerError("no game archive contains %s (game read as %s reads it)" % (name, target))
            # A file read from below is copied whole, made to serve every language of the game.
            source = view.open(w).read(name) if w == target else game_copy(view, name, d, [t for t, v in plan])
            writes.setdefault(target, {})[name] = dbc_add(source, name, d, d.client)
            receipts.setdefault(target, ([], {}, set()))[1][d.file] = d.client_ids
            if w != target:
                receipts[target][2].add(d.file)
        a = view.open(target) if M.game_files and os.path.exists(target) else None
        for name, source in sorted(M.game_files.items()):
            # already in that archive (identical, or it would be a conflict): not ours
            if a is not None and a.contains(name):
                continue
            with open(source, "rb") as f:
                writes.setdefault(target, {})[name] = f.read()
            receipts.setdefault(target, ([], {}, set()))[0].append(name)
    for target in writes:
        if not os.path.exists(target):
            created.add(target)
        files, dbc, added = receipts.get(target, ([], {}, set()))
        writes[target][receipt_name(M)] = receipt_text(M, files, dbc, added)
    # Every archive accepts its write (room in its hash table...) before anything is copied or written.
    for target, files in writes.items():
        if target not in created:
            mpq_archive.write_into_archive(target, files, check_only=True)
    if backup:
        back_up_archives(sorted(t for t in writes if t not in created),
                         sum(len(c) for files in writes.values() for c in files.values()))
    started["changed"] = True
    # Wow.exe before the archives: its files never reach a game that would refuse them.
    if refusals:
        copy = wow_exe.patch(client.wow_exe, [check for check, _ in refusals])
        say("  Wow.exe: %s check(s) turned off (copy of the original: %s)"
            % (", ".join(check for check, _ in refusals), copy))
    for target, files in writes.items():
        if target in created:
            mpq_archive.create_archive(target, files, hash_entries=max(1024, 1 << (2 * len(files) + 16).bit_length()))
            say("  archive created: %s" % target)
        else:
            mpq_archive.write_into_archive(target, files)
        client.forget(target)
        # DBC files that take rows; a whole DBC file among the game files is a game file
        dbc_names = [n for n in files if n.startswith("DBFilesClient\\")
                     and any(d.file == n.split("\\")[-1] for d in M.dbc)]
        for name in dbc_names:
            f = name.split("\\")[-1]
            say("  %s, %s: %d row(s) added" % (target, f, len([d for d in M.dbc if d.file == f][0].client)))
        game_file_count = len(files) - len(dbc_names) - 1
        if game_file_count:
            say("  %s: %d game file(s) written" % (target, game_file_count))

    # 2. Server DBC.
    for d in installed:
        if d.server is None:
            continue
        p = os.path.join(server.dbc, d.file)
        with open(p, "rb") as f:
            raw = f.read()
        write_file_atomic(p, dbc_add(raw, p, d, d.server))
        say("  %s: %d row(s) added" % (p, len(d.server)))

    # 3. Lua scripts, then the shared components: each copied unless the same or a newer
    #    version is there already.
    if M.lua:
        d = os.path.join(server.lua, M.lua["folder"])
        cp = M.lua.get("config_path")
        for rel, source in sorted(M.lua["files"].items()):
            with open(source, "rb") as f:
                content = f.read()
            if cp and cp["file"].lower() == rel.lower():
                content = set_conf_path(M, server, content)
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "wb") as f:
                f.write(content)
        say("  written: %s (%d script(s))" % (d, len(M.lua["files"])))
    for c in M.shared:
        d = os.path.join(server.lua, c["folder"])
        mine = shared_version(c, c["source"])
        there = shared_version(c, d) if os.path.isdir(d) else None
        if os.path.isdir(d) and there is not None and there >= mine:
            say("  kept: %s, version %d already there" % (d, there))
            continue
        if os.path.isdir(d):
            remove_tree(d)
        shutil.copytree(c["source"], d, ignore=lambda folder, names: [n for n in names if n in NEVER_COPIED])
        say("  copied: %s (version %d%s)" % (d, mine, "" if there is None else ", replaces %d" % there))

    # 4. Configuration: the .dist as is, the .conf with the manifest's values.
    if M.conf:
        os.makedirs(server.module_confs, exist_ok=True)
        shutil.copyfile(os.path.join(M.root, M.conf["template"]),
                        os.path.join(server.module_confs, M.conf["file"] + ".dist"))
        with open(os.path.join(server.module_confs, M.conf["file"]), "w", encoding="utf-8", newline="") as f:
            f.write(configured_conf(M, conf))
        say("  written: %s (and .dist)%s" % (os.path.join(server.module_confs, M.conf["file"]),
                                             ", the previous settings kept" if conf else ""))
        dropped = sorted(k for k in conf or {} if not re.search(r"(?m)^%s\s*=" % re.escape(k), configured_conf(M)))
        if dropped:
            say("  settings this version no longer has, dropped: %s" % ", ".join(dropped))

    # 5. Files of the worldserver's Data folder (maps, vmaps, mmaps); the ones they replace are kept.
    if M.server_data:
        install_server_data(M, server)

    # 5b. Addons, copied as they are into Interface\AddOns.
    for name, source in sorted(M.addons.items()):
        destination = addon_folder(client, name)
        shutil.copytree(source, destination, ignore=lambda folder, names: [n for n in names if n in NEVER_COPIED])
        say("  copied: %s" % destination)
    if not M.server_module:
        # No module in the sources: the core updater never sees this SQL, the installer applies it
        # (only with a worldserver folder, which gives the databases).
        for key in ("characters", "world"):
            if key not in dbs:
                continue
            for p in module_sql_files(M, key):
                with open(p, encoding="utf-8") as f:
                    dbs[key].run(f.read())
                say("  applied: %s" % os.path.relpath(p, M.root))
        return

    # 6. Sources: the package, minus what the manifest excludes.
    destination = os.path.join(server.modules, M.name)
    excluded = {os.path.normcase(os.path.join(M.root, x)) for x in M.excluded}

    def ignore(folder, names):
        return [n for n in names if n in NEVER_COPIED or os.path.normcase(os.path.join(folder, n)) in excluded]

    shutil.copytree(M.root, destination, ignore=ignore)
    say("  copied: %s" % destination)

    # 7. SQL: the core updater applies it on start; when it is off for a
    #    database, the installer applies it itself.
    for key, bit in (("characters", 2), ("world", 4)):
        if server.updates_mask & bit or key not in dbs:
            continue
        for p in module_sql_files(M, key):
            with open(p, encoding="utf-8") as f:
                dbs[key].run(f.read())
            say("  applied (updater off): %s" % os.path.relpath(p, M.root))


def module_sql_files(M, key):
    """The module's SQL files for one database, in the order the core updater applies them: every
    .sql under the folders of data/sql whose name holds the database's name ("world" for db-world,
    world...), sorted by file name."""
    base = os.path.join(M.root, "data", "sql")
    found = []
    if os.path.isdir(base):
        for n in os.listdir(base):
            if key in n and os.path.isdir(os.path.join(base, n)):
                found += glob.glob(os.path.join(base, n, "**", "*.sql"), recursive=True)
    return sorted(found, key=lambda p: os.path.basename(p))


def set_conf_path(M, server, content):
    """The Lua script that reads the .conf gets its real path, relative to the worldserver folder."""
    cp = M.lua["config_path"]
    target = os.path.join(server.module_confs, M.conf["file"])
    try:
        path = os.path.relpath(target, server.bin)
    except ValueError:                          # another drive
        path = target
    path = path.replace("\\", "/")
    text = content.decode("utf-8")
    text, n = re.subn(r'(?m)^(local %s = )".*"' % re.escape(cp["variable"]),
                      lambda m: '%s"%s"' % (m.group(1), path), text, count=1)
    if not n:
        raise InstallerError('%s: line "local %s = ..." not found' % (cp["file"], cp["variable"]))
    return text.encode("utf-8")


def missing_after_install(M, server, client, dbs):
    """What is missing after an install (empty if everything is in place)."""
    missing = []
    if M.server_module:
        d = os.path.join(server.modules, M.name)
        if not all(os.path.isfile(os.path.join(d, s)) for s in M.signature):
            missing.append("sources in %s" % d)
    for rel, source in M.server_data.items():
        q = os.path.join(server.data, *rel.split("/"))
        if not os.path.isfile(q):
            missing.append(q)
            continue
        with open(source, "rb") as a, open(q, "rb") as b:
            if a.read() != b.read():
                missing.append(q)
    if M.server_data and not os.path.isfile(server_data_receipt(M, server)):
        missing.append(server_data_receipt(M, server))
    for name, source in M.addons.items():
        target = addon_folder(client, name)
        for folder, _, files in os.walk(source):
            for n in files:
                p = os.path.join(folder, n)
                q = os.path.join(target, os.path.relpath(p, source))
                if not os.path.isfile(q):
                    missing.append(q)
                    continue
                with open(p, "rb") as a, open(q, "rb") as b:
                    if a.read() != b.read():
                        missing.append(q)
    if M.conf:
        for n in (M.conf["file"], M.conf["file"] + ".dist"):
            if not os.path.isfile(os.path.join(server.module_confs, n)):
                missing.append(n)
    if M.lua:
        for rel in M.lua["files"]:
            if not os.path.isfile(os.path.join(server.lua, M.lua["folder"], rel)):
                missing.append(os.path.join(M.lua["folder"], rel))
    for c in M.shared:
        d = os.path.join(server.lua, c["folder"])
        there = shared_version(c, d) if os.path.isdir(d) else None
        if there is None or there < shared_version(c, c["source"]):
            missing.append("%s, version %d or newer" % (d, shared_version(c, c["source"])))
    # The game, read as each archive installs write into serves it (every language).
    views = [view for target, view in client.write_targets()] if client else []
    for dd in installed_dbc(M, server):
        if dd.server is not None:
            p = os.path.join(server.dbc, dd.file)
            with open(p, "rb") as f:
                if len(dbc_survey(f.read(), p, dd, dd.server)[0]) != len(dd.server):
                    missing.append("%s rows (server)" % dd.file)
        if dd.client is not None:
            name = "DBFilesClient\\" + dd.file
            for view in views:
                w = view.winner(name)
                if w is None or len(dbc_survey(view.open(w).read(name), name, dd, dd.client)[0]) != len(dd.client):
                    missing.append("%s rows in the archive the game reads (%s)" % (dd.file, w))
    for name, source in M.game_files.items():
        with open(source, "rb") as f:
            content = f.read()
        for view in views:
            w = view.winner(name)
            if w is None or view.open(w).read(name) != content:
                missing.append("%s in the archive the game reads (%s)" % (name, w))
    # The SQL a package for the game applied itself (a server module's waits for the updater).
    if not M.server_module:
        for key, desc in sorted(M.databases.items()):
            if key not in dbs or not module_sql_files(M, key):
                continue
            existing = dbs[key].existing_tables([t for t, _ in desc["rows"]])
            for t, condition in desc["rows"]:
                if t.lower() not in existing or \
                        dbs[key].run("SELECT COUNT(*) FROM `%s` WHERE %s;" % (t, condition))[0][0] == "0":
                    missing.append("rows of %s (database %s)" % (t, dbs[key].name))
    missing += ["Wow.exe: its %s check still on" % check for check, _ in wow_exe_refusals(M, client)]
    return missing


# ------------------------------------------------------------------ run

def print_state(state):
    lines = state.lines()
    if not lines:
        say("  no trace of the module")
    for area, text in lines:
        say("  %-19s %s" % (area, text))


def print_conflicts(state):
    for area, text in state.conflict_lines():
        say("  %-19s %s" % (area, text))


def build_steps(server):
    """The lines that say how to rebuild the server."""
    lines = ["The server must now be rebuilt, with the worldserver stopped:"]
    if server.build_dir:
        lines += ['    cd /d "%s"' % server.build_dir, "    cmake .",
                  "    cmake --build . --config %s --target worldserver" % (server.build_config or "RelWithDebInfo")]
    else:
        lines.append("    run the CMake configuration again, then build worldserver")
    return lines


def print_build_steps(server):
    for line in build_steps(server):
        say(line)


def open_server(M, bin_dir, sources):
    """The Server of this worldserver folder, with its sources checked when the module needs them;
    None when the manifest does not use the folder, or makes it optional and it is left empty."""
    if "worldserver" not in M.fields or (not bin_dir and M.worldserver_optional):
        return None
    if not bin_dir:
        raise InstallerError("no worldserver folder given")
    server = Server(bin_dir, sources or None)
    if M.server_module and not server.has_valid_sources():
        raise InstallerError("AzerothCore sources not found (the folder that contains modules and src)%s"
                             % (": %s" % server.sources if server.sources else ""))
    return server


def open_databases(server, mysql):
    """{"world": Database, "characters": Database}, once the world database answers."""
    if not mysql or not os.path.isfile(mysql):
        raise InstallerError("mysql client not found (mysql.exe, in the bin folder of MySQL Server)")
    dbs = {key: Database(mysql, info) for key, info in server.databases.items()}
    try:
        dbs["world"].run("SELECT 1;")
    except InstallerError as e:
        raise InstallerError("the database does not answer (is MySQL running?) - %s" % e)
    return dbs


def check_package_place(M, server):
    if M.server_module and is_inside(M.root, server.modules):
        raise InstallerError("this package is stored in the server's modules folder (%s): put it somewhere else, "
                             "for example in Downloads, and run the installer from there" % M.root)


def refuse_while_running(server, client):
    """Nothing is written while the worldserver or the game runs."""
    ws = running_worldservers(server) if server else []
    if ws:
        raise InstallerError("the worldserver is running (%s): stop it, then run the installer again"
                             % ", ".join(p or n for n, p in ws))
    game = running_game(client) if client else []
    if game:
        raise InstallerError("the game is open (%s): close it, then run the installer again"
                             % ", ".join(p for n, p in game))


def install_and_check(M, server, client, dbs, patch_exe=False, backup=True, update=False, conf=None):
    """Installs, then reads everything back from disk; says what is left to do. update: the install
    that follows the removal of the installed version (update), conf: the settings of its .conf."""
    started = {}
    warning = missing_dependencies_text(M, server)
    if warning:
        say()
        say("WARNING: %s." % warning)
    try:
        install(M, server, client, dbs, patch_exe, backup, started, conf)
    except Exception:
        say()
        if update:
            say("The update stopped after the installed version was removed; the players' data is kept.")
            say("Run the installer again and choose Update: it removes what was put in place and installs")
            say("this version.")
        elif started:
            say("The installation stopped midway. Run the installer again: it removes what was put")
            say("in place; run it once more to install.")
        else:
            say("Nothing was changed: the installation stopped before its first write.")
        raise
    missing = missing_after_install(M, server, Client(client.folder) if client else None, dbs)
    heading("Check")
    if missing:
        raise InstallerError("after installation, missing: %s" % "; ".join(missing))
    say("  everything in place, read back from disk")
    say()
    say("Update complete." if update else "Installation complete.")
    kept = kept_text(M)
    if update and kept:
        say("Players' data kept: %s." % kept)
    if not M.server_module:
        if has_server_part(M) and server is None:
            say("Without a worldserver folder, only the game part is in place: the DBC rows and database")
            say("rows that need the server were left out.")
        elif has_server_part(M):
            say("The worldserver reads its DBC files and database rows when it starts.")
        return
    print_build_steps(server)
    if not has_sql(M):
        return
    if server.updates_mask & 6 == 6:
        say("On first start, the core updater applies the module's SQL.")
    else:
        say("The installer applied the module's SQL itself: the core updater is off")
        say("(Updates.EnableDatabases in worldserver.conf).")


def is_settings_file(M, server, path):
    """True for the module's .conf (not its .dist): the settings an update keeps."""
    return bool(M.conf) and os.path.normcase(path) == os.path.normcase(os.path.join(server.module_confs,
                                                                                     M.conf["file"]))


def kept_text(M):
    """The tables and rows an update keeps, in words ("" when the manifest keeps none)."""
    return ", ".join(["%s (database %s)" % (t, key) for key, d in sorted(M.databases.items())
                      for t in d.get("kept_on_update", [])] +
                     ["the module's rows of %s (database %s)" % (t, key) for key, d in sorted(M.databases.items())
                      for t, _ in d.get("rows_kept_on_update", [])])


def dropped_text(M):
    """The module's tables an update drops (not in kept_on_update), in words ("" when it keeps them all)."""
    return ", ".join("%s (database %s)" % (t, key) for key, d in sorted(M.databases.items())
                     for t in d.get("tables", []) if t not in d.get("kept_on_update", []))


def without_kept(M, server, state):
    """The State without what an update keeps (the module's .conf, the kept_on_update tables and rows):
    what is left of the installed version once its removal for an update is done."""
    s = copy.copy(state)
    s.confs = [p for p in state.confs if not is_settings_file(M, server, p)]
    for attribute in ("db_strong", "db_weak"):
        traces_by_db = {}
        for key, traces in getattr(state, attribute).items():
            desc = M.databases.get(key, {})
            kept = {"table %s" % t for t in desc.get("kept_on_update", [])} | \
                {"rows of %s" % t for t, _ in desc.get("rows_kept_on_update", [])}
            rest = [(t, n) for t, n in traces if t not in kept]
            if rest:
                traces_by_db[key] = rest
        setattr(s, attribute, traces_by_db)
    return s


def update_refusal(M, client, state, patch_exe):
    """Why an update cannot start (None: it can), checked before anything is written: items that are not
    the module's would stop the new version's install, or Wow.exe would refuse its files."""
    if state.server_conflicts or state.client_conflicts or state.file_conflicts:
        return ("items that carry the module's identifiers without being its own (listed as conflicts) would stop "
                "the new version's install: no update while they are there")
    refusals = wow_exe_refusals(M, client) if client is not None else []
    if any(s != "original" for _, s in refusals):
        return refusal_text(client, refusals)
    if refusals and not patch_exe:
        return refusal_text(client, refusals) + ": allow it (--patch-wow-exe in the console)"
    return None


def update_and_check(M, server, client, dbs, state, patch_exe=False, backup=True):
    """Replaces the installed version, in whole or in part, with this package: removes it as a removal
    does, except the players' data (the kept_on_update tables, the `before` statements not run), then
    installs. The core updater applies the new version's SQL on the next start: the SQL of a module that
    keeps tables must run again on them without harm. state: the survey."""
    why = update_refusal(M, client, state, patch_exe)
    if why:
        raise InstallerError(why)
    # Checked before the first write too: room in the archives, DBC files read in the right order.
    targets = install_targets(M, client, server)
    heading("Update")
    kept = kept_text(M)
    say("  players' data kept: %s" % (kept or "none, the module declares no table to keep"))
    if dropped_text(M):
        say("  tables dropped, filled again from the module's SQL (rows added by hand lost): %s" % dropped_text(M))
    if M.conf:
        say("  the settings of %s kept" % M.conf["file"])
    if backup:
        plan = removal_plan(M, state)
        written = sum(os.path.getsize(p) for p in M.game_files.values())
        for path, (rows, _) in plan.items():
            a = client.open(path)
            written += sum(a.size("DBFilesClient\\" + f) for f in rows if a.contains("DBFilesClient\\" + f))
        back_up_archives(sorted(set(plan) | set(targets)), written)
    conf = previous_conf(M, server)
    remove(M, server, client, dbs, state, backup=False, update=True)
    client = Client(client.folder) if client else None
    left = without_kept(M, server, survey(M, server, client, dbs))
    if left.strong() or left.weak():
        heading("Left after the removal")
        print_state(left)
        print_conflicts(left)
        raise InstallerError("the installed version is not completely removed (above): the new one was not installed. "
                             "Run the installer again and choose Update")
    install_and_check(M, server, client, dbs, patch_exe=patch_exe, backup=False, update=True, conf=conf)


def remove_and_check(M, server, client, dbs, state, leftovers=False, backup=True):
    warning = "" if leftovers else dependent_modules_text(M, server)
    if warning:
        say()
        say("WARNING: %s." % warning)
    remove(M, server, client, dbs, state, leftovers, backup)
    check_removal(M, server, client, dbs)


def check_removal(M, server, client, dbs):
    left = survey(M, server, Client(client.folder) if client else None, dbs)
    heading("Check")
    if left.strong():
        print_state(left)
        raise InstallerError("traces remain (above)")
    say("  no trace of the module left, state read again from disk and database")
    if left.has_conflicts():
        say("  stay in place, because they are not its own:")
        print_conflicts(left)
    say()
    say("Uninstallation complete.")
    if M.server_module:
        print_build_steps(server)
        say("Then the module is gone from the worldserver.")


def run_console(M, args, settings):
    """The run without window (--status, --yes): paths from the options or remembered; returns the
    exit code."""
    say("%s - WoW-mods installer" % M.title)
    say("=" * 60)
    # --server "" leaves the worldserver folder empty, for a manifest that makes it optional
    server = open_server(M, args.server if args.server is not None else settings.get("server"),
                         args.sources or settings.get("sources"))
    client = Client(args.client or settings.get("client") or "") if "game" in M.fields else None
    dbs = {}
    if uses_databases(M, server):
        dbs = open_databases(server, find_mysql(server, settings, args.mysql))
    heading("Folders")
    for label, value in folder_lines(M, server, client, dbs):
        say("  %-16s %s" % (label, value))
    remember(settings, M, server, client, dbs)
    check_package_place(M, server)

    heading("Current state")
    if client is not None:
        say("  (reading the game archives, a few seconds)")
    state = survey(M, server, client, dbs)
    print_state(state)
    if state.has_conflicts():
        say("  Carry its identifiers without proof that they are its own:")
        print_conflicts(state)
    for path, message in (client.unreadable if client else []):
        say("  archive ignored, unreadable: %s (%s)" % (path, message))
    mods = conflicting_mods(state, client) if client is not None and not state.strong() else []
    if mods:
        say("  Conflict with another mod, whose archive(s) hold them: %s" % ", ".join(mods))
        say("  Disabling them (renamed .disabled) lets this module install; it does not uninstall that mod.")
    if not state.strong() and not state.weak():
        refusals = wow_exe_refusals(M, client)
        if refusals:
            say("  Wow.exe: %s" % refusal_text(client, refusals))
    if state.strong():
        kept = kept_text(M)
        say("  --update replaces it with this package; players' data kept: %s" % (kept or "none declared"))
        if dropped_text(M):
            say("  an update drops these tables, filled again from the module's SQL: %s" % dropped_text(M))
    if args.status:
        return 0
    refuse_while_running(server, client)
    say()
    backup = not args.no_backup
    if mods and args.disable_mods:
        heading("Disabling the other mod's archives")
        disable_archives(mods)
        client = Client(client.folder)
        state = survey(M, server, client, dbs)
        heading("State after that")
        print_state(state)
        if state.has_conflicts():
            print_conflicts(state)
        say()
    if state.strong() and args.update:
        say("The module is present, in whole or in part: this run UPDATES it to this package, players' data kept.")
        update_and_check(M, server, client, dbs, state, patch_exe=args.patch_wow_exe, backup=backup)
        return 0
    if state.strong():
        say("The module is present, in whole or in part: this run REMOVES everything that is left of it.")
        remove_and_check(M, server, client, dbs, state, backup=backup)
        return 0
    if state.weak():
        say("CONFLICT: these items carry the module's identifiers, but nothing proves they are its own.")
        if mods and not args.disable_mods:
            say("--disable-mods disables the other mod's archives (it does not uninstall that mod).")
        changed = "only the other mod's archives were disabled" if mods and args.disable_mods else \
            "nothing was changed"
        if not state.removable():
            say("Stopped: %s. None of them is a leftover the installer can remove (a game file" % changed)
            say("of another archive, rows of an official archive): no install while they are there.")
            return 1
        if not args.leftovers:
            say("Stopped: %s (--leftovers removes them, if they are leftovers of the module)." % changed)
            return 1
        remove_and_check(M, server, client, dbs, state, leftovers=True, backup=backup)
        return 0
    say("No trace of the module: this run INSTALLS it.")
    install_and_check(M, server, client, dbs, patch_exe=args.patch_wow_exe, backup=backup)
    return 0


def run_compact(args, settings):
    """--compact without window: the custom patch archives of the game, what compacting each one frees, and
    those of the patch chosen (--compact patch-Y, Z by default); with --yes, compacts these. The game
    folder comes from --client or is remembered. Returns the exit code."""
    letter = letter_of_choice(args.compact)
    say("Compaction of the game archives - WoW-mods installer")
    say("=" * 60)
    client = Client(args.client or settings.get("client") or "")
    heading("Folders")
    say("  %-16s %s" % ("game", client.folder))
    settings["client"] = client.folder
    save_settings(settings)
    heading("Custom patch archives of the game")
    survey = compaction_survey(client)
    if not survey:
        say("  None: the game loads no custom patch archive, nothing to compact.")
        return 0
    for entry in survey:
        say("  %s %s" % ("*" if patch_letter(entry[0]) == letter else " ", compaction_line(client, entry)))
    chosen = [e for e in survey if patch_letter(e[0]) == letter]
    say()
    if not chosen:
        say("No archive of patch %s (patch-%s.MPQ, patch-xxXX-%s.MPQ): nothing to compact." % ((letter,) * 3))
        return 1
    refused = [e for e in chosen if e[4]]
    if refused:
        say("Stopped: %s cannot be compacted." % ", ".join(os.path.relpath(e[0], client.folder) for e in refused))
        return 1
    plan = [e[:4] for e in chosen]
    backup = not args.no_backup
    say("* patch %s: %s; disk space needed: %s%s; time: %s"
        % (letter, freed_text(sum(size - after for p, size, after, kept in plan)),
           size_text(sum(compaction_space(plan, backup).values())), " (backups included)" if backup else "",
           compaction_time(plan)))
    if not args.yes:
        say("--yes compacts them (--no-backup: without copying them first; --compact patch-Y: another patch).")
        return 0
    refuse_while_running(None, client)                # the game reads them; the server does not
    heading("Compacting")
    freed, copies = compact(client, [p for p, _, _, _ in plan], backup=backup)
    say()
    say("Done: the game reads the same files as before.")
    if copies:
        for line in backups_text(copies).splitlines():
            say(line)
    return 0


def folder_lines(M, server, client, dbs):
    """[(label, path)] of the places the installer works in."""
    lines = []
    if server is not None:
        lines.append(("worldserver", server.bin))
        if M.server_module:
            lines += [("sources", server.sources), ("configuration", server.module_confs),
                      ("Lua scripts", server.lua)]
        lines.append(("server DBC", server.dbc))
    elif "worldserver" in M.fields:
        lines.append(("worldserver", "none: the game part only"))
    if client is not None:
        lines.append(("game", client.folder))
    if dbs:
        lines.append(("databases", "%s, %s" % (dbs["world"].name, dbs["characters"].name)))
    return lines


def remember(settings, M, server, client, dbs):
    """Keeps the folders that worked, for the next run (a folder the module did without stays as it was)."""
    settings["module"] = M.root
    if server is not None:
        settings["server"] = server.bin
    if client is not None:
        settings["client"] = client.folder
    if M.server_module:
        settings["sources"] = server.sources
    if dbs:
        settings["mysql"] = dbs["world"].mysql
    save_settings(settings)


def module_folder(path):
    """The module folder of this path (installer.json itself accepted), or None."""
    if not path:
        return None
    folder = os.path.normpath(os.path.abspath(path))
    if os.path.isfile(folder) and os.path.basename(folder).lower() == MANIFEST_NAME:
        folder = os.path.dirname(folder)
    return folder if os.path.isfile(os.path.join(folder, MANIFEST_NAME)) else None


def describe_failure(e):
    """(message, unexpected) for an exception that stopped a run."""
    if isinstance(e, InstallerError):
        return str(e), False
    if isinstance(e, mpq_archive.MpqError):
        return "MPQ archive: %s" % e, False
    if isinstance(e, PermissionError):
        return "access denied to %s (file open in another program?)" % e.filename, False
    return "unexpected error: %s" % e, True


def run_guarded(action):
    """Runs action(); returns its exit code, or says why it failed."""
    try:
        return action()
    except KeyboardInterrupt:
        say()
        say("Interrupted.")
        return 1
    except Exception as e:
        message, unexpected = describe_failure(e)
        say()
        say("FAILED: %s" % message)
        if unexpected:
            say(traceback.format_exc())
            return 2
        return 1


def main(load_manifest):
    """Entry point: the window, or the console with --status or --yes.
    load_manifest(folder) returns the module description."""
    p = argparse.ArgumentParser(description="Installs a module, or removes it if it is present. Without "
                                            "--status or --yes, the installer opens its window.")
    p.add_argument("module", nargs="?", help="module folder (the one that contains %s)" % MANIFEST_NAME)
    p.add_argument("--server", help="worldserver folder (the one that contains worldserver.exe); \"\" leaves it "
                                    "empty when the module's manifest makes it optional")
    p.add_argument("--sources", help="AzerothCore sources folder")
    p.add_argument("--client", help="game folder")
    p.add_argument("--mysql", help="path of mysql.exe")
    p.add_argument("--status", action="store_true", help="show the current state, change nothing")
    p.add_argument("--yes", action="store_true", help="install or remove without window "
                                                      "(paths from the options or remembered)")
    p.add_argument("--update", action="store_true",
                   help="with --yes: the module is present, replace it with this package and keep the players' "
                        "data (the tables its manifest keeps); without it, a present module is removed")
    p.add_argument("--leftovers", action="store_true",
                   help="with --yes: the conflicting items are leftovers of the module, remove them")
    p.add_argument("--patch-wow-exe", action="store_true",
                   help="with --yes: patch Wow.exe when it would refuse the module's interface files")
    p.add_argument("--no-backup", action="store_true",
                   help="with --yes: do not copy the game archives about to change first")
    p.add_argument("--disable-mods", action="store_true",
                   help="with --yes: disable the archives of another mod in conflict with the module (renamed "
                        ".disabled; this does not uninstall that mod), then go on")
    p.add_argument("--compact", nargs="?", const=DEFAULT_COMPACTED, metavar="PATCH",
                   help="no module: show what compacting the game's custom patch archives would free, those of "
                        "PATCH (patch-Y, or its letter; Z by default) marked; with --yes, compact these (rewritten "
                        "without the space of removed files, same content)")
    args = p.parse_args()
    settings = load_settings()
    if args.compact is not None:
        sys.exit(run_guarded(lambda: run_compact(args, settings)))
    if not (args.status or args.yes):
        import window
        window.run(module_folder(args.module) or args.module, settings, load_manifest)
        return

    def action():
        folder = module_folder(args.module)
        if folder is None:
            raise InstallerError("the module folder must be given (the one that contains %s)" % MANIFEST_NAME)
        return run_console(load_manifest(folder), args, settings)

    sys.exit(run_guarded(action))
