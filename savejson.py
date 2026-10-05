"""The game's character save, read straight from its in-memory struct (read-only).

Since about 2026-10-03 the game no longer receives an online hero's save as JSON at the character select
screen (server-side change: no client patch).  Once the hero is loaded into the world, though, the game holds
its full FCharacterSaveV1 struct in native memory.  This module

  * finds that struct by its CharacterId (an FString pointing at the hero's GUID text), and
  * writes it out in the game's own save JSON format, walking the struct's live reflection data
    (UScriptStruct CharacterSaveV1), so field order, names and nesting follow the running build.

JSON conventions of the save format (checked against saves the game wrote): property names as keys,
FGameplayTag -> its name ('None' when empty), FDateTime -> ticks, TMap -> object keyed by the key's text,
TArray/TSet -> list, enums -> their short name, floats that hold a whole number -> int.
"""
import struct

import uemem

SAVE_STRUCT = 'CharacterSaveV1'
SERIALIZE_META = {'InternalVersion': 0, 'HardFormat': 'FCharacterSaveV1', 'SoftVersion': 5, 'FormatHash': 1764133460}
FSTRING_ID_LEN = 37                     # 36 GUID characters + NUL


class SaveNotFound(Exception):
    pass


# --------------------------------------------------------------------------- finding the struct
def find_saves(proc, character_id, regions):
    """Addresses of FCharacterSaveV1 structs whose MetaData.CharacterId is character_id.

    regions: [(base, size)] private read-write regions (offline_hero_sync.private_rw_regions).
    Two passes of C-speed bytes.find: the id text (UTF-16, NUL-terminated), then FString headers
    {Data -> that text, Num 37}.  Candidates are checked with the metadata layout afterwards."""
    text = character_id.encode('utf-16-le') + b'\x00\x00'
    bufs = set()
    for a, b in _chunks(proc, regions):
        i = b.find(text)
        while i >= 0:
            bufs.add(a + i)
            i = b.find(text, i + 1)
    if not bufs:
        return []
    out = []
    # FString {Data, Num, Max}: Num 37, Max = the allocation (37 or rounded up; 40 seen on Steam)
    tails = [struct.pack('<ii', FSTRING_ID_LEN, mx) for mx in (40, 37, 48, 38, 39, 64)]
    for a, b in _chunks(proc, regions):
        for tail in tails:
            i = b.find(tail)
            while i >= 0:
                h = i - 8
                if h >= 0 and (a + h) % 8 == 0 and int.from_bytes(b[h:h + 8], 'little') in bufs:
                    out.append(a + h)
                i = b.find(tail, i + 1)
    return sorted(set(out))


def _chunks(proc, regions, chunk=32 << 20):
    for base, size in regions:
        off = 0
        while off < size:
            n = min(size - off, chunk + 16)
            b = proc.read(base + off, n)
            if b:
                yield base + off, b
            off += chunk


# --------------------------------------------------------------------------- struct -> JSON
class Writer:
    def __init__(self, ue, objects=False):
        """objects: object references read as their raw address (game objects, not save structs)."""
        self.ue = ue
        self.objects = objects
        self._enums = {}
        self._order = {}

    def ordered_props(self, ustruct):
        """Props in declaration order (super first), cached."""
        r = self._order.get(ustruct)
        if r is None:
            chains = []
            for s in self.ue.supers(ustruct):
                lst, f = [], self.ue.q(s + uemem.S_CHILDPROPS)
                while f:
                    lst.append(uemem.Prop(self.ue, f))
                    f = self.ue.q(f + uemem.F_NEXT)
                chains.append(lst)
            r = [p for lst in reversed(chains) for p in lst]
            self._order[ustruct] = r
        return r

    def struct(self, ustruct, addr):
        return {p.name: self.value(p, addr + p.offset) for p in self.ordered_props(ustruct)}

    def enum_name(self, uenum, v):
        names = self._enums.get(uenum)
        if names is None:
            names = self._enums[uenum] = self.ue.enum_names(uenum)
        return names.get(v, str(v))

    def value(self, p, a):
        ue, k = self.ue, p.kind
        if k == 'StrProperty':
            return ue.fstring(a)
        if k == 'NameProperty':
            return ue.fname(a)
        if k == 'BoolProperty':
            byte_off, mask = p.bool_mask
            return bool(ue.u8(a + byte_off) & mask)
        if k == 'IntProperty':
            return ue.i32(a)
        if k == 'UInt32Property':
            return ue.u32(a)
        if k == 'Int64Property':
            return ue.i64(a)
        if k == 'UInt64Property':
            return ue.q(a)
        if k == 'Int16Property':
            return struct.unpack('<h', ue.read(a, 2))[0]
        if k == 'UInt16Property':
            return struct.unpack('<H', ue.read(a, 2))[0]
        if k == 'Int8Property':
            return struct.unpack('<b', ue.read(a, 1))[0]
        if k == 'FloatProperty':
            return num(ue.f32(a))
        if k == 'DoubleProperty':
            return num(ue.f64(a))
        if k == 'ByteProperty':
            v = ue.u8(a)
            e = p.enum
            return self.enum_name(e, v) if e else v
        if k == 'EnumProperty':
            v = int.from_bytes(ue.read(a, p.size), 'little', signed=True)
            return self.enum_name(p.enum, v)
        if k == 'StructProperty':
            st = p.struct
            name = ue.objname(st)
            if name == 'GameplayTag':
                return ue.tag(a)
            if name == 'GameplayTagContainer':
                return ue.tag_container(a)
            if name in ('DateTime', 'Timespan'):
                return ue.i64(a)
            if name == 'Guid':
                return '%08X%08X%08X%08X' % struct.unpack('<IIII', ue.read(a, 16))
            return self.struct(st, a)
        if k == 'ArrayProperty':
            inner = p.inner
            return [self.value(inner, e) for e in ue.tarray(a, inner.size)]
        if k == 'SetProperty':
            inner = p.inner
            return [self.value(inner, e + inner.offset) for e in ue.tset(a, p.elem_stride)]
        if k == 'MapProperty':
            kp, vp = p.key, p.value
            out = {}
            for e in ue.tset(a, p.elem_stride):
                key = self.value(kp, e + kp.offset)
                out[key if isinstance(key, str) else str(key)] = self.value(vp, e + vp.offset)
            return out
        if self.objects and k in ('ObjectProperty', 'ClassProperty', 'WeakObjectProperty'):
            return ue.q(a) if k != 'WeakObjectProperty' else None
        if k in ('ObjectProperty', 'SoftObjectProperty', 'ClassProperty', 'SoftClassProperty'):
            raise ValueError('object reference %s in the save struct' % p.name)
        if k == 'TextProperty':
            raise ValueError('FText %s in the save struct' % p.name)
        raise ValueError('unhandled property type %s (%s)' % (k, p.name))


def num(f):
    """Floats the way the save format writes them: whole numbers as ints."""
    if f != f or f in (float('inf'), float('-inf')):
        return 0
    if f == int(f) and abs(f) < 1e15:
        return int(f)
    return f


def save_struct(ue):
    st = ue.find_type(SAVE_STRUCT)
    if not st:
        raise SaveNotFound('the game has no %s type (patched?)' % SAVE_STRUCT)
    return st


def metadata_ok(ue, addr, character_id, online=None):
    """The struct at addr is a plausible FCharacterSaveV1 for character_id (layout from reflection)."""
    w = Writer(ue)
    st = save_struct(ue)
    props = {p.name: p for p in w.ordered_props(st)}
    md = props.get('MetaData')
    if not md or md.offset != 0:
        return False
    mprops = {p.name: p for p in w.ordered_props(md.struct)}
    try:
        if ue.fstring(addr + mprops['CharacterId'].offset) != character_id:
            return False
        level = ue.i32(addr + mprops['Level'].offset)
        power = ue.f32(addr + mprops['PowerLevel'].offset)
        upd = ue.i64(addr + mprops['GameDataUpdated'].offset)
        is_online = w.value(mprops['IsOnline'], addr + mprops['IsOnline'].offset)
    except (MemoryError, KeyError, UnicodeDecodeError):
        return False
    if online is not None and is_online != online:
        return False
    return 1 <= level <= 1000 and 0 < power < 100000 and 6.0e17 < upd < 7.0e17


def read_save(ue, addr):
    """{'SerializeMeta': ..., 'CharacterSaveV1': ...} from the struct at addr."""
    w = Writer(ue)
    return {'SerializeMeta': dict(SERIALIZE_META), 'CharacterSaveV1': w.struct(save_struct(ue), addr)}
