# Offline Hero Sync

Keeps an offline copy of your **Minecraft Dungeons II** online hero up to date, automatically. One way only (online to offline), read-only toward the game.

**Download:** see [Releases](../../releases/latest).
- `OfflineHeroSync-1.2.0.zip`: no install needed, bundles the official signed Python runtime.
- `OfflineHeroSync-1.2.0-python.zip`: source only, needs Python 3.8+.

Also on [Nexus Mods](https://www.nexusmods.com/minecraftdungeons2/mods/75).

```
OFFLINE HERO SYNC 1.2
=====================

Keeps an offline copy of your online Minecraft Dungeons II hero up to date, automatically.
While you play your online hero, its offline copy gets the same level, gear, enchantments,
emeralds, echo shards, quests, map progress, achievements and cosmetics.

Handy if you want to use gameplay mods on your main (they only work on offline heroes), or
just want an offline version of your main that keeps up with it.

NEW IN 1.2
----------
- An emerald icon by the clock shows what it's doing, and a notification tells you when your
  offline copy is made or updated. Everything is in its right-click menu.
- "Start with Windows" is one click in that menu. It's off until you turn it on.
- Updating is just starting the new version: it closes the old one for you.
- Xbox app / Minecraft Launcher version: not in this release yet. It's being tested; the beta
  is on GitHub (https://github.com/haayden/offline-hero-sync/releases).

Since 1.1 it copies your hero while you play it in the world (Mojang stopped sending the whole
hero to the character select screen around Oct 3), so it works for every hero, including ones
it has never seen before.

IS IT SAFE?
-----------
- There's no exe of mine in here. The tool is plain Python source in the app folder that you can
  read, run by the official Python runtime (python.exe / pythonw.exe in the runtime folder,
  digitally signed by the Python Software Foundation). Same code on GitHub:
  https://github.com/haayden/offline-hero-sync
- It opens the game read-only, so it can look at your hero but can't change anything in the
  game. No injection, no DLLs, no hooks. It never touches the internet, and it writes nothing
  but your offline copies and its own folder (%LOCALAPPDATA%\OfflineHeroSync: log, backups).
- It doesn't add itself to startup or hide. You see an icon by the clock while it runs.

READ THIS FIRST
---------------
- One way only: online -> offline. Nothing ever goes back to your online hero.
- It's a mirror. When your online hero changes, the offline copy is replaced with it, so
  progress you made on the offline copy gets overwritten. The replaced file is backed up first
  (the newest 20 per hero are kept, plus the first backup of every day).
- It never touches the offline copy while you're playing it.
- Your online hero is not changed. The tool only reads from the game. It never writes to the
  game and never sends anything anywhere.

HOW TO USE
----------
1. Unzip the folder somewhere it can stay (for example Documents\OfflineHeroSync).
2. Double-click "Start Offline Hero Sync.cmd". An emerald appears by the clock (if you don't
   see it, click the little ^ arrow next to the clock).
3. Start the game and load into the world with your online hero. Within 30 seconds it makes
   the offline copy and tells you. Restart the game and the copy shows up in your hero list.

That's it. Right-click the emerald to see what it's doing, turn "Start with Windows" on or off,
open the log or the backups folder, or quit it. If you don't turn on "Start with Windows",
double-click the .cmd again next time before you play.

UPDATING
--------
Unzip the new version and double-click "Start Offline Hero Sync.cmd". It closes the old version
by itself and takes over (and if "Start with Windows" was on, it now starts the new one).

Heroes don't have names in the save data, so the log lists them by level, power and skin.

WHEN DOES IT SYNC
-----------------
- Every 30 seconds while you play your online hero in the world, if anything changed.
- If you're playing the offline copy, it waits until you leave it.

WHAT IT CAN'T COPY
------------------
The game only has what Mojang's server sends it, so a few things come from the tool's earlier
copy of your hero when it has one, and are missing on a brand-new copy:
- Gates, doors and levers you opened in the world (they may be closed again on the copy).
- The full lists behind the collection screen. A new copy starts them from the gear you own,
  so the collection percentages can read lower.
- A few "do X once" achievements (equip a unique, reforge, buy from the merchant, defeat a
  monarch).
- The hidden roll data of gear you pick up after the copy was first made. The gear itself
  (item power, effects, enchantments, level) is copied exactly.

WHERE THINGS GO
---------------
Offline heroes:  %LOCALAPPDATA%\Dungeons2\Saved\SaveGames\Character<id>.sav
Tool data:       %LOCALAPPDATA%\OfflineHeroSync\
                   autosync.log  what it did and when
                   state.json    which offline copy belongs to which online hero
                   out\          the last copy it built of each hero
                   backups\      offline copies saved before they were replaced

To restore a backup: quit the tool (emerald menu > Quit), close the game, copy the file from
backups\ into SaveGames and rename it to Character<id>.sav (remove the date part).
To remove everything: in the emerald menu untick "Start with Windows", then Quit. Delete the
tool's folder and the OfflineHeroSync folder above. Delete the offline hero in the game if you
don't want it.

CPU USE
-------
While the game is closed it only looks at the process list every 10 seconds. While you play,
it reads the game's memory every 30 seconds; the first read after loading in takes a few
seconds of one CPU core, the rest well under a second.

ANTIVIRUS WARNINGS
------------------
There is no packed exe. The tool is the plain Python source in the app folder (read it), run by
a private copy of the official Python runtime in the runtime folder (python.exe / pythonw.exe,
digitally signed by the Python Software Foundation). The two .cmd files just start it.
If you already have Python 3.8 or newer you can also run it directly:
    pythonw app\offline_hero_sync.py

SAFETY DETAILS
--------------
- It opens the game with read-only access. No injection, no DLL, no hooks, so it doesn't trip
  the game's integrity check. No admin rights needed. No internet access.
- The only save files it writes are the offline copies it manages. It won't touch any other
  save, and your online hero has no save file on your PC to begin with.
- Only one copy runs at a time.

COMMAND LINE (for tinkerers, you don't need it)
-----------------------------------------------
Run these through "Offline Hero Sync (command line).cmd" from a command prompt in the folder.
  --install-autostart / --uninstall-autostart   start at login, or stop doing that
  --status                 running or not, which heroes, last log lines
  --stop                   stop the background sync
  --list                   online heroes the tool knows
  --hero 1                 dry run for one hero: builds the copy into the out folder only
  --hero 1 --write         one-off write (asks for --replace-played if the copy was played)
  --cli                    one-off step-by-step version in a console window
  --adopt ONLINE:OFFLINE   use an offline hero you already have as the copy of an online hero
  --no-live, --save-dir, --data-dir, --help

NOTES
-----
- Windows, Steam version. The Xbox app / Minecraft Launcher version keeps its saves in a
  different format; support for it is in testing (beta on GitHub).
- After a game patch the tool may need an update to read the game. It then just stops syncing
  and says why in the log; it never writes a broken copy.
```
