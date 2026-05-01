#!/usr/bin/env python3
"""
diffview - A WinMerge-compatible diff tool for Linux/Windows/macOS
Supports:
  - File diff
  - Directory diff (recursive)
  - Git commit/branch diff
  - Git merge integration
WinMerge-compatible keyboard shortcuts
"""

import sys
import os
import argparse
import subprocess
import curses
import difflib
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

# ── Platform detection ────────────────────────────────────────────────────────
IS_WINDOWS = sys.platform == "win32"

# On Windows, Unicode box-drawing chars often fail in the default console font.
# Use ASCII fallbacks automatically, or force unicode with --unicode flag.
_USE_UNICODE = not IS_WINDOWS   # overrideable by CLI flag


def _u(unicode_str: str, ascii_str: str) -> str:
    """Return unicode_str if unicode mode, else ascii_str."""
    return unicode_str if _USE_UNICODE else ascii_str


# ─────────────────────────────────────────────────────────────────────────────
#  Safe curses helpers  (the root cause of the crash)
# ─────────────────────────────────────────────────────────────────────────────

def safe_addstr(win, y: int, x: int, text: str, attr: int = 0):
    """
    addstr that never raises on the last cell of the screen.
    curses raises _curses.error when writing to the very last cell (bottom-right)
    because it tries to advance the cursor past the screen boundary.
    We truncate to (width - x - 1) to keep 1 char margin on the last row.
    """
    try:
        h, w = win.getmaxyx()
        max_len = w - x
        if y == h - 1:          # last row: reserve 1 cell to avoid ERR
            max_len = w - x - 1
        if max_len <= 0:
            return
        text = text[:max_len]
        if attr:
            win.addstr(y, x, text, attr)
        else:
            win.addstr(y, x, text)
    except curses.error:
        pass                     # silently ignore any remaining edge cases


def safe_addch(win, y: int, x: int, ch: str, attr: int = 0):
    try:
        h, w = win.getmaxyx()
        if x >= w - (1 if y == h - 1 else 0):
            return
        if attr:
            win.addch(y, x, ch, attr)
        else:
            win.addch(y, x, ch)
    except curses.error:
        pass


def fill_line(win, y: int, x: int, width: int, attr: int = 0):
    """Fill a portion of a line with spaces (safe version)."""
    safe_addstr(win, y, x, " " * width, attr)


# ─────────────────────────────────────────────────────────────────────────────
#  Data Types
# ─────────────────────────────────────────────────────────────────────────────

class DiffType(Enum):
    EQUAL   = "equal"
    INSERT  = "insert"
    DELETE  = "delete"
    REPLACE = "replace"


class FileStatus(Enum):
    SAME        = "same"
    DIFFERENT   = "different"
    LEFT_ONLY   = "left_only"
    RIGHT_ONLY  = "right_only"
    BINARY      = "binary"


@dataclass
class DiffBlock:
    type:        DiffType
    left_lines:  list = field(default_factory=list)
    right_lines: list = field(default_factory=list)
    left_start:  int  = 0
    right_start: int  = 0


@dataclass
class DirEntry:
    rel_path:   str
    status:     FileStatus
    left_path:  Optional[str] = None
    right_path: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  File / Diff helpers
# ─────────────────────────────────────────────────────────────────────────────

def read_file(path: Optional[str]) -> list:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [l.rstrip("\n").rstrip("\r") for l in f.readlines()]
    except Exception as e:
        return [f"<Error: {e}>"]


def save_file(path: str, lines: list):
    nl = "\r\n" if IS_WINDOWS else "\n"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(lines) + nl)


def is_binary(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except Exception:
        return True


def parse_diff(left_lines: list, right_lines: list):
    """Return (blocks, merged_rows).
    merged_rows: list of (left_line|None, right_line|None, DiffType)
    """
    matcher = difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False)
    blocks: list = []
    merged: list = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            b = DiffBlock(DiffType.EQUAL, left_lines[i1:i2], right_lines[j1:j2], i1, j1)
            blocks.append(b)
            for k in range(i2 - i1):
                merged.append((left_lines[i1+k], right_lines[j1+k], DiffType.EQUAL))
        elif tag == "insert":
            b = DiffBlock(DiffType.INSERT, [], right_lines[j1:j2], i1, j1)
            blocks.append(b)
            for line in right_lines[j1:j2]:
                merged.append((None, line, DiffType.INSERT))
        elif tag == "delete":
            b = DiffBlock(DiffType.DELETE, left_lines[i1:i2], [], i1, j1)
            blocks.append(b)
            for line in left_lines[i1:i2]:
                merged.append((line, None, DiffType.DELETE))
        elif tag == "replace":
            b = DiffBlock(DiffType.REPLACE, left_lines[i1:i2], right_lines[j1:j2], i1, j1)
            blocks.append(b)
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                ll = left_lines[i1+k]  if k < (i2-i1) else None
                rl = right_lines[j1+k] if k < (j2-j1) else None
                merged.append((ll, rl, DiffType.REPLACE))

    return blocks, merged


def get_change_indices(blocks: list) -> list:
    return [i for i, b in enumerate(blocks) if b.type != DiffType.EQUAL]


# ─────────────────────────────────────────────────────────────────────────────
#  Directory compare
# ─────────────────────────────────────────────────────────────────────────────

def compare_dirs(left_dir: str, right_dir: str) -> list:
    left_root  = Path(left_dir)
    right_root = Path(right_dir)

    left_files  = {p.relative_to(left_root)  for p in left_root.rglob("*")  if p.is_file()}
    right_files = {p.relative_to(right_root) for p in right_root.rglob("*") if p.is_file()}
    all_files   = sorted(left_files | right_files, key=lambda p: str(p).lower())

    entries = []
    for rel in all_files:
        lp  = str(left_root  / rel)
        rp  = str(right_root / rel)
        lin = rel in left_files
        rin = rel in right_files
        # Normalize rel_path to forward slashes for display
        rel_str = str(rel).replace("\\", "/")

        if lin and rin:
            if is_binary(lp) or is_binary(rp):
                try:
                    same = open(lp, "rb").read() == open(rp, "rb").read()
                    status = FileStatus.SAME if same else FileStatus.BINARY
                except Exception:
                    status = FileStatus.BINARY
            else:
                status = FileStatus.SAME if read_file(lp) == read_file(rp) else FileStatus.DIFFERENT
            entries.append(DirEntry(rel_str, status, lp, rp))
        elif lin:
            entries.append(DirEntry(rel_str, FileStatus.LEFT_ONLY,  lp,   None))
        else:
            entries.append(DirEntry(rel_str, FileStatus.RIGHT_ONLY, None, rp))

    return entries


# ─────────────────────────────────────────────────────────────────────────────
#  Git helpers
# ─────────────────────────────────────────────────────────────────────────────

def _git(*args, cwd=None) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True,
            cwd=cwd or os.getcwd()
        )
    except FileNotFoundError:
        return None


def git_root() -> Optional[str]:
    r = _git("rev-parse", "--show-toplevel")
    return r.stdout.strip() if r and r.returncode == 0 else None


def git_show_file(ref: str, rel_path: str) -> Optional[str]:
    """Extract file from git ref to a temp file. Returns temp path."""
    try:
        r = subprocess.run(
            ["git", "show", f"{ref}:{rel_path}"],
            capture_output=True
        )
        if r.returncode != 0:
            return None
        suffix = "_" + Path(rel_path).name
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(r.stdout)
        tmp.close()
        return tmp.name
    except Exception:
        return None


def git_list_files(ref: str) -> list:
    r = _git("ls-tree", "-r", "--name-only", ref)
    if not r or r.returncode != 0:
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def git_diff_files(ref_left: str, ref_right: str) -> list:
    root = git_root()
    if not root:
        return []

    r = _git("diff", "--name-status", ref_left, ref_right, cwd=root)
    changed = set()
    if r and r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                changed.add(parts[1].strip())

    left_files  = set(git_list_files(ref_left))
    right_files = set(git_list_files(ref_right))
    all_files   = sorted(left_files | right_files)

    entries = []
    for rel in all_files:
        lin = rel in left_files
        rin = rel in right_files
        lp  = git_show_file(ref_left,  rel) if lin else None
        rp  = git_show_file(ref_right, rel) if rin else None

        if lin and rin:
            status = FileStatus.DIFFERENT if rel in changed else FileStatus.SAME
        elif lin:
            status = FileStatus.LEFT_ONLY
        else:
            status = FileStatus.RIGHT_ONLY

        entries.append(DirEntry(rel.replace("\\", "/"), status, lp, rp))

    return entries


def is_git_ref(s: str) -> bool:
    if not s:
        return False
    r = _git("rev-parse", "--verify", s)
    return bool(r and r.returncode == 0)


def resolve_mode(left: str, right: str) -> str:
    """Return 'file', 'dir', or 'git'."""
    if os.path.isdir(left) and os.path.isdir(right):
        return "dir"
    if os.path.isfile(left) or os.path.isfile(right):
        return "file"
    if is_git_ref(left) and is_git_ref(right):
        return "git"
    return "file"


# ─────────────────────────────────────────────────────────────────────────────
#  Color pairs
# ─────────────────────────────────────────────────────────────────────────────

CP_NORMAL     = 1
CP_HEADER     = 2
CP_STATUS     = 3
CP_LINENUM    = 4
CP_EQUAL      = 5
CP_INSERT     = 6
CP_DELETE     = 7
CP_REPLACE    = 8
CP_CURSOR     = 9
CP_TITLE      = 10
CP_DIR_SAME   = 11
CP_DIR_DIFF   = 12
CP_DIR_LEFT   = 13
CP_DIR_RIGHT  = 14
CP_DIR_BINARY = 15
CP_DIR_CURSOR = 16


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(CP_NORMAL,     curses.COLOR_WHITE,   -1)
    curses.init_pair(CP_HEADER,     curses.COLOR_WHITE,   curses.COLOR_BLUE)
    curses.init_pair(CP_STATUS,     curses.COLOR_BLACK,   curses.COLOR_CYAN)
    curses.init_pair(CP_LINENUM,    curses.COLOR_CYAN,    -1)
    curses.init_pair(CP_EQUAL,      curses.COLOR_WHITE,   -1)
    curses.init_pair(CP_INSERT,     curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(CP_DELETE,     curses.COLOR_RED,     curses.COLOR_BLACK)
    curses.init_pair(CP_REPLACE,    curses.COLOR_YELLOW,  curses.COLOR_BLACK)
    curses.init_pair(CP_CURSOR,     curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(CP_TITLE,      curses.COLOR_YELLOW,  curses.COLOR_BLUE)
    curses.init_pair(CP_DIR_SAME,   curses.COLOR_WHITE,   -1)
    curses.init_pair(CP_DIR_DIFF,   curses.COLOR_YELLOW,  -1)
    curses.init_pair(CP_DIR_LEFT,   curses.COLOR_RED,     -1)
    curses.init_pair(CP_DIR_RIGHT,  curses.COLOR_GREEN,   -1)
    curses.init_pair(CP_DIR_BINARY, curses.COLOR_MAGENTA, -1)
    curses.init_pair(CP_DIR_CURSOR, curses.COLOR_BLACK,   curses.COLOR_YELLOW)


# ─────────────────────────────────────────────────────────────────────────────
#  Directory Browser View
# ─────────────────────────────────────────────────────────────────────────────

class DirViewer:

    # ASCII-safe symbols for each status
    _STATUS = {
        FileStatus.SAME:       (" == ", CP_DIR_SAME),
        FileStatus.DIFFERENT:  ("<-->", CP_DIR_DIFF),
        FileStatus.LEFT_ONLY:  ("<   ", CP_DIR_LEFT),
        FileStatus.RIGHT_ONLY: ("   >", CP_DIR_RIGHT),
        FileStatus.BINARY:     ("BIN ", CP_DIR_BINARY),
    }

    def __init__(self, stdscr, entries: list, left_label: str, right_label: str):
        self.stdscr      = stdscr
        self.entries     = entries
        self.left_label  = left_label
        self.right_label = right_label
        self.cursor      = 0
        self.scroll_top  = 0
        self.show_same   = True
        self.status_msg  = ""

    @property
    def visible(self) -> list:
        if self.show_same:
            return self.entries
        return [e for e in self.entries if e.status != FileStatus.SAME]

    # ── draw ─────────────────────────────────────────────────────────────────

    def draw(self):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()
        self._draw_header(h, w)
        self._draw_col_header(h, w)
        self._draw_list(h, w)
        self._draw_status(h, w)
        self._draw_keys(h, w)
        self.stdscr.refresh()

    def _draw_header(self, h, w):
        half    = w // 2
        divider = _u("│", "|")
        left_t  = f" {self.left_label} "
        right_t = f" {self.right_label} "

        fill_line(self.stdscr, 0, 0, w, curses.color_pair(CP_HEADER))
        safe_addstr(self.stdscr, 0, 0,      left_t[:half-1],       curses.color_pair(CP_TITLE))
        safe_addstr(self.stdscr, 0, half,   divider,               curses.color_pair(CP_HEADER))
        safe_addstr(self.stdscr, 0, half+1, right_t[:w-half-2],    curses.color_pair(CP_TITLE))

    def _draw_col_header(self, h, w):
        half   = w // 2
        name_w = (w - 8) // 2
        hdr    = f" {'Left File':<{name_w}} {'Stat':4} {'Right File':<{name_w}}"
        fill_line(self.stdscr, 1, 0, w, curses.color_pair(CP_STATUS))
        safe_addstr(self.stdscr, 1, 0, hdr[:w], curses.color_pair(CP_STATUS))

    def _draw_list(self, h, w):
        vis      = self.visible
        max_rows = h - 4          # rows 2 .. h-3  (header=0,col=1, status=h-2, keys=h-1)
        name_w   = max(1, (w - 8) // 2)

        for row in range(max_rows):
            idx    = self.scroll_top + row
            scr_y  = 2 + row
            if idx >= len(vis):
                break
            e      = vis[idx]
            is_cur = (idx == self.cursor)
            sym, cp = self._STATUS.get(e.status, ("??? ", CP_NORMAL))

            lname = (e.rel_path if e.status != FileStatus.RIGHT_ONLY else "")[:name_w]
            rname = (e.rel_path if e.status != FileStatus.LEFT_ONLY  else "")[:name_w]
            line  = f" {lname:<{name_w}} {sym} {rname:<{name_w}}"

            attr = (curses.color_pair(CP_DIR_CURSOR) | curses.A_BOLD
                    if is_cur else curses.color_pair(cp))
            fill_line(self.stdscr, scr_y, 0, w, attr)
            safe_addstr(self.stdscr, scr_y, 0, line[:w], attr)

    def _draw_status(self, h, w):
        vis   = self.visible
        diffs = sum(1 for e in self.entries if e.status != FileStatus.SAME)
        toggle = "[H] Show all" if not self.show_same else "[H] Hide same"
        n   = len(vis)
        cur = self.cursor + 1 if n else 0
        msg = (self.status_msg or
               f"  {n} shown / {len(self.entries)} total  ({diffs} differ)"
               f"  {cur}/{n}  {toggle}")
        fill_line(self.stdscr, h-2, 0, w, curses.color_pair(CP_STATUS))
        safe_addstr(self.stdscr, h-2, 0, msg[:w], curses.color_pair(CP_STATUS))

    def _draw_keys(self, h, w):
        hints = " Enter:Open  H:Toggle-same  Up/Dn:Move  PgUp/Dn  Home/End  F5:Refresh  Q:Quit"
        fill_line(self.stdscr, h-1, 0, w, curses.color_pair(CP_HEADER))
        safe_addstr(self.stdscr, h-1, 0, hints[:w], curses.color_pair(CP_HEADER))

    # ── input loop ───────────────────────────────────────────────────────────

    def run(self) -> Optional[DirEntry]:
        curses.curs_set(0)
        while True:
            h, w      = self.stdscr.getmaxyx()
            content_h = h - 4
            self.draw()
            self.status_msg = ""

            try:
                key = self.stdscr.get_wch()
            except curses.error:
                continue

            vis = self.visible

            if key in ("q", "Q"):
                return None

            if key in ("h", "H"):
                self.show_same = not self.show_same
                self.cursor    = min(self.cursor, max(0, len(self.visible)-1))
                continue

            if key == curses.KEY_UP:
                self.cursor = max(0, self.cursor - 1)
                if self.cursor < self.scroll_top:
                    self.scroll_top = self.cursor
                continue

            if key == curses.KEY_DOWN:
                self.cursor = min(len(vis)-1, self.cursor + 1)
                if self.cursor >= self.scroll_top + content_h:
                    self.scroll_top = self.cursor - content_h + 1
                continue

            if key == curses.KEY_PPAGE:
                self.cursor     = max(0, self.cursor - content_h)
                self.scroll_top = max(0, self.scroll_top - content_h)
                continue

            if key == curses.KEY_NPAGE:
                self.cursor     = min(len(vis)-1, self.cursor + content_h)
                self.scroll_top = min(max(0, len(vis) - content_h),
                                      self.scroll_top + content_h)
                continue

            if key == curses.KEY_HOME:
                self.cursor = 0
                self.scroll_top = 0
                continue

            if key == curses.KEY_END:
                self.cursor     = max(0, len(vis) - 1)
                self.scroll_top = max(0, len(vis) - content_h)
                continue

            if key == curses.KEY_F5:
                self.status_msg = "  [F5] Refreshed"
                continue

            if key in ("\n", "\r", curses.KEY_ENTER):
                if not vis:
                    continue
                e = vis[self.cursor]
                if e.status in (FileStatus.LEFT_ONLY, FileStatus.RIGHT_ONLY):
                    self.status_msg = f"  {e.rel_path}: only on one side"
                elif e.status == FileStatus.SAME:
                    self.status_msg = f"  {e.rel_path}: files are identical"
                elif e.status == FileStatus.BINARY:
                    self.status_msg = f"  {e.rel_path}: binary files differ"
                else:
                    return e
                continue


# ─────────────────────────────────────────────────────────────────────────────
#  File Diff Viewer
# ─────────────────────────────────────────────────────────────────────────────

class FileDiffViewer:

    _DIFF_CP = {
        DiffType.EQUAL:   CP_EQUAL,
        DiffType.INSERT:  CP_INSERT,
        DiffType.DELETE:  CP_DELETE,
        DiffType.REPLACE: CP_REPLACE,
    }

    def __init__(self, stdscr,
                 left_file:  Optional[str],
                 right_file: Optional[str],
                 left_label: str = "",
                 right_label: str = "",
                 readonly:   bool = False):
        self.stdscr      = stdscr
        self.left_file   = left_file
        self.right_file  = right_file
        self.left_label  = left_label  or (left_file  or "(none)")
        self.right_label = right_label or (right_file or "(none)")
        self.readonly    = readonly

        self.left_lines  = read_file(left_file)
        self.right_lines = read_file(right_file)

        self.diff_blocks    = []
        self.merged_lines   = []
        self.change_indices = []
        self.current_change = 0
        self.scroll_top     = 0
        self.cursor_line    = 0
        self.focus          = "left"
        self.show_help      = False
        self.status_msg     = ""
        self._reload()

    # ── diff state ───────────────────────────────────────────────────────────

    def _reload(self):
        self.diff_blocks, self.merged_lines = parse_diff(self.left_lines, self.right_lines)
        self.change_indices = get_change_indices(self.diff_blocks)
        self.current_change = min(self.current_change,
                                  max(0, len(self.change_indices) - 1))

    # ── drawing ──────────────────────────────────────────────────────────────

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if self.show_help:
            self._draw_help(h, w)
        else:
            self._draw_header(h, w)
            self._draw_content(1, h - 3, w)
            self._draw_status(h - 2, w)
            self._draw_keys(h - 1, w)
        self.stdscr.refresh()

    def _draw_header(self, h, w):
        half    = w // 2
        divider = _u("│", "|")
        left_t  = f" {self.left_label} "
        right_t = f" {self.right_label} "
        fill_line(self.stdscr, 0, 0, w, curses.color_pair(CP_HEADER))
        safe_addstr(self.stdscr, 0, 0,      left_t[:half-1],    curses.color_pair(CP_TITLE))
        safe_addstr(self.stdscr, 0, half,   divider,            curses.color_pair(CP_HEADER))
        safe_addstr(self.stdscr, 0, half+1, right_t[:w-half-2], curses.color_pair(CP_TITLE))

    def _draw_content(self, top_row: int, height: int, w: int):
        merged  = self.merged_lines
        half    = w // 2
        num_w   = 5            # "1234 "
        avail_l = half - num_w - 1
        avail_r = w - half - num_w - 2
        divider = _u("│", "|")

        # Pre-build line number arrays (only count non-None sides)
        ln_l, ln_r = 1, 1
        lnums: list = []
        rnums: list = []
        for ll, rl, _ in merged:
            lnums.append(ln_l if ll is not None else None)
            rnums.append(ln_r if rl is not None else None)
            if ll is not None: ln_l += 1
            if rl is not None: ln_r += 1

        for row in range(height):
            mi  = self.scroll_top + row
            scr = top_row + row
            if mi >= len(merged):
                break

            ll, rl, dtype = merged[mi]
            cp     = self._DIFF_CP.get(dtype, CP_EQUAL)
            is_cur = (mi == self.cursor_line)

            # ── left pane ──
            lnum  = f"{lnums[mi]:>4} " if lnums[mi] else "     "
            lt    = ((ll or "").expandtabs(4))[:avail_l].ljust(avail_l)
            l_cp  = (CP_CURSOR if is_cur and self.focus == "left" else cp)
            l_attr = curses.color_pair(l_cp) | (curses.A_BOLD if is_cur and self.focus == "left" else 0)

            safe_addstr(self.stdscr, scr, 0,     lnum, curses.color_pair(CP_LINENUM))
            fill_line(self.stdscr,   scr, num_w, avail_l, l_attr)
            safe_addstr(self.stdscr, scr, num_w, lt,  l_attr)

            # ── divider ──
            safe_addstr(self.stdscr, scr, half, divider, curses.color_pair(CP_LINENUM))

            # ── right pane ──
            rnum  = f"{rnums[mi]:>4} " if rnums[mi] else "     "
            rt    = ((rl or "").expandtabs(4))[:avail_r].ljust(avail_r)
            r_cp  = (CP_CURSOR if is_cur and self.focus == "right" else cp)
            r_attr = curses.color_pair(r_cp) | (curses.A_BOLD if is_cur and self.focus == "right" else 0)

            safe_addstr(self.stdscr, scr, half+1,        rnum, curses.color_pair(CP_LINENUM))
            fill_line(self.stdscr,   scr, half+1+num_w,  avail_r, r_attr)
            safe_addstr(self.stdscr, scr, half+1+num_w,  rt,  r_attr)

    def _draw_status(self, row: int, w: int):
        nc  = len(self.change_indices)
        cur = self.current_change + 1 if nc else 0
        ro  = " [RO]" if self.readonly else ""
        msg = (self.status_msg or
               f" Diff {cur}/{nc}  Line {self.cursor_line+1}/{len(self.merged_lines)}"
               f"  [{self.focus.upper()}]{ro}")
        fill_line(self.stdscr, row, 0, w, curses.color_pair(CP_STATUS))
        safe_addstr(self.stdscr, row, 0, msg[:w], curses.color_pair(CP_STATUS))

    def _draw_keys(self, row: int, w: int):
        hints = (" F1:Help  F7:Prev  F8:Next  Tab:Switch"
                 "  Alt+<>:Merge  Ctrl+S:Save  F5:Reload  Q:Back")
        fill_line(self.stdscr, row, 0, w, curses.color_pair(CP_HEADER))
        safe_addstr(self.stdscr, row, 0, hints[:w], curses.color_pair(CP_HEADER))

    def _draw_help(self, h: int, w: int):
        lines = [
            "+----------------------------------------------------------+",
            "|  diffview  --  WinMerge-compatible diff (Linux/Windows)  |",
            "+----------------------------------------------------------+",
            "|  NAVIGATION                                              |",
            "|  Alt+Down / F8     Next difference                       |",
            "|  Alt+Up   / F7     Previous difference                   |",
            "|  Tab               Switch focus  Left <-> Right          |",
            "|  Up/Down           Move cursor one line                  |",
            "|  PgUp / PgDn       Scroll one page                       |",
            "|  Home / End        Jump to first / last line             |",
            "+----------------------------------------------------------+",
            "|  MERGE  (disabled in read-only mode)                     |",
            "|  Alt+Right         Copy current diff to right pane       |",
            "|  Alt+Left          Copy current diff to left pane        |",
            "|  Ctrl+S            Save focused pane to disk             |",
            "+----------------------------------------------------------+",
            "|  MODES                                                   |",
            "|  diffview file1 file2      File compare                  |",
            "|  diffview dir1/  dir2/     Directory compare             |",
            "|  diffview HEAD~3 HEAD      Git commit compare            |",
            "|  diffview main feature     Git branch compare            |",
            "|  diffview --install-git    Register as git tool          |",
            "+----------------------------------------------------------+",
            "|  F5 / F1 / ?     Reload / Help / Help                   |",
            "|  Q  / ESC        Back to directory view / quit           |",
            "+----------------------------------------------------------+",
            "  Press any key to close...",
        ]
        sr = max(0, (h - len(lines)) // 2)
        sc = max(0, (w - 64) // 2)
        attr = curses.color_pair(CP_HEADER)
        for i, ln in enumerate(lines):
            if sr + i >= h - 1:
                break
            safe_addstr(self.stdscr, sr + i, sc, ln[:max(0, w - sc)], attr)

    # ── navigation ───────────────────────────────────────────────────────────

    def _scroll_to(self, line: int):
        h, _ = self.stdscr.getmaxyx()
        ch   = h - 3
        if line < self.scroll_top:
            self.scroll_top = line
        elif line >= self.scroll_top + ch:
            self.scroll_top = max(0, line - ch // 2)

    def _goto_change(self, idx: int):
        if not self.change_indices:
            return
        idx = max(0, min(idx, len(self.change_indices) - 1))
        self.current_change = idx
        bi  = self.change_indices[idx]
        row = 0
        for i, b in enumerate(self.diff_blocks):
            if i == bi:
                break
            if b.type == DiffType.INSERT:
                row += len(b.right_lines)
            elif b.type == DiffType.DELETE:
                row += len(b.left_lines)
            else:
                row += max(len(b.left_lines), len(b.right_lines))
        self.cursor_line = row
        self._scroll_to(row)

    # ── merge ─────────────────────────────────────────────────────────────────

    def _copy_to_right(self):
        if self.readonly or not self.change_indices:
            return
        bi = self.change_indices[self.current_change]
        b  = self.diff_blocks[bi]
        rs, re = b.right_start, b.right_start + len(b.right_lines)
        self.right_lines[rs:re] = b.left_lines
        self.status_msg = "Copied left -> right"
        self._reload()

    def _copy_to_left(self):
        if self.readonly or not self.change_indices:
            return
        bi = self.change_indices[self.current_change]
        b  = self.diff_blocks[bi]
        ls, le = b.left_start, b.left_start + len(b.left_lines)
        self.left_lines[ls:le] = b.right_lines
        self.status_msg = "Copied right -> left"
        self._reload()

    # ── ESC / Alt sequence reader ─────────────────────────────────────────────

    def _read_esc(self) -> str:
        """Read chars after ESC non-blocking. Returns accumulated sequence."""
        self.stdscr.nodelay(True)
        buf = ""
        try:
            while True:
                c = self.stdscr.get_wch()
                s = chr(c) if isinstance(c, int) and c < 256 else (c if isinstance(c, str) else "")
                buf += s
                # Stop at a letter that terminates a CSI sequence
                if s and s[-1].isalpha():
                    break
                if len(buf) > 10:   # safety limit
                    break
        except curses.error:
            pass
        self.stdscr.nodelay(False)
        return buf

    # ── main loop ────────────────────────────────────────────────────────────

    def run(self) -> bool:
        """Run viewer. Returns True = back to dir list, False = quit."""
        curses.curs_set(0)
        if self.change_indices:
            self._goto_change(0)

        while True:
            h, w = self.stdscr.getmaxyx()
            ch   = h - 3
            self.draw()

            try:
                key = self.stdscr.get_wch()
            except curses.error:
                continue

            self.status_msg = ""

            # ── help overlay ──
            if self.show_help:
                self.show_help = False
                continue

            if key in (curses.KEY_F1, "?"):
                self.show_help = True
                continue

            # ── ESC  (may start Alt sequence) ──
            if key == "\x1b":
                seq = self._read_esc()
                # Alt+Down
                if seq in ("B", "[B", "[1;3B", "OB"):
                    self._goto_change(self.current_change + 1)
                # Alt+Up
                elif seq in ("A", "[A", "[1;3A", "OA"):
                    self._goto_change(self.current_change - 1)
                # Alt+Right
                elif seq in ("C", "[C", "[1;3C", "OC"):
                    self._copy_to_right()
                # Alt+Left
                elif seq in ("D", "[D", "[1;3D", "OD"):
                    self._copy_to_left()
                # Plain ESC → back
                elif seq == "":
                    return True
                continue

            # ── quit / back ──
            if key in ("q", "Q"):
                return True

            # ── F5 reload ──
            if key == curses.KEY_F5:
                self.left_lines  = read_file(self.left_file)
                self.right_lines = read_file(self.right_file)
                self._reload()
                self.status_msg = "Files reloaded"
                continue

            # ── Ctrl+S save ──
            if key == "\x13":
                if self.readonly:
                    self.status_msg = "Read-only mode -- cannot save"
                elif self.focus == "left" and self.left_file:
                    save_file(self.left_file, self.left_lines)
                    self.status_msg = f"Saved: {self.left_file}"
                elif self.right_file:
                    save_file(self.right_file, self.right_lines)
                    self.status_msg = f"Saved: {self.right_file}"
                continue

            # ── Tab ──
            if key == "\t":
                self.focus = "right" if self.focus == "left" else "left"
                continue

            # ── cursor movement ──
            if key == curses.KEY_UP:
                self.cursor_line = max(0, self.cursor_line - 1)
                self._scroll_to(self.cursor_line)
                continue
            if key == curses.KEY_DOWN:
                self.cursor_line = min(len(self.merged_lines) - 1,
                                       self.cursor_line + 1)
                self._scroll_to(self.cursor_line)
                continue
            if key == curses.KEY_PPAGE:
                self.cursor_line = max(0, self.cursor_line - ch)
                self.scroll_top  = max(0, self.scroll_top  - ch)
                continue
            if key == curses.KEY_NPAGE:
                self.cursor_line = min(len(self.merged_lines) - 1,
                                       self.cursor_line + ch)
                self.scroll_top  = min(max(0, len(self.merged_lines) - ch),
                                       self.scroll_top + ch)
                continue
            if key == curses.KEY_HOME:
                self.cursor_line = 0
                self.scroll_top  = 0
                continue
            if key == curses.KEY_END:
                self.cursor_line = len(self.merged_lines) - 1
                self._scroll_to(self.cursor_line)
                continue

            # ── F7 / F8 prev/next diff ──
            if key == curses.KEY_F7:
                self._goto_change(self.current_change - 1)
                continue
            if key == curses.KEY_F8:
                self._goto_change(self.current_change + 1)
                continue


# ─────────────────────────────────────────────────────────────────────────────
#  Top-level runners
# ─────────────────────────────────────────────────────────────────────────────

def run_file_mode(stdscr, left: str, right: str, readonly: bool = False):
    init_colors()
    FileDiffViewer(stdscr, left, right, readonly=readonly).run()


def run_dir_mode(stdscr, entries: list, left_label: str, right_label: str,
                 readonly: bool = False):
    init_colors()
    browser = DirViewer(stdscr, entries, left_label, right_label)
    while True:
        entry = browser.run()
        if entry is None:
            break
        FileDiffViewer(
            stdscr,
            entry.left_path, entry.right_path,
            left_label  = f"{left_label}/{entry.rel_path}",
            right_label = f"{right_label}/{entry.rel_path}",
            readonly    = readonly,
        ).run()


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

EPILOG = """
EXAMPLES:
  diffview file1.py file2.py           File compare
  diffview src\\ dst\\                  Directory compare  (Windows)
  diffview src/ dst/                   Directory compare  (Linux/mac)
  diffview HEAD~3 HEAD                 Git: last 3 commits
  diffview main feature/login          Git: branch compare
  diffview abc1234 def5678             Git: commit SHA compare
  diffview --install-git               Register as git difftool/mergetool

DIRECTORY VIEW:
  Enter   Open file diff for selected entry
  H       Toggle show-all / hide-identical

GIT (after --install-git):
  git difftool HEAD~1
  git difftool main..feature
  git mergetool
"""


def main():
    global _USE_UNICODE

    parser = argparse.ArgumentParser(
        description="diffview -- WinMerge-compatible diff tool (Linux/Windows/macOS)",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("left",  nargs="?", help="Left  file / directory / git ref")
    parser.add_argument("right", nargs="?", help="Right file / directory / git ref")
    parser.add_argument("--install-git", action="store_true",
                        help="Register diffview as git difftool and mergetool")
    parser.add_argument("--readonly", "-r", action="store_true",
                        help="Disable all merge / save operations")
    parser.add_argument("--unicode", action="store_true",
                        help="Force Unicode box-drawing chars (Linux default)")
    parser.add_argument("--ascii", action="store_true",
                        help="Force ASCII-only UI (Windows default)")
    args = parser.parse_args()

    if args.unicode:
        _USE_UNICODE = True
    if args.ascii:
        _USE_UNICODE = False

    # ── git install ──────────────────────────────────────────────────────────
    if args.install_git:
        script = os.path.abspath(__file__)
        cmds = [
            ["git", "config", "--global", "diff.tool",                  "diffview"],
            ["git", "config", "--global", "difftool.diffview.cmd",
             f'python "{script}" "$LOCAL" "$REMOTE"'],
            ["git", "config", "--global", "difftool.prompt",            "false"],
            ["git", "config", "--global", "merge.tool",                 "diffview"],
            ["git", "config", "--global", "mergetool.diffview.cmd",
             f'python "{script}" "$LOCAL" "$REMOTE"'],
            ["git", "config", "--global", "mergetool.diffview.trustExitCode", "false"],
        ]
        for cmd in cmds:
            try:
                subprocess.run(cmd, check=True)
            except Exception as e:
                print(f"Warning: {e}")
        print("diffview registered as git difftool and mergetool")
        print(f"  Script: {script}")
        print()
        print("Usage:")
        print("  git difftool HEAD~1")
        print("  git difftool main..feature")
        print("  git mergetool")
        return

    if not args.left or not args.right:
        parser.print_help()
        sys.exit(1)

    left, right = args.left, args.right
    mode = resolve_mode(left, right)

    if mode == "git":
        entries = git_diff_files(left, right)
        if not entries:
            print(f"Error: no files found between '{left}' and '{right}'")
            sys.exit(1)
        curses.wrapper(run_dir_mode, entries, left, right, True)

    elif mode == "dir":
        entries = compare_dirs(left, right)
        curses.wrapper(run_dir_mode, entries, left, right, args.readonly)

    else:
        if not os.path.exists(left) and not os.path.exists(right):
            print(f"Error: neither '{left}' nor '{right}' exists.")
            sys.exit(1)
        curses.wrapper(run_file_mode, left, right, args.readonly)


if __name__ == "__main__":
    main()
