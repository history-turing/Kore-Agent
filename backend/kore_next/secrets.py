from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import secrets
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

SERVICE = "com.koreagent-next.desktop.model-api-key"


class SecretStore:
    """Store model credentials in Keychain or Windows DPAPI.

    Plain-text JSON written by early development builds is read for migration but is
    never written again.
    """

    def __init__(self, settings_dir: Path):
        self.path = settings_dir / "secrets.json"

    def permission_signing_key(self) -> bytes | None:
        """Return a stable OS-protected key, or disable persistent rules on unsupported hosts."""
        if sys.platform not in {"darwin", "win32"}:
            return None
        account = "__permission_rules_signing_key_v2"
        encoded = self.get(account, allow_environment=False)
        if not encoded:
            self.set(account, base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
            encoded = self.get(account, allow_environment=False)
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("permission signing key is invalid") from exc
        if len(key) < 32:
            raise RuntimeError("permission signing key is too short")
        return key

    def get(self, provider: str, *, allow_environment: bool = True) -> str:
        env = os.getenv(f"KORENEXT_{provider.upper()}_API_KEY", "") if allow_environment else ""
        if env:
            return env
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", provider, "-w"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get(provider, "")
        except (OSError, json.JSONDecodeError):
            return ""
        if sys.platform == "win32" and isinstance(value, dict) and value.get("encoding") == "dpapi":
            try:
                return self._dpapi_unprotect(str(value.get("value") or ""))
            except OSError:
                return ""
        return value if isinstance(value, str) else ""

    def source(self, provider: str) -> str:
        if os.getenv(f"KORENEXT_{provider.upper()}_API_KEY", ""):
            return "environment"
        if sys.platform == "darwin":
            return "keychain" if self.get(provider) else "none"
        if sys.platform == "win32":
            try:
                value = json.loads(self.path.read_text(encoding="utf-8")).get(provider)
            except (OSError, json.JSONDecodeError):
                return "none"
            return "dpapi" if isinstance(value, dict) and value.get("encoding") == "dpapi" else ("legacy_plaintext" if value else "none")
        return "none"

    def set(self, provider: str, secret: str) -> None:
        if sys.platform == "darwin":
            subprocess.run(
                ["/usr/bin/security", "add-generic-password", "-U", "-s", SERVICE, "-a", provider, "-w", secret],
                check=True,
                capture_output=True,
            )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if sys.platform == "win32":
            data[provider] = {"encoding": "dpapi", "value": self._dpapi_protect(secret)}
        else:
            raise RuntimeError("Persistent model API keys require macOS Keychain or Windows DPAPI; use an environment variable")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _dpapi_protect(secret: str) -> str:
        return base64.b64encode(SecretStore._crypt_protect(secret.encode("utf-8"))).decode("ascii")

    @staticmethod
    def _dpapi_unprotect(encoded: str) -> str:
        return SecretStore._crypt_unprotect(base64.b64decode(encoded)).decode("utf-8")

    @staticmethod
    def _crypt_protect(raw: bytes) -> bytes:
        if sys.platform != "win32":
            raise OSError("DPAPI is only available on Windows")
        source, source_buffer = SecretStore._blob(raw)
        entropy, entropy_buffer = SecretStore._blob(SERVICE.encode("utf-8"))
        output = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source),
            SERVICE,
            ctypes.byref(entropy),
            None,
            None,
            0x01,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    @staticmethod
    def _crypt_unprotect(raw: bytes) -> bytes:
        if sys.platform != "win32":
            raise OSError("DPAPI is only available on Windows")
        source, source_buffer = SecretStore._blob(raw)
        entropy, entropy_buffer = SecretStore._blob(SERVICE.encode("utf-8"))
        output = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            0x01,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    @staticmethod
    def _blob(raw: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
        buffer = ctypes.create_string_buffer(raw)
        blob = _DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        return blob, buffer


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
