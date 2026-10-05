"""Read-only UE 5.6 memory reflection for Minecraft Dungeons II.

Everything here goes through ReadProcessMemory on a handle opened with
PROCESS_QUERY_INFORMATION | PROCESS_VM_READ only (no write access is even
requested), so nothing in this module can modify the game.

Property offsets are resolved at runtime by walking the live UStruct's
ChildProperties FField chain, so struct layout changes from patches are picked
up automatically.  Only the two global RVAs below (GUObjectArray, FNamePool)
are build specific; they are validated on attach.  If they are stale (a game
patch moved them) the caller can ask for a slow scan of the image, or just
skip the live overlay; the server-copy path does not need this module.
"""
import struct
import time


class UEUnavailable(Exception):
    """The engine globals could not be found in this game build."""


# Steam build of 2026-10-01 (game version at the time of release).
RVA_OBJOBJECTS = 0xbf35a80      # FChunkedFixedUObjectArray (GUObjectArray.ObjObjects)
RVA_NAMEPOOL = 0xbe51ec0        # FNamePool; FNameEntryAllocator blocks at +0x10
OBJ_ITEM_STRIDE = 0x18
CHUNK = 65536
# FNamePool block 0: entries 'None' and 'ByteProperty' (FNameEntryHeader = wide:1, probe hash:5, len:10)
NAMEPOOL_BLOCK0_SIG = b'\x1e\x01None\x10\x03ByteProperty'

# UObject
O_CLASS, O_NAME, O_OUTER = 0x10, 0x18, 0x20
# UStruct (shipping: FStructBaseChain present)
S_SUPER, S_CHILDPROPS, S_SIZE = 0x40, 0x50, 0x58
# FField / FProperty (UE 5.6, tagged-pointer FFieldVariant)
F_CLASS, F_NEXT, F_NAME = 0x08, 0x18, 0x20
P_ARRAYDIM, P_ELEMSIZE, P_OFFSET = 0x30, 0x34, 0x48
P_EXTRA = 0x78                  # Struct / ElementProp / KeyProp / ByteProperty Enum / PropertyClass
P_EXTRA2 = 0x80                 # ArrayProperty Inner (0x78 = ArrayFlags) / map ValueProp / EnumProperty Enum
# FScriptSetLayout.Size: SetProperty @0x88, MapProperty @0x90 (verified on this build)
P_SET_SIZE, P_MAP_SIZE = 0x88, 0x90


class Prop:
    __slots__ = ('name', 'kind', 'offset', 'size', 'addr', 'ue')

    def __init__(self, ue, addr):
        self.ue, self.addr = ue, addr
        self.name = ue.fname(addr + F_NAME)
        self.kind = ue.fname(ue.q(addr + F_CLASS))           # FFieldClass::Name
        self.offset = ue.i32(addr + P_OFFSET)
        self.size = ue.i32(addr + P_ELEMSIZE)

    # sub-property accessors ------------------------------------------------
    @property
    def struct(self):            # FStructProperty
        return self.ue.q(self.addr + P_EXTRA)

    @property
    def inner(self):             # FArrayProperty Inner / FSetProperty ElementProp
        off = P_EXTRA2 if self.kind == 'ArrayProperty' else P_EXTRA
        return Prop(self.ue, self.ue.q(self.addr + off))

    @property
    def elem_stride(self):       # TSet / TMap sparse-array element size incl. hash ids
        off = P_MAP_SIZE if self.kind == 'MapProperty' else P_SET_SIZE
        return self.ue.i32(self.addr + off)

    @property
    def key(self):               # FMapProperty
        return Prop(self.ue, self.ue.q(self.addr + P_EXTRA))

    @property
    def value(self):
        return Prop(self.ue, self.ue.q(self.addr + P_EXTRA2))

    @property
    def enum(self):
        a = P_EXTRA2 if self.kind == 'EnumProperty' else P_EXTRA
        return self.ue.q(self.addr + a)

    @property
    def bool_mask(self):         # FBoolProperty: FieldSize, ByteOffset, ByteMask, FieldMask
        b = self.ue.read(self.addr + P_EXTRA, 4)
        return b[1], b[2]

    def __repr__(self):
        return '<%s %s @0x%x size 0x%x>' % (self.kind, self.name, self.offset, self.size)


class UE:
    def __init__(self, proc, rescan=False):
        """proc: a read-only winproc.Process.  rescan: brute-force the globals
        if the built-in RVAs do not match this game build (slow)."""
        self.p = proc
        self.base, self.size = self.p.main_module()
        self._names = {}
        self._props = {}
        self._classes = None
        self.objarr = self.base + RVA_OBJOBJECTS
        self.namepool = self.base + RVA_NAMEPOOL + 0x10
        if not self._valid():
            # another build (a patch, or the Xbox app / Minecraft Launcher version): find the tables (~1 s)
            self._rescan()

    # raw reads -------------------------------------------------------------
    def read(self, a, n):
        b = self.p.read(a, n)
        if b is None or len(b) < n:
            raise MemoryError('read failed @%x (%d)' % (a, n))
        return b

    def q(self, a): return struct.unpack('<Q', self.read(a, 8))[0]
    def u32(self, a): return struct.unpack('<I', self.read(a, 4))[0]
    def i32(self, a): return struct.unpack('<i', self.read(a, 4))[0]
    def i64(self, a): return struct.unpack('<q', self.read(a, 8))[0]
    def f32(self, a): return struct.unpack('<f', self.read(a, 4))[0]
    def f64(self, a): return struct.unpack('<d', self.read(a, 8))[0]
    def u8(self, a): return self.read(a, 1)[0]

    def alive(self):
        try:
            self.read(self.base, 2)
            return True
        except MemoryError:
            return False

    # names -----------------------------------------------------------------
    def name(self, idx):
        s = self._names.get(idx)
        if s is not None:
            return s
        blk = self.q(self.namepool + 8 * (idx >> 16))
        e = blk + 2 * (idx & 0xFFFF)
        h = struct.unpack('<H', self.read(e, 2))[0]
        n = h >> 6
        if n == 0:
            s = ''
        elif h & 1:
            s = self.read(e + 2, n * 2).decode('utf-16le')
        else:
            s = self.read(e + 2, n).decode('latin-1')
        self._names[idx] = s
        return s

    def fname(self, a):
        ci, num = struct.unpack('<II', self.read(a, 8))
        s = self.name(ci)
        return s + ('_%d' % (num - 1) if num else '')

    def tag(self, a):
        """FGameplayTag (an FName) -> str, 'None' for empty like the save format."""
        return self.fname(a) or 'None'

    def fstring(self, a):
        data, num = struct.unpack('<Qi', self.read(a, 12))
        if not data or num <= 0:
            return ''
        return self.read(data, num * 2).decode('utf-16le').rstrip('\x00')

    # containers ------------------------------------------------------------
    def tarray(self, a, stride):
        """TArray -> list of element addresses."""
        data, num, mx = struct.unpack('<Qii', self.read(a, 16))
        if not data or num <= 0 or num > 1_000_000 or num > mx:
            return []
        return [data + i * stride for i in range(num)]

    def tset(self, a, stride):
        """TSet/TMap sparse array -> list of element addresses (only allocated slots).
        TSparseArray: Data TArray @0, AllocationFlags TBitArray @0x10 (inline 4 dwords
        + secondary ptr @0x20, NumBits @0x28).  stride = Prop.elem_stride."""
        data, num, mx = struct.unpack('<Qii', self.read(a, 16))
        if not data or num <= 0:
            return []
        inl = self.read(a + 0x10, 16)
        sec = self.q(a + 0x20)
        nbits = self.i32(a + 0x28)
        words = (nbits + 31) // 32
        bits = self.read(sec, words * 4) if sec else inl[:words * 4]
        out = []
        for i in range(min(num, nbits)):
            if bits[i >> 3] >> (i & 7) & 1:
                out.append(data + i * stride)
        return out

    def tag_container(self, a):
        return [self.tag(e) for e in self.tarray(a, 8)]

    # objects ---------------------------------------------------------------
    def num_objs(self):
        return self.i32(self.objarr + 0x14)

    def obj(self, i):
        chunk = self.q(self.q(self.objarr) + 8 * (i // CHUNK))
        return self.q(chunk + OBJ_ITEM_STRIDE * (i % CHUNK))

    def iter_objects(self):
        n = self.num_objs()
        chunks = self.q(self.objarr)
        for c in range((n + CHUNK - 1) // CHUNK):
            cnt = min(CHUNK, n - c * CHUNK)
            raw = self.read(self.q(chunks + 8 * c), cnt * OBJ_ITEM_STRIDE)
            for i in range(cnt):
                o = struct.unpack_from('<Q', raw, i * OBJ_ITEM_STRIDE)[0]
                if o:
                    yield o

    def objname(self, o):
        return self.fname(o + O_NAME)

    def cls(self, o):
        return self.q(o + O_CLASS)

    def clsname(self, o):
        return self.objname(self.cls(o))

    def outer(self, o):
        return self.q(o + O_OUTER)

    def fullname(self, o):
        parts, x = [], o
        while x:
            parts.append(self.objname(x))
            x = self.outer(x)
        return self.clsname(o) + ' ' + '.'.join(reversed(parts))

    def supers(self, ustruct):
        x = ustruct
        while x:
            yield x
            x = self.q(x + S_SUPER)

    def is_a(self, o, class_name):
        return any(self.objname(c) == class_name for c in self.supers(self.cls(o)))

    def _index_types(self):
        """Map short name -> [UClass/UScriptStruct addr] for /Script types."""
        if self._classes is not None:
            return
        self._indexed_at = time.time()
        self._classes = {}
        kinds = {'Class', 'ScriptStruct', 'BlueprintGeneratedClass', 'Enum', 'UserDefinedEnum',
                 'UserDefinedStruct'}
        for o in self.iter_objects():
            try:
                k = self.clsname(o)
            except MemoryError:
                continue
            if k in kinds:
                self._classes.setdefault(self.objname(o), []).append(o)

    def find_type(self, name):
        self._index_types()
        r = self._classes.get(name)
        if not r and time.time() - self._indexed_at > 30:
            # indexed while the game was still loading: index again (at most every 30 s)
            self._classes = None
            self._index_types()
            r = self._classes.get(name)
        return r[0] if r else None

    def find_instances(self, class_name, include_cdo=False):
        """All live objects whose class (or a superclass) is class_name."""
        target = self.find_type(class_name)
        out = []
        for o in self.iter_objects():
            try:
                c = self.cls(o)
                hit = False
                x = c
                while x:
                    if x == target:
                        hit = True
                        break
                    x = self.q(x + S_SUPER)
                if hit and (include_cdo or not self.objname(o).startswith('Default__')):
                    out.append(o)
            except MemoryError:
                continue
        return out

    # reflection ------------------------------------------------------------
    def props(self, ustruct):
        """name -> Prop for a UStruct including its super chain (cached)."""
        r = self._props.get(ustruct)
        if r is not None:
            return r
        r = {}
        for s in self.supers(ustruct):
            f = self.q(s + S_CHILDPROPS)
            while f:
                p = Prop(self, f)
                r.setdefault(p.name, p)
                f = self.q(f + F_NEXT)
        self._props[ustruct] = r
        return r

    def struct_size(self, ustruct):
        return self.i32(ustruct + S_SIZE)

    def oprops(self, o):
        return self.props(self.cls(o))

    def field(self, o, name, ustruct=None):
        """Prop of a named field on object o (or struct type ustruct)."""
        p = self.props(ustruct or self.cls(o)).get(name)
        if p is None:
            raise KeyError(name)
        return p

    def enum_names(self, uenum):
        """UEnum::Names TArray<TPair<FName,int64>> at 0x40."""
        out = {}
        for e in self.tarray(uenum + 0x40, 0x10):
            nm = self.fname(e)
            out[self.i64(e + 8)] = nm.split('::')[-1]
        return out

    # validation / rescan ---------------------------------------------------
    def _valid(self):
        try:
            if self.name(0) != 'None':
                return False
            n = self.num_objs()
            if not 1000 < n < 5_000_000:
                return False
            return self.objname(self.obj(0)) == '/Script/CoreUObject'
        except (MemoryError, UnicodeDecodeError, struct.error):
            return False

    def _sections(self):
        hdr = self.read(self.base, 0x1000)
        e = int.from_bytes(hdr[0x3C:0x40], 'little')
        nsec = int.from_bytes(hdr[e + 6:e + 8], 'little')
        opt = int.from_bytes(hdr[e + 0x14:e + 0x16], 'little')
        out = {}
        for i in range(nsec):
            o = e + 0x18 + opt + 40 * i
            name = hdr[o:o + 8].rstrip(b'\0').decode('latin-1')
            vsize, va = struct.unpack_from('<II', hdr, o + 8)
            out[name] = (va, vsize)
        return out

    def exe_key(self):
        """Identifies this game build (PE TimeDateStamp + image size)."""
        hdr = self.read(self.base, 0x200)
        e = int.from_bytes(hdr[0x3C:0x40], 'little')
        return '%08x-%x' % (int.from_bytes(hdr[e + 8:e + 12], 'little'), self.size)

    def _rescan(self):
        """Finds GUObjectArray.ObjObjects and FNamePool in a build whose RVAs we don't know (another patch, or
        the Xbox app / Minecraft Launcher build).  Same engine, so the same data shapes:
          * FNamePool block 0 starts with the entries 'None' and 'ByteProperty' (header bytes include a
            hash of the string, identical in every build); the pool's Blocks[0] in .data points at it;
          * FChunkedFixedUObjectArray in .data: Objects**, PreAllocated*, Max, Num, MaxChunks, NumChunks
            with the chunk counts matching Max/Num (64K objects per chunk), object 0 = /Script/CoreUObject.
        Results: self.objarr / self.namepool (absolute), validated."""
        self._names.clear()
        secs = self._sections()
        dva, dsize = secs.get('.data', (0, 0))
        if not dsize:
            raise UEUnavailable('no .data section in the game executable')
        data = self.p.read(self.base + dva, dsize)
        if not data:
            raise UEUnavailable('could not read the game executable data section')
        # 1. the name pool's first block, somewhere on the heap
        sig = NAMEPOOL_BLOCK0_SIG
        block0 = None
        addr, m = 0, None
        from winproc import MEMORY_BASIC_INFORMATION
        m = MEMORY_BASIC_INFORMATION()
        while self.p.query(addr, m) and block0 is None:
            rb, rs = m.BaseAddress or 0, m.RegionSize
            if m.State == 0x1000 and m.Type == 0x20000 and m.Protect == 0x04 and rs < (1 << 31):
                off = 0
                while off < rs:
                    b = self.p.read(rb + off, min(rs - off, (32 << 20) + 64))
                    i = b.find(sig) if b else -1
                    if i >= 0:
                        block0 = rb + off + i
                        break
                    off += 32 << 20
            addr = rb + rs
            if addr >= 0x7FFFFFFFFFFF:
                break
        if block0 is None:
            raise UEUnavailable('could not find the name table in memory')
        needle = struct.pack('<Q', block0)
        i = data.find(needle)
        while i >= 0 and i % 8:
            i = data.find(needle, i + 1)
        if i < 0:
            raise UEUnavailable('could not find the name table pointer in the executable')
        self.namepool = self.base + dva + i
        if self.name(0) != 'None':
            raise UEUnavailable('name table found but entry 0 is not None')
        # 2. the object array
        ints = memoryview(data).cast('B')[:len(data) - len(data) % 4].cast('i')
        for k in range(4, len(ints) - 8, 2):            # k = index of Max (8-aligned struct start = k - 4)
            mx, num, mxc, numc = ints[k], ints[k + 1], ints[k + 2], ints[k + 3]
            if not (1000 < num <= mx < 50_000_000) or mxc != (mx + CHUNK - 1) // CHUNK                     or numc != (num + CHUNK - 1) // CHUNK:
                continue
            self.objarr = self.base + dva + (k - 4) * 4
            try:
                if self.objname(self.obj(0)) == '/Script/CoreUObject':
                    return
            except (MemoryError, UnicodeDecodeError, struct.error):
                pass
        raise UEUnavailable('could not find the object table in the executable')
