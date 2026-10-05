# WoW-mods installer

One program that installs, and removes, any AzerothCore module that carries a
manifest: an `installer.json` file at the root of the module folder, which
declares what the module needs. The format is described in
[MANIFEST.md](MANIFEST.md). It serves the modules listed in
[WoW-mods](https://github.com/Adrestie/WoW-mods) that carry one.

## Usage

Download `installer.exe` once, from the
[releases](https://github.com/Adrestie/WoW-mods-installer/releases), then run it, or drop a module folder (the one
that contains `installer.json`) on it. Its window holds the module folder,
then the folders that module's manifest asks for: the required ones in one
panel, the optional ones in another, under what they add to the module and
each with the reason it is optional. A folder the module does not use is not
shown. The AzerothCore sources and
`mysql.exe` are found by themselves when left empty. Under each field, a line
says whether the path given is the expected one. It remembers them in
`%APPDATA%\WoW-mods\installer-settings.json`. **Check** reads the server, the
game archives and the database, then shows the module's state and the traces
found, and the one button that fits: **Install**, **Remove** or **Remove
leftovers**, each confirmed before anything is written. What the installer
does goes to the log at the bottom of the window. Every text of the window can
be copied: selected with the mouse or Ctrl+A, then Ctrl+C, or from its
right-click menu; Ctrl+C on a dialog copies its whole message.

- **No trace of the module**: it INSTALLS. It copies the module into the
  server sources (`modules/`), puts its configuration and Lua scripts in
  place, adds its rows to the server DBC files, writes its rows and game files
  directly into the game's MPQ archives, and copies its addons into the game's
  `Interface\AddOns`. The server must then be recompiled; on first start, the
  core updater applies the module's SQL. A package without server module
  (`"server_module": false`) only writes the game files, the addons and the DBC
  rows, and applies its own SQL: no sources, no configuration, no scripts, no
  rebuild. When its manifest makes the worldserver folder optional, a player
  leaves that field empty: only the game part goes in, without the DBC rows
  and SQL that need the server.
- **Any trace of the module**: it REMOVES everything that is left, wherever it
  is, database included (read and cleaned through `mysql.exe`, with the
  credentials of `worldserver.conf`). An uninstall started by hand is finished
  by running the program again.
- **Only items that carry the module's identifiers without proof that they
  belong to it** (a DBC row with the same identifier and other content, a game
  file shipped in another version by another archive, database rows without
  the rest of the module): it is a CONFLICT. It does not install. It removes
  them only if the user confirms they are leftovers of the module, and never a
  game file. When they come from another mod's archives (neither Blizzard's
  nor one the installer writes into), it says so and offers to disable that
  mod: its archives are renamed `<name>.disabled`, which the game does not
  load; this does not uninstall the mod, and giving them back their names
  turns it on again.

It refuses to write anything while the worldserver or the game is running.

Only the archives the game loads count: in `Data`, the base archives,
`patch.MPQ` and `patch-?.MPQ` (one character); in the language folder, its base
archives, `patch-xxXX.MPQ` and `patch-xxXX-?.MPQ`. Another `.mpq` file is never
written into; what a module left in one is removed with the rest.

A module works whatever languages the client holds, and the player reads its
texts in the language of the game. Everything it puts into the game goes into
`Data\patch-Z.MPQ`, created if there is none, which the game reads last in
deDE, enGB, enUS, esES, esMX, frFR and koKR. Wow.exe reads a ruRU, zhCN or zhTW
language folder after `Data`: each one present also gets the same content in
its own `patch-xxXX-Z.MPQ`. No other archive is written into. A DBC file the
game reads from another archive is copied whole, with the texts of every
language folder of `Data`. When one of these archives has no room left (hash
table full, or a v1 archive past 4 GB), or when the game reads one of the
module's DBC files from an archive read after it, it refuses, before writing
anything.

Before a game archive changes, it offers to copy it beside itself
(`<name>.backup-<date>-<time>`), ticked by default, once it has checked that the
drive holds the copies and the data about to be written.

A module that changes `Interface\GlueXML` or `Interface\FrameXML` needs a
`Wow.exe` that does not check these files against Blizzard's signature:
otherwise the game quits at start, saying its interface files are corrupt.
When `Wow.exe` (3.3.5a, build 12340) still checks them and WarcraftXL does not
load, the installer offers to patch it: 2 bytes per check, after a copy,
`Wow.exe.wow-mods.bak`. Removal turns the check back on once no archive the
game loads changes these files. It refuses a `Wow.exe` it does not know.

With `--status` or `--yes`, it runs without window, in the console of the
program that started it, and asks nothing: the paths come from the options or
from the remembered ones. From a batch file, `start /wait installer.exe ...`
waits for it and gets its exit code (0 done, 1 stopped or failed, 2
unexpected error).

| Option | Effect |
|---|---|
| `module` | module folder (the one that contains `installer.json`) |
| `--server DIR` | worldserver folder (the one that contains `worldserver.exe`); `--server ""` leaves it empty, when the module's manifest makes it optional; ignored when the module does not use it |
| `--sources DIR` | AzerothCore sources, when `CMakeCache.txt` does not lead to them |
| `--client DIR` | game folder (the one that contains `Wow.exe` and `Data`) |
| `--mysql FILE` | path of `mysql.exe` |
| `--status` | show the current state, change nothing |
| `--yes` | install or remove without window |
| `--leftovers` | with `--yes`: the conflicting items are leftovers of the module, remove them |
| `--patch-wow-exe` | with `--yes`: patch `Wow.exe` when it would refuse the module's interface files |
| `--no-backup` | with `--yes`: do not copy the game archives about to change |
| `--disable-mods` | with `--yes`: disable the archives of another mod in conflict with the module (renamed `.disabled`; this does not uninstall that mod), then go on |

It also runs without being compiled: `python installer\installer.py [module]`
(Python 3.12 or later, standard library only, tkinter included).

## Building installer.exe

`build.cmd` builds `installer.exe` in this folder with PyInstaller
(`python -m pip install pyinstaller`). A change to a module's manifest needs
no rebuild: the manifest is read at run time.

## Licence

MIT, see [LICENSE](LICENSE).
