#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF-Compress-Py - robust batch PDF compression powered by Ghostscript.

  * Never touches your originals (unless you explicitly pass --in-place).
  * Tolerates slightly damaged PDFs instead of giving up on them.
  * Verifies every result before it is accepted and keeps the original when
    compression does not actually make the file smaller.
  * Works with any file name (Unicode, spaces, '%', brackets, ...) on
    Windows 10+ and Linux. Standard library only - no pip install needed.

Run without arguments in a terminal for an interactive menu, or see
`pdf_compress.py --help` for the command-line interface.

Ghostscript is a separate program (AGPL) that must be installed; this script
only drives it. See README.md for details.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXIT_OK = 0
EXIT_FILE_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


# =============================================================================
# Console helpers (Unicode-safe output, optional ANSI colors)
# =============================================================================

def _enable_windows_vt() -> bool:
    """Turn on ANSI escape processing in the Windows 10 console."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def setup_console(no_color: bool) -> bool:
    """Make stdout/stderr safe for any text and report whether colors can be used."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if IS_WINDOWS and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            else:
                stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
    if no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if not sys.stdout.isatty():
        return False
    return _enable_windows_vt() if IS_WINDOWS else True


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def truncate_middle(text: str, width: int) -> str:
    """Shorten text to fit `width` terminal cells, keeping both ends."""
    if display_width(text) <= width:
        return text
    if width <= 3:
        return text[:max(width, 0)]
    budget = width - 3
    head_budget = budget - budget // 2
    tail_budget = budget // 2
    head, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > head_budget:
            break
        head.append(ch)
        used += w
    tail, used = [], 0
    for ch in reversed(text):
        w = char_width(ch)
        if used + w > tail_budget:
            break
        tail.append(ch)
        used += w
    return "".join(head) + "..." + "".join(reversed(tail))


def human_size(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    n /= 1024
    if n < 1024:
        return f"{n:.1f} KB"
    n /= 1024
    if n < 1024:
        return f"{n:.2f} MB"
    return f"{n / 1024:.2f} GB"


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


STYLES = {"bold": "1", "dim": "90", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


class Logger:
    """Thread-safe, UTF-8 log file writer (does nothing when no path is given)."""

    def __init__(self, path: Optional[Path]) -> None:
        self._lock = threading.Lock()
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8", errors="replace")

    def write(self, message: str) -> None:
        if self._fh is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            for line in message.splitlines() or [""]:
                self._fh.write(f"{stamp}  {line}\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class UI:
    """All terminal output goes through here so parallel workers never garble it."""

    def __init__(self, color: bool, live: bool = False) -> None:
        self.color = color
        self.live = live  # transient single-line progress display
        self._lock = threading.RLock()
        self._status_active = False

    def style(self, text: str, *names: str) -> str:
        if not self.color or not names:
            return text
        codes = ";".join(STYLES[n] for n in names)
        return f"\x1b[{codes}m{text}\x1b[0m"

    @staticmethod
    def _columns() -> int:
        return max(shutil.get_terminal_size((80, 24)).columns - 1, 20)

    def _clear_status(self) -> None:
        if self._status_active:
            sys.stdout.write("\r" + " " * self._columns() + "\r")
            self._status_active = False

    def line(self, text: str = "", *styles: str) -> None:
        with self._lock:
            self._clear_status()
            print(self.style(text, *styles), flush=True)

    def status(self, text: str) -> None:
        if not self.live:
            return
        with self._lock:
            cols = self._columns()
            text = truncate_middle(text, cols)
            sys.stdout.write("\r" + text + " " * max(cols - display_width(text), 0))
            sys.stdout.flush()
            self._status_active = True

    def clear_status(self) -> None:
        with self._lock:
            self._clear_status()
            sys.stdout.flush()


# =============================================================================
# Ghostscript discovery
# =============================================================================

class GhostscriptNotFound(Exception):
    pass


def version_key(text: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:4])


def probe_ghostscript(path: str) -> Optional[str]:
    """Return the version string if `path` is a working Ghostscript executable."""
    try:
        proc = subprocess.run(
            [path, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if text and re.match(r"^\d+(\.\d+)+", text[0].strip()):
        return text[0].strip()
    return None


def _windows_gs_candidates() -> List[str]:
    found: List[Tuple[Tuple[int, ...], str]] = []
    bases = [os.environ.get(v) for v in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.append(os.path.join(local, "Programs"))
    seen = set()
    for base in bases:
        if not base or base.lower() in seen:
            continue
        seen.add(base.lower())
        for exe in ("gswin64c.exe", "gswin32c.exe"):
            for hit in Path(base).glob(f"gs/gs*/bin/{exe}"):
                found.append((version_key(hit.parent.parent.name), str(hit)))
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def find_ghostscript(explicit: Optional[str]) -> Tuple[str, str]:
    """Locate Ghostscript. Returns (executable path, version)."""
    script_dir = Path(__file__).resolve().parent
    candidates: List[str] = []

    requested = explicit or os.environ.get("PDF_COMPRESS_GS")
    if requested:
        resolved = shutil.which(requested) or requested
        version = probe_ghostscript(resolved)
        if version:
            return resolved, version
        raise GhostscriptNotFound(f"The Ghostscript executable you specified does not work: {requested}")

    # Portable layouts next to the script.
    exe_names = ("gswin64c.exe", "gswin32c.exe") if IS_WINDOWS else ("gs",)
    for folder in (script_dir / "ghostscript" / "bin", script_dir / "tools" / "ghostscript" / "bin"):
        for name in exe_names:
            candidates.append(str(folder / name))
    # PATH.
    for name in (("gswin64c", "gswin32c", "gs") if IS_WINDOWS else ("gs",)):
        hit = shutil.which(name)
        if hit:
            candidates.append(hit)
    # Default Windows installer locations (the installer does not add itself to PATH).
    if IS_WINDOWS:
        candidates.extend(_windows_gs_candidates())

    tried = set()
    for candidate in candidates:
        key = os.path.normcase(candidate)
        if key in tried or not os.path.isfile(candidate):
            continue
        tried.add(key)
        version = probe_ghostscript(candidate)
        if version:
            return candidate, version
    raise GhostscriptNotFound("Ghostscript was not found on this computer.")


def ghostscript_install_help() -> str:
    if IS_WINDOWS:
        how = (
            "  Windows:  winget install ArtifexSoftware.GhostScript\n"
            "            or download the installer from https://ghostscript.com/releases/gsdnld.html\n"
            "            (the script finds the default install location automatically)"
        )
    elif sys.platform == "darwin":
        how = "  macOS:    brew install ghostscript"
    else:
        how = ("  Debian/Ubuntu:  sudo apt install ghostscript\n"
               "  Fedora/RHEL:    sudo dnf install ghostscript\n"
               "  Arch:           sudo pacman -S ghostscript")
    return (
        "Install Ghostscript and run this script again:\n" + how + "\n"
        "Already installed somewhere unusual? Use --gs PATH or set PDF_COMPRESS_GS."
    )


# =============================================================================
# Compression profiles
# =============================================================================

@dataclasses.dataclass(frozen=True)
class Profile:
    name: str
    quality: Optional[int]      # JPEG quality 1-100; None = lossless (Flate, no downsampling)
    color_dpi: int
    gray_dpi: int
    mono_dpi: int
    downsample_type: str        # Bicubic | Average | Subsample
    threshold: float            # only downsample when image DPI > threshold * target DPI
    color: str                  # keep | rgb | gray
    passthrough_jpeg: bool      # keep already-JPEG images that need no change as they are

    @property
    def label(self) -> str:
        if self.quality is None:
            return f"{self.name} (no image downsampling or recompression)"
        return f"{self.name} ({self.color_dpi} DPI, JPEG quality {self.quality}, colors: {self.color})"


PRESETS: Dict[str, Profile] = {
    "standard": Profile("Standard", 85, 150, 150, 300, "Bicubic", 1.5, "rgb", True),
    "high": Profile("High quality", 95, 300, 300, 600, "Bicubic", 1.5, "keep", True),
    "max": Profile("Maximum compression", 70, 96, 96, 200, "Average", 1.5, "rgb", False),
    "lossless": Profile("Lossless", None, 300, 300, 600, "Bicubic", 1.5, "keep", True),
}

PRESET_HELP = {
    "standard": "150 DPI, JPEG 85 - good balance of quality and size (default)",
    "high": "300 DPI, JPEG 95, colors untouched - for printing and archiving",
    "max": "96 DPI, JPEG 70 - smallest files, e.g. for e-mail",
    "lossless": "no downsampling, no lossy recompression - structural optimization only",
}


def quality_to_qfactor(quality: int) -> float:
    """Convert a familiar JPEG quality (1-100) to Ghostscript's QFactor.

    Ghostscript scales the standard JPEG quantization tables by QFactor * 100 %,
    exactly like libjpeg's quality setting. A QFactor of precisely 1.0 is special:
    Ghostscript silently switches to its default tables, which breaks the
    "higher number = smaller file" rule, so that single value is nudged.
    """
    quality = max(1, min(100, quality))
    qfactor = (100 - quality) / 50.0 if quality >= 50 else 50.0 / quality
    qfactor = max(qfactor, 0.05)
    if abs(qfactor - 1.0) < 0.005:
        qfactor = 1.01
    return round(qfactor, 3)


def _jpeg_dict(qfactor: float) -> str:
    samples = "[2 1 1 2]" if qfactor >= 0.2 else "[1 1 1 1]"  # no chroma subsampling at the highest qualities
    return f"/QFactor {qfactor} /Blend 1 /HSamples {samples} /VSamples {samples}"


def build_gs_args(profile: Profile, *, source: str, output: str, strip_metadata: bool,
                  strict: bool, extra: Sequence[str] = ()) -> List[str]:
    lossy = profile.quality is not None
    args = [
        "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=pdfwrite",
        "-dAutoRotatePages=/None",
        "-dPreserveAnnots=true",
        "-dEmbedAllFonts=true", "-dSubsetFonts=true", "-dCompressFonts=true",
        "-dDetectDuplicateImages=true",
    ]
    if strict:
        args.append("-dPDFSTOPONERROR")

    # Image resampling.
    downsample = "true" if lossy else "false"
    for kind, dpi in (("Color", profile.color_dpi), ("Gray", profile.gray_dpi), ("Mono", profile.mono_dpi)):
        args += [
            f"-dDownsample{kind}Images={downsample}",
            f"-d{kind}ImageResolution={dpi}",
            f"-d{kind}ImageDownsampleThreshold={profile.threshold}",
        ]
        if kind != "Mono":
            args.append(f"-d{kind}ImageDownsampleType=/{profile.downsample_type}")

    # Image encoding.
    if lossy:
        args += ["-dAutoFilterColorImages=true", "-dAutoFilterGrayImages=true",
                 "-dColorImageFilter=/DCTEncode", "-dGrayImageFilter=/DCTEncode"]
    else:
        args += ["-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
                 "-dColorImageFilter=/FlateEncode", "-dGrayImageFilter=/FlateEncode"]
    passthrough = "true" if profile.passthrough_jpeg else "false"
    args += [f"-dPassThroughJPEGImages={passthrough}", f"-dPassThroughJPXImages={passthrough}"]

    # Color handling.
    if profile.color == "gray":
        args += ["-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray"]
    elif profile.color == "rgb":
        args.append("-sColorConversionStrategy=RGB")
    else:
        args.append("-sColorConversionStrategy=LeaveColorUnchanged")

    if strip_metadata:
        args += ["-dOmitInfoDate", "-dOmitXMP", "-dOmitID"]

    args += list(extra)  # later switches override earlier ones
    args.append(f"-sOutputFile={output}")

    if lossy:
        jpeg = _jpeg_dict(quality_to_qfactor(profile.quality))  # type: ignore[arg-type]
        color_dict = f"<< {jpeg} /ColorTransform 1 >>"
        gray_dict = f"<< {jpeg} >>"
        args += ["-c", ("<< /ColorACSImageDict " + color_dict + " /ColorImageDict " + color_dict +
                        " /GrayACSImageDict " + gray_dict + " /GrayImageDict " + gray_dict +
                        " >> setdistillerparams")]
    args += ["-f", source]
    if strip_metadata:
        args += ["-c", "[ /Title () /Author () /Subject () /Keywords () /Creator () /DOCINFO pdfmark"]
    return args


# Retry ladder: when the normal run fails, retry once with the fragile
# optimizations switched off before giving up on the file.
@dataclasses.dataclass(frozen=True)
class Attempt:
    name: str
    extra: Tuple[str, ...]


ATTEMPTS: Tuple[Attempt, ...] = (
    Attempt("normal", ()),
    Attempt("safe mode", (
        "-dDetectDuplicateImages=false",
        "-dPassThroughJPEGImages=false", "-dPassThroughJPXImages=false",
        "-dSubsetFonts=false", "-dCompressFonts=false",
    )),
)


# =============================================================================
# Running Ghostscript and judging the outcome
# =============================================================================

RE_PROCESSING = re.compile(r"^Processing pages \d+ through (\d+)\.")
RE_PAGE = re.compile(r"^Page (\d+)\s*$")
RE_PASSWORD = re.compile(r"requires a password|password did not work|incorrect password", re.I)
RE_ERRORS_HEADER = re.compile(r"^The following errors were encountered")
RE_WARNINGS_HEADER = re.compile(r"^The following warnings were encountered")
RE_INLINE_ERROR = re.compile(r"^\s*\*{4} Error:\s*(.*)$")

_CANCEL = threading.Event()
_PROCS_LOCK = threading.Lock()
_PROCS: List["subprocess.Popen[str]"] = []


def kill_running_processes() -> None:
    with _PROCS_LOCK:
        procs = list(_PROCS)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


class GsOutput:
    """Incrementally parses Ghostscript's console output."""

    def __init__(self) -> None:
        self.tail: Deque[str] = deque(maxlen=300)
        self.pages_expected: Optional[int] = None
        self.pages_written = 0
        self.password = False
        self.fatal_line: Optional[str] = None
        self.errors: List[str] = []      # problems Ghostscript recovered from
        self.warnings: List[str] = []
        self._block: Optional[List[str]] = None

    def feed(self, line: str) -> bool:
        """Consume one line; returns True if it announced a finished page."""
        self.tail.append(line)
        match = RE_PROCESSING.match(line)
        if match:
            self.pages_expected = int(match.group(1))
            return False
        if RE_PAGE.match(line):
            self.pages_written += 1
            return True
        if RE_PASSWORD.search(line):
            self.password = True
        if self.fatal_line is None and line.startswith("Error:"):
            self.fatal_line = line.strip()
        inline = RE_INLINE_ERROR.match(line)
        if inline and inline.group(1).strip():
            self._add(self.errors, inline.group(1).strip())
        if RE_ERRORS_HEADER.match(line):
            self._block = self.errors
        elif RE_WARNINGS_HEADER.match(line):
            self._block = self.warnings
        elif self._block is not None:
            if line.startswith("\t"):
                self._add(self._block, line.strip())
            elif line.strip():
                self._block = None
        return False

    @staticmethod
    def _add(bucket: List[str], text: str) -> None:
        if text not in bucket and len(bucket) < 12:
            bucket.append(text)


@dataclasses.dataclass
class GsRun:
    returncode: int
    out: GsOutput
    timed_out: bool = False
    cancelled: bool = False
    command: str = ""


def format_command(argv: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(argv)) if IS_WINDOWS else shlex.join(list(argv))


def run_ghostscript(gs_path: str, args: Sequence[str], workdir: Path, timeout: float,
                    on_page: Optional[Callable[[int, Optional[int]], None]]) -> GsRun:
    env = os.environ.copy()
    env.pop("GS_OPTIONS", None)  # user-level defaults must not change our results
    for var in ("TMPDIR", "TMP", "TEMP"):
        env[var] = str(workdir)
    argv = [gs_path, *args]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  cwd=str(workdir), env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW

    out = GsOutput()
    run = GsRun(returncode=-1, out=out, command=format_command(argv))
    proc = subprocess.Popen(argv, **kwargs)  # type: ignore[call-overload]
    with _PROCS_LOCK:
        _PROCS.append(proc)
    timer: Optional[threading.Timer] = None
    if timeout and timeout > 0:
        def _on_timeout() -> None:
            run.timed_out = True
            try:
                proc.kill()
            except OSError:
                pass
        timer = threading.Timer(timeout, _on_timeout)
        timer.daemon = True
        timer.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if out.feed(raw.rstrip("\r\n")) and on_page:
                on_page(out.pages_written, out.pages_expected)
        run.returncode = proc.wait()
    finally:
        if timer:
            timer.cancel()
        with _PROCS_LOCK:
            if proc in _PROCS:
                _PROCS.remove(proc)
    run.cancelled = _CANCEL.is_set()
    return run


@dataclasses.dataclass
class Verdict:
    ok: bool
    reason: str = ""
    fatal: bool = False                       # retrying cannot help
    notes: List[str] = dataclasses.field(default_factory=list)


def looks_like_pdf(path: Path) -> bool:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(8)
            fh.seek(max(size - 2048, 0))
            tail = fh.read()
    except OSError:
        return False
    return head.startswith(b"%PDF-") and b"%%EOF" in tail


def judge(run: GsRun, output: Path, timeout: float) -> Verdict:
    """Decide whether a Ghostscript run produced a trustworthy result.

    The exit code alone is not enough: for example Ghostscript exits with 0 and
    writes a tiny empty PDF when a file needs a password.
    """
    out = run.out
    if run.cancelled:
        return Verdict(False, "cancelled", fatal=True)
    if run.timed_out:
        return Verdict(False, f"timed out after {human_duration(timeout)}", fatal=True)
    if out.password:
        return Verdict(False, "password-protected (a password is required to open it)", fatal=True)
    if run.returncode != 0:
        detail = out.fatal_line or (out.errors[0] if out.errors else "")
        reason = f"Ghostscript failed (exit code {run.returncode})"
        return Verdict(False, f"{reason}: {detail[:160]}" if detail else reason)
    if not output.is_file() or not looks_like_pdf(output):
        return Verdict(False, "Ghostscript did not produce a valid PDF")
    if out.pages_written == 0:
        return Verdict(False, "Ghostscript processed no pages")
    if out.pages_expected and out.pages_written < out.pages_expected:
        return Verdict(False, f"only {out.pages_written} of {out.pages_expected} pages were written")
    return Verdict(True, notes=list(out.errors))


# =============================================================================
# Per-file processing
# =============================================================================

@dataclasses.dataclass
class Job:
    src: Path
    dst: Path
    rel: Path
    index: int = 0


@dataclasses.dataclass
class Result:
    job: Job
    status: str                  # ok | unchanged | failed | skipped
    in_size: int = 0
    out_size: int = 0
    seconds: float = 0.0
    reason: str = ""
    notes: List[str] = dataclasses.field(default_factory=list)
    attempt: str = ""
    details: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Context:
    gs_path: str
    profile: Profile
    strip_metadata: bool = False
    strict: bool = False
    timeout: float = 0.0
    temp_dir: Optional[Path] = None
    in_place: bool = False
    skip_existing: bool = False
    skip_unchanged: bool = False
    min_saving: float = 0.0
    verbose: bool = False
    ui: UI = dataclasses.field(default_factory=lambda: UI(False))
    log: Logger = dataclasses.field(default_factory=lambda: Logger(None))
    live: bool = False
    total: int = 1


def _short_windows_path(path: str) -> Optional[str]:
    """Return the 8.3 short form of an existing Windows path, if available."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        needed = kernel32.GetShortPathNameW(path, None, 0)
        if not needed:
            return None
        buf = ctypes.create_unicode_buffer(needed)
        return buf.value if kernel32.GetShortPathNameW(path, buf, needed) else None
    except Exception:
        return None


def pick_temp_root(requested: Optional[str]) -> Path:
    """Choose the folder for scratch files.

    On Windows the default temp folder lives inside the user profile, which
    contains non-ASCII characters for many users (e.g. C:\\Users\\Иван). Ghostscript
    receives its paths as command-line text, so an ASCII-only location is used
    whenever one can be found. Elsewhere UTF-8 paths are fine.
    """
    base = Path(os.path.abspath(requested)) if requested else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS or str(base).isascii():
        return base
    short = _short_windows_path(str(base))
    if short and short.isascii():
        return Path(short)
    for var in ("ProgramData", "PUBLIC"):
        root = os.environ.get(var)
        if root and root.isascii():
            candidate = Path(root) / "pdf_compress_tmp"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
            except OSError:
                continue
    return base


def stage_input(src: Path, workdir: Path) -> str:
    """Return a path Ghostscript can open safely.

    Names with non-ASCII characters, '%' or very long paths are exposed to
    Ghostscript through a short ASCII alias (hard link, or a copy if linking is
    impossible), which sidesteps every encoding and quoting pitfall.
    """
    path = os.path.abspath(src)
    if path.isascii() and "%" not in path and len(path) < 200:
        return path
    alias = workdir / "input.pdf"
    try:
        os.link(path, alias)
    except (OSError, NotImplementedError, AttributeError):
        shutil.copyfile(path, alias)
    return str(alias)


def install_file(src: Path, dst: Path) -> None:
    """Copy `src` to `dst` atomically (readers never see a half-written file)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def has_pdf_header(path: Path) -> bool:
    with open(path, "rb") as fh:
        return b"%PDF-" in fh.read(1024)


def process_job(job: Job, ctx: Context) -> Result:
    started = time.monotonic()

    def finish(status: str, **kw) -> Result:
        return Result(job=job, status=status, seconds=time.monotonic() - started, **kw)

    try:
        if _CANCEL.is_set():
            return finish("skipped", reason="cancelled")
        try:
            in_size = job.src.stat().st_size
        except OSError as exc:
            return finish("failed", reason=f"cannot read file: {exc.strerror or exc}")
        if ctx.skip_existing and not ctx.in_place and job.dst.exists():
            return finish("skipped", in_size=in_size, reason="output already exists")
        if in_size == 0:
            return finish("failed", in_size=in_size, reason="file is empty")
        try:
            if not has_pdf_header(job.src):
                return finish("failed", in_size=in_size, reason="not a PDF file (no %PDF- header found)")
        except OSError as exc:
            return finish("failed", in_size=in_size, reason=f"cannot read file: {exc.strerror or exc}")

        workdir = Path(tempfile.mkdtemp(prefix="pdfc_", dir=str(ctx.temp_dir) if ctx.temp_dir else None))
        try:
            return _compress_in_workdir(job, ctx, workdir, in_size, finish)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    except Exception as exc:  # a worker must never take the whole run down
        ctx.log.write(f"UNEXPECTED ERROR for {job.src}: {exc!r}")
        return finish("failed", reason=f"unexpected error: {exc!r}")


def _compress_in_workdir(job: Job, ctx: Context, workdir: Path, in_size: int, finish) -> Result:
    ctx.log.write(f"FILE {job.src} ({in_size} bytes)")
    gs_input = stage_input(job.src, workdir)
    gs_output = workdir / "out.pdf"

    on_page: Optional[Callable[[int, Optional[int]], None]] = None
    if ctx.live:
        label = str(job.rel)

        def on_page(done: int, total: Optional[int]) -> None:  # noqa: F811
            suffix = f"{done}/{total}" if total else str(done)
            ctx.ui.status(f"  [{job.index}/{ctx.total}] {label}  -  page {suffix}")

        ctx.ui.status(f"  [{job.index}/{ctx.total}] {job.rel}  -  starting ...")

    first_failure: Optional[Verdict] = None
    verdict = Verdict(False, "no attempt was made")
    used = ATTEMPTS[0]
    last_tail: List[str] = []
    for attempt in ATTEMPTS:
        if gs_output.exists():
            gs_output.unlink()
        args = build_gs_args(ctx.profile, source=gs_input, output=str(gs_output),
                             strip_metadata=ctx.strip_metadata, strict=ctx.strict, extra=attempt.extra)
        run = run_ghostscript(ctx.gs_path, args, workdir, ctx.timeout, on_page)
        verdict = judge(run, gs_output, ctx.timeout)
        used = attempt
        last_tail = list(run.out.tail)
        ctx.log.write(f"  attempt '{attempt.name}': exit={run.returncode} ok={verdict.ok} {verdict.reason}")
        ctx.log.write(f"  command: {run.command}")
        if not verdict.ok or ctx.verbose:
            ctx.log.write("  ghostscript output (tail):\n" + "\n".join("    " + x for x in run.out.tail))
        if verdict.ok:
            break
        if first_failure is None:
            first_failure = verdict
        if verdict.fatal:
            break

    if not verdict.ok:
        reason = (first_failure or verdict).reason
        if verdict.fatal and first_failure is not None:
            reason = verdict.reason
        if ctx.strict and not verdict.fatal:
            reason += " [strict mode is on; run without --strict to let Ghostscript repair the file]"
        details = [x for x in last_tail if x.strip()][-12:] if ctx.verbose else []
        return finish("failed", in_size=in_size, reason=reason, details=details)

    out_size = gs_output.stat().st_size
    notes = list(verdict.notes)
    if used.name != ATTEMPTS[0].name:
        notes.insert(0, f"the normal run failed, so it was retried in {used.name}")
    saving_pct = (1 - out_size / in_size) * 100 if in_size else 0.0

    if out_size >= in_size or saving_pct < ctx.min_saving:
        reason = "result was not smaller" if out_size >= in_size else f"saving below {ctx.min_saving:g}%"
        if not ctx.in_place and not ctx.skip_unchanged:
            install_file(job.src, job.dst)
        return finish("unchanged", in_size=in_size, out_size=in_size, reason=reason, notes=notes, attempt=used.name)

    install_file(gs_output, job.dst)
    return finish("ok", in_size=in_size, out_size=out_size, notes=notes, attempt=used.name)


# =============================================================================
# Input discovery
# =============================================================================

def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def iter_pdfs(root: Path, recursive: bool, exclude: Optional[Path]) -> Iterator[Tuple[Path, Path]]:
    def is_pdf(name: str) -> bool:
        return name.lower().endswith(".pdf")

    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            if exclude is not None and _is_within(here.resolve(), exclude):
                dirnames[:] = []
                continue
            dirnames.sort(key=str.lower)
            for name in sorted(filenames, key=str.lower):
                if is_pdf(name):
                    yield here / name, (here / name).relative_to(root)
    else:
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file() and is_pdf(entry.name):
                yield entry, Path(entry.name)


def collect_jobs(inputs: Sequence[str], recursive: bool, out_dir: Path, suffix: str,
                 in_place: bool) -> Tuple[List[Job], List[str]]:
    jobs: List[Job] = []
    problems: List[str] = []
    seen_src: set = set()
    seen_dst: set = set()
    exclude = None if in_place else out_dir.resolve()

    for raw in inputs:
        path = Path(raw)
        if path.is_file():
            items: Iterable[Tuple[Path, Path]] = [(path, Path(path.name))]
        elif path.is_dir():
            items = iter_pdfs(path, recursive, exclude)
        else:
            problems.append(f"not found: {raw}")
            continue
        for src, rel in items:
            src_key = os.path.normcase(os.path.abspath(src))
            if src_key in seen_src:
                continue
            seen_src.add(src_key)
            if in_place:
                dst = Path(os.path.abspath(src))
            else:
                dst = Path(os.path.abspath(out_dir / rel.parent / f"{rel.stem}{suffix}{rel.suffix}"))
                if os.path.normcase(str(dst)) == src_key:
                    problems.append(f"skipped {src}: the output would overwrite the original "
                                    f"(use -o/--output, --suffix, or --in-place)")
                    continue
                counter = 2
                while os.path.normcase(str(dst)) in seen_dst:
                    dst = dst.with_name(f"{rel.stem}{suffix}_{counter}{rel.suffix}")
                    counter += 1
            seen_dst.add(os.path.normcase(str(dst)))
            jobs.append(Job(src=Path(os.path.abspath(src)), dst=dst, rel=rel, index=len(jobs) + 1))
    return jobs, problems


# =============================================================================
# Reporting
# =============================================================================

def report_result(ui: UI, result: Result, done: int, total: int) -> None:
    width = len(str(total))
    ui.line(f"  [{done:>{width}}/{total}] {result.job.rel}")
    pad = " " * (width * 2 + 6)
    if result.status == "ok":
        pct = (1 - result.out_size / result.in_size) * 100
        ui.line(f"{pad}{human_size(result.in_size)} -> {human_size(result.out_size)}   "
                f"-{pct:.1f}%   {human_duration(result.seconds)}", "green")
    elif result.status == "unchanged":
        ui.line(f"{pad}{human_size(result.in_size)}   kept as is ({result.reason})", "yellow")
    elif result.status == "skipped":
        ui.line(f"{pad}SKIPPED: {result.reason}", "dim")
    else:
        ui.line(f"{pad}FAILED: {result.reason}", "red")
    for note in result.notes:
        ui.line(f"{pad}note: {note}", "yellow")
    for line in result.details:
        ui.line(f"{pad}gs> {line[:200]}", "dim")


def print_summary(ui: UI, results: List[Result], elapsed: float, out_dir: Optional[Path],
                  in_place: bool, log_path: Optional[Path]) -> None:
    ok = [r for r in results if r.status == "ok"]
    unchanged = [r for r in results if r.status == "unchanged"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]
    total_in = sum(r.in_size for r in ok + unchanged)
    total_out = sum(r.out_size for r in ok + unchanged)

    bar = "=" * 60
    ui.line()
    ui.line(bar, "cyan")
    ui.line(f"  DONE: {len(results)} file(s) in {human_duration(elapsed)}", "cyan", "bold")
    if total_in:
        saved = (1 - total_out / total_in) * 100
        ui.line(f"  Total size: {human_size(total_in)} -> {human_size(total_out)}  (saved {saved:.1f}%)", "cyan")
    ui.line(f"  Compressed: {len(ok)}   Unchanged: {len(unchanged)}   "
            f"Failed: {len(failed)}   Skipped: {len(skipped)}", "cyan")
    if out_dir is not None and not in_place:
        ui.line(f"  Output folder: {out_dir}", "cyan")
    if in_place:
        ui.line("  Originals were replaced only where the result was smaller.", "cyan")
    else:
        ui.line("  Originals were not modified.", "dim")
    if log_path:
        ui.line(f"  Log file: {log_path}", "dim")
    ui.line(bar, "cyan")

    noted = [r for r in results if r.notes and r.status in ("ok", "unchanged")]
    if noted:
        ui.line()
        ui.line("  Ghostscript reported problems with these files (output was still produced; "
                "please check them):", "yellow")
        for r in noted:
            ui.line(f"    - {r.job.rel}: {'; '.join(r.notes)}", "yellow")
    if failed:
        ui.line()
        ui.line("  Failed files:", "red")
        for r in sorted(failed, key=lambda x: x.job.index):
            ui.line(f"    - {r.job.rel}: {r.reason}", "red")
        ui.line("  Re-run with --log FILE for Ghostscript's full diagnostics.", "dim")


# =============================================================================
# Interactive mode
# =============================================================================

def ask(prompt: str) -> str:
    return input(prompt).strip()


def ask_int(prompt: str, default: int, low: int, high: int) -> int:
    while True:
        raw = ask(f"{prompt} [{low}-{high}] (Enter = {default}): ")
        if raw == "":
            return default
        if raw.isdigit() and low <= int(raw) <= high:
            return int(raw)
        print(f"  Please enter a whole number from {low} to {high}.")


def run_wizard(ui: UI, gs_version: str, file_count: int) -> Tuple[Profile, bool]:
    bar = "=" * 60
    while True:
        ui.line()
        ui.line(bar, "cyan")
        ui.line(f"   PDF-Compress-Py  |  Ghostscript {gs_version}  |  files found: {file_count}", "cyan")
        ui.line(bar, "cyan")
        ui.line()
        ui.line("Choose a compression mode:", "bold")
        ui.line()
        ui.line("  [Enter] Standard (recommended)", "green")
        ui.line(f"          {PRESET_HELP['standard']}", "dim")
        ui.line("  [1]     High quality")
        ui.line(f"          {PRESET_HELP['high']}", "dim")
        ui.line("  [2]     Maximum compression")
        ui.line(f"          {PRESET_HELP['max']}", "dim")
        ui.line("  [3]     Lossless")
        ui.line(f"          {PRESET_HELP['lossless']}", "dim")
        ui.line("  [4]     Custom - choose your own DPI and JPEG quality")
        ui.line()
        choice = ask("  Your choice (Enter = Standard): ")
        mapping = {"": "standard", "1": "high", "2": "max", "3": "lossless"}
        if choice in mapping:
            profile = PRESETS[mapping[choice]]
            break
        if choice == "4":
            ui.line()
            ui.line("  Vector text and fonts are always kept as they are; these settings", "dim")
            ui.line("  only affect raster images (scans, photos).", "dim")
            dpi = ask_int("  Image resolution, DPI", 150, 36, 1200)
            quality = ask_int("  JPEG quality", 85, 10, 100)
            profile = dataclasses.replace(
                PRESETS["standard"], name="Custom", color_dpi=dpi, gray_dpi=dpi,
                mono_dpi=min(1200, max(300, dpi * 2)), quality=quality, color="keep")
            break
        ui.line(f"  Unknown option: {choice!r}. Please choose again.", "red")
    strip = ask("  Remove metadata (author, dates, software)? [y/N]: ").lower().startswith("y")
    ui.line()
    return profile, strip


# =============================================================================
# Command line
# =============================================================================

def bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    presets = "\n".join(f"  {name:<9} {text}" for name, text in PRESET_HELP.items())
    parser = argparse.ArgumentParser(
        prog="pdf_compress",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Batch-compress PDF files with Ghostscript. Originals are never modified "
                    "unless --in-place is given.",
        epilog=f"presets:\n{presets}\n\n"
               "examples:\n"
               "  pdf_compress.py                       interactive menu (PDFs in the current folder)\n"
               "  pdf_compress.py scan.pdf              compress one file into ./compressed/\n"
               "  pdf_compress.py docs/ -r -o small/    whole tree, keep the folder structure\n"
               "  pdf_compress.py *.pdf --preset max    smallest possible files\n"
               "  pdf_compress.py a.pdf --dpi 120 --quality 75 --color gray\n"
               "\nexit codes: 0 success, 1 at least one file failed, 2 usage error / "
               "Ghostscript missing / nothing to do, 130 interrupted",
    )
    parser.add_argument("inputs", nargs="*", metavar="PATH",
                        help="PDF files and/or folders (default: current folder when options are given)")
    parser.add_argument("-r", "--recursive", action="store_true", help="also search sub-folders of folder inputs")
    out = parser.add_argument_group("output")
    out.add_argument("-o", "--output", metavar="DIR", default=None,
                     help="output folder (default: ./compressed); sub-folder layout is preserved")
    out.add_argument("--suffix", default="", metavar="TEXT",
                     help="text appended to every output file name, e.g. _small")
    out.add_argument("--in-place", action="store_true",
                     help="replace originals, but only when the result is smaller (no backup is kept!)")
    out.add_argument("--skip-existing", action="store_true", help="do not redo files whose output already exists")
    out.add_argument("--skip-unchanged", action="store_true",
                     help="do not copy a file to the output folder when it could not be made smaller")
    q = parser.add_argument_group("quality")
    q.add_argument("-p", "--preset", choices=sorted(PRESETS), default=None,
                   help="compression preset (default: standard)")
    q.add_argument("--dpi", type=bounded_int(36, 1200), metavar="N",
                   help="target resolution of raster images (overrides the preset)")
    q.add_argument("--quality", type=bounded_int(10, 100), metavar="Q",
                   help="JPEG quality 10-100 (overrides the preset)")
    q.add_argument("--color", choices=("keep", "rgb", "gray"), default=None,
                   help="color handling: keep, convert to RGB, or convert to grayscale")
    q.add_argument("--strip-metadata", action="store_true",
                   help="remove author, title, dates and XMP metadata")
    q.add_argument("--min-saving", type=float, default=0.0, metavar="PCT",
                   help="keep the original unless at least PCT percent is saved (default: 0)")
    r = parser.add_argument_group("robustness and performance")
    r.add_argument("--strict", action="store_true",
                   help="fail on any PDF error instead of repairing it (Ghostscript's -dPDFSTOPONERROR)")
    r.add_argument("-j", "--jobs", type=bounded_int(1, 64), default=min(4, os.cpu_count() or 1), metavar="N",
                   help="files processed in parallel (default: %(default)s)")
    r.add_argument("--timeout", type=float, default=0.0, metavar="SEC",
                   help="give up on a single file after SEC seconds (default: no limit)")
    r.add_argument("--temp-dir", metavar="DIR", help="folder for temporary files (default: system temp)")
    r.add_argument("--gs", metavar="PATH", help="path to the Ghostscript executable (default: auto-detect)")
    m = parser.add_argument_group("misc")
    m.add_argument("-n", "--dry-run", action="store_true", help="show what would be done and exit")
    m.add_argument("-v", "--verbose", action="store_true", help="print extra details")
    m.add_argument("--log", metavar="FILE", help="write a detailed log (commands and Ghostscript output) to FILE")
    m.add_argument("--no-color", action="store_true", help="disable colored output")
    m.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def resolve_profile(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Profile:
    profile = PRESETS[args.preset or "standard"]
    customized = False
    if args.dpi is not None or args.quality is not None:
        if profile.quality is None:
            parser.error("--dpi/--quality cannot be combined with the lossless preset")
        if args.dpi is not None:
            profile = dataclasses.replace(profile, color_dpi=args.dpi, gray_dpi=args.dpi,
                                          mono_dpi=min(1200, max(300, args.dpi * 2)))
        if args.quality is not None:
            profile = dataclasses.replace(profile, quality=args.quality)
        customized = True
    if args.color is not None:
        profile = dataclasses.replace(profile, color=args.color)
        customized = True
    if customized:
        profile = dataclasses.replace(profile, name=f"{profile.name}, customized")
    return profile


def wait_for_enter() -> None:
    try:
        input("\n  Press Enter to exit ")
    except (EOFError, KeyboardInterrupt):
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_intermixed_args(argv)

    if args.in_place and (args.output or args.suffix):
        parser.error("--in-place cannot be combined with --output or --suffix")
    if args.min_saving < 0 or args.min_saving >= 100:
        parser.error("--min-saving must be at least 0 and below 100")

    color = setup_console(args.no_color)
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    options_given = any([args.preset, args.dpi, args.quality, args.color, args.strip_metadata,
                         args.output, args.in_place, args.recursive, args.dry_run, args.log])
    interactive = not args.inputs and not options_given and tty
    if not args.inputs and not interactive and not options_given:
        parser.print_usage(sys.stderr)
        print("error: no input given (run without arguments in a terminal for the interactive menu)",
              file=sys.stderr)
        return EXIT_USAGE
    inputs = args.inputs or ["."]

    profile = resolve_profile(args, parser)
    ui = UI(color)
    try:
        code = _run(args, ui, profile, inputs, interactive)
    finally:
        if interactive:
            wait_for_enter()
    return code


def _run(args: argparse.Namespace, ui: UI, profile: Profile, inputs: List[str], interactive: bool) -> int:
    try:
        gs_path, gs_version = find_ghostscript(args.gs)
    except GhostscriptNotFound as exc:
        ui.line(f"Error: {exc}", "red")
        ui.line(ghostscript_install_help())
        return EXIT_USAGE
    if version_key(gs_version) < (9, 54):
        ui.line(f"Warning: Ghostscript {gs_version} is quite old; please update to 10.x for best results.", "yellow")

    in_place = args.in_place
    out_dir = Path(os.path.abspath(args.output or "compressed"))
    jobs, problems = collect_jobs(inputs, args.recursive, out_dir, args.suffix, in_place)
    for problem in problems:
        ui.line(f"Warning: {problem}", "yellow")
    if not jobs:
        ui.line("No PDF files found to process." if not problems else "Nothing to do.", "yellow")
        return EXIT_USAGE

    strip = args.strip_metadata
    if interactive:
        profile, strip = run_wizard(ui, gs_version, len(jobs))

    if args.dry_run:
        ui.line(f"Dry run - {len(jobs)} file(s) would be processed with: {profile.label}")
        for job in jobs:
            target = "(replace original if smaller)" if in_place else str(job.dst)
            ui.line(f"  {job.src}  ->  {target}")
        return EXIT_OK

    try:
        temp_dir: Optional[Path] = pick_temp_root(args.temp_dir)
    except OSError as exc:
        ui.line(f"Error: cannot use the temporary folder: {exc}", "red")
        return EXIT_USAGE

    log_path = Path(os.path.abspath(args.log)) if args.log else None
    log = Logger(log_path)
    workers = max(1, min(args.jobs, len(jobs)))
    ui.live = workers == 1 and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"

    ctx = Context(gs_path=gs_path, profile=profile, strip_metadata=strip, strict=args.strict,
                  timeout=args.timeout, temp_dir=temp_dir, in_place=in_place,
                  skip_existing=args.skip_existing, skip_unchanged=args.skip_unchanged,
                  min_saving=args.min_saving, verbose=args.verbose, ui=ui, log=log,
                  live=ui.live, total=len(jobs))

    log.write(f"pdf_compress {__version__} | Ghostscript {gs_version} ({gs_path})")
    log.write(f"profile: {profile.label} | strip_metadata={strip} strict={args.strict}")
    ui.line(f"  Mode:    {profile.label}")
    ui.line(f"  Files:   {len(jobs)}   (parallel jobs: {workers})")
    if strip:
        ui.line("  Metadata will be removed.", "yellow")
    ui.line("  Output:  " + ("replacing originals when smaller" if in_place else str(out_dir)))
    if args.strict:
        ui.line("  Strict mode: damaged files will not be repaired.", "dim")
    ui.line()

    results: List[Result] = []
    started = time.monotonic()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = [executor.submit(process_job, job, ctx) for job in jobs]
    try:
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            log.write(f"RESULT {result.job.src}: {result.status} {result.in_size}->{result.out_size} {result.reason}")
            report_result(ui, result, done, len(jobs))
        executor.shutdown(wait=True)
    except KeyboardInterrupt:
        _CANCEL.set()
        for future in futures:
            future.cancel()
        kill_running_processes()
        executor.shutdown(wait=True)
        ui.clear_status()
        ui.line()
        ui.line("Interrupted. Files that were already finished are kept; unfinished ones were discarded.", "yellow")
        log.write("interrupted by user")
        log.close()
        return EXIT_INTERRUPTED
    ui.clear_status()

    print_summary(ui, results, time.monotonic() - started, out_dir, in_place, log_path)
    log.write("finished: " + ", ".join(f"{s}={sum(1 for r in results if r.status == s)}"
                                       for s in ("ok", "unchanged", "failed", "skipped")))
    log.close()
    return EXIT_FILE_FAILED if any(r.status == "failed" for r in results) else EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)
