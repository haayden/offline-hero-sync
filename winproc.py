"""Read-only Win32 helpers (ctypes) for looking at the running game process.

This module can only READ another process.  The handle is opened with
PROCESS_QUERY_INFORMATION | PROCESS_VM_READ and nothing else, and no write,
protect, inject or thread API is even declared here.
"""
import ctypes
import ctypes.wintypes as wt

# Steam build.  The second name is a guess for the Microsoft Store / Xbox app
# build (UE names GDK builds "-WinGDK-"); it is untested.
GAME_EXES = ('Dungeons-Win64-Shipping.exe', 'Dungeons-WinGDK-Shipping.exe')

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_READ = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ   # the only access this tool ever asks for

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
TH32CS_SNAPPROCESS = 0x2


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                ('szExeFile', ctypes.c_wchar * 260)]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wt.DWORD), ('PartitionId', wt.WORD), ('RegionSize', ctypes.c_size_t),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]


k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD,
                                       ctypes.POINTER(wt.DWORD), wt.DWORD]


class GameNotRunning(Exception):
    pass


def find_games():
    """[(pid, exe name)] of every running game process (a stale second instance can linger)."""
    wanted = {n.lower() for n in GAME_EXES}
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return out
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() in wanted:
                out.append((pe.th32ProcessID, pe.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def find_game():
    """(pid, exe name) of the first running game process, or (None, None)."""
    g = find_games()
    return g[0] if g else (None, None)


class Process:
    """A read-only view of one game process."""

    def __init__(self, pid=None, exe=None):
        if pid is None:
            pid, exe = find_game()
        self.pid, self.exe = pid, exe
        self.h = None
        if not self.pid:
            raise GameNotRunning('Minecraft Dungeons II is not running')
        self.h = k32.OpenProcess(PROCESS_READ, False, self.pid)
        if not self.h:
            err = ctypes.get_last_error()
            raise GameNotRunning('could not open the game process for reading (Windows error %d)' % err)

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def alive(self):
        code = wt.DWORD()
        return bool(self.h) and bool(k32.GetExitCodeProcess(self.h, ctypes.byref(code))) and code.value == 259

    def main_module(self):
        """(base, SizeOfImage) of the game executable."""
        mods = (ctypes.c_void_p * 1024)()
        need = wt.DWORD()
        if not psapi.EnumProcessModulesEx(self.h, mods, ctypes.sizeof(mods), ctypes.byref(need), 3):
            raise GameNotRunning('could not list the game modules (Windows error %d)' % ctypes.get_last_error())
        base = mods[0]
        hdr = self.read(base, 0x1000)
        if not hdr:
            raise GameNotRunning('could not read the game executable header')
        e = int.from_bytes(hdr[0x3C:0x40], 'little')
        return base, int.from_bytes(hdr[e + 0x50:e + 0x54], 'little')

    def read(self, addr, n):
        """Up to n bytes at addr, or None."""
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
            if not got.value:
                return None
        return buf.raw[:got.value]

    def query(self, addr, mbi):
        return k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
