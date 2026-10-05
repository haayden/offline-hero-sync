"""Xbox app / Minecraft Launcher saves (GDK "WGS" containers).  New in 1.2.

The PC Game Pass build keeps its saves under
    %LOCALAPPDATA%\\Packages\\Microsoft.MinecraftDungeons2_8wekyb3d8bbwe\\SystemAppData\\wgs\\<user>_<title>\\
as containers instead of files:
    containers.index           version 0xE: every container (= one save slot, e.g. "Character<id>") with its
                               sequence number, flags, folder GUID, mtime and size
    <FOLDER GUID>\\container.N  version 4: the container's files (name, GUID); N = its sequence number
    <FOLDER GUID>\\<FILE GUID>  the data, the same bytes the Steam build writes to SaveGames\\<slot>.sav
Format as read and written by Z1ni/XGP-save-extractor and PalworldSaveTools (palworld_xgp_import).

Offline Hero Sync works on a mirror of these containers (one .sav per slot, like Steam's SaveGames), so all
of its checks (played copies, backups) stay the same; copies it writes go back into the containers while the
game is closed.  New containers are local-only (flag 5, no cloud id) like the game's own new saves; the Xbox
app uploads them.  Existing ones are updated the way the game does it: same folder, sequence number + 1.
"""
import datetime
import os
import shutil
import struct
import uuid
from pathlib import Path

PACKAGE = 'Microsoft.MinecraftDungeons2_8wekyb3d8bbwe'
INDEX_VERSION = 0xE
CONTAINER_VERSION = 4
FILETIME_EPOCH = 116444736000000000
DEFAULT_BLOB_NAME = 'Data'          # what Unreal's GDK save system names a slot's file (Palworld, also UE)


class WgsError(Exception):
    pass


def local_appdata():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')


def user_dirs():
    """[<user>_<title> dirs] of the game's WGS store, newest index first."""
    root = local_appdata() / 'Packages' / PACKAGE / 'SystemAppData' / 'wgs'
    if not root.is_dir():
        return []
    out = [d for d in root.iterdir() if d.is_dir() and d.name != 't' and 'backup' not in d.name.lower()
           and len(d.name.split('_')) == 2 and (d / 'containers.index').is_file()]
    return sorted(out, key=lambda d: (d / 'containers.index').stat().st_mtime, reverse=True)


def now_filetime():
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 10_000_000) + FILETIME_EPOCH


# ---- binary helpers ---------------------------------------------------------------------------------------------
class _R:
    def __init__(self, b):
        self.b, self.o = b, 0

    def take(self, n):
        if self.o + n > len(self.b):
            raise WgsError('truncated file')
        v = self.b[self.o:self.o + n]
        self.o += n
        return v

    def u8(self): return self.take(1)[0]
    def u32(self): return struct.unpack('<I', self.take(4))[0]
    def u64(self): return struct.unpack('<Q', self.take(8))[0]

    def s(self):
        n = self.u32()
        return self.take(2 * n).decode('utf-16-le') if n else ''


def _ws(s):
    return struct.pack('<I', len(s)) + s.encode('utf-16-le')


# ---- containers.index ---------------------------------------------------------------------------------------------
class Container:
    __slots__ = ('name', 'cloud_id', 'seq', 'flag', 'guid', 'mtime', 'reserved', 'size')

    def folder(self, user_dir):
        return Path(user_dir) / uuid.UUID(bytes=self.guid).bytes_le.hex().upper()


class Index:
    def __init__(self, user_dir):
        self.dir = Path(user_dir)
        r = _R((self.dir / 'containers.index').read_bytes())
        version = r.u32()
        if version != INDEX_VERSION:
            raise WgsError('unsupported containers.index version %d' % version)
        count = r.u32()
        self.display_name = r.s()       # empty on PC (console saves carry the game's name here)
        self.package = r.s()
        self.mtime = r.u64()
        self.flag2 = r.u32()
        self.index_id = r.s()
        self.unknown = r.u64()
        self.containers = []
        for _ in range(count):
            c = Container()
            c.name = r.s()
            if r.s() != c.name:
                raise WgsError('containers.index: name mismatch')
            c.cloud_id = r.s()
            c.seq = r.u8()
            c.flag = r.u32()
            c.guid = r.take(16)
            c.mtime = r.u64()
            c.reserved = r.u64()
            c.size = r.u64()
            self.containers.append(c)

    def to_bytes(self):
        out = [struct.pack('<II', INDEX_VERSION, len(self.containers)), _ws(self.display_name), _ws(self.package),
               struct.pack('<QI', self.mtime, self.flag2), _ws(self.index_id), struct.pack('<Q', self.unknown)]
        for c in self.containers:
            out += [_ws(c.name), _ws(c.name), _ws(c.cloud_id), struct.pack('<BI', c.seq, c.flag), c.guid,
                    struct.pack('<QQQ', c.mtime, c.reserved, c.size)]
        return b''.join(out)

    def write(self):
        tmp = self.dir / 'containers.index.ohs-tmp'
        tmp.write_bytes(self.to_bytes())
        os.replace(tmp, self.dir / 'containers.index')

    def latest(self):
        """{slot name: newest Container} (a slot can briefly have two entries while the app syncs)."""
        best = {}
        for c in self.containers:
            if c.name not in best or (c.seq, c.mtime) > (best[c.name].seq, best[c.name].mtime):
                best[c.name] = c
        return best


def read_files(folder, seq):
    """[(file name, data path)] of container.<seq> in folder."""
    p = Path(folder) / ('container.%d' % seq)
    if not p.is_file():
        return []
    r = _R(p.read_bytes())
    version = r.u32()
    if version != CONTAINER_VERSION:
        raise WgsError('unsupported container file version %d' % version)
    out = []
    for _ in range(r.u32()):
        name = r.take(128).decode('utf-16-le').rstrip('\x00')
        g1 = r.take(16)
        g2 = r.take(16)
        cands = [Path(folder) / uuid.UUID(bytes=g).bytes_le.hex().upper() for g in (g2, g1) if any(g)]
        path = next((c for c in cands if c.is_file()), None)
        if path:
            out.append((name, path))
    return out


# ---- the store ----------------------------------------------------------------------------------------------------
class Store:
    def __init__(self, user_dir):
        self.dir = Path(user_dir)

    def slots(self):
        """{slot name: (Container, [(file name, path)])}"""
        idx = Index(self.dir)
        return {n: (c, read_files(c.folder(self.dir), c.seq)) for n, c in idx.latest().items()}

    def read(self, slot):
        got = self.slots().get(slot)
        if not got or not got[1]:
            return None
        return got[1][0][1].read_bytes()

    def blob_name(self):
        """The file name the game uses inside its character containers (learned from an existing one)."""
        for name, (c, files) in self.slots().items():
            if name.startswith('Character') and len(files) == 1:
                return files[0][0]
        return DEFAULT_BLOB_NAME

    def backup(self, slot, backup_root):
        """Copies containers.index and the slot's container folder before a write."""
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        dest = Path(backup_root) / ('wgs-%s-%s' % (stamp, slot))
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.dir / 'containers.index', dest / 'containers.index')
        c = Index(self.dir).latest().get(slot)
        if c and c.folder(self.dir).is_dir():
            shutil.copytree(c.folder(self.dir), dest / c.folder(self.dir).name)
        return dest

    def write(self, slot, data, blob_name=None):
        """Creates or updates the container for slot with data (call only while the game is closed)."""
        idx = Index(self.dir)
        cur = idx.latest().get(slot)
        now = now_filetime()
        if cur:
            c = cur
            c.seq = (c.seq + 1) % 256 or 1
        else:
            c = Container()
            c.name, c.cloud_id, c.seq, c.flag, c.reserved = slot, '', 1, 5, 0
            c.guid = uuid.uuid4().bytes
        folder = c.folder(self.dir)
        old_files = list(folder.iterdir()) if folder.is_dir() else []
        name = blob_name or self.blob_name()
        if cur:
            existing = read_files(folder, cur.seq)
            if existing:
                name = existing[0][0]
        folder.mkdir(parents=True, exist_ok=True)
        fid = uuid.uuid4()
        (folder / fid.bytes_le.hex().upper()).write_bytes(data)
        listing = struct.pack('<II', CONTAINER_VERSION, 1) + name.encode('utf-16-le').ljust(128, b'\x00') + \
            b'\x00' * 16 + fid.bytes
        (folder / ('container.%d' % c.seq)).write_bytes(listing)
        c.mtime, c.size = now, len(data)
        idx.containers = [x for x in idx.containers if x.name != slot] + [c]
        idx.mtime = now
        idx.write()
        keep = {('container.%d' % c.seq).lower(), fid.bytes_le.hex().upper().lower()}
        for f in old_files:                      # the previous sequence's files, now unreferenced
            if f.name.lower() not in keep:
                try:
                    f.unlink()
                except OSError:
                    pass


def describe(user_dir):
    """Readable listing for --xbox-info (what the tool sees, to check before it writes anything)."""
    st = Store(user_dir)
    idx = Index(user_dir)
    lines = ['WGS store: %s' % user_dir, 'package: %s, %d containers' % (idx.package, len(idx.containers))]
    for name, (c, files) in sorted(st.slots().items()):
        head = b''
        if files:
            with open(files[0][1], 'rb') as f:
                head = f.read(48)
        lines.append('  %-50s seq %3d flag %d %s  files %s  starts %r' % (
            name, c.seq, c.flag, 'cloud' if c.cloud_id else 'local', [(n, p.stat().st_size) for n, p in files],
            head[:48]))
    return '\n'.join(lines)
