# Offline Hero Sync

Keeps an offline copy of your **Minecraft Dungeons II** online hero up to date, automatically. One way only (online to offline), read-only toward the game.

**Download:** see [Releases](../../releases/latest).
- `OfflineHeroSync-1.0.2.zip`: no install needed, bundles the official signed Python runtime.
- `OfflineHeroSync-1.0.2-python.zip`: source only, needs Python 3.8+.

Also on [Nexus Mods](https://www.nexusmods.com/minecraftdungeons2/mods/75).

```
OFFLINE HERO SYNC
=================

Keeps an offline copy of your online Minecraft Dungeons II hero up to date, automatically.
Whenever your online hero changes, its offline copy gets the same level, gear, emeralds,
enchantment points, quests, unlocks and cosmetics.

Handy if you want to use gameplay mods on your main (they only work on offline heroes), or
just want an offline version of your main that keeps up with it.

READ THIS FIRST
---------------
- One way only: online -> offline. Nothing ever goes back to your online hero.
- It's a mirror. When your online hero changes, the offline copy is replaced with it, so
  progress you made on the offline copy gets overwritten. The replaced file is backed up first
  (the newest 20 per hero are kept).
- It never touches the offline copy while you're playing it.
- Your online hero is not changed. The tool only reads from the game. It never writes to the
  game and never sends anything anywhere.

HOW TO USE
----------
1. Unzip the folder somewhere it can stay (for example Documents\OfflineHeroSync).
2. Double-click "Start Offline Hero Sync.cmd". Nothing pops up. It runs in the background.
3. Start the game and go to the character select screen. The first time, it makes an offline
   copy of each online hero it sees. Restart the game and the copy shows up in your hero list.

Start it with Windows (optional): open a command prompt in the folder and run
    "Offline Hero Sync (command line).cmd" --install-autostart
It starts now and at every login. Undo it with --uninstall-autostart.

Is it working? Open %LOCALAPPDATA%\OfflineHeroSync\autosync.log, or run
    "Offline Hero Sync (command line).cmd" --status
Stop it: "Offline Hero Sync (command line).cmd" --stop (or end pythonw.exe in Task Manager).

Heroes don't have names in the save data, so the log lists them by level, power and skin.

WHEN DOES IT SYNC
-----------------
- The game only keeps the server's copy of your online hero in memory for a short while
  after the character select screen loads. The tool watches for it and saves it right away.
- While you play the online hero, it also copies this session's emeralds, XP and items to the
  offline copy every 30 seconds.
- Quest and map progress come from the server's copy, so they update the next time you're on
  the character select screen.
- If you're playing the offline copy when something new comes in, it waits until you leave it.

WHERE THINGS GO
---------------
Offline heroes:  %LOCALAPPDATA%\Dungeons2\Saved\SaveGames\Character<id>.sav
Tool data:       %LOCALAPPDATA%\OfflineHeroSync\
                   autosync.log  what it did and when
                   state.json    which offline copy belongs to which online hero
                   cache\        the last server copy it saw of each online hero
                   backups\      offline copies saved before they were replaced

To restore a backup: stop the tool, close the game, copy the file from backups\ into SaveGames
and rename it to Character<id>.sav (remove the date part).
To remove everything: --uninstall-autostart, delete the tool's folder and the OfflineHeroSync
folder above. Delete the offline hero in the game if you don't want it.

CPU USE
-------
While the game is closed it only looks at the process list every 10 seconds. While the game
runs it reads the game's memory every 30 seconds at the menu (every 5 seconds for two minutes
after the game starts or you switch between menu and world) and every 5 minutes in-world.
One read takes 1 to 2 seconds of one CPU core. The live copy while you play the online hero
reads a few thousand values every 30 seconds.

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
  --list                   online heroes in game memory (or the cached ones)
  --hero 1                 dry run for one hero: builds the copy into the out folder only
  --hero 1 --write         one-off write (asks for --replace-played if the copy was played)
  --cli                    one-off step-by-step version in a console window
  --adopt ONLINE:OFFLINE   use an offline hero you already have as the copy of an online hero
  --no-live, --save-dir, --data-dir, --help

NOTES
-----
- Windows only. Tested on the Steam version. The Microsoft Store / Xbox app version is untested;
  if it keeps its saves somewhere else, use --save-dir.
- After a game patch the in-session copy may stop working. The tool then uses the server's copy
  from the character select screen, which still works.

```
