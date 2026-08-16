#!/usr/bin/env python3
"""CatPad++ 1.x - a single-file, blue-hue tabbed code editor."""

from __future__ import annotations

import os
import platform
import queue
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font, messagebox, simpledialog, ttk


# =========================
# CATPAD++ COLORS
# =========================

APP_BG = "#07152f"
PANEL_BG = "#0b2147"
EDITOR_BG = "#091a38"
EDITOR_FG = "#5ecbff"
GUTTER_BG = "#061329"
GUTTER_FG = "#397daf"
BUTTON_BG = "#000000"
BUTTON_FG = "#54c7ff"
BORDER = "#153b71"
SELECT_BG = "#165da8"
SELECT_FG = "#e8f8ff"
CURRENT_LINE = "#102958"
MUTED = "#4f89b6"

LANGUAGES = (
    "Plain Text", "Python", "C", "C++", "JavaScript", "HTML",
    "CSS", "JSON", "Markdown", "Shell", "Assembly",
)

# Primary extension is first; every syntax language has at least one save filter.
LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "Plain Text": (".txt",),
    "Python": (".py",),
    "C": (".c", ".h"),
    "C++": (".cpp", ".cc", ".cxx", ".hpp", ".h"),
    "JavaScript": (".js", ".mjs"),
    "HTML": (".html", ".htm"),
    "CSS": (".css",),
    "JSON": (".json",),
    "Markdown": (".md", ".markdown"),
    "Shell": (".sh", ".bash"),
    "Assembly": (".asm", ".s"),
}

EXTENSION_LANGUAGES = {
    extension: language
    for language, extensions in LANGUAGE_EXTENSIONS.items()
    for extension in extensions
    if not (extension == ".h" and language == "C++")  # prefer C for .h on open
}


def language_for_path(path: str | Path) -> str:
    return EXTENSION_LANGUAGES.get(Path(path).suffix.lower(), "Plain Text")


def default_extension_for_language(language: str) -> str:
    return LANGUAGE_EXTENSIONS.get(language, (".txt",))[0]


def _pattern_for_extensions(extensions: tuple[str, ...]) -> str:
    return " ".join(f"*{extension}" for extension in extensions)


def file_types_for_dialog(preferred_language: str | None = None) -> list[tuple[str, str]]:
    """File dialog filters covering every syntax language CatPad++ supports."""
    all_extensions: list[str] = []
    seen: set[str] = set()
    for language in LANGUAGES:
        for extension in LANGUAGE_EXTENSIONS[language]:
            if extension not in seen:
                seen.add(extension)
                all_extensions.append(extension)

    preferred = preferred_language if preferred_language in LANGUAGE_EXTENSIONS else None
    types: list[tuple[str, str]] = []
    if preferred:
        types.append((preferred, _pattern_for_extensions(LANGUAGE_EXTENSIONS[preferred])))
    types.append(("Text and code", _pattern_for_extensions(tuple(all_extensions))))
    for language in LANGUAGES:
        if language == preferred:
            continue
        types.append((language, _pattern_for_extensions(LANGUAGE_EXTENSIONS[language])))
    types.append(("All files", "*.*"))
    return types


def center_window(window: tk.Toplevel, width: int, height: int) -> None:
    window.update_idletasks()
    parent = window.master
    if parent and parent.winfo_ismapped():
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
    else:
        x = max(0, (window.winfo_screenwidth() - width) // 2)
        y = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def black_button(parent: tk.Misc, text: str, command, width: int | None = None) -> tk.Button:
    options = {
        "text": text,
        "command": command,
        "bg": BUTTON_BG,
        "fg": BUTTON_FG,
        "activebackground": "#071020",
        "activeforeground": "#a8e9ff",
        "relief": tk.FLAT,
        "bd": 0,
        "padx": 10,
        "pady": 5,
        "highlightthickness": 1,
        "highlightbackground": BORDER,
        "cursor": "hand2",
    }
    if width is not None:
        options["width"] = width
    return tk.Button(parent, **options)


# =========================
# EDITOR DOCUMENT
# =========================


class EditorDocument:
    SYNTAX_TAGS = ("keyword", "string", "comment", "number", "function", "class", "markup", "constant")

    KEYWORDS = {
        "Python": (
            "False None True and as assert async await break class continue def del elif else except "
            "finally for from global if import in is lambda nonlocal not or pass raise return try while with yield"
        ),
        "C": (
            "auto break case char const continue default do double else enum extern float for goto if inline int "
            "long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while"
        ),
        "C++": (
            "alignas alignof and and_eq asm atomic_cancel atomic_commit atomic_noexcept auto bitand bitor bool break "
            "case catch char char8_t char16_t char32_t class compl concept const consteval constexpr constinit const_cast "
            "continue co_await co_return co_yield decltype default delete do double dynamic_cast else enum explicit export "
            "extern false float for friend goto if inline int long mutable namespace new noexcept not not_eq nullptr operator "
            "or or_eq private protected public reflexpr register reinterpret_cast requires return short signed sizeof static "
            "static_assert static_cast struct switch synchronized template this thread_local throw true try typedef typeid "
            "typename union unsigned using virtual void volatile wchar_t while xor xor_eq"
        ),
        "JavaScript": (
            "async await break case catch class const continue debugger default delete do else export extends false finally "
            "for from function get if import in instanceof let new null of return set static super switch this throw true try "
            "typeof undefined var void while with yield"
        ),
        "CSS": "align-items animation background border bottom color content display flex float font gap grid height justify-content left margin max-width min-width opacity overflow padding position right top transform transition width z-index",
        "JSON": "true false null",
        "Shell": "case do done elif else esac export fi for function if in local readonly return select then until while",
        "Assembly": "section segment global extern bits org db dw dd dq resb resw resd resq equ times byte word dword qword ptr",
    }

    def __init__(self, app: "CatPadApp", name: str, path: str | None = None, language: str = "Plain Text") -> None:
        self.app = app
        self.name = name
        self.path = path
        self.language = language
        self.encoding = "UTF-8"
        self.modified = False
        self.loading = False
        self._highlight_job: str | None = None

        self.frame = tk.Frame(app.notebook, bg=EDITOR_BG, bd=0, highlightthickness=0)
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(1, weight=1)

        self.gutter = tk.Canvas(
            self.frame,
            width=48,
            bg=GUTTER_BG,
            bd=0,
            highlightthickness=0,
            takefocus=0,
        )
        self.gutter.grid(row=0, column=0, sticky="ns")

        self.text = tk.Text(
            self.frame,
            bg=EDITOR_BG,
            fg=EDITOR_FG,
            insertbackground="#b6ecff",
            selectbackground=SELECT_BG,
            selectforeground=SELECT_FG,
            inactiveselectbackground="#12467e",
            font=app.editor_font,
            wrap=tk.NONE,
            undo=True,
            autoseparators=True,
            maxundo=-1,
            relief=tk.FLAT,
            bd=0,
            padx=8,
            pady=5,
            tabs=(app.tab_pixels,),
            blockcursor=False,
        )
        self.text.grid(row=0, column=1, sticky="nsew")

        self.vbar = tk.Scrollbar(
            self.frame,
            orient=tk.VERTICAL,
            command=self._on_vertical_scroll,
            bg=PANEL_BG,
            troughcolor=GUTTER_BG,
            activebackground="#1d5795",
            highlightthickness=0,
            bd=0,
        )
        self.vbar.grid(row=0, column=2, sticky="ns")

        self.hbar = tk.Scrollbar(
            self.frame,
            orient=tk.HORIZONTAL,
            command=self.text.xview,
            bg=PANEL_BG,
            troughcolor=GUTTER_BG,
            activebackground="#1d5795",
            highlightthickness=0,
            bd=0,
        )
        self.hbar.grid(row=1, column=0, columnspan=3, sticky="ew")

        self.text.configure(yscrollcommand=self._on_text_scroll, xscrollcommand=self.hbar.set)
        self._configure_tags()
        self._bind_events()

    @property
    def display_name(self) -> str:
        return f"{self.name}{'*' if self.modified else ''}"

    def _configure_tags(self) -> None:
        colors = {
            "keyword": "#3e91ff",
            "string": "#55e0ff",
            "comment": "#4d83a8",
            "number": "#7bb7ff",
            "function": "#9ce8ff",
            "class": "#76d6ff",
            "markup": "#34c9e8",
            "constant": "#70a7ff",
        }
        for tag, color in colors.items():
            self.text.tag_configure(tag, foreground=color)
        self.text.tag_configure("comment", font=self.app.italic_font)
        self.text.tag_configure("current_line", background=CURRENT_LINE)
        self.text.tag_configure("search_match", background="#1c83d5", foreground="#ffffff")
        self.text.tag_lower("current_line")

    def _bind_events(self) -> None:
        self.text.bind("<<Modified>>", self._on_modified, add=True)
        self.text.bind("<KeyRelease>", self._on_cursor_activity, add=True)
        self.text.bind("<ButtonRelease-1>", self._on_cursor_activity, add=True)
        self.text.bind("<FocusIn>", self._on_cursor_activity, add=True)
        self.text.bind("<Configure>", lambda _e: self.frame.after_idle(self.redraw_line_numbers), add=True)
        self.text.bind("<Return>", self._handle_return)
        self.text.bind("<Tab>", self._handle_tab)
        self.text.bind("<Shift-Tab>", self._handle_shift_tab)
        self.text.bind("<ISO_Left_Tab>", self._handle_shift_tab)

    def set_content(self, content: str) -> None:
        self.loading = True
        try:
            self.text.delete("1.0", tk.END)
            self.text.insert("1.0", content)
            self.text.edit_reset()
            self.text.edit_modified(False)
            self.modified = False
        finally:
            self.loading = False
        self.text.mark_set(tk.INSERT, "1.0")
        self.text.see("1.0")
        self.schedule_highlight(10)
        self.frame.after_idle(self.redraw_line_numbers)
        self.update_current_line()

    def get_content(self) -> str:
        return self.text.get("1.0", "end-1c")

    def set_modified(self, value: bool) -> None:
        if self.modified == value:
            return
        self.modified = value
        self.app.update_document_labels()

    def _on_modified(self, _event=None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if not self.loading:
                self.set_modified(True)
                self.schedule_highlight()
                self.frame.after_idle(self.redraw_line_numbers)
                self.app.update_status()

    def _on_cursor_activity(self, _event=None) -> None:
        self.update_current_line()
        self.redraw_line_numbers()
        self.app.update_status()

    def update_current_line(self) -> None:
        self.text.tag_remove("current_line", "1.0", tk.END)
        self.text.tag_add("current_line", "insert linestart", "insert lineend+1c")
        self.text.tag_lower("current_line")

    def _on_text_scroll(self, first: str, last: str) -> None:
        self.vbar.set(first, last)
        self.frame.after_idle(self.redraw_line_numbers)

    def _on_vertical_scroll(self, *args) -> None:
        self.text.yview(*args)
        self.frame.after_idle(self.redraw_line_numbers)

    def redraw_line_numbers(self) -> None:
        if not self.app.line_numbers_var.get() or not self.gutter.winfo_exists():
            return
        self.gutter.delete("all")
        index = self.text.index("@0,0")
        seen: set[str] = set()
        while True:
            logical = self.text.index(f"{index} linestart")
            info = self.text.dlineinfo(index)
            if info is None:
                break
            if logical not in seen:
                seen.add(logical)
                y = info[1]
                line_number = logical.split(".")[0]
                self.gutter.create_text(
                    self.gutter.winfo_width() - 8,
                    y + 2,
                    anchor="ne",
                    text=line_number,
                    fill=GUTTER_FG,
                    font=self.app.gutter_font,
                )
            next_index = self.text.index(f"{logical}+1line")
            if next_index == logical or self.text.compare(next_index, ">=", "end"):
                break
            index = next_index

        digits = max(2, len(self.text.index("end-1c").split(".")[0]))
        wanted = 18 + digits * max(7, self.app.font_size // 2)
        if int(float(self.gutter.cget("width"))) != wanted:
            self.gutter.configure(width=wanted)

    def set_line_numbers_visible(self, visible: bool) -> None:
        if visible:
            self.gutter.grid()
            self.frame.after_idle(self.redraw_line_numbers)
        else:
            self.gutter.grid_remove()

    def set_word_wrap(self, enabled: bool) -> None:
        self.text.configure(wrap=tk.WORD if enabled else tk.NONE)
        if enabled:
            self.hbar.grid_remove()
        else:
            self.hbar.grid()
        self.frame.after_idle(self.redraw_line_numbers)

    def refresh_font_metrics(self) -> None:
        self.text.configure(tabs=(self.app.tab_pixels,))
        self.frame.after_idle(self.redraw_line_numbers)

    def _handle_return(self, _event=None):
        try:
            if self.text.tag_ranges(tk.SEL):
                self.text.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            pass
        before_cursor = self.text.get("insert linestart", "insert")
        indent_match = re.match(r"[ \t]*", before_cursor)
        indent = indent_match.group(0).replace("\t", " " * self.app.tab_width) if indent_match else ""
        stripped = before_cursor.rstrip()
        if self.language == "Python" and stripped.endswith(":"):
            indent += " " * self.app.tab_width
        elif self.language in {"C", "C++", "JavaScript", "CSS"} and stripped.endswith("{"):
            indent += " " * self.app.tab_width
        self.text.insert(tk.INSERT, "\n" + indent)
        self.text.see(tk.INSERT)
        return "break"

    def _selected_line_range(self) -> tuple[str, str]:
        try:
            first = self.text.index("sel.first linestart")
            last = self.text.index("sel.last linestart")
            if self.text.compare("sel.last", "==", last):
                last = self.text.index(f"{last}-1line")
            return first, last
        except tk.TclError:
            line = self.text.index("insert linestart")
            return line, line

    def _handle_tab(self, _event=None):
        if self.text.tag_ranges(tk.SEL):
            first, last = self._selected_line_range()
            line = first
            while True:
                self.text.insert(line, " " * self.app.tab_width)
                if self.text.compare(line, ">=", last):
                    break
                line = self.text.index(f"{line}+1line")
            return "break"
        self.text.insert(tk.INSERT, " " * self.app.tab_width)
        return "break"

    def _handle_shift_tab(self, _event=None):
        first, last = self._selected_line_range()
        line = first
        while True:
            sample = self.text.get(line, f"{line}+{self.app.tab_width}c")
            remove = len(sample) - len(sample.lstrip(" "))
            if remove:
                self.text.delete(line, f"{line}+{min(remove, self.app.tab_width)}c")
            elif self.text.get(line, f"{line}+1c") == "\t":
                self.text.delete(line, f"{line}+1c")
            if self.text.compare(line, ">=", last):
                break
            line = self.text.index(f"{line}+1line")
        return "break"

    def schedule_highlight(self, delay: int = 250) -> None:
        if self._highlight_job is not None:
            try:
                self.frame.after_cancel(self._highlight_job)
            except tk.TclError:
                pass
        self._highlight_job = self.frame.after(delay, self.highlight_syntax)

    def _apply_pattern(self, pattern: str, tag: str, content: str, base: str, flags: int = 0, group: int = 0) -> None:
        try:
            for count, match in enumerate(re.finditer(pattern, content, flags)):
                if count >= 25000:
                    break
                start, end = match.span(group)
                if start == end:
                    continue
                self.text.tag_add(tag, f"{base}+{start}c", f"{base}+{end}c")
        except (re.error, tk.TclError):
            return

    def highlight_syntax(self) -> None:
        self._highlight_job = None
        if not self.text.winfo_exists():
            return
        end_index = self.text.index("end-1c")
        total_chars = int(self.text.count("1.0", end_index, "chars")[0]) if self.text.compare(end_index, ">", "1.0") else 0
        if total_chars > 400_000:
            base = self.text.index("@0,0 linestart -100lines")
            visible_bottom = self.text.index(f"@0,{max(1, self.text.winfo_height())} linestart +100lines")
            limit = visible_bottom if self.text.compare(visible_bottom, "<", end_index) else end_index
        else:
            base, limit = "1.0", end_index
        content = self.text.get(base, limit)
        for tag in self.SYNTAX_TAGS:
            self.text.tag_remove(tag, base, limit)
        if not content or self.language == "Plain Text":
            return

        language = self.language
        keywords = self.KEYWORDS.get(language, "")
        if keywords:
            words = keywords.split()
            self._apply_pattern(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", "keyword", content, base)
        self._apply_pattern(r"(?<![\w.])(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b", "number", content, base)

        if language == "Python":
            self._apply_pattern(r"\bdef\s+([A-Za-z_]\w*)", "function", content, base, group=1)
            self._apply_pattern(r"\bclass\s+([A-Za-z_]\w*)", "class", content, base, group=1)
            string_pattern = r"(?:'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
            self._apply_pattern(string_pattern, "string", content, base)
            self._apply_pattern(r"(?m)#.*$", "comment", content, base)
        elif language in {"C", "C++", "JavaScript"}:
            if language == "JavaScript":
                self._apply_pattern(r"\bfunction\s+([A-Za-z_$][\w$]*)", "function", content, base, group=1)
                self._apply_pattern(r"\bclass\s+([A-Za-z_$][\w$]*)", "class", content, base, group=1)
            else:
                self._apply_pattern(r"\b([A-Za-z_]\w*)\s*(?=\([^;{}]*\)\s*\{)", "function", content, base, group=1)
            self._apply_pattern(r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`)", "string", content, base)
            self._apply_pattern(r"//[^\n]*|/\*[\s\S]*?\*/", "comment", content, base)
            self._apply_pattern(r"(?m)^\s*#\s*\w+", "constant", content, base)
        elif language == "HTML":
            self._apply_pattern(r"</?[A-Za-z][^>]*>", "markup", content, base)
            self._apply_pattern(r"(?:'[^']*'|\"[^\"]*\")", "string", content, base)
            self._apply_pattern(r"<!--[\s\S]*?-->", "comment", content, base)
        elif language == "CSS":
            self._apply_pattern(r"[#.]?[A-Za-z_-][\w-]*(?=\s*\{)", "markup", content, base)
            self._apply_pattern(r"(?:'[^']*'|\"[^\"]*\")", "string", content, base)
            self._apply_pattern(r"/\*[\s\S]*?\*/", "comment", content, base)
        elif language == "JSON":
            self._apply_pattern(r"\"(?:\\.|[^\"\\])*\"(?=\s*:)", "markup", content, base)
            self._apply_pattern(r"\"(?:\\.|[^\"\\])*\"", "string", content, base)
        elif language == "Markdown":
            self._apply_pattern(r"(?m)^#{1,6}\s+.*$", "markup", content, base)
            self._apply_pattern(r"`{1,3}[\s\S]*?`{1,3}", "string", content, base)
            self._apply_pattern(r"(?m)^\s*>.*$", "comment", content, base)
            self._apply_pattern(r"\[[^\]]+\]\([^)]+\)", "function", content, base)
        elif language == "Shell":
            self._apply_pattern(r"(?:'(?:[^']*)'|\"(?:\\.|[^\"\\])*\")", "string", content, base)
            self._apply_pattern(r"(?m)#.*$", "comment", content, base)
            self._apply_pattern(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "constant", content, base)
        elif language == "Assembly":
            self._apply_pattern(r"(?m)^\s*([A-Za-z_.$?][\w.$?]*):", "function", content, base, group=1)
            self._apply_pattern(r"(?:;|#)[^\n]*", "comment", content, base)
            self._apply_pattern(r"(?:'[^']*'|\"[^\"]*\")", "string", content, base)

        for tag in ("keyword", "number", "function", "class", "markup", "constant", "string", "comment"):
            self.text.tag_raise(tag)
        self.text.tag_raise(tk.SEL)


# =========================
# FIND AND REPLACE DIALOGS
# =========================


class FindDialog(tk.Toplevel):
    def __init__(self, app: "CatPadApp") -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Find - CatPad++")
        self.configure(bg=PANEL_BG)
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.query = tk.StringVar(value=app.last_search)
        self.match_case = tk.BooleanVar(value=app.last_match_case)

        tk.Label(self, text="Find:", bg=PANEL_BG, fg=EDITOR_FG).grid(row=0, column=0, padx=10, pady=(12, 5), sticky="w")
        self.entry = tk.Entry(
            self, textvariable=self.query, width=38, bg=EDITOR_BG, fg=EDITOR_FG,
            insertbackground=EDITOR_FG, selectbackground=SELECT_BG, relief=tk.FLAT,
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.entry.grid(row=0, column=1, columnspan=3, padx=(0, 12), pady=(12, 5), sticky="ew")
        check = tk.Checkbutton(
            self, text="Match case", variable=self.match_case, bg=PANEL_BG, fg=EDITOR_FG,
            activebackground=PANEL_BG, activeforeground=EDITOR_FG, selectcolor=EDITOR_BG,
        )
        check.grid(row=1, column=1, padx=0, pady=5, sticky="w")
        black_button(self, "Find Previous", lambda: self.find(False)).grid(row=2, column=1, padx=(0, 5), pady=(5, 12))
        black_button(self, "Find Next", lambda: self.find(True)).grid(row=2, column=2, padx=5, pady=(5, 12))
        black_button(self, "Close", self.close).grid(row=2, column=3, padx=(5, 12), pady=(5, 12))

        self.entry.bind("<Return>", lambda _e: self.find(True))
        self.entry.bind("<Shift-Return>", lambda _e: self.find(False))
        self.bind("<Escape>", lambda _e: self.close())
        center_window(self, 470, 145)
        self.entry.focus_set()
        self.entry.selection_range(0, tk.END)

    def find(self, forward: bool) -> None:
        self.app.last_search = self.query.get()
        self.app.last_match_case = self.match_case.get()
        self.app.find_text(self.query.get(), forward, self.match_case.get())
        self.entry.focus_set()

    def close(self) -> None:
        self.app.find_dialog = None
        self.destroy()


class ReplaceDialog(tk.Toplevel):
    def __init__(self, app: "CatPadApp") -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Replace - CatPad++")
        self.configure(bg=PANEL_BG)
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.find_var = tk.StringVar(value=app.last_search)
        self.replace_var = tk.StringVar()
        self.match_case = tk.BooleanVar(value=app.last_match_case)
        self.result_var = tk.StringVar(value="")

        for row, (label, variable) in enumerate((("Find:", self.find_var), ("Replace with:", self.replace_var))):
            tk.Label(self, text=label, bg=PANEL_BG, fg=EDITOR_FG).grid(row=row, column=0, padx=10, pady=(12 if row == 0 else 5, 5), sticky="w")
            entry = tk.Entry(
                self, textvariable=variable, width=38, bg=EDITOR_BG, fg=EDITOR_FG,
                insertbackground=EDITOR_FG, selectbackground=SELECT_BG, relief=tk.FLAT,
                highlightthickness=1, highlightbackground=BORDER,
            )
            entry.grid(row=row, column=1, columnspan=4, padx=(0, 12), pady=(12 if row == 0 else 5, 5), sticky="ew")
            if row == 0:
                self.find_entry = entry

        tk.Checkbutton(
            self, text="Match case", variable=self.match_case, bg=PANEL_BG, fg=EDITOR_FG,
            activebackground=PANEL_BG, activeforeground=EDITOR_FG, selectcolor=EDITOR_BG,
        ).grid(row=2, column=1, padx=0, pady=4, sticky="w")
        tk.Label(self, textvariable=self.result_var, bg=PANEL_BG, fg=MUTED).grid(row=2, column=2, columnspan=3, sticky="e", padx=12)
        black_button(self, "Find Next", self.find_next).grid(row=3, column=1, padx=(0, 4), pady=(6, 12))
        black_button(self, "Replace", self.replace_one).grid(row=3, column=2, padx=4, pady=(6, 12))
        black_button(self, "Replace All", self.replace_all).grid(row=3, column=3, padx=4, pady=(6, 12))
        black_button(self, "Close", self.close).grid(row=3, column=4, padx=(4, 12), pady=(6, 12))

        self.find_entry.bind("<Return>", lambda _e: self.find_next())
        self.bind("<Escape>", lambda _e: self.close())
        center_window(self, 575, 190)
        self.find_entry.focus_set()
        self.find_entry.selection_range(0, tk.END)

    def remember(self) -> None:
        self.app.last_search = self.find_var.get()
        self.app.last_match_case = self.match_case.get()

    def find_next(self) -> None:
        self.remember()
        self.app.find_text(self.find_var.get(), True, self.match_case.get())
        self.find_entry.focus_set()

    def replace_one(self) -> None:
        self.remember()
        replaced = self.app.replace_current_match(self.find_var.get(), self.replace_var.get(), self.match_case.get())
        self.result_var.set("Replaced" if replaced else "No active match")
        self.find_entry.focus_set()

    def replace_all(self) -> None:
        self.remember()
        count = self.app.replace_all(self.find_var.get(), self.replace_var.get(), self.match_case.get())
        self.result_var.set(f"{count} replacement{'s' if count != 1 else ''}")
        self.find_entry.focus_set()

    def close(self) -> None:
        self.app.replace_dialog = None
        self.destroy()


# =========================
# OUTPUT CONSOLE
# =========================


class OutputConsole:
    def __init__(self, app: "CatPadApp") -> None:
        self.app = app
        self.frame = tk.Frame(app.main_pane, bg=PANEL_BG, height=180, highlightthickness=1, highlightbackground=BORDER)
        self.frame.pack_propagate(False)

        header = tk.Frame(self.frame, bg=PANEL_BG)
        header.pack(side=tk.TOP, fill=tk.X)
        tk.Label(
            header, text="CatPad++ Output", bg=PANEL_BG, fg=EDITOR_FG,
            font=(app.ui_font_family, 10, "bold"), padx=8, pady=4,
        ).pack(side=tk.LEFT)
        black_button(header, "Close", app.hide_output).pack(side=tk.RIGHT, padx=(2, 6), pady=3)
        black_button(header, "Stop", app.stop_process).pack(side=tk.RIGHT, padx=2, pady=3)
        black_button(header, "Clear", self.clear).pack(side=tk.RIGHT, padx=2, pady=3)

        body = tk.Frame(self.frame, bg=EDITOR_BG)
        body.pack(fill=tk.BOTH, expand=True)
        scroll = tk.Scrollbar(body, orient=tk.VERTICAL, bg=PANEL_BG, troughcolor=GUTTER_BG, bd=0)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text = tk.Text(
            body, bg=EDITOR_BG, fg=EDITOR_FG, insertbackground=EDITOR_FG,
            font=app.editor_font, relief=tk.FLAT, bd=0, padx=8, pady=6,
            wrap=tk.WORD, state=tk.DISABLED, yscrollcommand=scroll.set,
        )
        self.text.pack(fill=tk.BOTH, expand=True)
        scroll.configure(command=self.text.yview)

    def append(self, content: str) -> None:
        if not self.text.winfo_exists():
            return
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, content)
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def clear(self) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)


# =========================
# CATPAD++ WINDOW
# =========================


class CatPadApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CatPad++ 1.x")
        self.root.geometry("1100x700")
        self.root.minsize(760, 480)
        self.root.configure(bg=APP_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

        self.documents: list[EditorDocument] = []
        self.new_counter = 0
        self.zoom_level = 100
        self.base_font_size = 12
        self.font_size = self.base_font_size
        self.tab_width = 4
        self.last_search = ""
        self.last_match_case = False
        self.find_dialog: FindDialog | None = None
        self.replace_dialog: ReplaceDialog | None = None
        self.current_process: subprocess.Popen[str] | None = None
        self.process_busy = False
        self.stop_requested = False
        self.process_lock = threading.Lock()
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.output_poll_job: str | None = None
        self.output_visible = False
        self.fullscreen_var = tk.BooleanVar(value=False)
        self.line_numbers_var = tk.BooleanVar(value=True)
        self.word_wrap_var = tk.BooleanVar(value=False)
        self.status_bar_var = tk.BooleanVar(value=True)
        self.document_panel_var = tk.BooleanVar(value=True)
        self.output_panel_var = tk.BooleanVar(value=False)
        self.language_var = tk.StringVar(value="Plain Text")

        system = platform.system()
        self.editor_font_family = "Menlo" if system == "Darwin" else "Consolas" if system == "Windows" else "DejaVu Sans Mono"
        self.ui_font_family = "SF Pro Text" if system == "Darwin" else "Segoe UI" if system == "Windows" else "DejaVu Sans"
        self.editor_font = font.Font(family=self.editor_font_family, size=self.font_size)
        self.italic_font = font.Font(family=self.editor_font_family, size=self.font_size, slant="italic")
        self.gutter_font = font.Font(family=self.editor_font_family, size=max(8, self.font_size - 1))
        self.tab_pixels = self.editor_font.measure(" " * self.tab_width)

        self._configure_theme()
        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()
        self._build_main_area()
        self._bind_shortcuts()
        self.new_document()
        self.root.after(80, lambda: self.current_document().text.focus_set() if self.current_document() else None)

    def _configure_theme(self) -> None:
        self.root.option_add("*Menu.background", PANEL_BG)
        self.root.option_add("*Menu.foreground", EDITOR_FG)
        self.root.option_add("*Menu.activeBackground", SELECT_BG)
        self.root.option_add("*Menu.activeForeground", SELECT_FG)
        self.root.option_add("*tearOff", False)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Cat.TNotebook", background=APP_BG, borderwidth=0, tabmargins=(2, 3, 2, 0))
        style.configure(
            "Cat.TNotebook.Tab", background="#0a1b37", foreground=MUTED,
            padding=(12, 6), borderwidth=1, font=(self.ui_font_family, 10),
        )
        style.map(
            "Cat.TNotebook.Tab",
            background=[("selected", PANEL_BG), ("active", "#123364")],
            foreground=[("selected", EDITOR_FG), ("active", "#8dddff")],
        )

    def _build_menu(self) -> None:
        self.menu_bar = tk.Menu(self.root, bg=PANEL_BG, fg=EDITOR_FG, activebackground=SELECT_BG, activeforeground=SELECT_FG)
        self.root.configure(menu=self.menu_bar)

        file_menu = tk.Menu(self.menu_bar)
        file_menu.add_command(label="New", accelerator="Ctrl+N", command=self.new_document)
        file_menu.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_file)
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_current)
        file_menu.add_command(label="Save As...", accelerator="Ctrl+Shift+S", command=self.save_current_as)
        file_menu.add_separator()
        file_menu.add_command(label="Close Tab", accelerator="Ctrl+W", command=self.close_current_tab)
        file_menu.add_command(label="Close All", command=self.close_all_documents)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.exit_app)
        self.menu_bar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(self.menu_bar)
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", accelerator="Ctrl+X", command=lambda: self.edit_event("<<Cut>>"))
        edit_menu.add_command(label="Copy", accelerator="Ctrl+C", command=lambda: self.edit_event("<<Copy>>"))
        edit_menu.add_command(label="Paste", accelerator="Ctrl+V", command=lambda: self.edit_event("<<Paste>>"))
        edit_menu.add_command(label="Delete", accelerator="Del", command=self.delete_selection)
        edit_menu.add_command(label="Select All", accelerator="Ctrl+A", command=self.select_all)
        edit_menu.add_separator()
        edit_menu.add_command(label="Duplicate Line", accelerator="Ctrl+D", command=self.duplicate_line)
        edit_menu.add_command(label="Delete Line", accelerator="Ctrl+Shift+K", command=self.delete_line)
        self.menu_bar.add_cascade(label="Edit", menu=edit_menu)

        search_menu = tk.Menu(self.menu_bar)
        search_menu.add_command(label="Find...", accelerator="Ctrl+F", command=self.show_find_dialog)
        search_menu.add_command(label="Find Next", accelerator="F3", command=lambda: self.find_again(True))
        search_menu.add_command(label="Find Previous", accelerator="Shift+F3", command=lambda: self.find_again(False))
        search_menu.add_command(label="Replace...", accelerator="Ctrl+H", command=self.show_replace_dialog)
        search_menu.add_command(label="Go To Line...", accelerator="Ctrl+G", command=self.go_to_line)
        self.menu_bar.add_cascade(label="Search", menu=search_menu)

        view_menu = tk.Menu(self.menu_bar)
        view_menu.add_checkbutton(label="Toggle Line Numbers", variable=self.line_numbers_var, command=self.toggle_line_numbers)
        view_menu.add_checkbutton(label="Toggle Word Wrap", variable=self.word_wrap_var, command=self.toggle_word_wrap)
        view_menu.add_command(label="Set Tab Width...", command=self.set_tab_width_dialog)
        view_menu.add_checkbutton(label="Toggle Document Panel", variable=self.document_panel_var, command=self.toggle_document_panel)
        view_menu.add_checkbutton(label="Toggle Output Console", variable=self.output_panel_var, command=self.toggle_output)
        view_menu.add_separator()
        view_menu.add_command(label="Zoom In", accelerator="Ctrl++", command=self.zoom_in)
        view_menu.add_command(label="Zoom Out", accelerator="Ctrl+-", command=self.zoom_out)
        view_menu.add_command(label="Reset Zoom", accelerator="Ctrl+0", command=self.reset_zoom)
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Toggle Status Bar", variable=self.status_bar_var, command=self.toggle_status_bar)
        view_menu.add_checkbutton(label="Fullscreen", accelerator="F11", variable=self.fullscreen_var, command=self.toggle_fullscreen)
        self.menu_bar.add_cascade(label="View", menu=view_menu)

        language_menu = tk.Menu(self.menu_bar)
        for language in LANGUAGES:
            language_menu.add_radiobutton(
                label=language, value=language, variable=self.language_var,
                command=lambda lang=language: self.set_language(lang),
            )
        self.menu_bar.add_cascade(label="Language", menu=language_menu)

        run_menu = tk.Menu(self.menu_bar)
        run_menu.add_command(label="Run Python File", accelerator="F5", command=self.run_python_file)
        run_menu.add_command(label="Open Terminal Command...", command=self.open_terminal_command)
        run_menu.add_command(label="Stop Running Process", command=self.stop_process)
        run_menu.add_separator()
        run_menu.add_command(label="Clear Output", command=self.clear_output)
        self.menu_bar.add_cascade(label="Run", menu=run_menu)

        help_menu = tk.Menu(self.menu_bar)
        help_menu.add_command(label="About CatPad++", command=self.show_about)
        help_menu.add_command(label="Keyboard Shortcuts", command=self.show_shortcuts)
        self.menu_bar.add_cascade(label="Help", menu=help_menu)

    def _build_toolbar(self) -> None:
        self.toolbar = tk.Frame(self.root, bg=PANEL_BG, highlightthickness=1, highlightbackground=BORDER)
        self.toolbar.pack(side=tk.TOP, fill=tk.X)
        actions = (
            ("New", self.new_document), ("Open", self.open_file), ("Save", self.save_current),
            ("Undo", self.undo), ("Redo", self.redo), ("Find", self.show_find_dialog),
            ("Run", self.run_python_file),
        )
        for text, command in actions:
            black_button(self.toolbar, text, command).pack(side=tk.LEFT, padx=(5, 0), pady=5)
        tk.Label(
            self.toolbar, text="CATPAD++ 1.x", bg=PANEL_BG, fg="#2c78ad",
            font=(self.ui_font_family, 9, "bold"), padx=10,
        ).pack(side=tk.RIGHT)

    def _build_status_bar(self) -> None:
        self.status_frame = tk.Frame(self.root, bg=GUTTER_BG, highlightthickness=1, highlightbackground=BORDER)
        self.status_label = tk.Label(
            self.status_frame, text="", bg=GUTTER_BG, fg=MUTED,
            anchor="e", padx=10, pady=4, font=(self.ui_font_family, 9),
        )
        self.status_label.pack(fill=tk.X)
        self.status_frame.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_main_area(self) -> None:
        self.main_pane = tk.PanedWindow(
            self.root, orient=tk.VERTICAL, bg=BORDER, sashwidth=5,
            bd=0, relief=tk.FLAT, showhandle=False,
        )
        self.main_pane.pack(fill=tk.BOTH, expand=True)

        self.workspace_frame = tk.Frame(self.main_pane, bg=APP_BG)
        self.main_pane.add(self.workspace_frame, stretch="always", minsize=240)

        self.editor_pane = tk.PanedWindow(
            self.workspace_frame, orient=tk.HORIZONTAL, bg=BORDER,
            sashwidth=5, bd=0, relief=tk.FLAT,
        )
        self.editor_pane.pack(fill=tk.BOTH, expand=True)

        self.document_panel = tk.Frame(self.editor_pane, bg=PANEL_BG, width=180, highlightthickness=1, highlightbackground=BORDER)
        self.document_panel.pack_propagate(False)
        tk.Label(
            self.document_panel, text="DOCUMENTS", bg=PANEL_BG, fg=MUTED,
            anchor="w", padx=9, pady=7, font=(self.ui_font_family, 9, "bold"),
        ).pack(fill=tk.X)
        self.document_list = tk.Listbox(
            self.document_panel, bg="#081a36", fg=EDITOR_FG, selectbackground=SELECT_BG,
            selectforeground=SELECT_FG, activestyle="none", relief=tk.FLAT, bd=0,
            highlightthickness=0, font=(self.ui_font_family, 10), exportselection=False,
        )
        self.document_list.pack(fill=tk.BOTH, expand=True, padx=1, pady=(0, 1))
        self.document_list.bind("<<ListboxSelect>>", self._select_from_document_list)
        self.editor_pane.add(self.document_panel, minsize=125, width=180)

        self.notebook = ttk.Notebook(self.editor_pane, style="Cat.TNotebook", takefocus=False)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.editor_pane.add(self.notebook, minsize=400, stretch="always")

        self.output_console = OutputConsole(self)

    def _bind_shortcuts(self) -> None:
        bindings = {
            "n": self.new_document,
            "o": self.open_file,
            "s": self.save_current,
            "w": self.close_current_tab,
            "z": self.undo,
            "y": self.redo,
            "f": self.show_find_dialog,
            "h": self.show_replace_dialog,
            "g": self.go_to_line,
            "a": self.select_all,
            "d": self.duplicate_line,
        }
        modifiers = ["Control"] + (["Command"] if platform.system() == "Darwin" else [])
        for modifier in modifiers:
            for key, command in bindings.items():
                self.root.bind(f"<{modifier}-{key}>", self._shortcut(command), add=True)
            self.root.bind(f"<{modifier}-Shift-S>", self._shortcut(self.save_current_as), add=True)
            self.root.bind(f"<{modifier}-Shift-K>", self._shortcut(self.delete_line), add=True)
            for key in ("plus", "equal", "KP_Add"):
                self.root.bind(f"<{modifier}-{key}>", self._shortcut(self.zoom_in), add=True)
            for key in ("minus", "KP_Subtract"):
                self.root.bind(f"<{modifier}-{key}>", self._shortcut(self.zoom_out), add=True)
            self.root.bind(f"<{modifier}-0>", self._shortcut(self.reset_zoom), add=True)
            self.root.bind(f"<{modifier}-x>", self._shortcut(lambda: self.edit_event("<<Cut>>")), add=True)
            self.root.bind(f"<{modifier}-c>", self._shortcut(lambda: self.edit_event("<<Copy>>")), add=True)
            self.root.bind(f"<{modifier}-v>", self._shortcut(lambda: self.edit_event("<<Paste>>")), add=True)
        if platform.system() == "Darwin":
            self.root.bind("<Command-Shift-Z>", self._shortcut(self.redo), add=True)
        self.root.bind("<F3>", self._shortcut(lambda: self.find_again(True)), add=True)
        self.root.bind("<Shift-F3>", self._shortcut(lambda: self.find_again(False)), add=True)
        self.root.bind("<F5>", self._shortcut(self.run_python_file), add=True)
        self.root.bind("<F11>", self._shortcut(self.toggle_fullscreen_from_key), add=True)

    @staticmethod
    def _shortcut(command):
        def handler(_event=None):
            command()
            return "break"
        return handler

    def current_document(self) -> EditorDocument | None:
        selected = self.notebook.select() if hasattr(self, "notebook") else ""
        if not selected:
            return None
        for document in self.documents:
            if str(document.frame) == selected:
                return document
        return None

    def new_document(self) -> EditorDocument:
        self.new_counter += 1
        document = EditorDocument(self, f"new {self.new_counter}")
        self.documents.append(document)
        self.notebook.add(document.frame, text=document.display_name)
        self.notebook.select(document.frame)
        document.set_line_numbers_visible(self.line_numbers_var.get())
        document.set_word_wrap(self.word_wrap_var.get())
        self.update_document_labels()
        self.root.after_idle(document.text.focus_set)
        return document

    def _on_tab_changed(self, _event=None) -> None:
        document = self.current_document()
        if document is None:
            return
        self.language_var.set(document.language)
        self.update_document_labels()
        document.update_current_line()
        document.redraw_line_numbers()
        document.schedule_highlight(20)
        self.update_status()
        self.root.after_idle(document.text.focus_set)

    def update_document_labels(self) -> None:
        for index, document in enumerate(self.documents):
            try:
                self.notebook.tab(document.frame, text=document.display_name)
            except tk.TclError:
                continue
        selected = self.current_document()
        self.document_list.delete(0, tk.END)
        for document in self.documents:
            self.document_list.insert(tk.END, document.display_name)
        if selected in self.documents:
            index = self.documents.index(selected)
            self.document_list.selection_clear(0, tk.END)
            self.document_list.selection_set(index)
            self.document_list.see(index)

    def _select_from_document_list(self, _event=None) -> None:
        selection = self.document_list.curselection()
        if selection and selection[0] < len(self.documents):
            self.notebook.select(self.documents[selection[0]].frame)

    def open_file(self) -> bool:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Open - CatPad++",
            filetypes=file_types_for_dialog(),
        )
        if not path:
            return False
        normalized = os.path.normcase(os.path.abspath(path))
        for document in self.documents:
            if document.path and os.path.normcase(os.path.abspath(document.path)) == normalized:
                self.notebook.select(document.frame)
                return True
        try:
            try:
                content = Path(path).read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
                messagebox.showwarning(
                    "Encoding warning",
                    "This file was not valid UTF-8. Invalid bytes were replaced so it could be opened.",
                    parent=self.root,
                )
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("Open failed", f"CatPad++ could not open this file:\n\n{exc}", parent=self.root)
            return False

        reusable = None
        if len(self.documents) == 1:
            candidate = self.documents[0]
            if not candidate.modified and not candidate.path and not candidate.get_content():
                reusable = candidate
        document = reusable or self.new_document()
        document.path = path
        document.name = Path(path).name
        document.language = language_for_path(path)
        document.set_content(content)
        self.notebook.select(document.frame)
        if self.current_document() is document:
            self.language_var.set(document.language)
        self.update_document_labels()
        self.update_status()
        return True

    def save_current(self) -> bool:
        document = self.current_document()
        return self.save_document(document) if document else False

    def save_current_as(self) -> bool:
        document = self.current_document()
        return self.save_document_as(document) if document else False

    def save_document(self, document: EditorDocument | None) -> bool:
        if document is None:
            return False
        if not document.path:
            return self.save_document_as(document)
        try:
            Path(document.path).write_text(document.get_content(), encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("Save failed", f"CatPad++ could not save this file:\n\n{exc}", parent=self.root)
            return False
        document.encoding = "UTF-8"
        document.text.edit_modified(False)
        document.set_modified(False)
        self.update_status()
        return True

    def save_document_as(self, document: EditorDocument | None) -> bool:
        if document is None:
            return False
        extension = default_extension_for_language(document.language)
        if document.path:
            initial_name = document.name
        else:
            stem = Path(document.name).stem or document.name
            initial_name = f"{stem}{extension}"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save As - CatPad++",
            initialfile=initial_name,
            defaultextension=extension,
            filetypes=file_types_for_dialog(document.language),
        )
        if not path:
            return False
        # If the dialog returned a bare name, keep the chosen syntax extension.
        if not Path(path).suffix:
            path = f"{path}{extension}"
        old_path, old_name, old_language = document.path, document.name, document.language
        document.path = path
        document.name = Path(path).name
        detected = language_for_path(path)
        if detected != "Plain Text" or old_language == "Plain Text":
            document.language = detected
        if not self.save_document(document):
            document.path, document.name, document.language = old_path, old_name, old_language
            self.update_document_labels()
            return False
        if self.current_document() is document:
            self.language_var.set(document.language)
        document.schedule_highlight(20)
        self.update_document_labels()
        return True

    def confirm_close(self, document: EditorDocument) -> bool:
        if not document.modified:
            return True
        answer = messagebox.askyesnocancel(
            "Save changes?",
            f"Save changes to {document.name}?",
            parent=self.root,
            default=messagebox.YES,
        )
        if answer is None:
            return False
        if answer:
            return self.save_document(document)
        return True

    def close_document(self, document: EditorDocument, ensure_one: bool = True) -> bool:
        if not self.confirm_close(document):
            return False
        if document._highlight_job is not None:
            try:
                document.frame.after_cancel(document._highlight_job)
            except tk.TclError:
                pass
        try:
            self.notebook.forget(document.frame)
            document.frame.destroy()
        except tk.TclError:
            pass
        if document in self.documents:
            self.documents.remove(document)
        if ensure_one and not self.documents:
            self.new_document()
        self.update_document_labels()
        self.update_status()
        return True

    def close_current_tab(self) -> bool:
        document = self.current_document()
        return self.close_document(document) if document else True

    def close_all_documents(self, for_exit: bool = False) -> bool:
        for document in list(self.documents):
            if not self.confirm_close(document):
                return False
        for document in list(self.documents):
            if document._highlight_job is not None:
                try:
                    document.frame.after_cancel(document._highlight_job)
                except tk.TclError:
                    pass
            try:
                self.notebook.forget(document.frame)
                document.frame.destroy()
            except tk.TclError:
                pass
        self.documents.clear()
        if not for_exit:
            self.new_document()
        self.update_document_labels()
        return True

    def exit_app(self) -> None:
        if not self.close_all_documents(for_exit=True):
            return
        self.stop_process(silent=True)
        self.root.destroy()

    def edit_event(self, virtual_event: str) -> None:
        document = self.current_document()
        if document:
            document.text.event_generate(virtual_event)
            document.text.focus_set()

    def undo(self) -> None:
        document = self.current_document()
        if document:
            try:
                document.text.edit_undo()
            except tk.TclError:
                pass

    def redo(self) -> None:
        document = self.current_document()
        if document:
            try:
                document.text.edit_redo()
            except tk.TclError:
                pass

    def delete_selection(self) -> None:
        document = self.current_document()
        if not document:
            return
        try:
            document.text.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            document.text.delete(tk.INSERT, f"{tk.INSERT}+1c")

    def select_all(self) -> None:
        document = self.current_document()
        if document:
            document.text.tag_add(tk.SEL, "1.0", "end-1c")
            document.text.mark_set(tk.INSERT, "1.0")
            document.text.see("1.0")

    def duplicate_line(self) -> None:
        document = self.current_document()
        if not document:
            return
        text = document.text
        start = text.index("insert linestart")
        end = text.index("insert lineend")
        content = text.get(start, end)
        text.insert(end, "\n" + content)
        new_line = text.index(f"{start}+1line")
        text.mark_set(tk.INSERT, new_line)
        text.see(new_line)

    def delete_line(self) -> None:
        document = self.current_document()
        if not document:
            return
        text = document.text
        start = text.index("insert linestart")
        end = text.index(f"{start}+1line")
        if text.compare(start, "==", "1.0") and text.compare(end, ">=", "end"):
            text.delete("1.0", "end-1c")
        else:
            text.delete(start, end)

    def show_find_dialog(self) -> None:
        if self.find_dialog and self.find_dialog.winfo_exists():
            self.find_dialog.deiconify()
            self.find_dialog.lift()
            self.find_dialog.entry.focus_set()
            return
        self.find_dialog = FindDialog(self)

    def show_replace_dialog(self) -> None:
        if self.replace_dialog and self.replace_dialog.winfo_exists():
            self.replace_dialog.deiconify()
            self.replace_dialog.lift()
            self.replace_dialog.find_entry.focus_set()
            return
        self.replace_dialog = ReplaceDialog(self)

    def find_again(self, forward: bool) -> None:
        if not self.last_search:
            self.show_find_dialog()
            return
        self.find_text(self.last_search, forward, self.last_match_case)

    def find_text(self, query: str, forward: bool = True, match_case: bool = False) -> bool:
        document = self.current_document()
        if not document or not query:
            if not query:
                self.root.bell()
            return False
        self.last_search, self.last_match_case = query, match_case
        text = document.text
        text.tag_remove("search_match", "1.0", tk.END)
        count = tk.IntVar(master=self.root)
        if forward:
            start = text.index("insert")
            found = text.search(query, start, stopindex="end-1c", nocase=not match_case, count=count)
            if not found:
                found = text.search(query, "1.0", stopindex=start, nocase=not match_case, count=count)
        else:
            start = text.index("insert")
            found = text.search(query, start, stopindex="1.0", backwards=True, nocase=not match_case, count=count)
            if not found:
                found = text.search(query, "end-1c", stopindex=start, backwards=True, nocase=not match_case, count=count)
        if not found:
            self.root.bell()
            return False
        length = max(1, count.get())
        end = text.index(f"{found}+{length}c")
        text.tag_add("search_match", found, end)
        text.tag_raise("search_match")
        text.mark_set(tk.INSERT, end if forward else found)
        text.see(found)
        text.focus_set()
        self.update_status()
        return True

    def replace_current_match(self, query: str, replacement: str, match_case: bool) -> bool:
        document = self.current_document()
        if not document or not query:
            return False
        ranges = document.text.tag_ranges("search_match")
        if len(ranges) != 2:
            if not self.find_text(query, True, match_case):
                return False
            ranges = document.text.tag_ranges("search_match")
            if len(ranges) != 2:
                return False
        start, end = str(ranges[0]), str(ranges[1])
        existing = document.text.get(start, end)
        matches = existing == query if match_case else existing.casefold() == query.casefold()
        if not matches:
            self.find_text(query, True, match_case)
            return False
        document.text.replace(start, end, replacement)
        document.text.mark_set(tk.INSERT, f"{start}+{len(replacement)}c")
        document.text.tag_remove("search_match", "1.0", tk.END)
        self.find_text(query, True, match_case)
        return True

    def replace_all(self, query: str, replacement: str, match_case: bool) -> int:
        document = self.current_document()
        if not document or not query:
            return 0
        text = document.text
        text.tag_remove("search_match", "1.0", tk.END)
        position = "1.0"
        count_var = tk.IntVar(master=self.root)
        replacements = 0
        text.edit_separator()
        while True:
            found = text.search(query, position, stopindex="end-1c", nocase=not match_case, count=count_var)
            if not found:
                break
            length = count_var.get()
            if length <= 0:
                break
            end = text.index(f"{found}+{length}c")
            text.replace(found, end, replacement)
            position = text.index(f"{found}+{len(replacement)}c")
            replacements += 1
        text.edit_separator()
        document.schedule_highlight()
        return replacements

    def go_to_line(self) -> None:
        document = self.current_document()
        if not document:
            return
        total = int(document.text.index("end-1c").split(".")[0])
        line = simpledialog.askinteger(
            "Go To Line", f"Line number (1-{total}):", parent=self.root,
            minvalue=1, maxvalue=max(1, total),
        )
        if line is not None:
            target = f"{line}.0"
            document.text.mark_set(tk.INSERT, target)
            document.text.see(target)
            document.text.focus_set()
            document.update_current_line()
            self.update_status()

    def toggle_line_numbers(self) -> None:
        visible = self.line_numbers_var.get()
        for document in self.documents:
            document.set_line_numbers_visible(visible)

    def toggle_word_wrap(self) -> None:
        enabled = self.word_wrap_var.get()
        for document in self.documents:
            document.set_word_wrap(enabled)

    def set_tab_width_dialog(self) -> None:
        width = simpledialog.askinteger(
            "Tab Width",
            "Spaces inserted by Tab (1-12):",
            parent=self.root,
            initialvalue=self.tab_width,
            minvalue=1,
            maxvalue=12,
        )
        if width is None:
            return
        self.tab_width = width
        self.tab_pixels = self.editor_font.measure(" " * self.tab_width)
        for document in self.documents:
            document.refresh_font_metrics()

    def toggle_document_panel(self) -> None:
        visible = self.document_panel_var.get()
        panes = [str(pane) for pane in self.editor_pane.panes()]
        if visible and str(self.document_panel) not in panes:
            self.editor_pane.add(self.document_panel, before=self.notebook, minsize=125, width=180)
        elif not visible and str(self.document_panel) in panes:
            self.editor_pane.forget(self.document_panel)

    def toggle_status_bar(self) -> None:
        if self.status_bar_var.get():
            self.status_frame.pack(side=tk.BOTTOM, fill=tk.X, before=self.main_pane)
        else:
            self.status_frame.pack_forget()

    def toggle_fullscreen(self) -> None:
        self.root.attributes("-fullscreen", self.fullscreen_var.get())

    def toggle_fullscreen_from_key(self) -> None:
        self.fullscreen_var.set(not self.fullscreen_var.get())
        self.toggle_fullscreen()

    def zoom_in(self) -> None:
        self.set_zoom(min(250, self.zoom_level + 10))

    def zoom_out(self) -> None:
        self.set_zoom(max(50, self.zoom_level - 10))

    def reset_zoom(self) -> None:
        self.set_zoom(100)

    def set_zoom(self, zoom: int) -> None:
        self.zoom_level = zoom
        self.font_size = max(6, round(self.base_font_size * zoom / 100))
        self.editor_font.configure(size=self.font_size)
        self.italic_font.configure(size=self.font_size)
        self.gutter_font.configure(size=max(7, self.font_size - 1))
        self.tab_pixels = self.editor_font.measure(" " * self.tab_width)
        for document in self.documents:
            document.refresh_font_metrics()
        self.update_status()

    def set_language(self, language: str) -> None:
        document = self.current_document()
        if not document:
            return
        document.language = language
        self.language_var.set(language)
        document.schedule_highlight(20)
        self.update_status()

    def update_status(self) -> None:
        document = self.current_document()
        if not document:
            self.status_label.configure(text="No document")
            return
        try:
            line, column = map(int, document.text.index(tk.INSERT).split("."))
            lines = int(document.text.index("end-1c").split(".")[0])
        except (ValueError, tk.TclError):
            line, column, lines = 1, 0, 1
        self.status_label.configure(
            text=f"Ln {line}, Col {column + 1}  |  Lines: {lines}  |  {document.encoding}  |  {document.language}  |  {self.zoom_level}%"
        )

    def toggle_output(self) -> None:
        if self.output_panel_var.get():
            self.show_output()
        else:
            self.hide_output()

    def show_output(self) -> None:
        panes = [str(pane) for pane in self.main_pane.panes()]
        if str(self.output_console.frame) not in panes:
            self.main_pane.add(self.output_console.frame, minsize=100, height=190, stretch="never")
        self.output_visible = True
        self.output_panel_var.set(True)

    def hide_output(self) -> None:
        panes = [str(pane) for pane in self.main_pane.panes()]
        if str(self.output_console.frame) in panes:
            self.main_pane.forget(self.output_console.frame)
        self.output_visible = False
        self.output_panel_var.set(False)

    def clear_output(self) -> None:
        self.output_console.clear()
        self.show_output()

    def run_python_file(self) -> None:
        document = self.current_document()
        if not document:
            return
        if not document.path or document.modified:
            if not self.save_document(document):
                return
        if Path(document.path).suffix.lower() != ".py":
            messagebox.showinfo("Run Python", "Save the current document with a .py extension before running it.", parent=self.root)
            return
        self._start_process(
            [sys.executable, "-u", document.path],
            cwd=str(Path(document.path).resolve().parent),
            label=f"Running {Path(document.path).name}",
        )

    def open_terminal_command(self) -> None:
        command = simpledialog.askstring(
            "Terminal Command",
            "Enter a command and its arguments:\n(commands run without a shell)",
            parent=self.root,
        )
        if not command or not command.strip():
            return
        if os.name == "nt":
            args: str | list[str] = command
        else:
            try:
                args = shlex.split(command)
            except ValueError as exc:
                messagebox.showerror("Invalid command", str(exc), parent=self.root)
                return
        if not args:
            return
        document = self.current_document()
        cwd = str(Path(document.path).resolve().parent) if document and document.path else os.getcwd()
        self._start_process(args, cwd=cwd, label=f"Command: {command}")

    def _start_process(self, args: str | list[str], cwd: str, label: str) -> None:
        with self.process_lock:
            if self.process_busy or (self.current_process and self.current_process.poll() is None):
                messagebox.showinfo("Process running", "Stop the current process before starting another one.", parent=self.root)
                return
            self.process_busy = True
            self.stop_requested = False
        self.show_output()
        self.output_console.append(f"> {label}...\n\n")
        self._ensure_output_polling()

        def worker() -> None:
            try:
                startupinfo = None
                creationflags = 0
                if os.name == "nt":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                process = subprocess.Popen(
                    args,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    shell=False,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
                with self.process_lock:
                    self.current_process = process
                    stop_requested = self.stop_requested
                if stop_requested:
                    process.terminate()
                assert process.stdout is not None
                for line in iter(process.stdout.readline, ""):
                    self._append_output_threadsafe(line)
                process.stdout.close()
                code = process.wait()
                self._append_output_threadsafe(f"\nProcess finished with exit code {code}\n")
            except (OSError, ValueError) as exc:
                self._append_output_threadsafe(f"CatPad++ could not start the process:\n{exc}\n")
            finally:
                with self.process_lock:
                    self.current_process = None
                    self.process_busy = False

        threading.Thread(target=worker, name="CatPadRunner", daemon=True).start()

    def _append_output_threadsafe(self, content: str) -> None:
        self.output_queue.put(content)

    def _ensure_output_polling(self) -> None:
        if self.output_poll_job is None:
            self.output_poll_job = self.root.after(50, self._poll_output_queue)

    def _poll_output_queue(self) -> None:
        self.output_poll_job = None
        while True:
            try:
                content = self.output_queue.get_nowait()
            except queue.Empty:
                break
            self.output_console.append(content)
        with self.process_lock:
            busy = self.process_busy
        if busy or not self.output_queue.empty():
            self.output_poll_job = self.root.after(75, self._poll_output_queue)

    def stop_process(self, silent: bool = False) -> None:
        with self.process_lock:
            process = self.current_process
            busy = self.process_busy
        if not process or process.poll() is not None:
            if busy:
                with self.process_lock:
                    self.stop_requested = True
            if not silent:
                message = "The process is still starting. Try Stop again.\n" if busy else "No process is currently running.\n"
                self.output_console.append(message)
            return
        try:
            process.terminate()
            if not silent:
                self.output_console.append("\n> Stop requested.\n")
        except OSError as exc:
            if not silent:
                self.output_console.append(f"\nCould not stop process: {exc}\n")

    def show_about(self) -> None:
        messagebox.showinfo(
            "About CatPad++",
            "CatPad++ 1.x\n\n"
            "A lightweight blue-hue programmer's text editor\n"
            "written for Python 3.14.\n\n"
            "Inspired by classic tabbed source-code editors.\n\n"
            "CatSDK",
            parent=self.root,
        )

    def show_shortcuts(self) -> None:
        messagebox.showinfo(
            "CatPad++ Keyboard Shortcuts",
            "Ctrl/Command+N    New\n"
            "Ctrl/Command+O    Open\n"
            "Ctrl/Command+S    Save\n"
            "Ctrl/Command+Shift+S    Save As\n"
            "Ctrl/Command+W    Close Tab\n\n"
            "Ctrl/Command+Z    Undo\n"
            "Ctrl/Command+Y    Redo\n"
            "Ctrl/Command+D    Duplicate Line\n"
            "Ctrl/Command+Shift+K    Delete Line\n\n"
            "Ctrl/Command+F    Find\n"
            "Ctrl/Command+H    Replace\n"
            "Ctrl/Command+G    Go To Line\n"
            "F3 / Shift+F3    Find Next / Previous\n\n"
            "Ctrl/Command++    Zoom In\n"
            "Ctrl/Command+-    Zoom Out\n"
            "Ctrl/Command+0    Reset Zoom\n"
            "F5    Run Python\n"
            "F11   Fullscreen",
            parent=self.root,
        )


def main() -> None:
    root = tk.Tk()
    CatPadApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
