"""Windows handle-relative, no-follow opens for source snapshots."""
import ctypes as C
import msvcrt
import ntpath
import os
import struct
from ctypes import wintypes as W

import _winapi

_kernel = C.WinDLL("kernel32", use_last_error=True)
_nt = C.WinDLL("ntdll")


class _UnicodeString(C.Structure):
    _fields_ = [("Length", W.USHORT), ("MaximumLength", W.USHORT), ("Buffer", W.LPWSTR)]


class _ObjectAttributes(C.Structure):
    _fields_ = [("Length", W.ULONG), ("RootDirectory", W.HANDLE),
                ("ObjectName", C.POINTER(_UnicodeString)), ("Attributes", W.ULONG),
                ("SecurityDescriptor", W.LPVOID), ("SecurityQualityOfService", W.LPVOID)]


class _IoStatusBlock(C.Structure):
    _fields_ = [("Status", W.LPVOID), ("Information", C.c_size_t)]


class _BasicInfo(C.Structure):
    _fields_ = [(name, C.c_longlong) for name in ("created", "accessed", "written", "changed")]
    _fields_.append(("attributes", W.DWORD))


_nt.NtOpenFile.argtypes = [C.POINTER(W.HANDLE), W.ULONG, C.POINTER(_ObjectAttributes),
                            C.POINTER(_IoStatusBlock), W.ULONG, W.ULONG]
_nt.NtOpenFile.restype = W.LONG
_nt.RtlNtStatusToDosError.argtypes = [W.LONG]
_nt.RtlNtStatusToDosError.restype = W.ULONG
_kernel.GetFileInformationByHandleEx.argtypes = [W.HANDLE, C.c_int, W.LPVOID, W.DWORD]
_kernel.GetFinalPathNameByHandleW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD]
_kernel.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, W.LPVOID, W.DWORD, W.LPVOID,
                                    W.DWORD, C.POINTER(W.DWORD), W.LPVOID]

_FILE_OPEN_REPARSE_POINT = 0x00200000
_SYNCHRONOUS_NONALERT = 0x20
_SHARE_READ = 1


def _as_fd(handle):
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise


def open_volume(anchor):
    # FILE_FLAG_BACKUP_SEMANTICS enables an ordinary directory handle.
    handle = _winapi.CreateFile(anchor, _winapi.FILE_GENERIC_READ, 7, 0, 3,
                                0x02000000 | _FILE_OPEN_REPARSE_POINT, 0)
    return _as_fd(handle)


def open_child(parent, name):
    """Open precisely one component relative to a live directory handle."""
    buffer = C.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le"))
    string = _UnicodeString(length, length + 2, C.cast(buffer, W.LPWSTR))
    attrs = _ObjectAttributes(C.sizeof(_ObjectAttributes), msvcrt.get_osfhandle(parent),
                              C.pointer(string), 0, None, None)
    handle = W.HANDLE()
    status = _nt.NtOpenFile(C.byref(handle), _winapi.FILE_GENERIC_READ, C.byref(attrs),
                            C.byref(_IoStatusBlock()), _SHARE_READ,
                            _FILE_OPEN_REPARSE_POINT | _SYNCHRONOUS_NONALERT)
    if status < 0:
        raise C.WinError(_nt.RtlNtStatusToDosError(status))
    fd = _as_fd(handle.value)
    try:
        # The native lookup can accept a DOS short name. Only the stored name is eligible.
        path = C.create_unicode_buffer(32768)
        size = _kernel.GetFinalPathNameByHandleW(handle, path, len(path), 0)
        if not size or size >= len(path):
            raise C.WinError(C.get_last_error())
        if ntpath.basename(path.value) != name:
            raise ValueError(f"snapshot path uses a filesystem alias: {name!r}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def basic_info(fd):
    info = _BasicInfo()
    if not _kernel.GetFileInformationByHandleEx(msvcrt.get_osfhandle(fd), 0, C.byref(info),
                                                C.sizeof(info)):
        raise C.WinError(C.get_last_error())
    return info.attributes, info.changed


def relative_link(fd):
    """Return the stored relative symlink target; reject other reparse tags and absolute links."""
    # FSCTL_GET_REPARSE_POINT, MAXIMUM_REPARSE_DATA_BUFFER_SIZE.
    data = C.create_string_buffer(16 * 1024)
    used = W.DWORD()
    if not _kernel.DeviceIoControl(msvcrt.get_osfhandle(fd), 0x900A8, None, 0, data,
                                   len(data), C.byref(used), None):
        raise C.WinError(C.get_last_error())
    raw = data.raw[:used.value]
    tag, = struct.unpack_from("<I", raw)
    if tag != 0xA000000C:  # IO_REPARSE_TAG_SYMLINK
        raise ValueError("unsupported filesystem reparse point")
    offset, length, _, _, flags = struct.unpack_from("<4HI", raw, 8)
    if flags != 1:  # SYMLINK_FLAG_RELATIVE
        return ""
    start = 20 + offset
    if length % 2 or start + length > len(raw):
        raise ValueError("invalid filesystem symlink")
    # Native Windows link separators have an unambiguous meaning at this boundary.
    return raw[start:start + length].decode("utf-16-le").replace("\\", "/")
