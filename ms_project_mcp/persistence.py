from __future__ import annotations

import base64
import getpass
import os
import secrets
import subprocess
from pathlib import Path
from typing import Callable, Mapping


STATE_DIR_ENV = "MSP_MCP_STATE_DIR"
STATE_VENDOR_DIR = "OpenAI"
STATE_PRODUCT_DIR = "MicrosoftProjectMCP"
SECRET_FILE = "confirmation-secret.key"


def resolve_state_dir(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    override = env.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (Path(local_app_data) / STATE_VENDOR_DIR / STATE_PRODUCT_DIR).resolve()
    # Non-Windows development fallback; Windows production normally always has LOCALAPPDATA.
    return (Path.home() / ".local" / "state" / "ms-project-mcp").resolve()


def ensure_state_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _default_acl_applier(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    account = f"{domain}\\{user}" if domain else user
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.run(
        # LOCALAPPDATA already inherits the user's profile ACL. Do not strip
        # inherited application/sandbox ACEs here: doing so can make the file
        # inaccessible to the process that just created it. Reinforce the
        # current user's full-control entry and otherwise fail open.
        ["icacls.exe", str(path), "/grant:r", f"{account}:(F)"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


def apply_user_only_acl(path: Path) -> None:
    """Apply a best-effort current-user ACL without making startup depend on ACL tooling."""
    if not path.exists():
        return
    try:
        _default_acl_applier(path)
    except Exception:
        pass


def _read_secret(path: Path) -> bytes:
    raw = path.read_bytes().strip()
    try:
        value = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise RuntimeError("Persistent Microsoft Project MCP secret is malformed") from exc
    if len(value) != 32:
        raise RuntimeError("Persistent Microsoft Project MCP secret has an invalid length")
    return value


def load_or_create_secret(
    state_dir: Path,
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    acl_applier: Callable[[Path], None] = _default_acl_applier,
) -> bytes:
    root = ensure_state_dir(state_dir)
    path = root / SECRET_FILE
    if path.exists():
        value = _read_secret(path)
        try:
            acl_applier(path)
        except Exception:
            pass
        return value

    value = random_bytes(32)
    encoded = base64.urlsafe_b64encode(value) + b"\n"
    temporary = root / f".{SECRET_FILE}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard-link makes publishing complete contents atomic and refuses to replace
            # another process's winning secret.
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    persisted = _read_secret(path)
    try:
        acl_applier(path)
    except Exception:
        pass
    return persisted
