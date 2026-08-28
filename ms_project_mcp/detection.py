from __future__ import annotations

import importlib.util
import os
import platform
from dataclasses import dataclass
from typing import Any, Callable

from .models import DesktopProjectDetection


@dataclass(frozen=True)
class RegistrySnapshot:
    prog_ids: tuple[str, ...] = ()
    clsids: tuple[str, ...] = ()
    executable_paths: tuple[str, ...] = ()
    version_hints: tuple[str, ...] = ()
    architecture_hints: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


ModuleFinder = Callable[[str], Any | None]
RegistryReader = Callable[[], RegistrySnapshot]
PathExists = Callable[[str], bool]


def _extract_executable(command: str) -> str | None:
    value = command.strip()
    if not value:
        return None
    if value.startswith('"'):
        end = value.find('"', 1)
        return value[1:end] if end > 1 else None
    lowered = value.lower()
    marker = ".exe"
    end = lowered.find(marker)
    if end >= 0:
        return value[: end + len(marker)].strip()
    return value.split(" ", 1)[0]


def _read_windows_registry() -> RegistrySnapshot:
    import winreg

    prog_ids: set[str] = set()
    clsids: set[str] = set()
    executable_paths: set[str] = set()
    version_hints: set[str] = set()
    architecture_hints: set[str] = set()
    errors: list[str] = []
    candidates = ("MSProject.Application",) + tuple(
        f"MSProject.Application.{version}" for version in range(12, 25)
    )
    views = (
        (getattr(winreg, "KEY_WOW64_64KEY", 0), "64-bit"),
        (getattr(winreg, "KEY_WOW64_32KEY", 0), "32-bit"),
    )
    for view_flag, architecture in views:
        for prog_id in candidates:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CLASSES_ROOT,
                    f"{prog_id}\\CLSID",
                    0,
                    winreg.KEY_READ | view_flag,
                ) as key:
                    clsid = str(winreg.QueryValueEx(key, None)[0]).strip()
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{architecture}:{prog_id}:{type(exc).__name__}")
                continue
            if not clsid:
                continue
            prog_ids.add(prog_id)
            clsids.add(clsid)
            architecture_hints.add(architecture)
            suffix = prog_id.rsplit(".", 1)[-1]
            if suffix.isdigit():
                version_hints.add(suffix)
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CLASSES_ROOT,
                    f"CLSID\\{clsid}\\LocalServer32",
                    0,
                    winreg.KEY_READ | view_flag,
                ) as key:
                    command = str(winreg.QueryValueEx(key, None)[0])
                executable = _extract_executable(command)
                if executable:
                    executable_paths.add(os.path.expandvars(executable))
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{architecture}:{clsid}:LocalServer32:{type(exc).__name__}")
    return RegistrySnapshot(
        prog_ids=tuple(sorted(prog_ids)),
        clsids=tuple(sorted(clsids)),
        executable_paths=tuple(sorted(executable_paths)),
        version_hints=tuple(sorted(version_hints)),
        architecture_hints=tuple(sorted(architecture_hints)),
        errors=tuple(errors),
    )


def probe_desktop_project(
    *,
    platform_reader: Callable[[], str] = platform.system,
    module_finder: ModuleFinder = importlib.util.find_spec,
    registry_reader: RegistryReader = _read_windows_registry,
    path_exists: PathExists = os.path.isfile,
) -> DesktopProjectDetection:
    platform_name = platform_reader()
    windows = platform_name.lower() == "windows"
    pythoncom_importable = module_finder("pythoncom") is not None
    win32com_importable = module_finder("win32com") is not None
    snapshot = RegistrySnapshot()
    errors: list[str] = []
    if windows:
        try:
            snapshot = registry_reader()
        except Exception as exc:
            errors.append(f"registry_probe:{type(exc).__name__}")
    existing_paths = tuple(path for path in snapshot.executable_paths if path_exists(path))
    errors.extend(snapshot.errors)
    return DesktopProjectDetection(
        platform=platform_name,
        windows=windows,
        com_registered=bool(snapshot.clsids),
        prog_ids=snapshot.prog_ids,
        clsids=snapshot.clsids,
        executable_paths=snapshot.executable_paths,
        existing_executable_paths=existing_paths,
        version_hints=snapshot.version_hints,
        architecture_hints=snapshot.architecture_hints,
        pywin32_importable=pythoncom_importable and win32com_importable,
        pythoncom_importable=pythoncom_importable,
        win32com_importable=win32com_importable,
        probe_errors=tuple(errors),
        activation_attempted=False,
    )
