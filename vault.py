"""Current-user Windows DPAPI storage; secrets never appear in config.toml."""
import asyncio
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time
from contextlib import asynccontextmanager


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

    def read(self, name):
        path = self.root / f"{name}.dpapi"
        if not path.exists():
            return None
        try:
            return json.loads(crypt(base64.b64decode(path.read_text("utf-8-sig")), True))
        except Exception:
            raise RuntimeError("저장된 인증정보를 열 수 없습니다. configure.ps1에서 다시 저장하세요.") from None

    def write(self, name, data):
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
        import msvcrt
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{name}.lock"
        handle = path.open("a+b")
        handle.write(b"0")
        handle.flush()
        deadline = time.monotonic() + 30
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("인증 처리가 사용 중입니다. 잠시 후 다시 시도하세요.") from None
                    await asyncio.sleep(0.1)
            yield
        finally:
            if acquired:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()
