# -*- coding: utf-8 -*-
"""
Windows Battery Saver Wizard
----------------------------
A lightweight GUI utility built with Tkinter & Windows powercfg to optimize
battery life and power plans on Windows laptops.

Key Features:
  * MAXIMUM SAVER (On Battery): Switches to Power Saver plan, limits CPU state,
    disables Turbo Boost, lowers screen refresh rate, reduces brightness, 
    and applies Wi-Fi/USB/PCIe power-saving policies.
  * BALANCED / GAMING (On AC): Restores CPU limits to 100%, enables Turbo Boost,
    activates the Balanced power plan, and sets refresh rate to maximum.
  * RESTORE ORIGINALS: Backs up original settings before making any changes
    and allows 1-click full restoration.
  * BATTERY DASHBOARD: Real-time battery discharge monitoring (W), remaining
    time estimation based on actual current draw, and battery health reporting.

Requirements: Windows OS, Python 3.8+
Note: Administrator privileges may be required for certain powercfg commands.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from collections import deque
from ctypes import wintypes
from tkinter import messagebox

# --------------------------------------------------------------------------
# Configuration & Global Constants
# --------------------------------------------------------------------------
NORMAL_SCHEME = "381b4222-f694-41f0-9685-ff5bb260df2e"  # Balanced Scheme GUID
# Use "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c" for High Performance if preferred.

POLL_EVERY = 15     # Interval for WMI battery measurements (seconds)
RATE_SAMPLES = 8    # Number of samples kept for power consumption averaging

# --------------------------------------------------------------------------
# Standard Windows PowerCFG GUIDs
# --------------------------------------------------------------------------
GUID_POWER_SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"   # Power Saver

SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
PROCTHROTTLEMAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"    # Max processor state
PERFBOOSTMODE = "be337238-0d82-4146-a960-4f3749d470c7"      # Turbo Boost mode
PERFEPP = "36687f9e-e3a5-4dbf-b1dc-15eb381c6863"            # Energy/Performance Preference
PERFEPP1 = "4b70f900-cdd9-4e66-aa26-ae8417f98173"           # EPP (Heterogeneous class 1)

SUB_VIDEO = "7516b95f-f776-4464-8c53-06167f40cc99"
VIDEO_BRIGHTNESS = "aded5e82-b909-4619-9949-f5d71dac0bcb"
VIDEO_OFF = "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"

SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
STANDBY_IDLE = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"

SUB_WIFI = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"
WIFI_POWER_SAVE = "12bbebe6-58d6-4636-95bb-3217ef867c1a"

SUB_USB = "2a737441-1930-4402-8d77-b2bebba308a3"
USB_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"

SUB_PCIE = "501a4d13-42af-4429-9fd1-a8218c268e20"
PCIE_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"


def _dur(seconds: int) -> str:
    return f"{seconds} sec" if seconds < 60 else f"{seconds // 60} min"


SETTINGS = {
    "cpu_max": {
        "title": "Max Processor State",
        "targets": [(SUB_PROCESSOR, PROCTHROTTLEMAX)],
        "fmt": lambda v: f"{v}%",
    },
    "boost": {
        "title": "Turbo Boost",
        "targets": [(SUB_PROCESSOR, PERFBOOSTMODE)],
        "fmt": lambda v: "Disabled" if v == 0 else "Default",
    },
    "epp": {
        "title": "Energy Preference (EPP)",
        "targets": [(SUB_PROCESSOR, PERFEPP), (SUB_PROCESSOR, PERFEPP1)],
        "fmt": lambda v: f"{v} (higher = more efficient)",
    },
    "brightness": {
        "title": "Display Brightness",
        "targets": [(SUB_VIDEO, VIDEO_BRIGHTNESS)],
        "fmt": lambda v: f"{v}%",
    },
    "screen_off": {
        "title": "Turn Off Display After",
        "targets": [(SUB_VIDEO, VIDEO_OFF)],
        "fmt": _dur,
    },
    "sleep": {
        "title": "Sleep After",
        "targets": [(SUB_SLEEP, STANDBY_IDLE)],
        "fmt": _dur,
    },
    "wifi": {
        "title": "Wi-Fi Power Saving",
        "targets": [(SUB_WIFI, WIFI_POWER_SAVE)],
        "fmt": lambda v: "Maximum Saving",
    },
    "usb": {
        "title": "USB Selective Suspend",
        "targets": [(SUB_USB, USB_SUSPEND)],
        "fmt": lambda v: "Enabled",
    },
    "aspm": {
        "title": "PCIe Power Management (ASPM)",
        "targets": [(SUB_PCIE, PCIE_ASPM)],
        "fmt": lambda v: "Maximum Saving",
    },
}

# Fixed values applied during "Maximum Saver" mode for specific non-slider settings
FIXED_SAVER = {"boost": 0, "epp": 75, "wifi": 3, "usb": 1, "aspm": 2}

# --------------------------------------------------------------------------
# UI Color Palette (Dark Theme)
# --------------------------------------------------------------------------
BG = "#121212"
CARD = "#1E1E1E"
BTN = "#2B2B2B"
BTN_HOVER = "#3C3C3C"
TEXT = "#EAEAEA"
MUTED = "#9A9A9A"
GREEN = "#2ECC71"
GREEN_HOVER = "#27AE60"
ORANGE = "#FF6D00"
ORANGE_HOVER = "#E65100"
PURPLE = "#8E44AD"
PURPLE_HOVER = "#7D3C98"
WARN = "#F1C40F"
RED = "#E74C3C"


# --------------------------------------------------------------------------
# PowerCFG Helpers
# --------------------------------------------------------------------------
class PowerCfgError(Exception):
    pass


GUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _decode(data: bytes) -> str:
    for enc in ("oem", "utf-8"):
        try:
            return data.decode(enc, errors="replace").strip()
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def run_powercfg(*args: str) -> str:
    """Executes a powercfg command silently; raises PowerCfgError on failure."""
    cmd = ["powercfg", *args]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(cmd, capture_output=True, creationflags=flags)
    except FileNotFoundError:
        raise PowerCfgError("powercfg executable not found. This application requires Windows OS.")
    out = _decode(result.stdout)
    err = _decode(result.stderr)
    if result.returncode != 0:
        detail = err or out or f"exit code {result.returncode}"
        raise PowerCfgError(f"'powercfg {' '.join(args)}' failed:\n{detail}")
    return out


def get_active_scheme() -> str:
    match = GUID_RE.search(run_powercfg("/getactivescheme"))
    if not match:
        raise PowerCfgError("Could not retrieve active power scheme.")
    return match.group(0).lower()


def ensure_scheme(guid: str) -> None:
    """Creates the power scheme duplicate if it does not exist on the system."""
    try:
        run_powercfg("/query", guid)
    except PowerCfgError:
        run_powercfg("/duplicatescheme", guid, guid)


def query_values(scheme: str, sub: str, setting: str):
    """Returns (AC, DC) current values, or None if the setting is missing."""
    try:
        out = run_powercfg("/query", scheme, sub, setting)
    except PowerCfgError:
        return None
    nums = re.findall(r"0x([0-9a-fA-F]{8})", out)
    if len(nums) < 2:
        return None
    return int(nums[-2], 16), int(nums[-1], 16)


def set_setting(scheme: str, key: str, value: int, *, dc: bool = True, ac: bool = False) -> None:
    ok, last = False, None
    for sub, setting in SETTINGS[key]["targets"]:
        try:
            if dc:
                run_powercfg("/setdcvalueindex", scheme, sub, setting, str(value))
            if ac:
                run_powercfg("/setacvalueindex", scheme, sub, setting, str(value))
            ok = True
        except PowerCfgError as exc:
            last = exc
    if not ok and last is not None:
        raise last


# --------------------------------------------------------------------------
# Display Refresh Rate Control (Windows User32 API)
# --------------------------------------------------------------------------
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
CDS_UPDATEREGISTRY = 0x00000001
CDS_TEST = 0x00000002


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class RefreshError(Exception):
    pass


def _user32():
    try:
        user32 = ctypes.WinDLL("user32")
    except (AttributeError, OSError):
        raise RefreshError("Display refresh rate can only be adjusted on Windows.")
    user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
    user32.EnumDisplaySettingsW.restype = wintypes.BOOL
    user32.ChangeDisplaySettingsExW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(DEVMODEW), wintypes.HWND, wintypes.DWORD, wintypes.LPVOID
    ]
    user32.ChangeDisplaySettingsExW.restype = ctypes.c_long
    return user32


def _new_devmode() -> DEVMODEW:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    return dm


def get_refresh_info():
    """Returns (current_hz, supported_rates_list) for the primary display."""
    user32 = _user32()
    cur = _new_devmode()
    if not user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(cur)):
        raise RefreshError("Failed to read current display settings.")
    rates, index, dm = set(), 0, _new_devmode()
    while user32.EnumDisplaySettingsW(None, index, ctypes.byref(dm)):
        if (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmBitsPerPel) == (
                cur.dmPelsWidth, cur.dmPelsHeight, cur.dmBitsPerPel):
            rates.add(int(dm.dmDisplayFrequency))
        index += 1
    rates = {r for r in rates if r >= 50}
    rates = sorted(r for r in rates if (r + 1) not in rates)  # Deduplicate 59/60, 164/165 Hz pairs
    return int(cur.dmDisplayFrequency), rates


def set_refresh_rate(hz: int) -> None:
    user32 = _user32()
    dm = _new_devmode()
    if not user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        raise RefreshError("Failed to read current display settings.")
    dm.dmDisplayFrequency = hz
    dm.dmFields = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
    result = user32.ChangeDisplaySettingsExW(None, ctypes.byref(dm), None, CDS_TEST, None)
    if result != 0:
        raise RefreshError(f"{hz} Hz is not supported in this display mode (code {result}).")
    result = user32.ChangeDisplaySettingsExW(None, ctypes.byref(dm), None, CDS_UPDATEREGISTRY, None)
    if result == 1:
        raise RefreshError("A system reboot is required to apply the refresh rate change.")
    if result != 0:
        raise RefreshError(f"Failed to apply {hz} Hz (code {result}).")


def pick_saver_rate(rates) -> int | None:
    return min(rates, key=lambda r: abs(r - 60)) if rates else None


# --------------------------------------------------------------------------
# Backup & Restore Logic
# --------------------------------------------------------------------------
def backup_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "WinBatterySaverWizard")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "backup.json")


def has_backup() -> bool:
    try:
        return os.path.exists(backup_path())
    except OSError:
        return False


def load_backup():
    try:
        with open(backup_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def ensure_backup() -> None:
    """Backs up initial power settings once before any modifications take place."""
    if has_backup():
        return
    active = get_active_scheme()
    schemes = {}
    for guid in {active, GUID_POWER_SAVER, NORMAL_SCHEME}:
        values = {}
        for spec in SETTINGS.values():
            for sub, setting in spec["targets"]:
                got = query_values(guid, sub, setting)
                if got:
                    values[f"{sub}|{setting}"] = {"ac": got[0], "dc": got[1]}
        if values:
            schemes[guid] = values
    try:
        refresh = get_refresh_info()[0]
    except RefreshError:
        refresh = None
    data = {"active_scheme": active, "refresh_hz": refresh, "schemes": schemes}
    with open(backup_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def restore_scheme_values(scheme: str, values: dict) -> int:
    restored = 0
    for key, val in values.items():
        sub, setting = key.split("|")
        for kind, cmd in (("ac", "/setacvalueindex"), ("dc", "/setdcvalueindex")):
            try:
                run_powercfg(cmd, scheme, sub, setting, str(val[kind]))
                restored += 1
            except PowerCfgError:
                pass
    return restored


# --------------------------------------------------------------------------
# Actions & Profile Handlers
# --------------------------------------------------------------------------
def apply_max_saving(values: dict, refresh_hz: int | None) -> str:
    ensure_backup()
    ensure_scheme(GUID_POWER_SAVER)
    done, skipped, last_error = [], [], None
    for key, val in values.items():
        try:
            set_setting(GUID_POWER_SAVER, key, val, dc=True, ac=False)
            done.append(f"{SETTINGS[key]['title']}: {SETTINGS[key]['fmt'](val)}")
        except PowerCfgError as exc:
            skipped.append(SETTINGS[key]["title"])
            last_error = exc
    if values and not done and last_error is not None:
        raise last_error
    run_powercfg("/setactive", GUID_POWER_SAVER)
    if refresh_hz:
        try:
            cur = get_refresh_info()[0]
            if cur != refresh_hz:
                set_refresh_rate(refresh_hz)
            done.append(f"Display Refresh Rate: {refresh_hz} Hz")
        except RefreshError as exc:
            skipped.append(f"Display Refresh Rate ({exc})")

    msg = "Maximum Saver mode activated!\n\nPower Plan: Power Saver"
    if done:
        msg += "\n" + "\n".join(f"• {line}" for line in done)
    if skipped:
        msg += "\n\nSkipped (unsupported or inaccessible on this hardware):\n"
        msg += "\n".join(f"• {line}" for line in skipped)
    return msg


def apply_normal(refresh_max: bool) -> str:
    ensure_backup()
    base = (load_backup() or {}).get("schemes", {})
    ensure_scheme(NORMAL_SCHEME)
    for scheme in (NORMAL_SCHEME, GUID_POWER_SAVER):
        if scheme in base:
            restore_scheme_values(scheme, base[scheme])
        if scheme == NORMAL_SCHEME or scheme not in base:
            for key, val in (("cpu_max", 100), ("boost", 2)):
                try:
                    set_setting(scheme, key, val, dc=True, ac=True)
                except PowerCfgError:
                    if scheme == NORMAL_SCHEME and key == "cpu_max":
                        raise
    run_powercfg("/setactive", NORMAL_SCHEME)
    msg = ("Balanced / Gaming mode activated!\n\n"
           "• Power Plan: Balanced\n"
           "• Max Processor State: 100% (AC & DC)\n"
           "• Turbo Boost: Default")
    if refresh_max:
        try:
            _cur, rates = get_refresh_info()
            if rates:
                max_hz = max(rates)
                set_refresh_rate(max_hz)
                msg += f"\n• Display Refresh Rate: {max_hz} Hz"
        except RefreshError as exc:
            msg += f"\n\nCould not change refresh rate: {exc}"
    return msg


def apply_single(key: str, value: int) -> str:
    ensure_backup()
    scheme = get_active_scheme()
    set_setting(scheme, key, value, dc=True, ac=False)
    run_powercfg("/setactive", scheme)
    return f"{SETTINGS[key]['title']} set to: {SETTINGS[key]['fmt'](value)} (on battery, active plan)"


def apply_refresh(hz: int) -> str:
    ensure_backup()
    set_refresh_rate(hz)
    return f"Display refresh rate set to {hz} Hz"


def reset_all() -> str:
    base = load_backup()
    if not base:
        raise PowerCfgError("Backup file could not be read.")
    restored = 0
    for scheme, values in base.get("schemes", {}).items():
        restored += restore_scheme_values(scheme, values)
    target = base.get("active_scheme") or NORMAL_SCHEME
    try:
        run_powercfg("/setactive", target)
    except PowerCfgError:
        run_powercfg("/setactive", NORMAL_SCHEME)
    msg = f"Restored to original settings!\n\n• Power plan and {restored} settings restored from backup."
    hz = base.get("refresh_hz")
    if hz:
        try:
            set_refresh_rate(int(hz))
            msg += f"\n• Display Refresh Rate: {hz} Hz"
        except RefreshError as exc:
            msg += f"\n\nRefresh rate could not be restored: {exc}"
    try:
        os.remove(backup_path())
    except OSError:
        pass
    return msg


def restore_windows_defaults() -> str:
    run_powercfg("/restoredefaultschemes")
    return ("Windows default power plans restored.\n\n"
            "If refresh rate was altered, manually set it back via the buttons on the right.")


# --------------------------------------------------------------------------
# Battery Diagnostics via WMI & Windows API
# --------------------------------------------------------------------------
class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlags", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def read_system_power():
    try:
        sps = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            return None
        return sps
    except Exception:
        return None


PS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$o=@{};"
    "$s=Get-CimInstance -Namespace root/wmi -ClassName BatteryStatus|Select-Object -First 1;"
    "if($s){$o.rem=$s.RemainingCapacity;$o.dis=$s.DischargeRate;$o.chg=$s.ChargeRate};"
    "$f=Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity|Select-Object -First 1;"
    "if($f){$o.full=$f.FullChargedCapacity};"
    "$d=Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData|Select-Object -First 1;"
    "if($d){$o.design=$d.DesignedCapacity};"
    "$o|ConvertTo-Json -Compress"
)


def poll_wmi():
    """Polls WMI for capacity (mWh), discharge/charge rate (mW), and battery health data."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
        capture_output=True, creationflags=flags, timeout=25)
    text = _decode(result.stdout)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    data = json.loads(text[start:end + 1])
    return data or None


def fmt_hours(hours: float) -> str:
    total = int(round(hours * 60))
    return f"{total // 60} h {total % 60:02d} m"


# --------------------------------------------------------------------------
# User Interface (Tkinter)
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Windows Battery Saver Wizard")
        self.configure(bg=BG)
        self.resizable(False, False)

        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.polling = False
        self.wmi: dict = {}
        self.wmi_ok = None
        self.rates: deque = deque(maxlen=RATE_SAMPLES)
        self.last_ac = None
        self.tick_n = 0
        self.action_buttons: list = []
        self.refresh_buttons: list = []
        self.include: dict = {}
        self.controls: dict = {}
        self.avail_rates: list = []

        self._build_header()
        body = tk.Frame(self, bg=BG)
        body.pack(padx=20, pady=(0, 6))
        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="n", padx=(0, 14))
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="n")

        self._build_battery(left)
        self._build_modes(left)
        self._build_admin_note(left)
        self._build_advanced(right)
        self._build_tip()
        self._center()
        self.after(300, self._tick)

    # -- UI Helpers ---------------------------------------------------------
    def _center(self) -> None:
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = max((self.winfo_screenwidth() - w) // 2, 0)
        y = max((self.winfo_screenheight() - h) // 2 - 20, 0)
        self.geometry(f"+{x}+{y}")

    def _button(self, parent, text, command, bg=BTN, hover=BTN_HOVER, fg=TEXT,
                font=("Segoe UI", 9, "bold"), register=True, **kw) -> tk.Button:
        btn = tk.Button(parent, text=text, command=command, font=font, bg=bg, fg=fg,
                        activebackground=hover, activeforeground=fg, disabledforeground=MUTED,
                        relief="flat", bd=0, cursor="hand2", **kw)
        btn.bind("<Enter>", lambda _e: btn.config(bg=hover) if str(btn["state"]) == "normal" else None)
        btn.bind("<Leave>", lambda _e: btn.config(bg=bg))
        if register:
            self.action_buttons.append(btn)
        return btn

    def _card(self, parent, **pack) -> tk.Frame:
        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="x", pady=(0, 10), **pack)
        return card

    # -- Header Section -----------------------------------------------------
    def _build_header(self) -> None:
        tk.Label(self, text="Ultra Battery Saver for Laptops",
                 font=("Segoe UI", 20, "bold"), bg=BG, fg=TEXT).pack(pady=(16, 0))
        tk.Label(self, text="Universal Power & Refresh Rate Optimizer for Laptops",
                 font=("Segoe UI", 10), bg=BG, fg=MUTED).pack(pady=(2, 12))

    # -- Battery Info Panel -------------------------------------------------
    def _build_battery(self, parent) -> None:
        card = self._card(parent)
        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(top, text="BATTERY STATUS", font=("Segoe UI", 9, "bold"), bg=CARD, fg=MUTED).pack(side="left")
        self._button(top, "↻ Refresh", self._poll_wmi_now, register=False,
                     font=("Segoe UI", 8), padx=6).pack(side="right")

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=14, pady=(6, 0))
        self.pct_label = tk.Label(row, text="—", font=("Segoe UI", 30, "bold"), bg=CARD, fg=TEXT)
        self.pct_label.pack(side="left")
        self.state_label = tk.Label(row, text="", font=("Segoe UI", 10), bg=CARD, fg=MUTED)
        self.state_label.pack(side="left", padx=12)

        self.bar = tk.Canvas(card, width=370, height=12, bg=BG, highlightthickness=0)
        self.bar.pack(padx=14, pady=(4, 8))

        self.est_main = tk.Label(card, text="Measuring…", font=("Segoe UI", 15, "bold"),
                                 bg=CARD, fg=GREEN, anchor="w")
        self.est_main.pack(fill="x", padx=14)
        self.est_sub = tk.Label(card, text="", font=("Segoe UI", 9), bg=CARD, fg=MUTED,
                                justify="left", anchor="w", wraplength=370)
        self.est_sub.pack(fill="x", padx=14, pady=(2, 0))
        self.health_label = tk.Label(card, text="", font=("Segoe UI", 9), bg=CARD, fg=MUTED,
                                     anchor="w", wraplength=370, justify="left")
        self.health_label.pack(fill="x", padx=14, pady=(2, 10))

    def _draw_bar(self, pct) -> None:
        self.bar.delete("all")
        width = int(self.bar["width"])
        if pct is None:
            return
        color = GREEN if pct > 50 else (WARN if pct > 20 else RED)
        self.bar.create_rectangle(0, 0, int(width * pct / 100), 12, fill=color, width=0)

    def _refresh_battery_ui(self) -> None:
        sps = read_system_power()
        if sps is None or sps.BatteryFlag == 128:
            self.pct_label.config(text="—")
            self.state_label.config(text="")
            self.est_main.config(text="Battery information unavailable", fg=MUTED)
            self.est_sub.config(text="No battery detected or read permission denied.")
            self.health_label.config(text="")
            self._draw_bar(None)
            return

        pct = sps.BatteryLifePercent if sps.BatteryLifePercent <= 100 else None
        on_ac = sps.ACLineStatus == 1
        charging = bool(sps.BatteryFlag & 8)

        if self.last_ac is not None and on_ac != self.last_ac:
            self.rates.clear()
            self.after(2000, self._poll_wmi)
        self.last_ac = on_ac

        self.pct_label.config(text=f"%{pct}" if pct is not None else "—")
        self._draw_bar(pct)
        if on_ac:
            self.state_label.config(text="🔌 Plugged in" + (" • Charging" if charging else ""))
        else:
            self.state_label.config(text="🔋 On Battery")

        d = self.wmi
        rem, full, design, chg = d.get("rem"), d.get("full"), d.get("design"), d.get("chg")
        avg_mw = sum(self.rates) / len(self.rates) if self.rates else None
        win_secs = sps.BatteryLifeTime if sps.BatteryLifeTime != 0xFFFFFFFF else None

        if on_ac:
            if charging and rem and full and chg and 0 < chg < 200000 and full > rem:
                self.est_main.config(text=f"Fully charged in ≈ {fmt_hours((full - rem) / chg)}", fg=GREEN)
                self.est_sub.config(text=f"Charge Power: {chg / 1000:.1f} W")
            else:
                self.est_main.config(text="Plugged into AC power", fg=GREEN)
                self.est_sub.config(text="Battery discharge calculation starts when unplugged.")
        elif avg_mw and rem:
            hours = rem / avg_mw
            self.est_main.config(text=f"≈ {fmt_hours(hours)} remaining", fg=GREEN if hours >= 3 else WARN)
            parts = [f"Discharge Rate: {avg_mw / 1000:.1f} W (Average of recent samples)"]
            if full:
                parts.append(f"Estimated total run time from 100%: {fmt_hours(full / avg_mw)}")
            if win_secs:
                parts.append(f"Windows system estimate: {fmt_hours(win_secs / 3600)}")
            if len(self.rates) < 3:
                parts.append("Initial sampling; estimate will stabilize shortly.")
            self.est_sub.config(text="\n".join(parts))
        elif self.wmi_ok is False:
            if win_secs:
                self.est_main.config(text=f"≈ {fmt_hours(win_secs / 3600)} remaining", fg=WARN)
                self.est_sub.config(text="Windows estimate used. WMI metrics unavailable; try running as Admin.")
            else:
                self.est_main.config(text="Estimate unavailable", fg=MUTED)
                self.est_sub.config(text="WMI battery polling failed; try running as Administrator.")
        else:
            self.est_main.config(text="Measuring…", fg=MUTED)
            self.est_sub.config(text="Measuring power draw in a few seconds.")

        if full and design:
            self.health_label.config(
                text=f"Battery Health: {full / design * 100:.0f}%  ({full / 1000:.1f} / {design / 1000:.1f} Wh)")
        else:
            self.health_label.config(text="")

    def _poll_wmi(self) -> None:
        if self.polling:
            return
        self.polling = True

        def work() -> None:
            try:
                data = poll_wmi()
            except Exception:
                data = None
            self.q.put(("wmi", data))

        threading.Thread(target=work, daemon=True).start()

    def _poll_wmi_now(self) -> None:
        self._poll_wmi()
        self._refresh_battery_ui()

    # -- Primary Profile Buttons -------------------------------------------
    def _build_modes(self, parent) -> None:
        self._button(parent, "🔋 MAXIMUM SAVER (On Battery)", self._mode_saver, GREEN, GREEN_HOVER, "#0B1F10",
                     font=("Segoe UI", 13, "bold"), height=2).pack(fill="x", pady=(0, 8))
        self._button(parent, "⚡ BALANCED / GAMING (On AC)", self._mode_normal, ORANGE, ORANGE_HOVER, "#FFFFFF",
                     font=("Segoe UI", 13, "bold"), height=2).pack(fill="x", pady=(0, 8))
        self._button(parent, "↩ RESTORE ORIGINAL SETTINGS", self._reset, PURPLE, PURPLE_HOVER, "#FFFFFF",
                     font=("Segoe UI", 11, "bold"), height=2).pack(fill="x", pady=(0, 8))
        self.status = tk.Label(parent, text="Active Profile: Not selected yet", font=("Segoe UI", 10),
                               bg=BG, fg=MUTED, wraplength=400, justify="left")
        self.status.pack(pady=(2, 6), anchor="w")

    def _build_admin_note(self, parent) -> None:
        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="x")
        if is_admin():
            tk.Label(card, text="✔ Administrator Privileges Active", font=("Segoe UI", 10, "bold"),
                     bg=CARD, fg=GREEN, pady=8).pack()
        else:
            tk.Label(card,
                     text=("⚠ Note: Certain powercfg operations require Administrator privileges.\n"
                           "If you encounter errors, restart the app as Administrator."),
                     font=("Segoe UI", 9), bg=CARD, fg=WARN, justify="center", wraplength=380,
                     pady=6).pack(padx=8)
            tk.Button(card, text="Restart as Administrator", command=self._relaunch_as_admin,
                      font=("Segoe UI", 9, "underline"), bg=CARD, fg=TEXT, activebackground=CARD,
                      activeforeground=GREEN, relief="flat", bd=0, cursor="hand2").pack(pady=(0, 8))

    # -- Advanced Configuration Panel --------------------------------------
    def _build_advanced(self, parent) -> None:
        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="both")
        tk.Label(card, text="⚙ ADVANCED SETTINGS", font=("Segoe UI", 9, "bold"),
                 bg=CARD, fg=MUTED).grid(row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 0))
        tk.Label(card,
                 text=("☑ Checked options will be included in the 'Maximum Saver' profile.\n"
                       "'Apply' modifies the parameter on the currently active power plan."),
                 font=("Segoe UI", 8), bg=CARD, fg=MUTED, justify="left").grid(
            row=1, column=0, columnspan=4, sticky="w", padx=14, pady=(2, 6))

        rows = [
            ("cpu_max", "scale", dict(frm=30, to=100, step=5, default=55)),
            ("boost", "fixed", "On Battery: Disabled"),
            ("epp", "fixed", "On Battery: Efficient (75)"),
            ("refresh", "refresh", None),
            ("brightness", "scale", dict(frm=10, to=100, step=5, default=40)),
            ("screen_off", "choice", dict(options=[("30 sec", 30), ("1 min", 60), ("2 min", 120), ("5 min", 300)],
                                         default="1 min")),
            ("sleep", "choice", dict(options=[("5 min", 300), ("10 min", 600), ("15 min", 900), ("30 min", 1800)],
                                     default="10 min")),
            ("wifi", "fixed", "On Battery: Max Saving"),
            ("usb", "fixed", "On Battery: Enabled"),
            ("aspm", "fixed", "On Battery: Max Saving"),
        ]
        for i, (key, kind, spec) in enumerate(rows, start=2):
            title = "Display Refresh Rate" if key == "refresh" else SETTINGS[key]["title"]
            var = tk.BooleanVar(value=(key != "sleep"))
            self.include[key] = var
            tk.Checkbutton(card, variable=var, bg=CARD, activebackground=CARD, selectcolor=BG,
                           fg=GREEN, activeforeground=GREEN, highlightthickness=0, bd=0).grid(
                row=i, column=0, padx=(12, 0), pady=3)
            tk.Label(card, text=title, font=("Segoe UI", 10), bg=CARD, fg=TEXT, anchor="w",
                     width=26).grid(row=i, column=1, sticky="w", pady=3)
            holder = tk.Frame(card, bg=CARD)
            holder.grid(row=i, column=2, sticky="w", padx=6, pady=3)

            if kind == "scale":
                scale = tk.Scale(holder, from_=spec["frm"], to=spec["to"], resolution=spec["step"],
                                 orient="horizontal", length=170, bg=CARD, fg=TEXT, troughcolor=BG,
                                 highlightthickness=0, bd=0, activebackground=GREEN, sliderrelief="flat")
                scale.set(spec["default"])
                scale.pack()
                self.controls[key] = scale
            elif kind == "choice":
                labels = [lab for lab, _sec in spec["options"]]
                svar = tk.StringVar(value=spec["default"])
                menu = tk.OptionMenu(holder, svar, *labels)
                menu.config(bg=BTN, fg=TEXT, activebackground=BTN_HOVER, activeforeground=TEXT,
                            highlightthickness=0, relief="flat", bd=0, width=8, font=("Segoe UI", 9))
                menu["menu"].config(bg=CARD, fg=TEXT, activebackground=GREEN, activeforeground="#000000")
                menu.pack()
                self.controls[key] = (svar, dict(spec["options"]))
            elif kind == "fixed":
                tk.Label(holder, text=spec, font=("Segoe UI", 9), bg=CARD, fg=MUTED).pack()
            elif kind == "refresh":
                self._build_refresh_controls(holder)

            if key != "refresh":
                self._button(card, "Apply", lambda k=key: self._apply_single(k), padx=10, pady=2).grid(
                    row=i, column=3, padx=(4, 12), pady=3)
        tk.Label(card, text="Refresh rate: ≈60 Hz on Saver, Maximum on Normal (buttons apply immediately).",
                 font=("Segoe UI", 8), bg=CARD, fg=MUTED).grid(
            row=len(rows) + 2, column=0, columnspan=4, sticky="w", padx=14, pady=(2, 10))

    def _build_refresh_controls(self, holder) -> None:
        self.hz_label = tk.Label(holder, text="", font=("Segoe UI", 9), bg=CARD, fg=TEXT)
        self.hz_label.pack(anchor="w")
        frame = tk.Frame(holder, bg=CARD)
        frame.pack(anchor="w")
        try:
            current, rates = get_refresh_info()
            self.avail_rates = rates
            self.hz_label.config(text=f"Current: {current} Hz")
            for rate in rates:
                btn = self._button(frame, f"{rate} Hz", lambda r=rate: self._apply_refresh(r), padx=7, pady=1)
                btn.pack(side="left", padx=(0, 4), pady=2)
        except RefreshError as exc:
            self.hz_label.config(text="Unavailable", fg=MUTED)
            tk.Label(frame, text=str(exc), font=("Segoe UI", 8), bg=CARD, fg=MUTED,
                     wraplength=220, justify="left").pack()

    def _update_refresh_label(self) -> None:
        try:
            current, _rates = get_refresh_info()
            self.hz_label.config(text=f"Current: {current} Hz")
        except Exception:
            pass

    def _build_tip(self) -> None:
        tk.Label(self,
                 text=("Tip: While on battery, use iGPU (Hybrid/Optimus) mode and close heavy background apps. "
                       "Remaining time is calculated live based on current power draw."),
                 font=("Segoe UI", 9), bg=BG, fg=MUTED, wraplength=860, justify="center").pack(pady=(4, 12))

    # -- Value Resolvers ---------------------------------------------------
    def _value_of(self, key: str) -> int:
        ctrl = self.controls.get(key)
        if key in FIXED_SAVER:
            return FIXED_SAVER[key]
        if isinstance(ctrl, tuple):
            svar, mapping = ctrl
            return mapping[svar.get()]
        return int(ctrl.get())

    def _collect_saver_values(self) -> dict:
        values = {}
        for key in SETTINGS:
            if self.include[key].get():
                values[key] = self._value_of(key)
        return values

    # -- Event Triggers ----------------------------------------------------
    def _mode_saver(self) -> None:
        values = self._collect_saver_values()
        refresh = pick_saver_rate(self.avail_rates) if self.include["refresh"].get() else None
        self._run(lambda: apply_max_saving(values, refresh), "Maximum Saver", "mode")

    def _mode_normal(self) -> None:
        want_refresh = self.include["refresh"].get()
        self._run(lambda: apply_normal(want_refresh), "Balanced / Gaming Mode", "mode")

    def _apply_single(self, key: str) -> None:
        value = self._value_of(key)
        self._run(lambda: apply_single(key, value), SETTINGS[key]["title"], "single")

    def _apply_refresh(self, hz: int) -> None:
        self._run(lambda: apply_refresh(hz), "Display Refresh Rate", "single")

    def _reset(self) -> None:
        if has_backup():
            if not messagebox.askyesno(
                    "Restore Originals",
                    "Do you want to restore all original power settings, power plans, and refresh rate?"):
                return
            self._run(reset_all, "Restoring Settings", "reset")
        else:
            if not messagebox.askyesno(
                    "No Backup Found",
                    "No previous backup was found. Reset Windows default power plans?\n\n"
                    "(Note: Any custom OEM vendor plans might be removed.)"):
                return
            self._run(restore_windows_defaults, "Restoring Windows Defaults", "reset")

    # -- Thread Execution Engine -------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in self.action_buttons:
            try:
                btn.config(state=state)
            except tk.TclError:
                pass

    def _run(self, action, label: str, kind: str) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.status.config(text=f"⏳ Applying {label}…", fg=MUTED)

        def work() -> None:
            try:
                res = ("ok", action())
            except (PowerCfgError, RefreshError) as exc:
                res = ("err", str(exc))
            except Exception as exc:
                res = ("err", f"Unexpected error: {exc}")
            self.q.put(("done", label, kind, res))

        threading.Thread(target=work, daemon=True).start()

    def _on_done(self, label: str, kind: str, res) -> None:
        self._set_busy(False)
        state, payload = res
        if state != "ok":
            hint = "" if is_admin() else "\n\nTip: Try re-launching the application as Administrator."
            self.status.config(text=f"✖ Could not apply {label}", fg=RED)
            messagebox.showerror("Error", f"{payload}{hint}")
            return
        self.rates.clear()
        self.after(3000, self._poll_wmi)
        self._update_refresh_label()
        if kind == "single":
            self.status.config(text=f"✔ {payload}", fg=GREEN)
        elif kind == "reset":
            self.status.config(text="Active Profile: Original Settings", fg=TEXT)
            messagebox.showinfo("Success", payload)
        else:
            self.status.config(text=f"Active Profile: {label}", fg=TEXT)
            messagebox.showinfo("Success", payload)

    def _tick(self) -> None:
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.tick_n % 3 == 0:
            self._refresh_battery_ui()
        if self.tick_n % POLL_EVERY == 0:
            self._poll_wmi()
        self.tick_n += 1
        self.after(1000, self._tick)

    def _handle(self, item) -> None:
        if item[0] == "wmi":
            self.polling = False
            data = item[1]
            if data and data.get("rem") is not None:
                self.wmi.update(data)
                self.wmi_ok = True
                dis = data.get("dis")
                if self.last_ac is False and isinstance(dis, (int, float)) and 0 < abs(dis) < 200000:
                    self.rates.append(abs(dis))
            else:
                self.wmi_ok = False
            self._refresh_battery_ui()
        elif item[0] == "done":
            self._on_done(item[1], item[2], item[3])

    def _relaunch_as_admin(self) -> None:
        try:
            if getattr(sys, "frozen", False):
                exe, params = sys.executable, " ".join(f'"{a}"' for a in sys.argv[1:])
            else:
                exe, params = sys.executable, f'"{os.path.abspath(__file__)}"'
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            if result > 32:
                self.destroy()
            else:
                messagebox.showwarning("Cancelled", "Administrator privilege was not granted.")
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to restart as Administrator:\n{exc}")


def main() -> None:
    try:  # Enable High-DPI scaling awareness
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()