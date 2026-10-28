"""Windows DPAPI/macOS Keychain storage; secrets never appear in config.toml."""
import asyncio
import base64
import ctypes
from ctypes import wintypes
import json
import hashlib
import errno
import sys
import os
from pathlib import Path
import time
from contextlib import asynccontextmanager


SETUP_COMMAND = "configure.ps1" if os.name == "nt" else "configure_web.py"


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for credential storage.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("Windows credential encryption/decryption failed.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _keychain(self):
        # Explicit backend: never fall back to another (possibly plaintext) store.
        from keyring.backends.macOS import Keyring
        backend = Keyring()
        service = "toss-securities-mcp:" + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        return backend, service

    def read(self, name):
        if sys.platform == "darwin":
            try:
                backend, service = self._keychain()
                value = backend.get_password(service, name)
                if value is None:
                    return None
                data = json.loads(value)
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                return data
            except Exception:
                raise RuntimeError("macOS 키체인의 인증정보를 열 수 없습니다. configure_web.py에서 다시 저장하세요.") from None
        path = self.root / f"{name}.dpapi"
        if not path.exists():
            return None
        try:
            return json.loads(crypt(base64.b64decode(path.read_text("utf-8-sig")), True))
        except Exception:
            raise RuntimeError("저장된 인증정보를 열 수 없습니다. configure.ps1에서 다시 저장하세요.") from None

    def write(self, name, data):
        if sys.platform == "darwin":
            try:
                backend, service = self._keychain()
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                backend.set_password(service, name, json.dumps(data))
                return
            except Exception:
                raise RuntimeError("macOS 키체인에 인증정보를 저장할 수 없습니다. 키체인 접근을 확인하세요.") from None
        self.root.mkdir(parents=True, exist_ok=True)
        encoded = base64.b64encode(crypt(json.dumps(data).encode("utf-8"))).decode("ascii")
        path = self.root / f"{name}.dpapi"
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="ascii")
        os.replace(temporary, path)

    @asynccontextmanager
    async def lock(self, name):
        # A shared lock/cache prevents concurrent Codex processes from issuing
        # mutually revoking tokens (Toss permits one token per client).
        if sys.platform == "darwin":
            import fcntl
        elif os.name == "nt":
            import msvcrt
        else:
            raise RuntimeError("Windows 또는 macOS가 필요합니다.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / f"{name}.lock"
        handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600), "a+b")
        handle.write(b"0")
        handle.flush()
        deadline = time.monotonic() + 30
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if sys.platform == "darwin":
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as error:
                    if sys.platform == "darwin" and error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise RuntimeError("인증 잠금을 처리할 수 없습니다.") from None
                    if time.monotonic() >= deadline:
                        raise RuntimeError("인증 처리가 사용 중입니다. 잠시 후 다시 시도하세요.") from None
                    await asyncio.sleep(0.1)
            yield
        finally:
            try:
                if acquired:
                    handle.seek(0)
                    if sys.platform == "darwin":
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    else:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()
