# Offline Hero Sync

Keeps an offline copy of your **Minecraft Dungeons II** online hero up to date, automatically. One way only (online to offline), read-only toward the game.

**Download:** see [Releases](../../releases/latest).
- `OfflineHeroSync-1.1.0.zip`: no install needed, bundles the official signed Python runtime.
- `OfflineHeroSync-1.1.0-python.zip`: source only, needs Python 3.8+.

Also on [Nexus Mods](https://www.nexusmods.com/minecraftdungeons2/mods/75).

```
OFFLINE HERO SYNC 1.1
=====================

Keeps an offline copy of your online Minecraft Dungeons II hero up to date, automatically.
While you play your online hero, its offline copy gets the same level, gear, enchantments,
emeralds, echo shards, quests, map progress, achievements and cosmetics.

Handy if you want to use gameplay mods on your main (they only work on offline heroes), or
just want an offline version of your main that keeps up with it.

NEW IN 1.1
----------
Since about Oct 3 the game no longer receives your whole online hero at the character select
screen (a change on Mojang's side), which is where 1.0 copied it from. 1.1 rebuilds the hero
from what the game has loaded while you play it in the world instead, so it works again for
everyone, including heroes it has never seen before.

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
2. Double-click "Start Offline Hero Sync.cmd". Nothing pops up. It runs in the background.
3. Start the game and load into the world with your online hero. Within 30 seconds it makes
   the offline copy. Restart the game and the copy shows up in your hero list.

Start it with Windows (optional): open a command prompt in the folder and run
    "Offline Hero Sync (command line).cmd" --install-autostart
It starts now and at every login. Undo it with --uninstall-autostart.

Is it working? Open %LOCALAPPDATA%\OfflineHeroSync\autosync.log, or run
    "Offline Hero Sync (command line).cmd" --status
Stop it: "Offline Hero Sync (command line).cmd" --stop (or end pythonw.exe in Task Manager).

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

To restore a backup: stop the tool, close the game, copy the file from backups\ into SaveGames
and rename it to Character<id>.sav (remove the date part).
To remove everything: --uninstall-autostart, delete the tool's folder and the OfflineHeroSync
folder above. Delete the offline hero in the game if you don't want it.

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

COMMAND LINE (optional)
-----------------------
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
- Windows, Steam version. The Xbox app / Minecraft Launcher version isn't supported yet (it
  keeps its saves in a different place and format); that's next.
- After a game patch the tool may need an update to read the game. It then just stops syncing
  and says why in the log; it never writes a broken copy.
```
