"""Remote login secrets belong to the operating system's credential store."""

from __future__ import annotations

import ctypes
import importlib
import sys
from ctypes import wintypes
from typing import Any, cast


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _windows() -> Any:
    api = cast("Any", ctypes).WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api


def get_password(target: str) -> str | None:
    if sys.platform != "win32":
        try:
            keyring = importlib.import_module("keyring")
        except ImportError:
            return None
        return cast("str | None", keyring.get_password("MailFlow", target))
    api = _windows()
    credential = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(credential)):
        return None
    try:
        value = credential.contents
        return ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(credential)


def set_password(target: str, password: str, username: str) -> None:
    if sys.platform != "win32":
        keyring = importlib.import_module("keyring")

        keyring.set_password("MailFlow", target, password)
        return
    blob = (ctypes.c_ubyte * len(password.encode("utf-8"))).from_buffer_copy(
        password.encode("utf-8")
    )
    credential = _Credential(
        Type=1,
        TargetName=target,
        UserName=username,
        CredentialBlobSize=len(blob),
        CredentialBlob=blob,
        Persist=2,
    )
    if not _windows().CredWriteW(ctypes.byref(credential), 0):
        raise OSError("credential store unavailable")


def delete_password(target: str) -> None:
    if sys.platform != "win32":
        try:
            keyring = importlib.import_module("keyring")

            keyring.delete_password("MailFlow", target)
        except (ImportError, RuntimeError):
            pass
        return
    _windows().CredDeleteW(target, 1, 0)
