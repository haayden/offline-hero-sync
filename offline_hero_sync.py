"""Offline Hero Sync - keeps an OFFLINE copy of each ONLINE Minecraft Dungeons II hero up to date.

Run it with no arguments (double-click the exe) and it runs hidden in the background: while
the game is running it watches for your online heroes and mirrors each one into its own
offline hero.  One way only: online -> offline.  Log: %LOCALAPPDATA%\\OfflineHeroSync\\autosync.log

What it does
  Online heroes live on the game's servers, so there is no save file for them
  on your PC.  But when the game loads your heroes (character select screen), it
  briefly keeps the server's own copy of each one in memory, in exactly the
  format the game uses for offline save files.  This tool finds that copy by
  reading the game's memory, caches it, gives it its own character id, marks it
  offline, and saves it as an offline hero.  While you play the online hero it
  also reads your current attributes and inventory from the game, so the copy
  follows this session's emeralds, XP and items too.

What it does NOT do
  - It never writes to the game process.  The process handle is opened with
    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ only; no injection, no DLL,
    no hooks (see winproc.py, which has no write function at all).
  - It never sends anything anywhere and never touches the network.
  - It never changes your online hero.  Nothing is ever pushed back to online.
  - The only files it writes in the game's SaveGames folder are the offline
    copies it manages (recorded in state.json).  Every other save is only read.
  - It never writes an offline copy while that offline hero is loaded in-world.
    Progress made on an offline copy IS replaced the next time the online hero
    changes (it is a mirror); the replaced file is backed up first.

Files (default %LOCALAPPDATA%\\OfflineHeroSync, change with --data-dir)
  autosync.log  what the background sync did
  state.json    which offline copy belongs to which online hero
  cache\\        the last server copy seen of each online hero
  out\\          the last built copy of each hero (also made on dry runs)
  backups\\      offline copies saved before they were replaced (newest 20 per hero)

Command line (optional)
  OfflineHeroSync                         background auto-sync (same as --auto)
  OfflineHeroSync --install-autostart     start it at every login (and now)
  OfflineHeroSync --uninstall-autostart   remove that and stop it
  OfflineHeroSync --status | --stop
  OfflineHeroSync --list                  online heroes in game memory (or cached)
  OfflineHeroSync --hero 1                dry run: build the copy into out\\ only
  OfflineHeroSync --hero 1 --write        one-off write
  OfflineHeroSync --hero 1 --watch 60 --write
  OfflineHeroSync --cli                   one-off step-by-step console version
  more: --adopt ONLINE:OFFLINE, --replace-played, --no-live, --quiet, --save-dir, --data-dir, --rescan
"""
import argparse
import copy
import datetime
import hashlib
import json
import math
import os
import random
import re
import shutil
import struct
import sys
import time
import uuid
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from uemem import UE  # noqa: E402
import rebuild  # noqa: E402
import wgs  # noqa: E402
from winproc import Process, GameNotRunning, MEMORY_BASIC_INFORMATION, find_games  # noqa: F401  # noqa: E402

APP = 'Offline Hero Sync'
VERSION = '1.2.0'
XBOX_AUTO = False       # pick the Xbox app saves by itself when there are no Steam saves (on once Xbox is confirmed)

HEADER = b'{"SerializeMeta":{'
MAX_BLOB = 64 << 20
MEM_COMMIT, MEM_PRIVATE, PAGE_READWRITE = 0x1000, 0x20000, 0x04
TICKS_EPOCH = 621355968000000000          # .NET ticks at 1970-01-01
TESTED_SOFT_VERSION = 5                   # FCharacterSaveV1 SoftVersion this release was tested with
SAVE_NAME_RE = re.compile(r'^Character[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.sav$')
KEEP_BACKUPS = 20                         # routine backups kept per hero (played copies are never pruned)

MERCHANT_SLOT = 'SW.ItemSlot.Inventory.VillageMerchant'
POWER_SLOTS = ('SW.ItemSlot.Equipment.MeleeWeapon', 'SW.ItemSlot.Equipment.RangedWeapon',
               'SW.ItemSlot.Equipment.Armor.', 'SW.ItemSlot.Equipment.Artifact.')
KV_SOLD, KV_DISCOUNT = 'SW.KeyValue.MerchantItemSold', 'SW.KeyValue.MerchantDiscount'
# FPowerGeneratorValuesSaveV1 fields that live in the unreflected 0x24 bytes of
# FPowerGeneratorValues, with the layout guess tried first (calibrated at runtime).
HIDDEN_PGV = [('PlayerLevel', 'i', 0x00), ('AreaThreatLevel', 'i', 0x04),
              ('RecommendedThreatLevel', 'i', 0x08), ('ThreatSliderOffset', 'i', 0x0C),
              ('ItemPowerMin', 'f', 0x10), ('ItemPowerMax', 'f', 0x14), ('RNGRoll', 'f', 0x18)]


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------- paths / state
def local_appdata():
    v = os.environ.get('LOCALAPPDATA')
    return Path(v) if v else Path.home() / 'AppData' / 'Local'


def default_save_dir():
    """Steam build: %LOCALAPPDATA%\\Dungeons2\\Saved\\SaveGames.  A packaged (Store) build may keep it
    under %LOCALAPPDATA%\\Packages\\<app>\\LocalCache\\Local instead (untested guess)."""
    la = local_appdata()
    first = la / 'Dungeons2' / 'Saved' / 'SaveGames'
    if first.is_dir():
        return first
    try:
        for p in sorted((la / 'Packages').glob('*/LocalCache/Local/Dungeons2/Saved/SaveGames')):
            if p.is_dir():
                return p
    except OSError:
        pass
    return first


def pick_xbox_store(xbox):
    """The Xbox app / Minecraft Launcher save store to use, or None for the Steam build.
    xbox: True = use it, False = never, None = only when there is no Steam SaveGames folder and XBOX_AUTO is on
    (off in 1.2.0: the Xbox writer is a beta that no Xbox player has confirmed yet, so it needs --xbox)."""
    if xbox is False or (xbox is None and not XBOX_AUTO):
        return None
    dirs = wgs.user_dirs()
    if not dirs:
        return None
    if xbox is None and (local_appdata() / 'Dungeons2' / 'Saved' / 'SaveGames').is_dir():
        return None
    return dirs[0]


class Ctx:
    def __init__(self, save_dir=None, data_dir=None, xbox=None):
        self.data_dir = Path(data_dir) if data_dir else local_appdata() / 'OfflineHeroSync'
        self.wgs = None
        xdir = None if save_dir else pick_xbox_store(xbox)
        if xdir:
            # Xbox app build: its saves live in WGS containers; the tool works on a mirror of them (one .sav per
            # slot, like Steam's SaveGames) and writes its copies back while the game is closed (xbox_sync)
            self.wgs = wgs.Store(xdir)
            self.save_dir = self.data_dir / 'xbox' / xdir.name / 'SaveGames'
        else:
            self.save_dir = Path(save_dir) if save_dir else default_save_dir()
        self.out_dir = self.data_dir / 'out'
        self.backup_dir = self.data_dir / 'backups'
        self.cache_dir = self.data_dir / 'cache'
        self.state_file = self.data_dir / 'state.json'
        self.log_file = self.data_dir / 'autosync.log'

    # ---- Xbox app build ------------------------------------------------------------------------------------------
    def xbox_sync(self, game_running, log=None):
        """Mirror <-> WGS containers.  Every slot is copied into the mirror unless the mirror holds a copy this
        tool wrote that has not reached the game yet; those are written into the containers while the game is
        closed (the game caches its saves while it runs), each container backed up first.
        Returns the slots written into the game."""
        if not self.wgs:
            return []
        st = self.load_state()
        managed = {h['offline_id']: h for h in st['heroes'].values() if h.get('offline_id')}

        def pending(h, path):
            try:
                return path.is_file() and content_hash(json.loads(path.read_text(encoding='utf-8'))) == \
                    h.get('written_hash') and h.get('xbox_written_hash') != h.get('written_hash')
            except (OSError, ValueError):
                return False

        self.save_dir.mkdir(parents=True, exist_ok=True)
        slots = self.wgs.slots()
        for name, (c, files) in slots.items():
            if not files:
                continue
            mirror = self.save_dir / (name + '.sav')
            data = files[0][1].read_bytes()
            try:
                if mirror.read_bytes() == data:
                    continue
            except OSError:
                pass
            oid = name[len('Character'):] if name.startswith('Character') else None
            if oid in managed and pending(managed[oid], mirror):
                continue
            tmp = mirror.with_suffix('.ohs-tmp')
            tmp.write_bytes(data)
            os.replace(tmp, mirror)
        for oid, h in managed.items():                 # a copy deleted in the game: drop it from the mirror too
            mirror = self.save_dir / ('Character%s.sav' % oid)
            if 'Character' + oid not in slots and mirror.is_file() and h.get('xbox_written_hash') and \
                    not pending(h, mirror):
                mirror.unlink()
        written = []
        if not game_running:
            for oid, h in managed.items():
                mirror = self.save_dir / ('Character%s.sav' % oid)
                if not pending(h, mirror):
                    continue
                slot = 'Character' + oid
                bk = self.wgs.backup(slot, self.backup_dir / 'xbox') if slot in slots else None
                self.wgs.write(slot, mirror.read_bytes())
                h['xbox_written_hash'] = h['written_hash']
                written.append(slot)
                if log:
                    log('Xbox: offline copy %s written into the game\'s saves%s' % (
                        oid[:8], ' (previous version backed up to %s)' % bk if bk else ''))
            if written:
                self.save_state(st)
        return written

    # The game only keeps the server copy in memory for a short while after it fetches it
    # (around the character select screen), so every copy seen is cached here as the base
    # for later syncs.
    def cache_blob(self, online_id, save, text):
        """Store a server copy if it is newer than the cached one.  Returns True if stored."""
        p = self.cache_dir / ('%s.json' % online_id)
        new = save['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0)
        try:
            old = json.loads(p.read_text(encoding='utf-8'))['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0)
            if old >= new:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(text, encoding='utf-8', newline='')
        os.replace(tmp, p)
        return True

    def cached_heroes(self):
        """{online id: (0, save, text)} from the cache, same shape as online_heroes()."""
        out = {}
        for p in sorted(self.cache_dir.glob('*.json')) if self.cache_dir.is_dir() else []:
            try:
                text = p.read_text(encoding='utf-8')
                save = json.loads(text)
                md = save['CharacterSaveV1']['MetaData']
                if md.get('IsOnline') is True and md.get('CharacterId') == p.stem:
                    out[p.stem] = (0, save, text)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return out

    def load_state(self):
        try:
            st = json.loads(self.state_file.read_text(encoding='utf-8'))
            st.setdefault('heroes', {})
            return st
        except (OSError, ValueError):
            return {'tool': APP, 'heroes': {}}

    def save_state(self, st):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        st['tool'] = APP
        tmp = self.state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(st, indent=2), encoding='utf-8')
        os.replace(tmp, self.state_file)

    def _global_general(self):
        """The 'General' blob of GlobalSaveDataDefault.sav (bytes stored -1; read only)."""
        try:
            raw = (self.save_dir / 'GlobalSaveDataDefault.sav').read_bytes()
            g = json.loads(bytes((b + 1) & 0xFF for b in raw).decode('utf-8'))
            for blob in g.get('blobs', []):
                if 'activeCharacterId' in blob:
                    return blob
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def global_active_character(self):
        return self._global_general().get('activeCharacterId')

    def deleted_character_ids(self):
        return set(self._global_general().get('deletedCharacterIds') or [])


# --------------------------------------------------------------------------- helpers
def now_ticks():
    return TICKS_EPOCH + int(time.time() * 10_000_000)


def ticks_str(t, fmt='%Y-%m-%d %H:%M'):
    """.NET UTC ticks -> local time string."""
    try:
        return datetime.datetime.fromtimestamp((t - TICKS_EPOCH) / 10_000_000).strftime(fmt)
    except (OverflowError, ValueError, TypeError, OSError):
        return str(t)


def num(v):
    """float32 from memory -> shortest JSON number that round-trips as float32
    (the game writes floats that way: 0.05, 430.146, 1)."""
    if isinstance(v, int):
        return v
    if math.isnan(v) or math.isinf(v):
        return 0
    s = repr(v)
    for p in range(1, 10):
        s = '%.*g' % (p, v)
        if struct.unpack('<f', struct.pack('<f', float(s)))[0] == struct.unpack('<f', struct.pack('<f', v))[0]:
            break
    x = float(s)
    return int(x) if x.is_integer() and abs(x) < 2 ** 53 else x


def new_character_id(save_dir, avoid):
    """Time-based (v1) GUID like the game's own ids, with a random node so no MAC is used.
    Never one that already has a save file or is in `avoid`."""
    while True:
        cid = str(uuid.uuid1(node=random.getrandbits(48) | 0x010000000000))
        if cid not in avoid and not (save_dir / ('Character%s.sav' % cid)).exists():
            return cid


def dumps(obj):
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


def content_hash(save):
    """Hash of a character save ignoring GameDataUpdated, to tell whether the
    offline copy was played since this tool last wrote it."""
    s = copy.deepcopy(save)
    s.get('CharacterSaveV1', {}).get('MetaData', {}).pop('GameDataUpdated', None)
    return hashlib.sha256(json.dumps(s, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def tag_tail(tag, prefix):
    return tag[len(prefix):].replace('.', ' ') if tag and tag.startswith(prefix) else (tag or '?')


def attrs_of(c):
    return {a['AttributeName']: a['CurrentValue'] for a in c.get('Ability', {}).get('Attributes', [])}


def describe(save):
    """One line a player recognises: level, power, skin, location."""
    c = save['CharacterSaveV1']
    md = c['MetaData']
    skin = c.get('Cosmetics', {}).get('Cosmetics', {}).get('SW.Skin', {}).get('TypeTag', '')
    parts = ['level %s' % md.get('Level', '?'), 'power %s' % md.get('PowerLevel', '?')]
    if skin:
        parts.append('%s skin' % tag_tail(skin, 'SW.Skin.'))
    loc = md.get('CurrentLocation')
    if loc and loc != 'None':
        parts.append('last in %s' % tag_tail(loc, 'SW.Area.'))
    return ', '.join(parts)


def short_summary(save):
    c = save['CharacterSaveV1']
    at = attrs_of(c)
    E = c['Inventory']['Entries']
    hero_items = [e for e in E if not e['ItemData']['TargetSlotOverride'].startswith(MERCHANT_SLOT)]
    eq = [e for e in E if e['EquippedSlot'] != 'None']
    qs = c['quest']['Quests']
    return [
        describe(save),
        '%s emeralds, %s echo shards, %s enchantment points' % (
            at.get('Emeralds', 0), at.get('SpringStone', 0), at.get('EnchantmentPoints', 0)),
        '%d items (%d equipped), %d of %d quests completed' % (
            len(hero_items), len(eq), sum(q['State'] == 'Completed' for q in qs), len(qs)),
    ]


# --------------------------------------------------------------------------- blob scan
def private_rw_regions(proc):
    addr, m = 0, MEMORY_BASIC_INFORMATION()
    while proc.query(addr, m):
        base, size = m.BaseAddress or 0, m.RegionSize
        if m.State == MEM_COMMIT and m.Type == MEM_PRIVATE and m.Protect == PAGE_READWRITE:
            yield base, size
        addr = base + size
        if addr >= 0x7FFFFFFFFFFF:
            break


def read_json_at(proc, addr, region_end):
    buf = b''
    while len(buf) < MAX_BLOB:
        n = min(1 << 20, region_end - addr - len(buf))
        if n <= 0:
            break
        b = proc.read(addr + len(buf), n)
        if not b:
            break
        z = b.find(b'\x00')
        if z >= 0:
            buf += b[:z]
            break
        buf += b
    try:
        txt = buf.decode('utf-8')
        obj, end = json.JSONDecoder().raw_decode(txt)
    except (UnicodeDecodeError, ValueError):
        return None, None
    return obj, txt[:end]


def scan_blobs(proc):
    """All complete FCharacterSaveV1 JSON documents in the game's heap."""
    out, chunk, ov = [], 32 << 20, len(HEADER)
    for base, size in private_rw_regions(proc):
        off = 0
        while off < size:
            n = min(size - off, chunk + ov)
            b = proc.read(base + off, n)
            if b:
                i = b.find(HEADER)
                while 0 <= i < chunk:
                    obj, txt = read_json_at(proc, base + off + i, base + size)
                    if isinstance(obj, dict) and obj.get('SerializeMeta', {}).get('HardFormat') == 'FCharacterSaveV1' \
                            and isinstance(obj.get('CharacterSaveV1'), dict) \
                            and isinstance(obj['CharacterSaveV1'].get('MetaData'), dict):
                        out.append((base + off + i, obj, txt))
                    i = b.find(HEADER, i + 1)
            off += chunk
    return out


def online_heroes(blobs):
    """{online character id: (addr, obj, text)} keeping the freshest copy of each."""
    best = {}
    for a, o, t in blobs:
        md = o['CharacterSaveV1']['MetaData']
        cid = md.get('CharacterId')
        if md.get('IsOnline') is not True or not isinstance(cid, str) or not cid:
            continue
        if cid not in best or md.get('GameDataUpdated', 0) > best[cid][1]['CharacterSaveV1']['MetaData'].get(
                'GameDataUpdated', 0):
            best[cid] = (a, o, t)
    return best


# --------------------------------------------------------------------------- live reflection
class Live:
    """In-world reader.  All offsets come from runtime reflection."""

    def __init__(self, ue):
        self.ue = ue
        self.gi = None
        self.notes = []

    def _game_instance(self):
        ue = self.ue
        if self.gi:
            try:
                if ue.is_a(self.gi, 'GameInstance'):
                    return self.gi
            except MemoryError:
                pass
        gis = ue.find_instances('GameInstance')
        self.gi = gis[0] if gis else None
        return self.gi

    def ptr(self, o, field):
        return self.ue.q(o + self.ue.field(o, field).offset)

    def player(self):
        """(PlayerController, Pawn or 0, PlayerState or 0, ActiveCharacterId)"""
        ue = self.ue
        gi = self._game_instance()
        if not gi:
            return None
        lps = ue.tarray(gi + ue.field(gi, 'LocalPlayers').offset, 8)
        if not lps:
            return None
        lp = ue.q(lps[0])
        pc = self.ptr(lp, 'PlayerController') if lp else 0
        if not pc:
            return None
        pawn = self.ptr(pc, 'AcknowledgedPawn') or self.ptr(pc, 'Pawn')
        ps = self.ptr(pc, 'PlayerState')
        cid = ''
        if ps and 'ActiveCharacterId' in ue.oprops(ps):
            cid = ue.fstring(ps + ue.field(ps, 'ActiveCharacterId').offset)
        return pc, pawn, ps, cid

    def active_character(self):
        """(character id selected in the running game or '', in_world)"""
        pl = self.player()
        if not pl:
            return '', False
        pc, pawn, ps, cid = pl
        return cid, bool(pawn) and self.ue.is_a(pawn, 'Character')

    def owned(self, owners, class_names):
        """Objects whose Outer is one of owners and whose class is_a one of class_names."""
        ue = self.ue
        targets = {ue.find_type(c): c for c in class_names}
        targets.pop(None, None)
        res = {c: [] for c in class_names}
        owners = set(o for o in owners if o)
        for o in ue.iter_objects():
            try:
                if ue.outer(o) not in owners:
                    continue
                x = ue.cls(o)
                while x:
                    if x in targets:
                        res[targets[x]].append(o)
                        break
                    x = ue.q(x + 0x40)
            except MemoryError:
                continue
        return res

    # attributes ------------------------------------------------------------
    def attributes(self, sets):
        ue = self.ue
        vals = {}
        for s in sets:
            for n, p in ue.oprops(s).items():
                if p.kind == 'StructProperty' and ue.objname(p.struct) == 'GameplayAttributeData':
                    base, cur = struct.unpack('<ff', ue.read(s + p.offset + 8, 8))
                    vals.setdefault(n, (base, cur))
        return vals

    # inventory -------------------------------------------------------------
    def _t(self, name):
        return self.ue.find_type(name)

    def _f(self, sname, field):
        return self.ue.props(self._t(sname))[field]

    def _effect(self, a):
        ue = self.ue
        gd = self._f('ItemEffect', 'GeneratorData').offset
        return {
            'TypeTag': ue.tag(a + self._f('ItemEffect', 'TypeTag').offset),
            'Intensity': ue.f32(a + self._f('ItemEffect', 'Intensity').offset),
            'EnchantmentPointsInvested': ue.u8(a + self._f('ItemEffect', 'EnchantmentPointsInvested').offset),
            'GeneratorParentTemplate': ue.tag(a + gd + self._f('EffectGeneratorData', 'GeneratorParentTemplate').offset),
        }

    def _effects(self, arr_addr):
        ue = self.ue
        return [self._effect(e) for e in ue.tarray(arr_addr, ue.struct_size(self._t('ItemEffect')))]

    def _tags(self, container_addr):
        return self.ue.tag_container(container_addr + self._f('GameplayTagContainer', 'GameplayTags').offset)

    def _entry(self, a):
        ue = self.ue
        F = self._f
        idp = a + F('InventoryEntry', 'ItemData').offset
        gen = idp + F('ItemData', 'GeneratorData').offset
        pgv = gen + F('ItemGeneratorData', 'PowerGeneratorValues').offset
        prog = idp + F('ItemData', 'ItemProgression').offset
        batches = []
        for b in ue.tarray(idp + F('ItemData', 'Effects').offset, ue.struct_size(self._t('ItemEffectBatch'))):
            batches.append({'TypeTag': ue.tag(b + F('ItemEffectBatch', 'TypeTag').offset),
                            'Effects': self._effects(b + F('ItemEffectBatch', 'EffectsInThisBatch').offset)})
        levels = []
        for lv in ue.tarray(prog + F('ItemProgressionData', 'ItemLevels').offset,
                            ue.struct_size(self._t('ItemLevelEffectsCollection'))):
            levels.append({'Effects': self._effects(lv + F('ItemLevelEffectsCollection', 'LevelEffects').offset),
                           'Tags': self._tags(lv + F('ItemLevelEffectsCollection', 'LevelTags').offset)})
        meta = {}
        for kv in ue.tarray(a + F('InventoryEntry', 'MetaData').offset, ue.struct_size(self._t('KeyValue'))):
            t = ue.tag(kv + F('KeyValue', 'TypeTag').offset)
            meta[t] = {'Int': ue.i32(kv + F('KeyValue', 'Int').offset),
                       'Float': ue.f32(kv + F('KeyValue', 'float').offset),
                       'Bool': bool(ue.u8(kv + F('KeyValue', 'bool').offset))}
        return {
            'uid': ue.q(idp + F('ItemData', 'SessionUID').offset),
            'TypeTag': ue.tag(idp + F('ItemData', 'TypeTag').offset),
            'RarityTag': ue.tag(idp + F('ItemData', 'RarityTag').offset),
            'Batches': batches,
            'EffectRerolls': ue.u8(idp + F('ItemData', 'EffectRerolls').offset),
            'Seed': ue.u32(gen),                       # unreflected: calibrated against the blob
            'PGVRaw': ue.read(pgv, 0x24),              # unreflected: calibrated against the blob
            'ItemPower': ue.f32(pgv + F('PowerGeneratorValues', 'ItemPower').offset),
            'ItemPowerOriginal': ue.f32(pgv + F('PowerGeneratorValues', 'ItemPowerOriginal').offset),
            'CurrentLevel': ue.i32(prog + F('ItemProgressionData', 'CurrentLevel').offset),
            'CurrentXP': ue.f32(prog + F('ItemProgressionData', 'CurrentXP').offset),
            'Levels': levels,
            'DynamicPropertyTags': self._tags(idp + F('ItemData', 'DynamicPropertyTags').offset),
            'TargetSlotOverride': ue.tag(idp + F('ItemData', 'TargetSlotOverride').offset),
            'PickupTimestamp': ue.i64(idp + F('ItemData', 'PickupTimestamp').offset),
            'StackCount': ue.i32(a + F('InventoryEntry', 'StackCount').offset),
            'EquippedSlot': ue.tag(a + F('InventoryEntry', 'EquippedSlot').offset),
            'Meta': meta,
        }

    def inventory(self, comps):
        """All FInventoryEntry of the richest InventoryManagerComponent, deduped by SessionUID."""
        ue = self.ue
        best = []
        for c in comps:
            rep = c + ue.field(c, 'ReplicatedItems').offset
            items = rep + self._f('SlotEntryContainerReplicated', 'Items').offset
            entries, seen = [], set()
            for slot in ue.tarray(items, ue.struct_size(self._t('SlotEntry'))):
                arr = slot + self._f('SlotEntry', 'ItemInventoryEntries').offset
                for e in ue.tarray(arr, ue.struct_size(self._t('InventoryEntry'))):
                    d = self._entry(e)
                    key = d['uid'] or (d['Seed'], d['TypeTag'])
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append(d)
            if len(entries) > len(best):
                best = entries
        return best

    def read(self, online_id):
        """-> dict(attrs, inventory) or None with a reason in self.notes."""
        self.notes = []
        pl = self.player()
        if not pl:
            self.notes.append('no local player')
            return None
        pc, pawn, ps, cid = pl
        if cid != online_id:
            self.notes.append('this hero is not the one loaded in-world')
            return None
        if not pawn or not self.ue.is_a(pawn, 'Character'):
            self.notes.append('hero not loaded in-world (menu)')
            return None
        key = (pawn, pc, ps)
        if getattr(self, '_owned_key', None) != key:
            # walking every object is the slow part; the components live as long as the pawn does
            self._owned = self.owned([pawn, pc, ps], ['AttributeSet', 'InventoryManagerComponent'])
            self._owned_key = key
        got = self._owned
        attrs = self.attributes(got['AttributeSet'])
        inv = self.inventory(got['InventoryManagerComponent'])
        if not attrs:
            self.notes.append('no attribute sets found on the hero')
        if not inv:
            self.notes.append('no inventory entries found')
        return {'attrs': attrs, 'inventory': inv, 'pawn': self.ue.fullname(pawn)}


# --------------------------------------------------------------------------- overlay
def calibrate(blob_by_seed, live_items):
    """Verify the seed offset and find the unreflected PowerGeneratorValues layout."""
    matched = [(l, blob_by_seed[l['Seed']]) for l in live_items if l['Seed'] in blob_by_seed]
    need = max(3, int(0.3 * len(live_items)))
    if len(matched) < min(need, len(blob_by_seed)):
        return None, '%d/%d live items matched the server copy by GenesisRandomSeed' % (len(matched), len(live_items))
    layout = {}
    for name, kind, guess in HIDDEN_PGV:
        def hits(off):
            n = 0
            for l, b in matched:
                want = b['ItemData']['GeneratorData']['PowerGeneratorValues'][name]
                got = struct.unpack_from('<' + kind, l['PGVRaw'], off)[0]
                if kind == 'f':
                    n += struct.pack('<f', got) == struct.pack('<f', want)
                else:
                    n += got == want
            return n
        if hits(guess) >= 0.95 * len(matched):
            layout[name] = guess
            continue
        best = max(range(0, 0x24, 4), key=hits)
        if hits(best) >= 0.95 * len(matched):
            layout[name] = best
    return {'matched': len(matched), 'layout': layout}, None


def effect_out(e, prev):
    p = prev or {}
    return {'TypeTag': e['TypeTag'], 'Intensity': num(e['Intensity']), 'Quality': p.get('Quality', 0),
            'EnchantmentPointsInvested': e['EnchantmentPointsInvested'],
            'GeneratorData': {'GeneratorParentTemplate': e['GeneratorParentTemplate'],
                              'Locked': p.get('GeneratorData', {}).get('Locked', False)}}


def live_to_entry(l, base, calib, level):
    """Save-format entry from a live item; unreflected fields from the matched blob entry."""
    ent = copy.deepcopy(base) if base else None
    old_batches = {}
    if base:
        for b in base['ItemData']['Effects']:
            for e in b['EffectsInThisBatch']:
                old_batches[(b['TypeTag'], e['TypeTag'])] = e
    effects = [{'TypeTag': b['TypeTag'],
                'EffectsInThisBatch': [effect_out(e, old_batches.get((b['TypeTag'], e['TypeTag'])))
                                       for e in b['Effects']]} for b in l['Batches']]
    old_levels = base['ItemData']['ItemProgression']['ItemLevels'] if base else []
    levels = []
    for i, lv in enumerate(l['Levels']):
        olds = {e['TypeTag']: e for e in (old_levels[i]['LevelEffects'] if i < len(old_levels) else [])}
        levels.append({'LevelEffects': [effect_out(e, olds.get(e['TypeTag'])) for e in lv['Effects']],
                       'LevelTags': lv['Tags']})
    if base:
        pgv = dict(base['ItemData']['GeneratorData']['PowerGeneratorValues'])
    else:
        lay = (calib or {}).get('layout', {})
        ip = num(l['ItemPower'])
        pgv = {'PlayerLevel': level, 'AreaThreatLevel': 1, 'RecommendedThreatLevel': 1, 'ThreatSliderOffset': 0,
               'ItemPowerMin': ip, 'ItemPowerMax': ip, 'RNGRoll': 0}
        for name, kind, _ in HIDDEN_PGV:
            if name in lay:
                v = struct.unpack_from('<' + kind, l['PGVRaw'], lay[name])[0]
                pgv[name] = num(v) if kind == 'f' else v
        pgv = {k: pgv[k] for k in ('PlayerLevel', 'AreaThreatLevel', 'RecommendedThreatLevel', 'ThreatSliderOffset',
                                   'ItemPowerMin', 'ItemPowerMax', 'RNGRoll')}
    pgv['ItemPower'] = num(l['ItemPower'])
    pgv['ItemPowerOriginal'] = num(l['ItemPowerOriginal'])
    sold = l['Meta'].get(KV_SOLD)
    disc = l['Meta'].get(KV_DISCOUNT)
    item = {
        'TypeTag': l['TypeTag'], 'RarityTag': l['RarityTag'], 'Effects': effects,
        'ItemProgression': {'CurrentLevel': l['CurrentLevel'], 'CurrentXP': num(l['CurrentXP']), 'ItemLevels': levels},
        'GeneratorData': {'GenesisRandomSeed': l['Seed'], 'PowerGeneratorValues': pgv},
        'DynamicPropertyTags': l['DynamicPropertyTags'], 'TargetSlotOverride': l['TargetSlotOverride'],
        'PickupTimestamp': l['PickupTimestamp'], 'EffectRerolls': l['EffectRerolls'],
    }
    out = {'ItemData': item, 'StackCount': l['StackCount'], 'EquippedSlot': l['EquippedSlot'],
           'MerchantItemSold': sold['Bool'] if sold else (ent['MerchantItemSold'] if ent else False),
           'MerchantDiscount': num(disc['Float']) if disc else (ent['MerchantDiscount'] if ent else 0)}
    if ent:   # keep any keys the game added that we do not model, in the original order
        for k in ent:
            if k not in out:
                out[k] = ent[k]
        out = {k: out[k] for k in list(ent) + [k for k in out if k not in ent]}
        out['ItemData'] = {k: item.get(k, ent['ItemData'].get(k)) for k in
                           list(ent['ItemData']) + [k for k in item if k not in ent['ItemData']]}
    return out


def apply_live(save, live, report):
    c = save['CharacterSaveV1']
    # attributes: the save's attribute names are the AttributeSet property names
    changed = []
    for a in c['Ability']['Attributes']:
        v = live['attrs'].get(a['AttributeName'])
        if v is not None:
            nv = num(v[1])
            if nv != a['CurrentValue']:
                changed.append('%s %s->%s' % (a['AttributeName'], a['CurrentValue'], nv))
            a['CurrentValue'] = nv
    report['live_attr_changes'] = changed
    lvl = live['attrs'].get('Level')
    if lvl:
        c['MetaData']['Level'] = int(round(lvl[1]))
    inv = live['inventory']
    if not inv:
        report['live_inventory'] = 'skipped (no live entries)'
        return
    entries = c['Inventory']['Entries']
    by_seed = {e['ItemData']['GeneratorData']['GenesisRandomSeed']: e for e in entries}
    calib, why = calibrate(by_seed, inv)
    if not calib:
        report['live_inventory'] = 'skipped (%s)' % why
        return
    live_has_merchant = any(l['TargetSlotOverride'].startswith(MERCHANT_SLOT) for l in inv)
    level = c['MetaData']['Level']
    new_entries, added, removed = [], 0, 0
    live_by_seed = {l['Seed']: l for l in inv}
    for e in entries:                                   # keep blob order
        s = e['ItemData']['GeneratorData']['GenesisRandomSeed']
        if s in live_by_seed:
            new_entries.append(live_to_entry(live_by_seed.pop(s), e, calib, level))
        elif e['ItemData']['TargetSlotOverride'].startswith(MERCHANT_SLOT) and not live_has_merchant:
            new_entries.append(e)                       # merchant stock is not replicated: keep
        else:
            removed += 1                                # sold / salvaged in-world since the blob
    for l in inv:                                       # picked up since the blob
        if l['Seed'] in live_by_seed:
            new_entries.append(live_to_entry(l, None, calib, level))
            added += 1
    c['Inventory']['Entries'] = new_entries
    disc = c['LootProgression']['DiscoveredLoot']
    for t in dict.fromkeys(l['TypeTag'] for l in inv):
        if t not in disc and t != 'None':
            disc.append(t)
    pw = [e['ItemData']['GeneratorData']['PowerGeneratorValues']['ItemPower'] for e in new_entries
          if e['EquippedSlot'].startswith(POWER_SLOTS)]
    if pw:
        c['MetaData']['PowerLevel'] = math.floor(sum(pw) / len(pw))
    report['live_inventory'] = 'merged: %d matched, %d added, %d removed; hidden fields calibrated: %s' % (
        calib['matched'], added, removed, ','.join(calib['layout']) or 'none')


# --------------------------------------------------------------------------- build / verify
def shape_diff(a, b, path='', out=None):
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            if k not in b:
                out.append('only in output: ' + path + '/' + k)
            else:
                shape_diff(a[k], b[k], path + '/' + k, out)
        for k in b:
            if k not in a:
                out.append('only in reference: ' + path + '/' + k)
    elif isinstance(a, list) and isinstance(b, list):
        if a and b:
            def merged(lst):
                m = {}
                for x in lst:
                    if isinstance(x, dict):
                        for k, v in x.items():
                            m.setdefault(k, v)
                return m or lst[0]
            shape_diff(merged(a), merged(b), path + '[]', out)
    elif type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append('type differs: %s (%s vs %s)' % (path, type(a).__name__, type(b).__name__))
    return out


DATA_KEYS = ('/Cosmetics/Cosmetics/', '/Achievements/')   # map keys that are data, not schema


def find_template(ctx, exclude):
    """An offline hero the player made in the game itself, to compare structure with."""
    for f in sorted(ctx.save_dir.glob('Character*.sav')):
        try:
            o = json.loads(f.read_text(encoding='utf-8'))
            md = o['CharacterSaveV1']['MetaData']
            if md.get('IsOnline') is False and md.get('CharacterId') not in exclude:
                return f, o
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None, None


def verify(ctx, save, text, source, live_applied, exclude):
    problems, notes = [], []
    if json.loads(text) != save:
        problems.append('JSON round-trip mismatch')
    md = save['CharacterSaveV1']['MetaData']
    if md['IsOnline'] is not False:
        problems.append('IsOnline is not false')
    if md['CharacterId'] == source['CharacterSaveV1']['MetaData']['CharacterId']:
        problems.append('offline id equals online id')
    sv = save.get('SerializeMeta', {}).get('SoftVersion')
    if sv != TESTED_SOFT_VERSION:
        notes.append('save format version %s (this release was tested with %s)' % (sv, TESTED_SOFT_VERSION))
    if not live_applied:
        a = copy.deepcopy(save)
        b = copy.deepcopy(source)
        for x in (a, b):
            for k in ('CharacterId', 'IsOnline', 'GameDataUpdated', 'IsGuest'):
                x['CharacterSaveV1']['MetaData'].pop(k, None)
        if a != b:
            problems.append('output differs from the server copy beyond id/IsOnline/timestamp')
        else:
            notes.append('identical to the server copy except CharacterId/IsOnline')
    else:
        real = [d for d in shape_diff(save, source) if not any(k in d for k in DATA_KEYS)]
        notes.append('structure vs server copy: %s' % ('key-for-key match' if not real else '; '.join(real)))
        problems.extend(real)
    tf, tmpl = find_template(ctx, exclude)
    if tmpl:
        diffs = shape_diff(save, tmpl)
        real = [d for d in diffs if not any(k in d for k in DATA_KEYS)]
        notes.append('structure vs %s: %s' % (tf.name, 'key-for-key match' if not real else '; '.join(real)))
        if real and tmpl.get('SerializeMeta', {}).get('SoftVersion') == sv:
            problems.extend(real)
    return problems, notes


def summarize(save, report):
    c = save['CharacterSaveV1']
    md = c['MetaData']
    at = attrs_of(c)
    E = c['Inventory']['Entries']
    merch = [e for e in E if e['ItemData']['TargetSlotOverride'].startswith(MERCHANT_SLOT)]
    eq = [e for e in E if e['EquippedSlot'] != 'None']
    lines = [
        'source     : %s' % report['source'],
        'live       : %s' % report.get('live', 'not used'),
        'hero       : level %s, power %s, location %s, difficulty %s' % (
            md['Level'], md['PowerLevel'], md['CurrentLocation'], md['CurrentDifficulty']),
        'currencies : emeralds %s, echo shards (SpringStone) %s, enchant points %s, XP %s' % (
            at.get('Emeralds'), at.get('SpringStone'), at.get('EnchantmentPoints'), at.get('XP')),
        'town       : merchant lvl %s (refresh charges %s), blacksmith %s, enchantsmith %s' % (
            at.get('VillageMerchantUpgradeLevel'), at.get('VillageMerchantRefreshCharges'),
            at.get('OldBlacksmithUpgradeLevel'), at.get('EnchantsmithUpgradeLevel')),
        'inventory  : %d entries (%d hero items + %d merchant stock)' % (len(E), len(E) - len(merch), len(merch)),
        'cosmetics  : %s' % ', '.join('%s=%s' % (k, v['TypeTag']) for k, v in c['Cosmetics']['Cosmetics'].items()),
        'progress   : %d quests (%d completed), %d loot discovered, %d minecart stations, %d dungeon doors' % (
            len(c['quest']['Quests']), sum(q['State'] == 'Completed' for q in c['quest']['Quests']),
            len(c['LootProgression']['DiscoveredLoot']),
            len(c['WorldExploration']['DiscoveredMinecartStationTags']),
            len(c['WorldExploration']['DiscoveredDungeonDoors'])),
        'equipped   :',
    ]
    for e in eq:
        it = e['ItemData']
        enchants = [x['TypeTag'].split('.')[-1] for b in it['Effects'] for x in b['EffectsInThisBatch']]
        lines.append('   %-40s %-38s %-8s P%-4s %s' % (
            e['EquippedSlot'].replace('SW.ItemSlot.Equipment.', ''), it['TypeTag'].replace('SW.Item.', ''),
            it['RarityTag'].replace('SW.Rarity.', ''), it['GeneratorData']['PowerGeneratorValues']['ItemPower'],
            ','.join(enchants)))
    return '\n'.join(lines)


def build_offline(ctx, online_id, offline_id, source, source_text, live=None, extra_exclude=()):
    """source (+ optional live reading) -> (save, text, report, problems, notes).  Pure: writes nothing."""
    report = {}
    save = copy.deepcopy(source)
    live_applied = False
    if live:
        apply_live(save, live, report)
        live_applied = True
        report['live'] = 'in-world %s; attrs: %s; inventory: %s' % (
            live['pawn'].split('.')[-1], ', '.join(report['live_attr_changes']) or 'no change',
            report['live_inventory'])
    md = save['CharacterSaveV1']['MetaData']
    src_updated = source['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0)
    md['CharacterId'] = offline_id
    md['IsOnline'] = False
    md['IsGuest'] = False
    md['GameDataUpdated'] = src_updated if not live_applied else max(now_ticks(), src_updated + 1)
    text = dumps(save)
    if source_text and not live_applied:
        # keep the server's exact bytes (number formatting etc.); only the id and flag change
        t2 = source_text.replace('"CharacterId":"%s"' % online_id, '"CharacterId":"%s"' % offline_id, 1)
        t2 = t2.replace('"IsOnline":true', '"IsOnline":false', 1)
        t2 = t2.replace('"IsGuest":true', '"IsGuest":false', 1)
        try:
            if json.loads(t2) == save:
                text = t2
        except ValueError:
            pass
    problems, notes = verify(ctx, save, text, source, live_applied,
                             {offline_id, online_id} | set(extra_exclude))
    return save, text, report, problems, notes


# --------------------------------------------------------------------------- the offline file
def target_path(ctx, offline_id):
    name = 'Character%s.sav' % offline_id
    if not SAVE_NAME_RE.match(name):
        raise SyncError('bad offline id %r' % offline_id)
    return ctx.save_dir / name


def offline_status(ctx, hero):
    """State of this hero's offline copy in SaveGames:
    ('none'|'ours'|'played'|'foreign'|'unreadable', parsed save or None)."""
    oid = hero.get('offline_id')
    if not oid:
        return 'none', None
    target = target_path(ctx, oid)
    if not target.exists():
        return 'none', None
    try:
        cur = json.loads(target.read_text(encoding='utf-8'))
        cid = cur['CharacterSaveV1']['MetaData']['CharacterId']
    except (OSError, ValueError, KeyError, TypeError):
        return 'unreadable', None
    # only a file this tool wrote, at this exact path, with the content hash recorded at the time,
    # counts as ours; anything else is left alone
    if not hero.get('written') or not hero.get('written_hash') or cid != oid \
            or Path(hero.get('written_path', '')).name != target.name:
        return 'foreign', cur
    if content_hash(cur) == hero['written_hash']:
        return 'ours', cur
    return 'played', cur


def write_offline(ctx, hero, online_id, save, text, replace_played=False, in_world_id=None):
    """Write the offline hero into SaveGames.  Returns a status line; raises SyncError on refusal."""
    offline_id = hero['offline_id']
    target = target_path(ctx, offline_id)
    if online_id in target.name or offline_id == online_id:
        raise SyncError('refusing to write the online character file')
    if save['CharacterSaveV1']['MetaData'].get('IsOnline') is not False \
            or save['CharacterSaveV1']['MetaData'].get('CharacterId') != offline_id:
        raise SyncError('internal check failed: output is not the offline copy')
    if not ctx.save_dir.is_dir():
        raise SyncError('SaveGames folder not found: %s (use --save-dir)' % ctx.save_dir)
    if in_world_id == offline_id:
        raise SyncError('you are playing the offline copy right now. Go back to the character select screen '
                        'and try again')
    status, cur = offline_status(ctx, hero)
    if status == 'foreign':
        raise SyncError('%s exists but was not made by this tool - leaving it alone' % target.name)
    if status == 'unreadable':
        raise SyncError('%s exists but cannot be read - leaving it alone' % target.name)
    if status == 'played' and not replace_played:
        raise SyncError('the offline copy was played since the last sync. Replacing it would lose that '
                        'offline progress (use --replace-played to replace it anyway; it is backed up first)')
    if status in ('ours', 'played'):
        if status == 'ours' and content_hash(cur) == content_hash(save):
            return 'unchanged; %s left as is' % target.name
        ctx.backup_dir.mkdir(parents=True, exist_ok=True)
        tag = '.played' if status == 'played' else ''
        day = time.strftime('%Y%m%d')
        if not tag and not any(ctx.backup_dir.glob('%s.%s-*.daily.sav' % (target.stem, day))):
            tag = '.daily'   # the first backup of each day is kept for good: auto-sync rewrites every 30 s
        bk = ctx.backup_dir / ('%s.%s%s.sav' % (target.stem, time.strftime('%Y%m%d-%H%M%S'), tag))
        shutil.copy2(target, bk)
        hero['last_backup'] = str(bk)
        # keep the newest KEEP_BACKUPS of each kind; .played ones hold real offline progress, so they are
        # counted separately and never pushed out by routine ones; .daily ones are never pruned
        allb = sorted(p for p in ctx.backup_dir.glob(target.stem + '.*.sav') if not p.name.endswith('.daily.sav'))
        for kind in (False, True):
            group = [p for p in allb if p.name.endswith('.played.sav') == kind]
            for o in group[:-KEEP_BACKUPS]:
                o.unlink()
    else:
        hero.pop('last_backup', None)
    tmp = target.with_name(target.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    os.replace(tmp, target)
    hero['written'] = True
    hero['written_path'] = str(target)
    hero['written_hash'] = content_hash(save)
    hero['written_at'] = datetime.datetime.now().isoformat(timespec='seconds')
    return 'WRITTEN to %s' % target


# --------------------------------------------------------------------------- session
class Syncer:
    """Reads every running game process (a stale second instance can linger next to the real one)."""

    def __init__(self, ctx, no_live=False, rescan=False):
        self.ctx = ctx
        self.no_live = no_live
        self.rescan = rescan
        self.procs = {}          # pid -> winproc.Process (read-only)
        self.lives = {}          # pid -> Live
        self.ue_errors = {}      # pid -> why the in-world reader is unavailable
        self.ue_errors_at = {}   # pid -> when it last failed
        self.rebuilders = {}     # pid -> rebuild.Rebuilder

    def attach(self):
        found = dict(find_games())
        for pid in list(self.procs):
            if pid not in found or not self.procs[pid].alive():
                self.procs.pop(pid).close()
                self.lives.pop(pid, None)
                self.ue_errors.pop(pid, None)
                self.ue_errors_at.pop(pid, None)
        err = None
        for pid, exe in found.items():
            if pid not in self.procs:
                try:
                    self.procs[pid] = Process(pid, exe)
                except GameNotRunning as e:
                    err = e
        if not self.procs:
            raise err or GameNotRunning('Minecraft Dungeons II is not running')

    def detach(self):
        for p in self.procs.values():
            p.close()
        self.procs, self.lives, self.ue_errors, self.ue_errors_at = {}, {}, {}, {}

    def live_readers(self):
        if self.no_live:
            return []
        out = []
        for pid, proc in self.procs.items():
            failed_at = self.ue_errors_at.get(pid, 0)
            if pid not in self.lives and time.time() - failed_at > 60:   # retry: early in startup the
                try:                                                    # engine tables are not ready yet
                    self.lives[pid] = Live(UE(proc, rescan=self.rescan))
                    self.ue_errors.pop(pid, None)
                except Exception as e:   # UEUnavailable / MemoryError / GameNotRunning: optional reader
                    self.ue_errors[pid] = str(e)
                    self.ue_errors_at[pid] = time.time()
            if pid in self.lives:
                out.append(self.lives[pid])
        return out

    def active_in_game(self):
        """(character id selected in the running game, loaded in-world?) from live memory if
        possible, otherwise the last selected id from GlobalSaveDataDefault.sav."""
        best = None
        for lv in self.live_readers():
            try:
                cid, in_world = lv.active_character()
            except Exception:            # optional reading; any engine surprise just means "unknown"
                continue
            if cid and in_world:
                return cid, True
            if cid and not best:
                best = (cid, False)
        return best or (self.ctx.global_active_character(), False)

    def in_world_id(self):
        cid, in_world = self.active_in_game()
        return cid if in_world else None

    def scan(self):
        """Online heroes in game memory right now (each one is also cached)."""
        self.attach()
        blobs = []
        for p in list(self.procs.values()):
            try:
                blobs += scan_blobs(p)
            except MemoryError:
                continue
        heroes = online_heroes(blobs)
        for hid, (_, save, text) in heroes.items():
            try:
                self.ctx.cache_blob(hid, save, text)
            except OSError:
                pass
        return heroes

    def newest_base(self, online_id, hero, blob, cached):
        """The newest earlier full copy of this hero (server copy in memory, cached server copy, or this tool's
        last output), as an online save, or None."""
        cands = [b[1] for b in (blob, cached) if b]
        prev = hero.get('last_output')
        if prev and Path(prev).exists():
            try:
                o = json.loads(Path(prev).read_text(encoding='utf-8'))
                o['CharacterSaveV1']['MetaData']['CharacterId'] = online_id
                o['CharacterSaveV1']['MetaData']['IsOnline'] = True
                cands.append(o)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if not cands:
            return None
        return max(cands, key=lambda o: o['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0))

    def rebuild(self, online_id, base):
        """-> (full online save rebuilt from the game, notes, '') or (None, [], why)."""
        if self.no_live:
            return None, [], 'in-world reading turned off'
        self.live_readers()
        why = []
        for pid, lv in list(self.lives.items()):
            rb = self.rebuilders.get(pid)
            try:
                if rb is None or rb.live is not lv:
                    rb = self.rebuilders[pid] = rebuild.Rebuilder(lv, sys.modules[__name__])
                save, notes = rb.build(online_id, base)
                return save, notes, ''
            except rebuild.RebuildError as e:
                why.append(str(e))
            except Exception as e:     # optional reading; any engine surprise falls back to the older paths
                if rb is not None:
                    rb._key = None
                why.append('failed: %r' % (e,))
        return None, [], '; '.join(why) or 'game memory not readable'

    def read_live(self, online_id):
        if self.no_live:
            return None, 'turned off'
        readers = self.live_readers()
        if not readers:
            return None, 'unavailable (%s)' % '; '.join(sorted(set(self.ue_errors.values())) or ['no game'])
        notes = []
        for lv in readers:
            try:
                r = lv.read(online_id)
            except Exception as e:       # optional reading; fall back to the server copy
                lv._owned_key = None
                notes.append('read failed: %r' % (e,))
                continue
            if r:
                return r, ''
            notes += lv.notes
        return None, '; '.join(dict.fromkeys(notes))

    def build(self, online_id, heroes, st):
        """-> dict with save/text/report/problems/notes/out_path for one online hero.  Writes only to out\\."""
        hero = st['heroes'].setdefault(online_id, {})
        deleted = self.ctx.deleted_character_ids()
        known = {h.get('offline_id') for h in st['heroes'].values()} | set(heroes) | deleted
        if hero.get('offline_id') in deleted:
            # the player deleted the offline copy in the game; the game would hide that id, so use a new one
            hero.setdefault('retired_ids', []).append(hero['offline_id'])
            for k in ('offline_id', 'written', 'written_hash', 'written_path', 'written_at'):
                hero.pop(k, None)
        if not hero.get('offline_id'):
            hero['offline_id'] = new_character_id(self.ctx.save_dir, known)
        offline_id = hero['offline_id']
        if offline_id == online_id or offline_id in heroes:
            raise SyncError('offline id collides with an online id')
        blob = heroes.get(online_id)
        cached = None if blob else self.ctx.cached_heroes().get(online_id)
        source_text = None
        # 1.1: in the world the whole hero is rebuilt from the game (since ~2026-10-03 the server no longer
        # sends the save itself); the newest earlier copy only fills in what the game does not have
        rebuilt, rnotes, rwhy = self.rebuild(online_id, self.newest_base(online_id, hero, blob, cached))
        if rebuilt:
            live = None
            source = rebuilt
            src = 'rebuilt from the game in-world' + (' (%s)' % '; '.join(rnotes) if rnotes else '')
        elif blob:
            addr, source, source_text = blob
            src = 'server copy in game memory, saved %s' % ticks_str(
                source['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0))
        elif cached:
            addr, source, source_text = cached
            src = 'server copy cached by this tool, saved %s (not in game memory right now)' % ticks_str(
                source['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0))
        else:
            prev = hero.get('last_output')
            if not prev or not Path(prev).exists():
                raise SyncError('this hero is not loaded right now. Load into the world with it (the game no '
                                'longer gets the full hero at the character select screen), then try again')
            source = json.loads(Path(prev).read_text(encoding='utf-8'))
            source['CharacterSaveV1']['MetaData']['CharacterId'] = online_id
            source['CharacterSaveV1']['MetaData']['IsOnline'] = True
            src = 'previous sync %s (no server copy in memory right now)' % Path(prev).name
        if not rebuilt:
            live, why = self.read_live(online_id)
            if rwhy:
                why = '%s; rebuild: %s' % (why, rwhy) if why else 'rebuild: ' + rwhy
        else:
            why = 'not needed, the whole hero was rebuilt from the game'
        others = {h.get('offline_id') for k, h in st['heroes'].items() if k != online_id} - {None}
        try:
            save, text, report, problems, notes = build_offline(self.ctx, online_id, offline_id, source,
                                                                source_text, live, extra_exclude=others)
        except Exception as e:
            if not live:
                raise
            why, live = 'in-world reading could not be merged: %r' % (e,), None
            save, text, report, problems, notes = build_offline(self.ctx, online_id, offline_id, source,
                                                                source_text, None, extra_exclude=others)
        if live and problems:
            # the in-world overlay produced something that does not check out: use the server copy alone
            why, live = 'in-world reading skipped, it failed the checks: %s' % '; '.join(problems), None
            save, text, report, problems, notes = build_offline(self.ctx, online_id, offline_id, source,
                                                                source_text, None, extra_exclude=others)
        report['source'] = src
        if not live:
            report['live'] = 'not used (%s)' % (why or 'not in-world')
        self.ctx.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.ctx.out_dir / ('Character%s.sav' % offline_id)
        out_path.write_text(text, encoding='utf-8', newline='')
        hero['last_output'] = str(out_path)
        hero['last_sync'] = datetime.datetime.now().isoformat(timespec='seconds')
        hero['source_game_data_updated'] = source['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0)
        hero['description'] = describe(save)
        return {'hero': hero, 'online_id': online_id, 'offline_id': offline_id, 'save': save, 'text': text,
                'report': report, 'problems': problems, 'notes': notes, 'out_path': out_path,
                'live_used': bool(live)}


# --------------------------------------------------------------------------- guided mode
class Quit(Exception):
    pass


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:            # no console input left (piped or closed): stop instead of looping
        raise Quit()


def yes(prompt):
    return ask(prompt + ' [y/N]: ').strip().lower() in ('y', 'yes')


def hero_menu(heroes, active):
    ids = sorted(heroes, key=lambda k: -heroes[k][1]['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0))
    for i, k in enumerate(ids, 1):
        md = heroes[k][1]['CharacterSaveV1']['MetaData']
        print('  %d) %s%s' % (i, describe(heroes[k][1]), '   <- selected in game' if k == active else ''))
        print('     server save %s, id %s' % (ticks_str(md.get('GameDataUpdated', 0)), k[:8]))
    return ids


def guided(ctx, args):
    print('%s %s' % (APP, VERSION))
    print('Makes an offline copy of an online Minecraft Dungeons II hero.')
    print('Your online hero is not changed and nothing is sent anywhere.')
    print()
    print('1. Start the game.')
    print('2. Go to the character select screen (or load into the world with your online hero).')
    ask('3. Press Enter here when you are there... ')
    s = Syncer(ctx, no_live=args.no_live, rescan=args.rescan)
    while True:
        try:
            print('\nReading the game\'s memory (read only, takes a few seconds)...')
            heroes = s.scan()
        except GameNotRunning as e:
            print('%s. Start the game, then press Enter to try again (or close this window).' % e)
            ask('')
            continue
        if heroes:
            break
        print('No online hero found in the game\'s memory yet.')
        print('Open the character select screen so the game loads your heroes, then press Enter to look again.')
        ask('')
        s.detach()

    st = ctx.load_state()
    active = s.active_in_game()[0]
    print('\nFound %d online hero%s:' % (len(heroes), '' if len(heroes) == 1 else 'es'))
    ids = hero_menu(heroes, active)
    if len(ids) == 1:
        online_id = ids[0]
    else:
        while True:
            a = ask('Which one? (1-%d): ' % len(ids)).strip()
            if a.isdigit() and 1 <= int(a) <= len(ids):
                online_id = ids[int(a) - 1]
                break
            if a == '':
                print('Nothing chosen, nothing written.')
                return 0

    r = s.build(online_id, heroes, st)
    ctx.save_state(st)
    print('\nOffline copy ready:')
    for line in short_summary(r['save']):
        print('  ' + line)
    print('  (%s)' % ('includes what you have done this session' if r['live_used']
                      else 'as of the server\'s last save of this hero'))
    if r['problems']:
        print('\nSomething looks wrong, so nothing was written:')
        for p in r['problems']:
            print('  - ' + p)
        print('The built file is in %s if you want to look at it.' % r['out_path'])
        return 1

    hero = r['hero']
    status, cur = offline_status(ctx, hero)
    if status == 'foreign':
        print('\n%s already exists and was not made by this tool, so it will not be touched.'
              % target_path(ctx, hero['offline_id']).name)
        return 1
    if status == 'unreadable':
        print('\nThe existing offline copy cannot be read, so it will not be touched.')
        return 1
    if not ctx.save_dir.is_dir():
        print('\nCould not find the game\'s SaveGames folder (looked in %s).' % ctx.save_dir)
        print('Run it from a command prompt with --save-dir "<your SaveGames folder>".')
        return 1
    if status == 'ours' and content_hash(cur) == content_hash(r['save']):
        print('\nYour offline copy already matches your online hero. Nothing to do.')
        return 0
    replace_played = False
    print()
    if status == 'none':
        print('This adds a NEW offline hero to:')
        print('  %s' % ctx.save_dir)
        prompt = 'Create the offline hero?'
    else:
        print('You already have an offline copy of this hero: %s.' % describe(cur))
        if status == 'played':
            print('It has been played since the last sync. Updating it REPLACES that offline progress')
            print('with your online hero. The current offline copy is backed up first, to:')
            print('  %s' % ctx.backup_dir)
            replace_played = True
            prompt = 'Replace the offline copy with your online hero?'
        else:
            print('It will be updated to match your online hero (the old file is backed up first).')
            prompt = 'Update the offline copy?'
    if not yes(prompt):
        print('Nothing written.')
        return 0
    try:
        msg = write_offline(ctx, hero, online_id, r['save'], r['text'], replace_played=replace_played,
                            in_world_id=s.in_world_id())
    except SyncError as e:
        print('Not written: %s.' % e)
        return 1
    ctx.save_state(st)
    print('\nDone. %s' % msg)
    print()
    print('Restart the game so it loads the file, then pick the offline hero')
    print('(%s) from the character list.' % describe(r['save']))
    print('Progress you make on the offline copy stays offline. It never goes back to your online hero.')
    return 0


# --------------------------------------------------------------------------- command line mode
def pick_hero(spec, heroes, st):
    """heroes: what to number (in memory, or the cache when nothing is in memory)."""
    ids = sorted(heroes, key=lambda k: -heroes[k][1]['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0))
    if spec is None:
        if len(ids) == 1:
            return ids[0]
        if not ids:
            raise SyncError('no online hero in game memory right now and none cached yet. Open the character '
                            'select screen and run --list again')
        raise SyncError('%d online heroes found; choose one with --hero (see --list)' % len(ids))
    if spec.isdigit() and len(spec) < 4:
        n = int(spec)
        if 1 <= n <= len(ids):
            return ids[n - 1]
        if not ids:
            raise SyncError('--hero %s: no online hero in game memory right now and none cached yet. Open the '
                            'character select screen and run --list again' % spec)
        raise SyncError('--hero %s: only %d hero%s (see --list)' % (spec, len(ids), '' if len(ids) == 1 else 'es'))
    hits = [k for k in set(ids) | set(st['heroes']) if k.startswith(spec.lower())]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SyncError('--hero %s: no such hero in game memory, in the cache or in state.json' % spec)
    raise SyncError('--hero %s matches %d heroes; give more of the id' % (spec, len(hits)))


def cli_once(s, ctx, args, sticky):
    heroes = s.scan()
    st = ctx.load_state()
    cached = ctx.cached_heroes()
    pickable = heroes or cached
    if args.list:
        active = s.active_in_game()[0]
        if not pickable:
            print('No online hero in game memory right now, and none cached yet.')
            print('The game only keeps it for a short while after the character select screen loads,')
            print('so go back to the character select screen and run --list again.')
            return True
        if not heroes:
            print('No online hero in game memory right now. Cached copies from earlier:')
        hero_menu(pickable, active)
        return True
    online_id = sticky.get('id') or pick_hero(args.hero, pickable, st)
    sticky['id'] = online_id
    r = s.build(online_id, heroes, st)
    status = 'dry run (nothing written to the SaveGames folder)'
    if args.write:
        if r['problems']:
            status = 'NOT written: verification failed'
        else:
            try:
                status = write_offline(ctx, r['hero'], online_id, r['save'], r['text'],
                                       replace_played=args.replace_played, in_world_id=s.in_world_id())
            except SyncError as e:
                status = 'NOT written: %s' % e
    ctx.save_state(st)
    if not args.quiet or args.write or r['problems']:
        print('=' * 78)
        print(time.strftime('%H:%M:%S'), 'online %s -> offline %s' % (online_id, r['offline_id']))
        print(summarize(r['save'], r['report']))
        for n in r['notes']:
            print('check      : ' + n)
        for p in r['problems']:
            print('PROBLEM    : ' + p)
        print('output     : %s (%d bytes)' % (r['out_path'], len(r['text'])))
        print('status     : %s' % status)
    return not r['problems'] and not status.startswith('NOT')


def cli(ctx, args):
    s = Syncer(ctx, no_live=args.no_live, rescan=args.rescan)
    sticky = {}
    if not args.watch:
        try:
            return 0 if cli_once(s, ctx, args, sticky) else 1
        except (SyncError, GameNotRunning) as e:
            print('ERROR:', e)
            return 2
    print('watching every %gs (%s); Ctrl+C to stop' % (args.watch, 'WRITE' if args.write else 'dry run'))
    while True:
        try:
            cli_once(s, ctx, args, sticky)
        except SyncError as e:
            print(time.strftime('%H:%M:%S'), 'skipped:', e)
        except GameNotRunning as e:
            print(time.strftime('%H:%M:%S'), 'waiting:', e)
            s.detach()
        except MemoryError as e:             # game exited / map change mid-read
            print(time.strftime('%H:%M:%S'), 'read failed, retrying next cycle:', e)
            s.detach()
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0


# --------------------------------------------------------------------------- auto mode (the default)
MUTEX_NAME = 'Local\\OfflineHeroSync.running'
STOP_EVENT_NAME = 'Local\\OfflineHeroSync.stop'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'OfflineHeroSync'
IDLE_SLEEP = 10          # s between checks while the game is not running (a process list, ~1 ms)
TICK = 30                # s between ticks while the game runs
BURST_TICK = 5           # s between memory scans right after the game starts or the menu/hero changes,
BURST_LEN = 120          #   for this long: the server copy only sits in memory for a short while
INWORLD_SCAN = 300       # s between memory scans while in-world (the server copy arrives at the menu)
HEARTBEAT = 600
LOG_MAX = 1 << 20


class AutoLog:
    def __init__(self, ctx, echo=False):
        self.path = ctx.log_file
        self.echo = echo
        self.last = {}

    def __call__(self, msg, key=None):
        """key: log only when the message for this key changes (no repeats every tick)."""
        if key is not None:
            if self.last.get(key) == msg:
                return
            self.last[key] = msg
        line = '%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LOG_MAX:
                os.replace(self.path, self.path.with_name(self.path.name + '.1'))
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line)
        except OSError:
            pass
        if self.echo:
            try:
                print(line, end='', flush=True)
            except Exception:
                pass


def _k32():
    import ctypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, res, args in (('CreateMutexW', ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]),
                            ('OpenMutexW', ctypes.c_void_p, [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]),
                            ('CreateEventW', ctypes.c_void_p,
                             [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]),
                            ('OpenEventW', ctypes.c_void_p, [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]),
                            ('SetEvent', ctypes.c_int, [ctypes.c_void_p]),
                            ('ResetEvent', ctypes.c_int, [ctypes.c_void_p]),
                            ('CloseHandle', ctypes.c_int, [ctypes.c_void_p]),
                            ('WaitForSingleObject', ctypes.c_uint32, [ctypes.c_void_p, ctypes.c_uint32])):
        f = getattr(k, name)
        f.restype, f.argtypes = res, args
    return ctypes, k


def acquire_instance():
    """Named mutex so only one auto-sync runs per user session.  None if one already runs."""
    ctypes, k = _k32()
    h = k.CreateMutexW(None, False, MUTEX_NAME)
    if h and ctypes.get_last_error() == 183:          # ERROR_ALREADY_EXISTS
        k.CloseHandle(h)
        return None
    return h


def instance_running():
    ctypes, k = _k32()
    h = k.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
    if h:
        k.CloseHandle(h)
        return True
    return False


def request_stop():
    ctypes, k = _k32()
    h = k.OpenEventW(0x0002, False, STOP_EVENT_NAME)  # EVENT_MODIFY_STATE
    if not h:
        return False
    k.SetEvent(h)
    k.CloseHandle(h)
    return True


class AutoSync:
    """Background one-way mirror: online hero -> its offline copy, whenever the online side changes."""

    def __init__(self, ctx, no_live=False, rescan=False, echo=False):
        self.ctx = ctx
        self.s = Syncer(ctx, no_live=no_live, rescan=rescan)
        self.log = AutoLog(ctx, echo)
        self.pids = set()
        self.last_state = None
        self.burst_until = 0
        self.next_scan = 0
        self.next_live = 0
        self.next_heartbeat = time.time() + HEARTBEAT
        self.scans = []
        self.tray = None                 # tray.Tray when run from run_auto (None in tests / --no-tray)
        self.session_writes = []         # descriptions of copies written while the game ran
        self.last_copy = ''              # "level 100, Silver, 02:51" for the tray
        self.noted = set()               # problems already shown as a notification

    # ---- tray ----------------------------------------------------------------------------------------------------
    def status(self, text):
        if self.tray:
            self.tray.set_status(text + ('\nLast copy: ' + self.last_copy if self.last_copy else ''))

    def notify(self, title, text, key=None, warning=False):
        if not self.tray or (key and key in self.noted):
            return
        if key:
            self.noted.add(key)
        self.tray.notify(title, text, warning)

    def run(self):
        _, k = _k32()
        ev = k.CreateEventW(None, True, False, STOP_EVENT_NAME)
        k.ResetEvent(ev)
        self.log('started %s %s (pid %d); SaveGames: %s' % (APP, VERSION, os.getpid(), self.ctx.save_dir))
        while True:
            try:
                delay = self.tick()
            except Exception as e:            # never die on one bad tick
                self.log('tick failed: %r' % (e,), key='tickerr')
                self.s.detach()
                self.pids = set()
                delay = TICK
            if k.WaitForSingleObject(ev, int(delay * 1000)) == 0:
                self.log('stop requested, exiting')
                return 0

    def describe_state(self, cid, in_world, offline_ids):
        if not self.s.lives:
            return 'in-world reading unavailable (%s); menu/world state unknown' % (
                '; '.join(sorted(set(self.s.ue_errors.values()))) or 'no reader')
        if not in_world:
            return 'at the menu' + (' (hero %s selected)' % cid[:8] if cid else '')
        if cid in offline_ids:
            return 'playing the offline copy %s (it will not be written while you play it)' % cid[:8]
        if cid and (self.ctx.save_dir / ('Character%s.sav' % cid)).exists():
            return 'playing offline hero %s' % cid[:8]
        return 'playing online hero %s' % cid[:8]

    def xbox(self, game_running, force=False):
        if not self.ctx.wgs or (not force and time.time() < getattr(self, 'next_xbox', 0)):
            return []
        self.next_xbox = time.time() + 60
        try:
            return self.ctx.xbox_sync(game_running, self.log)
        except Exception as e:            # never die on the save store; say why
            self.log('Xbox saves: %r' % (e,), key='xbox')
            self.notify('Offline Hero Sync', 'Could not read or write the Xbox app saves: %s' % e, key='xbox',
                        warning=True)
        return []

    def tick(self):
        now = time.time()
        if not find_games():
            if self.pids:
                self.log('game closed')
                self.s.detach()
                self.pids, self.last_state = set(), None
                written = self.xbox(False, force=True)
                if self.session_writes:
                    if self.ctx.wgs:
                        if written:
                            self.notify('Offline copy saved', 'Your offline copy (%s) is in the game\'s saves now. '
                                        'Start the game to play it.' % self.session_writes[-1])
                    else:
                        self.notify('Offline copy up to date', 'Your offline copy (%s) is up to date. You\'ll see '
                                    'the changes next time you start the game.' % self.session_writes[-1])
                self.session_writes = []
            self.xbox(False)
            self.log('waiting for the game to start', key='game')
            self.status('Waiting for the game to start')
            return IDLE_SLEEP
        self.s.attach()
        pids = set(self.s.procs)
        if pids != self.pids:
            self.log('game found (process %s)' % ', '.join(str(p) for p in sorted(pids)), key='game')
            self.pids = pids
            self.burst_until, self.next_scan = now + BURST_LEN, 0
            self.xbox(True, force=True)
        cid, in_world = self.s.active_in_game()
        st = self.ctx.load_state()
        offline_ids = {h.get('offline_id') for h in st['heroes'].values()} - {None}
        if (cid, in_world) != self.last_state:
            self.log('state: ' + self.describe_state(cid, in_world, offline_ids))
            self.last_state = (cid, in_world)
            self.burst_until, self.next_scan = now + BURST_LEN, 0
        if not self.s.lives:
            self.status('Game running, waiting until it can read it')
        elif in_world and cid in offline_ids:
            self.status('You\'re playing the offline copy. It updates after you leave it')
        elif in_world and cid and not (self.ctx.save_dir / ('Character%s.sav' % cid)).exists():
            self.status('Copying your online hero while you play')
        elif in_world:
            self.status('Playing an offline hero. Play your online hero to copy it')
        else:
            self.status('Game running: load into the world with your online hero')

        heroes = {}
        if now >= self.next_scan:
            w0, c0 = time.perf_counter(), time.process_time()
            heroes = self.s.scan()                        # also caches every server copy it sees
            self.scans.append((time.perf_counter() - w0, time.process_time() - c0))
            self.next_scan = now + (BURST_TICK if now < self.burst_until else (INWORLD_SCAN if in_world else TICK))
            if heroes:
                self.log('server copy in memory: ' + '; '.join(
                    '%s %s, saved %s' % (h[:8], describe(v[1]), ticks_str(
                        v[1]['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0))) for h, v in heroes.items()),
                    key='seen')

        synced = set()
        for hid, (_, save, _) in self.ctx.cached_heroes().items():   # newer server copy than last sync?
            upd = save['CharacterSaveV1']['MetaData'].get('GameDataUpdated', 0)
            if upd != st['heroes'].get(hid, {}).get('synced_source_updated'):
                self.sync(hid, heroes, cid, in_world, 'server save %s' % ticks_str(upd), upd)
                synced.add(hid)

        if in_world and cid and cid not in offline_ids and cid not in synced and self.s.lives \
                and now >= self.next_live:
            self.next_live = now + TICK
            if not (self.ctx.save_dir / ('Character%s.sav' % cid)).exists():   # online (offline heroes have a file)
                self.sync(cid, heroes, cid, in_world, 'game (in-world)', None)

        if now >= self.next_heartbeat and self.scans:
            n = len(self.scans)
            self.log('heartbeat: %d memory scans in the last %d min, avg %.1f s wall / %.1f s CPU each' % (
                n, HEARTBEAT // 60, sum(w for w, _ in self.scans) / n, sum(c for _, c in self.scans) / n))
            self.scans, self.next_heartbeat = [], now + HEARTBEAT
        return BURST_TICK if time.time() < self.burst_until else TICK

    def sync(self, hid, heroes, cid, in_world, why, upd):
        st = self.ctx.load_state()
        hero = st['heroes'].get(hid, {})
        oid = hero.get('offline_id')
        playing_copy = (in_world and oid and cid == oid) or \
            (not self.s.lives and oid and self.ctx.global_active_character() == oid)
        if playing_copy:
            self.log('hero %s: not synced, you are playing its offline copy right now. It syncs after you '
                     'leave it.' % hid[:8], key='skip-' + hid)
            return
        try:
            r = self.s.build(hid, heroes, st)
        except SyncError as e:
            self.log('hero %s: %s' % (hid[:8], e), key='build-' + hid)
            return
        hero, oid = r['hero'], r['offline_id']
        if r['problems']:
            self.ctx.save_state(st)
            self.log('hero %s: built copy failed its checks, not written: %s' % (hid[:8], '; '.join(r['problems'])),
                     key='prob-' + hid)
            self.notify('Offline Hero Sync', 'Could not make a safe copy of your hero, so nothing was written. '
                        'Right-click the emerald by the clock > Open the log for details.', key='prob-' + hid,
                        warning=True)
            return
        new_hash = content_hash(r['save'])
        if new_hash == hero.get('last_source_hash'):
            if upd is not None:
                hero['synced_source_updated'] = upd
            self.ctx.save_state(st)
            return
        cid2, in_world2 = self.s.active_in_game()           # look again right before writing
        if in_world2 and cid2 == oid:
            self.ctx.save_state(st)
            self.log('hero %s: not synced, you are playing its offline copy right now.' % hid[:8], key='skip-' + hid)
            return
        is_new = not target_path(self.ctx, oid).exists()
        try:
            msg = write_offline(self.ctx, hero, hid, r['save'], r['text'], replace_played=True,
                                in_world_id=cid2 if in_world2 else None)
        except SyncError as e:
            self.ctx.save_state(st)
            self.log('hero %s: not written: %s' % (hid[:8], e), key='w-' + hid)
            return
        hero['last_source_hash'] = new_hash
        if upd is not None:
            hero['synced_source_updated'] = upd
        self.ctx.save_state(st)
        self.log.last.pop('skip-' + hid, None)
        if msg.startswith('WRITTEN'):
            self.log('hero %s (%s): offline copy %s updated from the %s (%s)%s' % (
                hid[:8], describe(r['save']), oid[:8], why,
                "with this session's changes" if r['live_used'] else 'server copy',
                '; previous file backed up' if hero.get('last_backup') else ''))
            short = short_describe(r['save'])
            self.last_copy = '%s, %s' % (short, time.strftime('%H:%M'))
            self.session_writes.append(short)
            if is_new:
                self.notify('Offline copy made', 'Your %s hero has an offline copy now. %s' % (
                    short, 'Close the game and it\'s saved into the game; start it again to see it.'
                    if self.ctx.wgs else 'Restart the game to see it in your hero list.'))


def short_describe(save):
    """'level 100, Silver' for notifications."""
    c = save['CharacterSaveV1']
    skin = c.get('Cosmetics', {}).get('Cosmetics', {}).get('SW.Skin', {}).get('TypeTag', '')
    return 'level %s%s' % (c['MetaData'].get('Level', '?'), ', ' + tag_tail(skin, 'SW.Skin.') if skin else '')


def autostart_installed():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(on, argv_extra=()):
    """Only the registry entry (the tray's checkbox): never starts or stops a process."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command(argv_extra)[0])
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def start_tray(ctx, argv_extra=()):
    try:
        import tray
    except Exception:
        return None
    st = ctx.load_state()
    first = not st.get('autostart_offered')
    if first:
        # first start: only point at the menu's "Start with Windows" (never added without the player ticking it)
        st['autostart_offered'] = True
        ctx.save_state(st)
    elif autostart_installed():
        # already starting with Windows, maybe from an older folder: point it at this copy
        try:
            set_autostart(True, argv_extra)
        except OSError:
            pass
    t = tray.Tray(APP, icon_path=str(Path(__file__).resolve().with_name('icon.ico')), on_quit=request_stop,
                  autostart=(autostart_installed, lambda on: set_autostart(on, argv_extra)),
                  log_path=str(ctx.log_file), backup_dir=str(ctx.backup_dir)).start()
    t.notify('Offline Hero Sync is running', 'Load into the world with your online hero to make its offline copy. '
             'It runs from the emerald by the clock.%s'
             % (' Right-click it for "Start with Windows".' if first and not autostart_installed() else ''))
    return t


def run_auto(ctx, no_live=False, rescan=False, no_tray=False, argv_extra=()):
    if ctx.wgs:
        AutoLog(ctx)('saves: Xbox app / Minecraft Launcher build (%s); copies are written into the game while '
                     'it is closed' % ctx.wgs.dir)
    echo = sys.stdout is not None and hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
    log = AutoLog(ctx, echo)
    h = acquire_instance()
    if not h:
        # an older (or the same) version is running: every version since 1.0.2 quits on the stop event, so the copy
        # that was started last takes over and updating is just starting the new one
        request_stop()
        for _ in range(60):
            time.sleep(0.5)
            h = acquire_instance()
            if h:
                log('took over from the copy that was already running')
                break
        else:
            log('another copy is already running and did not stop; this one exits')
            return 0
    a = AutoSync(ctx, no_live=no_live, rescan=rescan, echo=echo)
    if not no_tray:
        try:
            a.tray = start_tray(ctx, argv_extra)
        except Exception as e:            # the tray is a nicety; the sync runs without it
            log('tray icon unavailable: %r' % (e,))
    try:
        return a.run()
    finally:
        if a.tray:
            a.tray.stop()


# --------------------------------------------------------------------------- adopt / autostart / status
def adopt(ctx, spec):
    """Take over an existing offline hero as the offline copy of an online hero."""
    online_id, _, offline_id = spec.partition(':')
    online_id, offline_id = online_id.strip().lower(), offline_id.strip().lower()
    if not SAVE_NAME_RE.match('Character%s.sav' % online_id):
        raise SyncError('bad online id %r (expected ONLINE-ID:OFFLINE-ID)' % online_id)
    target = target_path(ctx, offline_id)
    if online_id == offline_id:
        raise SyncError('online and offline id are the same')
    try:
        cur = json.loads(target.read_text(encoding='utf-8'))
        md = cur['CharacterSaveV1']['MetaData']
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SyncError('cannot read %s: %r' % (target, e))
    if md.get('CharacterId') != offline_id or md.get('IsOnline') is not False:
        raise SyncError('%s is not an offline hero with id %s' % (target.name, offline_id))
    st = ctx.load_state()
    for k, h in st['heroes'].items():
        if k != online_id and h.get('offline_id') == offline_id:
            raise SyncError('%s is already the offline copy of %s' % (offline_id, k))
    hero = st['heroes'].setdefault(online_id, {})
    if hero.get('offline_id') not in (None, offline_id) and hero.get('written'):
        raise SyncError('online hero %s already has offline copy %s' % (online_id, hero['offline_id']))
    hero.update(offline_id=offline_id, written=True, written_path=str(target), written_hash=content_hash(cur),
                written_at=datetime.datetime.now().isoformat(timespec='seconds'),
                adopted=datetime.datetime.now().isoformat(timespec='seconds'))
    ctx.save_state(st)
    return 'adopted %s (%s) as the offline copy of online hero %s' % (target.name, describe(cur), online_id)


def autostart_command(argv_extra=()):
    if getattr(sys, 'frozen', False):
        parts = [sys.executable]
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name('pythonw.exe')
        parts = [str(pyw if pyw.exists() else exe), str(Path(__file__).resolve())]
    parts += ['--auto'] + list(argv_extra)
    import subprocess
    return subprocess.list2cmdline(parts), parts


def install_autostart(argv_extra=()):
    import subprocess
    import winreg
    cmd, parts = autostart_command(argv_extra)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
    lines = ['Autostart installed: HKCU\\%s\\%s = %s' % (RUN_KEY, RUN_VALUE, cmd)]
    if instance_running():
        lines.append('Auto-sync is already running.')
    else:
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        subprocess.Popen(parts, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lines.append('Auto-sync started in the background.')
    return '\n'.join(lines)


def uninstall_autostart():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
        msg = 'Autostart removed.'
    except FileNotFoundError:
        msg = 'Autostart was not installed.'
    if request_stop():
        msg += ' Stopped the running auto-sync.'
    return msg


def status_text(ctx):
    import winreg
    lines = ['auto-sync : %s' % ('running' if instance_running() else 'not running')]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            lines.append('autostart : %s' % winreg.QueryValueEx(k, RUN_VALUE)[0])
    except OSError:
        lines.append('autostart : not installed')
    lines.append('game      : %s' % (', '.join('%s pid %d' % (e, p) for p, e in find_games()) or 'not running'))
    st = ctx.load_state()
    for hid, h in st['heroes'].items():
        lines.append('hero      : online %s -> offline %s, last written %s%s' % (
            hid[:8], (h.get('offline_id') or '?')[:8], h.get('written_at', 'never'),
            ', %s' % h['description'] if h.get('description') else ''))
    lines.append('saves     : %s' % ('Xbox app build, %s (mirror %s)' % (ctx.wgs.dir, ctx.save_dir) if ctx.wgs
                                     else ctx.save_dir))
    lines.append('log       : %s' % ctx.log_file)
    try:
        tail = ctx.log_file.read_text(encoding='utf-8').splitlines()[-12:]
        lines += ['  ' + t for t in tail]
    except OSError:
        pass
    return '\n'.join(lines)


AUTO_OPTS = ('--auto', '--save-dir', '--data-dir', '--no-live', '--rescan', '--xbox', '--steam', '--no-tray')


def wants_auto(argv):
    """No options, or only --auto and options the background sync understands."""
    i = 0
    while i < len(argv):
        name = argv[i].split('=', 1)[0]
        if name not in AUTO_OPTS:
            return False
        if name in ('--save-dir', '--data-dir') and '=' not in argv[i]:
            i += 1
        i += 1
    return True


def setup_console(new_window):
    """The exe has no console of its own (so the background sync stays hidden).  For command-line
    use, borrow the console it was started from (or keep a pipe/file it was given), or open a
    console window.  Returns True when a new console window was opened."""
    if not getattr(sys, 'frozen', False):
        return False
    if not new_window and sys.stdout is not None:
        try:
            sys.stdout.fileno()
            return False                      # output already goes to a pipe or file
        except (OSError, ValueError, AttributeError):
            pass
    import ctypes
    k32 = ctypes.windll.kernel32
    opened = False
    if new_window:
        k32.FreeConsole()
    if new_window or not k32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
        k32.AllocConsole()
        opened = True
    enc = 'cp%d' % (k32.GetConsoleOutputCP() or 437)
    try:
        sys.stdout = open('CONOUT$', 'w', encoding=enc, errors='replace', buffering=1)
        sys.stderr = sys.stdout
        sys.stdin = open('CONIN$', 'r', encoding=enc, errors='replace')
    except OSError:
        pass
    return opened


def build_parser():
    ap = argparse.ArgumentParser(
        prog='OfflineHeroSync',
        description='Keeps an offline copy of each online Minecraft Dungeons II hero in sync with the online '
                    'hero. Run with no options to start the background auto-sync.')
    ap.add_argument('--auto', action='store_true', help='run the background auto-sync (the default)')
    ap.add_argument('--install-autostart', action='store_true',
                    help='start the auto-sync at login (and start it now)')
    ap.add_argument('--uninstall-autostart', action='store_true', help='remove the login autostart and stop it')
    ap.add_argument('--status', action='store_true', help='is it running, which heroes, last log lines')
    ap.add_argument('--stop', action='store_true', help='stop the running auto-sync')
    ap.add_argument('--adopt', metavar='ONLINE_ID:OFFLINE_ID',
                    help='use an existing offline hero as the offline copy of an online hero')
    ap.add_argument('--cli', action='store_true', help='one-off step-by-step console version')
    ap.add_argument('--list', action='store_true', help='list the online heroes in game memory (or cached)')
    ap.add_argument('--hero', metavar='N|ID', help='which hero: number from --list, or the start of its id')
    ap.add_argument('--dry-run', action='store_true', help='build the copy into the out folder only (default)')
    ap.add_argument('--write', action='store_true', help='write the offline hero into the SaveGames folder')
    ap.add_argument('--watch', type=float, metavar='N', help='re-sync every N seconds (dry run unless --write)')
    ap.add_argument('--replace-played', action='store_true',
                    help='with --write: also replace an offline copy that was played since the last sync '
                         '(it is backed up first)')
    ap.add_argument('--no-live', action='store_true', help='use only the server copy, skip the in-world reading')
    ap.add_argument('--quiet', action='store_true', help='in watch dry runs, print only problems')
    ap.add_argument('--save-dir', help='the game\'s SaveGames folder (default: %%LOCALAPPDATA%%\\Dungeons2\\Saved\\SaveGames)')
    ap.add_argument('--data-dir', help='where state, cache, backups and the log go (default: %%LOCALAPPDATA%%\\OfflineHeroSync)')
    ap.add_argument('--rescan', action='store_true',
                    help='if a game patch moved the engine tables, search for them (slow); '
                         'only needed for the in-world reading')
    ap.add_argument('--xbox', action='store_true',
                    help='use the Xbox app / Minecraft Launcher saves (default: only when there is no Steam '
                         'SaveGames folder)')
    ap.add_argument('--steam', action='store_true', help='use the Steam saves even if Xbox app saves exist')
    ap.add_argument('--no-tray', action='store_true', help='background sync without the tray icon and notifications')
    ap.add_argument('--xbox-info', action='store_true',
                    help='list the Xbox app saves this tool sees (read-only) and exit')
    ap.add_argument('--version', action='version', version='%s %s' % (APP, VERSION))
    return ap


def xbox_choice(args):
    return True if getattr(args, 'xbox', False) else False if getattr(args, 'steam', False) else None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if sys.platform != 'win32':
        print('This tool only works on Windows.')
        return 2
    if wants_auto(argv):
        args = build_parser().parse_args(argv)
        extra = []
        for k in ('save_dir', 'data_dir'):
            if getattr(args, k):
                extra += ['--' + k.replace('_', '-'), str(Path(getattr(args, k)).resolve())]
        extra += ['--xbox'] if args.xbox else ['--steam'] if args.steam else []
        return run_auto(Ctx(args.save_dir, args.data_dir, xbox_choice(args)), no_live=args.no_live,
                        rescan=args.rescan, no_tray=args.no_tray, argv_extra=extra)
    opened = setup_console(new_window='--cli' in argv)
    try:
        return run(argv)
    finally:
        if opened and '--cli' not in argv:
            try:
                input('\nPress Enter to close.')
            except (EOFError, KeyboardInterrupt, RuntimeError):
                pass


def run(argv):
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:                   # --help / --version / bad option
        return e.code or 0
    if args.xbox_info:
        dirs = wgs.user_dirs()
        if not dirs:
            print('No Xbox app / Minecraft Launcher saves found (%s).' % (
                local_appdata() / 'Packages' / wgs.PACKAGE / 'SystemAppData' / 'wgs'))
        for d in dirs:
            try:
                print(wgs.describe(d))
            except Exception as e:
                print('%s: could not read: %r' % (d, e))
        return 0
    ctx = Ctx(args.save_dir, args.data_dir, xbox_choice(args))
    extra = []
    for k in ('save_dir', 'data_dir'):
        if getattr(args, k):
            extra += ['--' + k.replace('_', '-'), str(Path(getattr(args, k)).resolve())]
    extra += ['--xbox'] if args.xbox else ['--steam'] if args.steam else []
    if ctx.wgs:
        try:
            ctx.xbox_sync(bool(find_games()))
        except Exception as e:
            print('Xbox saves could not be read: %r' % (e,))
    try:
        if args.adopt:
            print(adopt(ctx, args.adopt))
            return 0
        if args.install_autostart:
            print(install_autostart(extra))
            return 0
        if args.uninstall_autostart:
            print(uninstall_autostart())
            return 0
        if args.stop:
            print('Stop sent.' if request_stop() else 'Auto-sync is not running.')
            return 0
        if args.status:
            print(status_text(ctx))
            return 0
    except SyncError as e:
        print('ERROR:', e)
        return 2
    if args.list or args.hero or args.write or args.watch or args.dry_run:
        return cli(ctx, args)
    try:
        rc = guided(ctx, args)
    except (KeyboardInterrupt, Quit):
        print('\nStopped.')
        return 1
    except (SyncError, GameNotRunning, MemoryError) as e:
        print('\nERROR: %s' % e)
        rc = 2
    except Exception as e:                    # keep the window open so the message can be read
        print('\nUnexpected error: %r' % (e,))
        print('Please report it on the mod page with this message.')
        rc = 3
    print()
    try:
        ask('Press Enter to close.')
    except Quit:
        pass
    return rc


if __name__ == '__main__':
    sys.exit(main())
