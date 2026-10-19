# -*- coding: utf-8 -*-
"""The loot editor's model (MIT licence): DynamicLoot's families, a module's
loot as the editor holds it, and the <Module>.Loot.* lines it is written as.

No window here: the editor (loot_editor_window.py) shows and changes a
Project; this module reads the families, writes the lines and reads them
back. The grammar is DynamicLoot's (its docs/SPECIFICATION.md, step 2):

    <Module>.Loot.<list>[<i>] = [item, item, ...]
    <Module>.Loot.Rate = <percent>
    <Module>.Loot.NoPity = [item, ...]
    <Module>.Loot.<Family> = <draws>

<draws>: independent draws separated by ";", each a chain of fallbacks
separated by ",", each fallback <list[i] or item>:<quantity>:<chance>."""
import os
import re

# The families of DynamicLoot, in the order of its table, for a server whose
# sources cannot be read (families_from_sources is preferred).
BUILT_IN_FAMILIES = [
    "VanillaWorld", "VanillaWorldBoss", "VanillaRare", "VanillaDungeonTrash", "VanillaDungeonBoss",
    "VanillaRaidTrash", "VanillaRaidBoss",
    "BcWorld", "BcWorldBoss", "BcRare", "BcDungeonTrash", "BcDungeonBoss", "BcRaidTrash", "BcRaidBoss",
    "WotlkWorld", "WotlkWorldBoss", "WotlkRare", "WotlkDungeonTrash", "WotlkDungeonBoss", "WotlkHeroicTrash",
    "WotlkHeroicBoss", "WotlkRaidTrash", "WotlkRaidBoss", "IcecrownRaidBoss",
    "MobLvl_1_9", "MobLvl_10_19", "MobLvl_20_29", "MobLvl_30_39", "MobLvl_40_49", "MobLvl_50_59",
    "MobLvl_60_69", "MobLvl_70_79", "MobLvl_80_89",
    "VanillaOres", "BcOres", "WotlkOresLow", "WotlkOresMedium", "WotlkOresHigh",
    "VanillaHerbs", "BcHerbs", "WotlkHerbsLow", "WotlkHerbsMedium", "WotlkHerbsHigh",
    "Skinning80", "Skinning71", "Skinning61", "Skinning1",
    "VanillaCorpseHerbs", "BcCorpseHerbs", "WotlkCorpseHerbs", "VanillaCorpseOres", "BcCorpseOres",
    "WotlkCorpseOres", "VanillaCorpseSalvage", "BcCorpseSalvage", "WotlkCorpseSalvage",
    "WorldChest", "DungeonChest", "HeroicDungeonChest",
    "VanillaContainer", "BcContainer", "WotlkContainer",
    "IcecrownRaidTrash",
]

_EXPANSIONS = ("Vanilla", "Bc", "Wotlk")

# How the editor shows the families: tabs, each made of groups, each a list of families in the order the
# lines are written. A family the server knows and this layout does not goes to the "other" tab.
LAYOUT = [
    ("creatures", [
        ("world", [e + k for e in _EXPANSIONS for k in ("World", "WorldBoss", "Rare")]),
        ("trash", ["VanillaDungeonTrash", "BcDungeonTrash", "WotlkDungeonTrash", "WotlkHeroicTrash",
                   "VanillaRaidTrash", "BcRaidTrash", "WotlkRaidTrash", "IcecrownRaidTrash"]),
        ("dungeon_boss", ["VanillaDungeonBoss", "BcDungeonBoss", "WotlkDungeonBoss", "WotlkHeroicBoss"]),
        ("raid_boss", ["VanillaRaidBoss", "BcRaidBoss", "WotlkRaidBoss", "IcecrownRaidBoss"]),
    ]),
    ("levels", [
        ("levels", ["MobLvl_1_9", "MobLvl_10_19", "MobLvl_20_29", "MobLvl_30_39", "MobLvl_40_49",
                    "MobLvl_50_59", "MobLvl_60_69", "MobLvl_70_79", "MobLvl_80_89"]),
    ]),
    ("gathering", [
        ("ores", ["VanillaOres", "BcOres", "WotlkOresLow", "WotlkOresMedium", "WotlkOresHigh"]),
        ("herbs", ["VanillaHerbs", "BcHerbs", "WotlkHerbsLow", "WotlkHerbsMedium", "WotlkHerbsHigh"]),
    ]),
    ("skinning", [
        ("skinning", ["Skinning1", "Skinning61", "Skinning71", "Skinning80"]),
        ("corpses", [e + "Corpse" + k for k in ("Herbs", "Ores", "Salvage") for e in _EXPANSIONS]),
    ]),
    ("chests", [
        ("chests", ["WorldChest", "DungeonChest", "HeroicDungeonChest"]),
        ("containers", ["VanillaContainer", "BcContainer", "WotlkContainer"]),
    ]),
]

MAX_QUANTITY = 255
LIST_NAME = re.compile(r"^[A-Za-z0-9_]+\[[0-9]+\]$")
PREFIX = re.compile(r"^[A-Za-z0-9_]+$")
LOOT_MARK = ".Loot."


def families_from_sources(sources):
    """The families of the DynamicLoot installed in these AzerothCore sources, in the order of its table;
    None when its DynamicLootFamilies.cpp cannot be read."""
    if not sources:
        return None
    path = os.path.join(sources, "modules", "dynamicloot", "src", "DynamicLootFamilies.cpp")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    found = re.findall(r'\{\s*"(\w+)"\s*,\s*S::\w+\s*\}', text)
    return found or None


def layout_for(families):
    """[(tab, [(group, [family])])] of the families the server knows, the layout's order first, those it
    does not place in an "other" tab."""
    known = set(families)
    placed = set()
    tabs = []
    for tab, groups in LAYOUT:
        kept = []
        for group, names in groups:
            names = [n for n in names if n in known]
            placed.update(names)
            if names:
                kept.append((group, names))
        if kept:
            tabs.append((tab, kept))
    rest = [f for f in families if f not in placed]
    if rest:
        tabs.append(("other", [("other", rest)]))
    return tabs


class Draw(object):
    """One fallback: an item entry (int) or a list name (str), how many, at what chance in percent."""

    def __init__(self, target, quantity=1, chance=100.0):
        self.target, self.quantity, self.chance = target, quantity, chance

    @property
    def is_list(self):
        return isinstance(self.target, str)

    def text(self):
        return "%s:%d:%s" % (self.target, self.quantity, number(self.chance))

    def copy(self):
        return Draw(self.target, self.quantity, self.chance)


class Project(object):
    """A module's loot: its prefix, rate, NoPity items, lists ({name: [items]}, in their order) and, by
    family, its draws: a list of chains (independent draws), each a list of Draw (fallbacks)."""

    def __init__(self, prefix=""):
        self.prefix = prefix
        self.rate = 100.0
        self.no_pity = []
        self.lists = {}
        self.lines = {}

    def chains(self, family):
        return self.lines.setdefault(family, [])

    def items(self):
        """Every item entry the project names, once."""
        seen = []
        for entry in self.no_pity + [i for items in self.lists.values() for i in items] + \
                [d.target for chains in self.lines.values() for c in chains for d in c if not d.is_list]:
            if entry not in seen:
                seen.append(entry)
        return seen

    def new_list_name(self, base="items"):
        i = 0
        while "%s[%d]" % (base, i) in self.lists:
            i += 1
        return "%s[%d]" % (base, i)

    def rename_list(self, old, new):
        self.lists = {(new if k == old else k): v for k, v in self.lists.items()}
        for chains in self.lines.values():
            for chain in chains:
                for d in chain:
                    if d.target == old:
                        d.target = new

    def remove_list(self, name):
        """Removes the list and every draw that names it (a chain left empty goes too)."""
        self.lists.pop(name, None)
        for family in list(self.lines):
            chains = [[d for d in c if d.target != name] for c in self.lines[family]]
            self.lines[family] = [c for c in chains if c]

    def prune(self):
        """Drops empty chains and families without draws."""
        for family in list(self.lines):
            self.lines[family] = [c for c in self.lines[family] if c]
            if not self.lines[family]:
                del self.lines[family]


def number(value):
    """A chance or a rate as the .conf writes it: 5, 5.5, 12.34."""
    text = ("%.4f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"


def generate(project, families):
    """The <Prefix>.Loot.* lines of the project, in the editor's order, grouped with comments."""
    p = project.prefix + LOOT_MARK
    out = []
    if project.lists:
        out.append("# Lists")
        out += ["%s%s = [%s]" % (p, name, ", ".join(str(i) for i in items)) for name, items in project.lists.items()]
        out.append("")
    out.append("%sRate = %s" % (p, number(project.rate)))
    if project.no_pity:
        out.append("%sNoPity = [%s]" % (p, ", ".join(str(i) for i in project.no_pity)))
    for tab, groups in layout_for(families):
        for group, names in groups:
            written = [n for n in names if project.lines.get(n)]
            if not written:
                continue
            out.append("")
            out.append("# " + GROUP_COMMENTS.get(group, group))
            for n in written:
                out.append("%s%s = %s" % (p, n, " ; ".join(" , ".join(d.text() for d in chain)
                                                          for chain in project.lines[n])))
    # draws on families this server does not know (an imported file): kept, at the end
    unknown = [n for n in project.lines if n not in families and project.lines[n]]
    if unknown:
        out.append("")
        out.append("# Families this version of DynamicLoot does not know (ignored by the server)")
        for n in unknown:
            out.append("%s%s = %s" % (p, n, " ; ".join(" , ".join(d.text() for d in chain)
                                                      for chain in project.lines[n])))
    return "\n".join(out) + "\n"


GROUP_COMMENTS = {
    "world": "Creatures of the open world, world bosses and rares",
    "trash": "The other monsters of dungeons and raids",
    "dungeon_boss": "Dungeon bosses, normal and heroic",
    "raid_boss": "Raid bosses",
    "levels": "By the creature's level, on top of the lines above",
    "ores": "Veins",
    "herbs": "Herbs",
    "skinning": "Skinning, by the creature's level",
    "corpses": "Corpses gathered by herbalism, mining or engineering",
    "chests": "Chests",
    "containers": "Inventory containers",
    "other": "Other families",
}


# ---------------------------------------------------------------- reading lines back

def _parse_items(value):
    text = value.strip()
    if len(text) < 2 or text[0] != "[" or text[-1] != "]":
        raise ValueError("not written [item, item, ...]")
    inside = text[1:-1].strip()
    if not inside:
        raise ValueError("an empty list")
    items = []
    for piece in inside.split(","):
        piece = piece.strip()
        if not piece.isdigit() or int(piece) == 0:
            raise ValueError('"%s" is not an item number' % piece)
        items.append(int(piece))
    return items


def _parse_draws(value, lists):
    chains = []
    for chain_text in value.split(";"):
        if not chain_text.strip():
            continue
        chain = []
        for draw_text in chain_text.split(","):
            parts = [s.strip() for s in draw_text.split(":")]
            if len(parts) != 3:
                raise ValueError('"%s" is not <list or item>:<quantity>:<chance>' % draw_text.strip())
            target, quantity, chance = parts
            if LIST_NAME.match(target):
                if target not in lists:
                    raise ValueError("no list %s" % target)
            elif target.isdigit() and int(target):
                target = int(target)
            else:
                raise ValueError('"%s" is neither a list nor an item number' % target)
            if not quantity.isdigit() or not 1 <= int(quantity) <= MAX_QUANTITY:
                raise ValueError('"%s" is not a quantity from 1 to %d' % (quantity, MAX_QUANTITY))
            try:
                c = float(chance)
            except ValueError:
                c = -1.0
            if not 0.0 <= c <= 100.0:
                raise ValueError('"%s" is not a chance from 0 to 100' % chance)
            chain.append(Draw(target, int(quantity), c))
        chains.append(chain)
    return chains


def read_settings(text):
    """{key: value} of a .conf text, as AzerothCore reads it: comments skipped, quotes removed, a key
    written twice keeps its first value."""
    values = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*([^\s=]+)\s*=\s*(.*?)\s*$", line)
        if m and m.group(1) not in values:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            values[m.group(1)] = v
    return values


def prefixes_in(text):
    """The prefixes of the <Prefix>.Loot.* settings of a .conf text, in their order."""
    found = []
    for key in read_settings(text):
        i = key.find(LOOT_MARK)
        if i > 0 and PREFIX.match(key[:i]) and key[:i] not in found:
            found.append(key[:i])
    return found


def parse(text, prefix):
    """(Project, [problems]) from the <prefix>.Loot.* settings of a .conf text. A setting that cannot be
    read is a problem and is left out, as DynamicLoot does."""
    project = Project(prefix)
    problems = []
    mark = prefix + LOOT_MARK
    settings = [(k[len(mark):], v) for k, v in read_settings(text).items() if k.startswith(mark) and len(k) > len(mark)]
    for name, value in settings:
        if LIST_NAME.match(name):
            try:
                project.lists[name] = _parse_items(value)
            except ValueError as e:
                problems.append("%s%s: %s" % (mark, name, e))
    for name, value in settings:
        if LIST_NAME.match(name):
            continue
        try:
            if name == "Rate":
                rate = float(value)
                if rate < 0:
                    raise ValueError
                project.rate = rate
            elif name == "NoPity":
                project.no_pity = _parse_items(value)
            else:
                project.lines[name] = _parse_draws(value, project.lists)
        except ValueError as e:
            problems.append("%s%s: %s" % (mark, name, e or '"%s" is not a percentage of 0 or more' % value))
    return project, problems


# ---------------------------------------------------------------- checking

def check(project, families, existing=None):
    """([errors], [warnings]) before generating. existing: the set of item entries the world database
    holds, when known (DynamicLoot refuses a whole list, or a whole line, for one missing item)."""
    errors, warnings = [], []
    if not PREFIX.match(project.prefix or ""):
        errors.append(("prefix", project.prefix))
    for name, items in project.lists.items():
        if not LIST_NAME.match(name):
            errors.append(("list_name", name))
        if not items:
            errors.append(("list_empty", name))
    used_lists = {d.target for chains in project.lines.values() for c in chains for d in c if d.is_list}
    for name in project.lists:
        if name not in used_lists:
            warnings.append(("list_unused", name))
    for family, chains in project.lines.items():
        if family not in families and chains:
            warnings.append(("family_unknown", family))
        for chain in chains:
            for d in chain:
                if d.is_list and d.target not in project.lists:
                    errors.append(("list_missing", "%s: %s" % (family, d.target)))
                if not 1 <= d.quantity <= MAX_QUANTITY:
                    errors.append(("quantity", "%s: %s" % (family, d.text())))
                if not 0 <= d.chance <= 100:
                    errors.append(("chance", "%s: %s" % (family, d.text())))
                if d.chance == 0:
                    warnings.append(("chance_zero", "%s: %s" % (family, d.text())))
    no_pity = set(project.no_pity)
    for name, items in project.lists.items():
        excluded = [i for i in items if i in no_pity]
        if excluded and len(excluded) < len(items):
            warnings.append(("mixed_no_pity", name))
    if existing is not None:
        for entry in project.items():
            if entry not in existing:
                errors.append(("item_missing", str(entry)))
    return errors, warnings
