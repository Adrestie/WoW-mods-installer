# -*- coding: utf-8 -*-
"""The loot editor's texts, in French and in English (MIT licence). Any other
language of the game gets English."""
import re

TEXTS = {
    "title": ("Éditeur de butin DynamicLoot", "DynamicLoot loot editor"),
    "subtitle": ("Glissez des objets dans les familles, puis générez les lignes du .conf de votre mod.",
                 "Drag items into the families, then generate the lines of your mod's .conf."),
    "game": ("Dossier du jeu", "Game folder"),
    "game_hint": ("Le dossier de Wow.exe : les icônes viennent de ses archives.",
                  "The folder of Wow.exe: the icons come from its archives."),
    "server": ("Dossier du worldserver", "Worldserver folder"),
    "server_hint": ("Le dossier de worldserver.exe : sa configuration donne la base world.",
                    "The folder of worldserver.exe: its configuration gives the world database."),
    "browse": ("Parcourir…", "Browse..."),
    "connect": ("Se connecter", "Connect"),
    "connecting": ("Connexion…", "Connecting..."),
    "connected": ("Base %s, %d familles de DynamicLoot%s, langue du jeu %s.",
                  "Database %s, %d DynamicLoot families%s, game language %s."),
    "families_builtin": (" (liste intégrée : source de DynamicLoot introuvable)",
                         " (built-in list: DynamicLoot's source not found)"),
    "no_icons": ("sans icônes (dossier du jeu absent)", "no icons (no game folder)"),
    "not_connected": ("Renseignez les dossiers, puis « Se connecter ».", "Fill in the folders, then press Connect."),
    "prefix": ("Préfixe du mod", "Mod prefix"),
    "rate": ("Taux (%)", "Rate (%)"),
    "import": ("Importer un .conf…", "Import a .conf..."),
    "new": ("Tout effacer", "Clear all"),
    "language": ("Langue", "Language"),
    "search": ("RECHERCHE", "SEARCH"),
    "search_hint": ("Nom, numéro, ou plage (87001-87372)", "Name, entry, or range (87001-87372)"),
    "search_button": ("Chercher", "Search"),
    "quality": ("Qualité", "Quality"),
    "any_quality": ("Toutes", "Any"),
    "qualities": (["Médiocre", "Courante", "Inhabituelle", "Rare", "Épique", "Légendaire", "Artefact", "Héritage"],
                  ["Poor", "Common", "Uncommon", "Rare", "Epic", "Legendary", "Artifact", "Heirloom"]),
    "results": ("%d objet(s)", "%d item(s)"),
    "results_more": ("%d premiers objets : précisez la recherche", "first %d items: narrow the search"),
    "searching": ("Recherche…", "Searching..."),
    "add_all": ("Tout ajouter à la liste", "Add all to the list"),
    "drop_delete": ("Déposez ici pour retirer", "Drop here to remove"),
    "lists": ("LISTES", "LISTS"),
    "new_list": ("Nouvelle liste", "New list"),
    "list_hint": ("Une liste se tire sans poids : glissez-la dans une famille comme un objet.",
                  "A list is drawn without weights: drag it into a family like an item."),
    "drop_items": ("Déposez des objets ici", "Drop items here"),
    "rename": ("Renommer", "Rename"),
    "delete": ("Supprimer", "Delete"),
    "no_pity": ("SANS PROTECTION (NoPity)", "NO BAD LUCK PROTECTION (NoPity)"),
    "no_pity_hint": ("Objets auxquels la protection contre la malchance n'ajoute rien.",
                     "Items the bad luck protection adds nothing to."),
    "drop_family": ("Déposez un objet ou une liste : un nouveau tirage", "Drop an item or a list: a new draw"),
    "draw_hint": ("Un dé : les objets se tentent de gauche à droite, le premier qui sort arrête le dé.",
                  "One die: the items are tried left to right, the first that drops stops the die."),
    "preview": ("APERÇU", "PREVIEW"),
    "generate": ("Générer", "Generate"),
    "edit_title": ("Tirage", "Draw"),
    "chance": ("Chance (%)", "Chance (%)"),
    "quantity": ("Quantité", "Quantity"),
    "move_left": ("◀ Avant", "◀ Earlier"),
    "move_right": ("Après ▶", "Later ▶"),
    "ok": ("OK", "OK"),
    "cancel": ("Annuler", "Cancel"),
    "close": ("Fermer", "Close"),
    "save": ("Enregistrer…", "Save..."),
    "copy": ("Copier", "Copy"),
    "remove": ("Retirer", "Remove"),
    "generated": ("Lignes générées", "Generated lines"),
    "generated_text": ("Copiées dans le presse-papiers : collez-les dans le .conf de votre mod (configs\\modules), "
                       "puis relancez le worldserver.",
                       "Copied to the clipboard: paste them into your mod's .conf (configs\\modules), then restart "
                       "the worldserver."),
    "warnings": ("À vérifier :", "To check:"),
    "errors_title": ("Impossible de générer", "Cannot generate"),
    "errors_text": ("DynamicLoot refuserait ces lignes :", "DynamicLoot would refuse these lines:"),
    "import_title": ("Importer", "Import"),
    "import_choose": ("Ce fichier déclare plusieurs mods. Lequel importer ?",
                      "This file declares several mods. Which one?"),
    "import_none": ("Aucune ligne <Mod>.Loot.* dans ce fichier.", "No <Mod>.Loot.* line in this file."),
    "import_done": ("%s : %d liste(s), %d famille(s) importées.", "%s: %d list(s), %d family(ies) imported."),
    "import_problems": ("Lignes illisibles, laissées de côté :", "Unreadable lines, left out:"),
    "clear_confirm": ("Tout effacer ? Les listes et les tirages seront perdus.",
                      "Clear all? The lists and draws will be lost."),
    "rename_prompt": ("Nouveau nom (mot[n], par exemple cards[0]) :", "New name (word[n], for example cards[0]):"),
    "error": ("Erreur", "Error"),
    "language_restart": ("", ""),
    "tabs": ({"creatures": "Créatures", "levels": "Niveaux", "gathering": "Récolte",
              "skinning": "Dépeçage", "chests": "Coffres", "other": "Autres"},
             {"creatures": "Creatures", "levels": "Levels", "gathering": "Gathering",
              "skinning": "Skinning", "chests": "Chests", "other": "Other"}),
    "groups": ({"world": "Monde, boss du monde et rares", "trash": "Monstres ordinaires des donjons et des raids",
                "dungeon_boss": "Boss de donjon", "raid_boss": "Boss de raid",
                "levels": "Par niveau de la créature, en plus de sa famille", "ores": "Minerais", "herbs": "Herbes",
                "skinning": "Dépeçage", "corpses": "Cadavres d'herboristerie, de minage, d'ingénierie",
                "chests": "Coffres", "containers": "Conteneurs d'inventaire", "other": "Autres familles"},
               {"world": "World, world bosses and rares", "trash": "The other monsters of dungeons and raids",
                "dungeon_boss": "Dungeon bosses", "raid_boss": "Raid bosses",
                "levels": "By the creature's level, on top of its family", "ores": "Ores", "herbs": "Herbs",
                "skinning": "Skinning", "corpses": "Corpses gathered by herbalism, mining, engineering",
                "chests": "Chests", "containers": "Inventory containers", "other": "Other families"}),
    "problem": ({"prefix": "préfixe de mod invalide (lettres, chiffres, _)", "list_name": "nom de liste invalide",
                 "list_empty": "liste vide", "list_unused": "liste qu'aucune famille ne tire",
                 "family_unknown": "famille que ce DynamicLoot ne connaît pas (ignorée)",
                 "list_missing": "liste inexistante", "quantity": "quantité hors de 1 à 255",
                 "chance": "chance hors de 0 à 100", "chance_zero": "chance de 0 % : ne tombe jamais",
                 "mixed_no_pity": "liste qui mêle objets NoPity et autres : tirée sans protection",
                 "item_missing": "objet absent de la base world"},
                {"prefix": "invalid mod prefix (letters, digits, _)", "list_name": "invalid list name",
                 "list_empty": "empty list", "list_unused": "list no family draws",
                 "family_unknown": "family this DynamicLoot does not know (ignored)",
                 "list_missing": "no such list", "quantity": "quantity out of 1 to 255",
                 "chance": "chance out of 0 to 100", "chance_zero": "chance of 0 %: never drops",
                 "mixed_no_pity": "list mixing NoPity items and others: drawn without protection",
                 "item_missing": "item missing from the world database"}),
}

_EXPANSION = {"Vanilla": "Vanilla", "Bc": "BC", "Wotlk": "WotLK"}

_KINDS = (
    {"World": "créatures du monde (hors instance)", "WorldBoss": "boss du monde", "Rare": "rares (extérieur et donjons)",
     "DungeonTrash": "monstres ordinaires de donjon", "DungeonBoss": "boss de donjon",
     "HeroicTrash": "monstres ordinaires de donjon héroïque", "HeroicBoss": "boss de donjon héroïque",
     "RaidTrash": "monstres ordinaires de raid", "RaidBoss": "boss de raid",
     "Ores": "minerais", "OresLow": "minerais bas (cobalt)", "OresMedium": "minerais moyens (saronite)",
     "OresHigh": "minerais hauts (titane)", "Herbs": "herbes",
     "HerbsLow": "herbes basses (trèfle d'or, feuillefeu, lys tigré)",
     "HerbsMedium": "herbes moyennes (rose de Talandra, langue de serpent, fleur-de-liche)",
     "HerbsHigh": "herbes hautes (glacépine, lotus givré)",
     "CorpseHerbs": "cadavres d'herboristerie", "CorpseOres": "cadavres de minage",
     "CorpseSalvage": "cadavres d'ingénierie", "Container": "conteneurs d'inventaire"},
    {"World": "creatures of the world (outside instances)", "WorldBoss": "world bosses",
     "Rare": "rares (outside and in dungeons)", "DungeonTrash": "ordinary dungeon monsters",
     "DungeonBoss": "dungeon bosses", "HeroicTrash": "ordinary heroic dungeon monsters",
     "HeroicBoss": "heroic dungeon bosses", "RaidTrash": "ordinary raid monsters", "RaidBoss": "raid bosses",
     "Ores": "ores", "OresLow": "low ores (cobalt)", "OresMedium": "medium ores (saronite)",
     "OresHigh": "high ores (titanium)", "Herbs": "herbs",
     "HerbsLow": "low herbs (goldclover, fire leaf, tiger lily)",
     "HerbsMedium": "medium herbs (Talandra's rose, adder's tongue, lichbloom)",
     "HerbsHigh": "high herbs (icethorn, frost lotus)",
     "CorpseHerbs": "corpses for herbalism", "CorpseOres": "corpses for mining",
     "CorpseSalvage": "corpses for engineering", "Container": "inventory containers"},
)

_SINGLE = (
    {"IcecrownRaidBoss": "boss de la Citadelle de la Couronne de glace",
     "IcecrownRaidTrash": "monstres ordinaires de la Citadelle de la Couronne de glace",
     "WorldChest": "vrais coffres du monde", "DungeonChest": "vrais coffres de donjon (normal)",
     "HeroicDungeonChest": "vrais coffres de donjon (héroïque)",
     "Skinning1": "dépeçage, niveau 1 à 60", "Skinning61": "dépeçage, niveau 61 à 70",
     "Skinning71": "dépeçage, niveau 71 à 79", "Skinning80": "dépeçage, niveau 80 et plus"},
    {"IcecrownRaidBoss": "bosses of Icecrown Citadel", "IcecrownRaidTrash": "ordinary monsters of Icecrown Citadel",
     "WorldChest": "real chests of the world", "DungeonChest": "real dungeon chests (normal)",
     "HeroicDungeonChest": "real dungeon chests (heroic)",
     "Skinning1": "skinning, level 1 to 60", "Skinning61": "skinning, level 61 to 70",
     "Skinning71": "skinning, level 71 to 79", "Skinning80": "skinning, level 80 and above"},
)


class Texts(object):
    """The texts of one language: t("key"), t("key", args...) formats."""

    def __init__(self, language):
        self.language = "fr" if language == "fr" else "en"
        self.index = 0 if self.language == "fr" else 1

    def __call__(self, key, *args):
        text = TEXTS[key][self.index]
        return text % args if args else text

    def family(self, name):
        """What a family holds, in words ("" for a family this editor does not describe)."""
        i = self.index
        if name in _SINGLE[i]:
            return _SINGLE[i][name]
        m = re.match(r"^MobLvl_(\d+)_(\d+)$", name)
        if m:
            low, high = m.groups()
            if high == "89":
                return ("niveau %s et plus" if i == 0 else "level %s and above") % low
            return ("niveau %s à %s" if i == 0 else "level %s to %s") % (low, high)
        for expansion in _EXPANSION:
            if name.startswith(expansion):
                kind = _KINDS[i].get(name[len(expansion):])
                if kind:
                    return "%s, %s" % (_EXPANSION[expansion], kind)
        return ""


def language_of(locale):
    """The editor's language for a game locale ("frFR" -> "fr")."""
    return "fr" if locale and locale.lower().startswith("fr") else "en"
