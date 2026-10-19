# -*- coding: utf-8 -*-
"""The DynamicLoot loot editor (MIT licence).

Searches items in the world database, shows their icons from the game, and
writes the <Mod>.Loot.* lines of a mod's .conf from what is dragged into
DynamicLoot's families. Built into loot-editor.exe by build_editor.cmd."""
import loot_editor_window

if __name__ == "__main__":
    loot_editor_window.run()
