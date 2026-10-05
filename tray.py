"""Tray icon + notifications for the background sync (Windows only, ctypes, no extra packages).  New in 1.1.1.

The icon shows the tool is running; hovering shows what it last did; right-click:
    <status line>               (greyed: what it is doing / the last copy it made)
    Start with Windows          (checkbox: the HKCU Run entry)
    Open the log
    Open the backups folder
    ---
    Quit
Notifications use the tray icon's balloon, which Windows 10/11 shows as a normal notification.
Everything runs on its own thread with its own window and message loop; the sync calls set_status() and
notify() from its thread.
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading
from pathlib import Path

user32 = ctypes.WinDLL('user32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_USER, WM_APP = 0x0002, 0x0010, 0x0111, 0x0400, 0x8000
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
WM_TRAY = WM_APP + 1
WM_REFRESH = WM_APP + 2
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
NIIF_INFO, NIIF_WARNING, NIIF_USER, NIIF_LARGE_ICON = 0x1, 0x2, 0x4, 0x20
MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_CHECKED = 0x0, 0x1, 0x800, 0x8
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
IDI_APPLICATION = 32512
CMD_AUTOSTART, CMD_LOG, CMD_BACKUPS, CMD_QUIT = 1, 2, 3, 4


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('hWnd', wt.HWND), ('uID', wt.UINT), ('uFlags', wt.UINT),
                ('uCallbackMessage', wt.UINT), ('hIcon', wt.HICON), ('szTip', wt.WCHAR * 128),
                ('dwState', wt.DWORD), ('dwStateMask', wt.DWORD), ('szInfo', wt.WCHAR * 256),
                ('uVersion', wt.UINT), ('szInfoTitle', wt.WCHAR * 64), ('dwInfoFlags', wt.DWORD),
                ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', wt.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', wt.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON),
                ('hCursor', wt.HANDLE), ('hbrBackground', wt.HANDLE), ('lpszMenuName', wt.LPCWSTR),
                ('lpszClassName', wt.LPCWSTR)]


for fn, res, args in (
        ('DefWindowProcW', LRESULT, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        ('RegisterClassW', wt.ATOM, [ctypes.POINTER(WNDCLASSW)]),
        ('CreateWindowExW', wt.HWND, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]),
        ('DestroyWindow', wt.BOOL, [wt.HWND]),
        ('GetMessageW', wt.BOOL, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]),
        ('TranslateMessage', wt.BOOL, [ctypes.POINTER(wt.MSG)]),
        ('DispatchMessageW', LRESULT, [ctypes.POINTER(wt.MSG)]),
        ('PostMessageW', wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        ('PostQuitMessage', None, [ctypes.c_int]),
        ('CreatePopupMenu', wt.HMENU, []),
        ('AppendMenuW', wt.BOOL, [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]),
        ('TrackPopupMenu', wt.BOOL, [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                     wt.LPVOID]),
        ('DestroyMenu', wt.BOOL, [wt.HMENU]),
        ('GetCursorPos', wt.BOOL, [ctypes.POINTER(wt.POINT)]),
        ('SetForegroundWindow', wt.BOOL, [wt.HWND]),
        ('LoadImageW', wt.HANDLE, [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]),
        ('LoadIconW', wt.HICON, [wt.HINSTANCE, wt.LPVOID]),
        ('RegisterWindowMessageW', wt.UINT, [wt.LPCWSTR])):
    f = getattr(user32, fn)
    f.restype, f.argtypes = res, args
shell32.Shell_NotifyIconW.restype = wt.BOOL
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.ShellExecuteW.restype = wt.HINSTANCE
shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


class Tray:
    def __init__(self, title, icon_path=None, on_quit=None, autostart=None, log_path=None, backup_dir=None):
        """autostart: (is_installed() -> bool, set(bool) -> None) or None."""
        self.title = title
        self.icon_path = icon_path
        self.on_quit = on_quit
        self.autostart = autostart
        self.log_path = log_path
        self.backup_dir = backup_dir
        self.status = 'Starting...'
        self.hwnd = None
        self._lock = threading.Lock()
        self._pending = []                 # balloons queued before the icon exists
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name='tray', daemon=True)

    # ---- called from the sync thread ------------------------------------------------------------------------------
    def start(self):
        self._thread.start()
        self._ready.wait(5)
        return self

    def set_status(self, text):
        with self._lock:
            self.status = text
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def notify(self, title, text, warning=False):
        with self._lock:
            self._pending.append((title, text, warning))
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ---- the tray thread ------------------------------------------------------------------------------------------
    def _nid(self, flags):
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        n.hWnd, n.uID, n.uFlags = self.hwnd, 1, flags
        n.uCallbackMessage = WM_TRAY
        n.hIcon = self.hicon
        with self._lock:
            tip = '%s\n%s' % (self.title, self.status)
        n.szTip = tip[:127]
        return n

    def _refresh(self):
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid(NIF_TIP | NIF_SHOWTIP)))
        while True:
            with self._lock:
                if not self._pending:
                    break
                title, text, warning = self._pending.pop(0)
            n = self._nid(NIF_INFO)
            n.szInfoTitle = title[:63]
            n.szInfo = text[:255]
            n.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(n))

    def _menu(self):
        m = user32.CreatePopupMenu()
        with self._lock:
            status = self.status
        user32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, status[:120])
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        if self.autostart:
            on = False
            try:
                on = self.autostart[0]()
            except Exception:
                pass
            user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if on else 0), CMD_AUTOSTART, 'Start with Windows')
        if self.log_path:
            user32.AppendMenuW(m, MF_STRING, CMD_LOG, 'Open the log')
        if self.backup_dir:
            user32.AppendMenuW(m, MF_STRING, CMD_BACKUPS, 'Open the backups folder')
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, CMD_QUIT, 'Quit Offline Hero Sync')
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd,
                                    None)
        user32.DestroyMenu(m)
        self._command(cmd)

    def _command(self, cmd):
        if cmd == CMD_AUTOSTART and self.autostart:
            try:
                self.autostart[1](not self.autostart[0]())
            except Exception as e:
                self.notify(self.title, 'Could not change autostart: %s' % e, warning=True)
        elif cmd == CMD_LOG and self.log_path:
            if Path(self.log_path).exists():
                shell32.ShellExecuteW(None, 'open', str(self.log_path), None, None, 1)
        elif cmd == CMD_BACKUPS and self.backup_dir:
            Path(self.backup_dir).mkdir(parents=True, exist_ok=True)
            shell32.ShellExecuteW(None, 'open', str(self.backup_dir), None, None, 1)
        elif cmd == CMD_QUIT:
            if self.on_quit:
                self.on_quit()
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_TRAY:
            ev = lp & 0xFFFF
            if ev in (WM_RBUTTONUP, WM_CONTEXTMENU, WM_LBUTTONUP):
                self._menu()
            return 0
        if msg == WM_REFRESH:
            self._refresh()
            return 0
        if msg == self._taskbar_created:            # Explorer restarted: put the icon back
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP |
                                                                      NIF_SHOWTIP)))
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)                       # keep a reference: ctypes callback
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = 'OfflineHeroSyncTray'
        user32.RegisterClassW(ctypes.byref(wc))
        self._taskbar_created = user32.RegisterWindowMessageW('TaskbarCreated')
        self.hicon = None
        if self.icon_path and os.path.exists(self.icon_path):
            self.hicon = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, 0, 0,
                                           LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not self.hicon:
            self.hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, self.title, 0, 0, 0, 0, 0, None, None, hinst, None)
        n = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(n))
        n.uVersion = 4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(n))
        self._ready.set()
        self._refresh()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
