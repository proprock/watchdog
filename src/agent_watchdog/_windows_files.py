"""Windows file operations for replacing daemon control state during reads."""

import ctypes
import msvcrt
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO

if sys.platform != "win32":
    raise ImportError("Windows file operations require Windows")

_DELETE = 0x00010000
_GENERIC_READ = 0x80000000
_SHARE_READ_WRITE_DELETE = 0x7
_OPEN_EXISTING = 3
_FILE_RENAME_INFO_EX = 22
_RENAME_REPLACE_IF_EXISTS = 0x1
_RENAME_POSIX_SEMANTICS = 0x2
_UNSUPPORTED_RENAME = (1, 50, 87)  # Invalid function, not supported, invalid parameter.

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.SetFileInformationByHandle.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_kernel32.SetFileInformationByHandle.restype = wintypes.BOOL


class _RenameInfo(ctypes.Structure):
    _fields_ = (
        ("flags", wintypes.DWORD),
        ("root", wintypes.HANDLE),
        ("name_length", wintypes.DWORD),
        ("name", wintypes.WCHAR * 1),
    )


def _open_handle(path: Path, access: int) -> int:
    handle = _kernel32.CreateFileW(
        str(path), access, _SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, 0, None
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = str(path)
        raise error
    return handle


def open_shared_read(path: Path) -> BinaryIO:
    handle = _open_handle(path, _GENERIC_READ)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except OSError:
        _kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def replace_shared(source: Path, target: Path) -> None:
    # MoveFileEx (os.replace) cannot replace an open destination even if the
    # reader shares delete access. POSIX rename preserves that reader's handle.
    name = str(target.absolute()).encode("utf-16-le", errors="surrogatepass")
    buffer = ctypes.create_string_buffer(
        max(ctypes.sizeof(_RenameInfo), _RenameInfo.name.offset + len(name) + 2)
    )
    info = _RenameInfo.from_buffer(buffer)
    info.flags = _RENAME_REPLACE_IF_EXISTS | _RENAME_POSIX_SEMANTICS
    info.name_length = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, name, len(name))

    handle = _open_handle(source, _DELETE)
    try:
        if _kernel32.SetFileInformationByHandle(handle, _FILE_RENAME_INFO_EX, buffer, len(buffer)):
            return
        error = ctypes.WinError(ctypes.get_last_error())
        if error.winerror not in _UNSUPPORTED_RENAME:
            error.filename = str(target)
            raise error
    finally:
        _kernel32.CloseHandle(handle)
    # Filesystems without FileRenameInfoEx still get the ordinary bounded replacement.
    source.replace(target)
