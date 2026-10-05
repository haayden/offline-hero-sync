"""Builds the Nexus zip WITHOUT PyInstaller: a private copy of the official, PSF-signed Python runtime plus the
plain .py sources and two .cmd launchers. PyInstaller one-file exes get flagged by antivirus heuristics (Nexus
marked 1.0 "Some suspicious files"); signed python.exe/pythonw.exe do not.

    python build_embedded.py VERSION        ->  ..\\release\\OfflineHeroSync-<VERSION>.zip
"""
import compileall
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = Path(sys.base_prefix)                       # the local official CPython install (C:\Python314)
TAG = "python%d%d" % sys.version_info[:2]
RUNTIME_FILES = ["python.exe", "pythonw.exe", TAG + ".dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"]
PYDS = ["_ctypes.pyd", "libffi-8.dll", "_uuid.pyd", "_hashlib.pyd", "libcrypto-3.dll", "select.pyd", "_socket.pyd",
        "_queue.pyd", "_bz2.pyd", "_lzma.pyd", "_zstd.pyd"]
SKIP_LIB = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "venv", "lib2to3", "site-packages",
            "pydoc_data", "__phello__", "_pyrepl", "sqlite3", "dbm", "curses", "wsgiref", "xmlrpc", "http", "email",
            "html", "urllib", "asyncio", "unittest", "multiprocessing", "concurrent"}
APP_FILES = ["offline_hero_sync.py", "uemem.py", "winproc.py", "rebuild.py", "savejson.py"]

START_CMD = '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0app\\offline_hero_sync.py" %*\r\n'
CLI_CMD = '@echo off\r\n"%~dp0runtime\\python.exe" "%~dp0app\\offline_hero_sync.py" %*\r\n'


def build_stdlib_dir(dest):
    """All of Lib (minus big unused packages) compiled to sourceless .pyc in a plain folder. Not a zip: Nexus
    quarantines nested archives, and 1.0.1's python314.zip inside the release zip was one."""
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "Lib"
        shutil.copytree(PY / "Lib", lib, ignore=lambda d, names: [n for n in names if n in SKIP_LIB or n == "__pycache__"])
        compileall.compile_dir(str(lib), quiet=1, legacy=True, optimize=0)
        for p in sorted(lib.rglob("*.pyc")):
            out = dest / p.relative_to(lib)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)


def main():
    version = sys.argv[1]
    stage = HERE.parent / "build" / "embedded" / "OfflineHeroSync"
    if stage.exists():
        shutil.rmtree(stage)
    rt, app = stage / "runtime", stage / "app"
    rt.mkdir(parents=True)
    app.mkdir()
    for f in RUNTIME_FILES:
        shutil.copy2(PY / f, rt / f)
    for f in PYDS:
        if (PY / "DLLs" / f).exists():
            shutil.copy2(PY / "DLLs" / f, rt / f)
    build_stdlib_dir(rt / "Lib")
    (rt / (TAG + "._pth")).write_text("Lib\n.\n..\\app\n", encoding="ascii")
    for f in APP_FILES:
        shutil.copy2(HERE / f, app / f)
    (stage / "Start Offline Hero Sync.cmd").write_text(START_CMD, encoding="ascii", newline="")
    (stage / "Offline Hero Sync (command line).cmd").write_text(CLI_CMD, encoding="ascii", newline="")
    shutil.copy2(HERE.parent / "release" / "README.txt", stage / "README.txt")

    # smoke test with the bundled runtime only (isolated from the dev install's site-packages)
    out = subprocess.run([str(rt / "python.exe"), str(app / "offline_hero_sync.py"), "--version"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print("bundled runtime --version:", out.returncode, out.stdout.strip()[-200:])
    if out.returncode != 0:
        sys.exit("bundled runtime failed")

    zpath = HERE.parent / "release" / ("OfflineHeroSync-%s.zip" % version)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, ("OfflineHeroSync/" + p.relative_to(stage).as_posix()))
    print("zip:", zpath, zpath.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
