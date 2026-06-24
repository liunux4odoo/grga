import html
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, font as tkfont, messagebox, ttk

from grga.search import build_rga_command_preview, check_environment, search_directory

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "grga", "settings.json")
DEFAULT_DPI = 96
DEFAULT_TK_SCALING = DEFAULT_DPI / 72
MIN_DISPLAY_SCALE = 1.0
MAX_DISPLAY_SCALE = 2.5
DEFAULT_GITIGNORE_PATTERNS = [
    "__pycache__/",
    "node_modules/",
    ".git/",
    ".venv/",
    "venv/",
    "dist/",
    "build/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".DS_Store",
    "Thumbs.db",
    "~$*",
    ".~lock.*#",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*~",
]
DEFAULT_GITIGNORE_CONTENT = "\n".join(DEFAULT_GITIGNORE_PATTERNS) + "\n"
MATCH_COLORS = ["#fff59d", "#90caf9", "#a5d6a7", "#ffcc80", "#ce93d8", "#80cbc4", "#ef9a9a", "#b0bec5"]


class GrgaSearchApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GRGA Search")
        self.matches_by_file = {}
        self.match_tag_by_text = {}
        self.search_thread = None
        self.search_after_id = None
        self.search_pending = False
        self.environment_ok = True
        self.last_gitignore_directory = None
        self.last_search_started_at = None
        self.last_search_finished_at = None
        self.last_search_command = ""
        self.last_search_result = None

        settings = self._load_settings()
        self.display_scale = self._configure_display_scale(settings)
        self.geometry(f"{self._scale_size(1100)}x{self._scale_size(700)}")

        self.directory_var = tk.StringVar(value=settings.get("directory") or os.getcwd())
        self.mode_var = tk.StringVar(value=settings.get("mode", "auto"))
        self.case_var = tk.BooleanVar(value=settings.get("case_sensitive", False))
        self.max_count_var = tk.StringVar(value=settings.get("max_count", ""))
        self.timeout_var = tk.StringVar(value=settings.get("timeout", "600"))
        self.auto_delay_var = tk.StringVar(value=settings.get("auto_delay", "3"))
        self.before_context_var = tk.StringVar(value=settings.get("before_context", "5"))
        self.after_context_var = tk.StringVar(value=settings.get("after_context", "5"))
        self.multiline_var = tk.BooleanVar(value=settings.get("multiline", False))
        self.path_only_var = tk.BooleanVar(value=settings.get("path_only", False))
        self.file_globs_var = tk.StringVar(value=settings.get("file_globs", ""))
        self.keyword_var = tk.StringVar(value=settings.get("keyword", ""))
        self.status_var = tk.StringVar(value="Ready")

        self.suspend_search_trace = False

        self._build_layout()
        self._bind_search_traces()
        self._update_command_preview()
        self.keyword_entry.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._ensure_current_directory_gitignore)
        self.after(100, self._check_environment_on_startup)

    def _load_settings(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _configure_display_scale(self, settings):
        scale = self._read_display_scale_setting(settings)
        if scale is None:
            scale = self._detect_display_scale()
        scale = max(MIN_DISPLAY_SCALE, min(MAX_DISPLAY_SCALE, scale))
        self.tk.call("tk", "scaling", DEFAULT_TK_SCALING * scale)
        self._scale_named_fonts(scale)
        return scale

    def _scale_named_fonts(self, scale):
        if scale <= 1.0:
            return
        for name in tkfont.names(self):
            named = tkfont.nametofont(name)
            size = named.cget("size")
            if size >= 0:
                continue
            named.configure(size=-int(round(abs(size) * scale)))

    def _read_display_scale_setting(self, settings):
        for value in (os.environ.get("GRGA_DISPLAY_SCALE"), settings.get("display_scale")):
            try:
                scale = float(value)
            except (TypeError, ValueError):
                continue
            if scale > 0:
                self.display_scale_setting = settings.get("display_scale")
                return scale
        self.display_scale_setting = settings.get("display_scale")
        return None

    def _detect_display_scale(self):
        try:
            dpi = self.winfo_fpixels("1i")
        except tk.TclError:
            return 1.0
        return dpi / DEFAULT_DPI

    def _scale_size(self, value):
        return max(1, int(round(value * self.display_scale)))

    def _check_environment_on_startup(self):
        result = check_environment()
        self.environment_ok = result.get("ok", False)
        errors = result.get("errors", [])
        warnings = result.get("warnings", [])
        if errors:
            self.status_var.set("缺少必需命令：rg/rga")
            messagebox.showerror("命令检查失败", "\n".join(errors))
        if warnings:
            messagebox.showwarning("功能受限", "\n".join(warnings))

    def _ensure_current_directory_gitignore(self):
        self._ensure_gitignore_file(self.directory_var.get())

    def _ensure_gitignore_file(self, directory, force=False):
        path = os.path.abspath(os.path.expanduser(directory.strip() or os.getcwd()))
        gitignore_path = os.path.join(path, ".gitignore")
        if not force and path == self.last_gitignore_directory:
            return gitignore_path if os.path.exists(gitignore_path) else None
        self.last_gitignore_directory = path
        if not os.path.isdir(path):
            return None
        if os.path.exists(gitignore_path):
            return gitignore_path
        try:
            with open(gitignore_path, "w", encoding="utf-8") as handle:
                handle.write(DEFAULT_GITIGNORE_CONTENT)
        except OSError as exc:
            self.status_var.set(f"无法生成 .gitignore：{exc}")
            return None
        self.status_var.set("已生成默认 .gitignore")
        return gitignore_path

    def _save_settings(self):
        data = {
            "directory": self.directory_var.get(),
            "mode": self.mode_var.get(),
            "case_sensitive": self.case_var.get(),
            "max_count": self.max_count_var.get(),
            "timeout": self.timeout_var.get(),
            "auto_delay": self.auto_delay_var.get(),
            "before_context": self.before_context_var.get(),
            "after_context": self.after_context_var.get(),
            "multiline": self.multiline_var.get(),
            "path_only": self.path_only_var.get(),
            "file_globs": self.file_globs_var.get(),
            "keyword": self.keyword_var.get(),
        }
        if self.display_scale_setting not in (None, ""):
            data["display_scale"] = self.display_scale_setting
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _on_close(self):
        self._save_settings()
        self.destroy()

    def _build_layout(self):
        root = ttk.Frame(self, padding=8)
        root.pack(fill=tk.BOTH, expand=True)

        params = ttk.Frame(root)
        params.pack(fill=tk.X)

        self.input_widgets = []

        ttk.Label(params, text="目录").pack(side=tk.LEFT)
        directory_entry = ttk.Entry(params, textvariable=self.directory_var, width=45)
        directory_entry.pack(side=tk.LEFT, padx=(4, 6), fill=tk.X, expand=True)
        browse_button = ttk.Button(params, text="浏览", command=self._browse_directory)
        browse_button.pack(side=tk.LEFT, padx=(0, 6))

        ttk.Label(params, text="排除规则由目录内 .gitignore 控制").pack(side=tk.LEFT, padx=(0, 4))
        gitignore_button = ttk.Button(params, text="编辑 .gitignore", command=self._edit_gitignore)
        gitignore_button.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(params, text="模式").pack(side=tk.LEFT)
        mode_combo = ttk.Combobox(params, textvariable=self.mode_var, values=("auto", "regex", "glob"), width=8, state="readonly")
        mode_combo.pack(side=tk.LEFT, padx=(4, 10))

        case_check = ttk.Checkbutton(params, text="区分大小写", variable=self.case_var)
        case_check.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(params, text="最大匹配").pack(side=tk.LEFT)
        max_count_entry = ttk.Entry(params, textvariable=self.max_count_var, width=8)
        max_count_entry.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(params, text="超时").pack(side=tk.LEFT)
        timeout_entry = ttk.Entry(params, textvariable=self.timeout_var, width=6)
        timeout_entry.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(params, text="自动延迟").pack(side=tk.LEFT)
        auto_delay_entry = ttk.Entry(params, textvariable=self.auto_delay_var, width=5)
        auto_delay_entry.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(params, text="前行数").pack(side=tk.LEFT)
        before_entry = ttk.Entry(params, textvariable=self.before_context_var, width=5)
        before_entry.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(params, text="后行数").pack(side=tk.LEFT)
        after_entry = ttk.Entry(params, textvariable=self.after_context_var, width=5)
        after_entry.pack(side=tk.LEFT, padx=(4, 10))

        multiline_check = ttk.Checkbutton(params, text="跨行匹配", variable=self.multiline_var)
        multiline_check.pack(side=tk.LEFT)

        path_only_check = ttk.Checkbutton(params, text="仅匹配路径", variable=self.path_only_var)
        path_only_check.pack(side=tk.LEFT, padx=(10, 0))

        file_globs_bar = ttk.Frame(root)
        file_globs_bar.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(file_globs_bar, text="文件范围").pack(side=tk.LEFT)
        file_globs_entry = ttk.Entry(file_globs_bar, textvariable=self.file_globs_var)
        file_globs_entry.pack(side=tk.LEFT, padx=(4, 6), fill=tk.X, expand=True)
        ttk.Label(file_globs_bar, text="留空=所有文件，多个 --iglob 模式用空格分隔，! 表示排除").pack(side=tk.LEFT)

        search_bar = ttk.Frame(root)
        search_bar.pack(fill=tk.X, pady=(8, 8))

        ttk.Label(search_bar, text="关键字").pack(side=tk.LEFT)
        self.keyword_entry = ttk.Entry(search_bar, textvariable=self.keyword_var)
        self.keyword_entry.pack(side=tk.LEFT, padx=(4, 6), fill=tk.X, expand=True)
        self.keyword_entry.bind("<Return>", lambda event: self._start_search())
        clear_button = ttk.Button(search_bar, text="排除", command=self._clear_search)
        clear_button.pack(side=tk.LEFT)
        copy_button = ttk.Button(search_bar, text="复制结果", command=self._copy_results_to_clipboard)
        copy_button.pack(side=tk.LEFT, padx=(6, 0))

        command_bar = ttk.Frame(root)
        command_bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(command_bar, text="命令").pack(side=tk.LEFT, anchor=tk.N)
        self.command_text = tk.Text(command_bar, height=2, wrap=tk.WORD, state=tk.DISABLED)
        self.command_text.pack(side=tk.LEFT, padx=(4, 0), fill=tk.X, expand=True)

        self.input_widgets = [
            (directory_entry, "normal"),
            (browse_button, "normal"),
            (gitignore_button, "normal"),
            (mode_combo, "readonly"),
            (case_check, "normal"),
            (max_count_entry, "normal"),
            (timeout_entry, "normal"),
            (auto_delay_entry, "normal"),
            (before_entry, "normal"),
            (after_entry, "normal"),
            (multiline_check, "normal"),
            (path_only_check, "normal"),
            (file_globs_entry, "normal"),
            (self.keyword_entry, "normal"),
            (clear_button, "normal"),
            (copy_button, "normal"),
        ]

        pane = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=3)

        ttk.Label(left, text="命中文件").pack(anchor=tk.W)
        self.file_list = tk.Listbox(left, exportselection=False)
        self.file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(4, 0))
        self.file_list.bind("<<ListboxSelect>>", self._show_selected_file)
        self.file_list.bind("<Double-Button-1>", self._open_selected_file)
        file_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.file_list.yview)
        file_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(4, 0))
        self.file_list.configure(yscrollcommand=file_scroll.set)

        ttk.Label(right, text="匹配内容").pack(anchor=tk.W)
        content_frame = ttk.Frame(right)
        content_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.content = tk.Text(
            content_frame,
            wrap=tk.NONE,
            state=tk.DISABLED,
            spacing1=self._scale_size(3),
            spacing3=self._scale_size(3),
        )
        self.match_color_tags = []
        for index, color in enumerate(MATCH_COLORS):
            tag = f"match{index}"
            self.content.tag_configure(tag, background=color, foreground="#000000")
            self.match_color_tags.append(tag)
        self.content.tag_configure("separator", foreground="#9e9e9e")
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        content_y = ttk.Scrollbar(content_frame, orient=tk.VERTICAL, command=self.content.yview)
        content_y.pack(side=tk.RIGHT, fill=tk.Y)
        content_x = ttk.Scrollbar(right, orient=tk.HORIZONTAL, command=self.content.xview)
        content_x.pack(fill=tk.X)
        self.content.configure(yscrollcommand=content_y.set, xscrollcommand=content_x.set)

        ttk.Label(root, textvariable=self.status_var).pack(fill=tk.X, pady=(6, 0))

    def _bind_search_traces(self):
        self.directory_var.trace_add("write", lambda *args: self._on_directory_changed())
        for var in (
            self.mode_var,
            self.case_var,
            self.max_count_var,
            self.timeout_var,
            self.auto_delay_var,
            self.before_context_var,
            self.after_context_var,
            self.multiline_var,
            self.path_only_var,
            self.file_globs_var,
            self.keyword_var,
        ):
            var.trace_add("write", lambda *args: self._schedule_search())

    def _browse_directory(self):
        directory = filedialog.askdirectory(initialdir=self.directory_var.get() or os.getcwd())
        if directory:
            self.directory_var.set(directory)

    def _on_directory_changed(self):
        self._ensure_current_directory_gitignore()
        self._schedule_search()

    def _edit_gitignore(self):
        gitignore_path = self._ensure_gitignore_file(self.directory_var.get(), force=True)
        if not gitignore_path:
            messagebox.showwarning("无法编辑", "当前目录无效或无法创建 .gitignore")
            return
        dialog = tk.Toplevel(self)
        dialog.title("编辑 .gitignore")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("560x420")

        frame = ttk.Frame(dialog, padding=8)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=gitignore_path).pack(anchor=tk.W)
        text = tk.Text(frame, height=16, wrap=tk.NONE)
        text.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        try:
            with open(gitignore_path, "r", encoding="utf-8") as handle:
                text.insert("1.0", handle.read())
        except OSError as exc:
            dialog.destroy()
            messagebox.showerror("读取失败", str(exc))
            return

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)

        def save():
            try:
                with open(gitignore_path, "w", encoding="utf-8") as handle:
                    handle.write(text.get("1.0", tk.END).rstrip() + "\n")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc))
                return
            dialog.destroy()
            self._schedule_search()

        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="保存", command=save).pack(side=tk.RIGHT, padx=(0, 6))
        text.focus_set()
        dialog.wait_window()

    def _clear_search(self):
        self.suspend_search_trace = True
        self.keyword_var.set("")
        self.suspend_search_trace = False
        self.last_search_started_at = None
        self.last_search_finished_at = None
        self.last_search_command = ""
        self.last_search_result = None
        self._set_command_preview("")
        self._clear_results()
        self.status_var.set("Ready")

    def _cancel_scheduled_search(self):
        if not self.search_after_id:
            return
        try:
            self.after_cancel(self.search_after_id)
        except tk.TclError:
            pass
        self.search_after_id = None

    def _schedule_search(self):
        if self.suspend_search_trace:
            return
        self._update_command_preview()
        self._cancel_scheduled_search()
        self.search_after_id = self.after(self._auto_delay_ms(), self._start_search)

    def _update_command_preview(self):
        keywords = self._parse_keywords(self.keyword_var.get())
        if not keywords:
            self._set_command_preview("")
            return
        max_count = self._parse_int_or_none(self.max_count_var.get())
        try:
            command = build_rga_command_preview(
                keywords,
                pattern_mode=self.mode_var.get(),
                case_sensitive=self.case_var.get(),
                max_count=max_count,
                before_context=self._parse_int_or_default(self.before_context_var.get(), 5),
                after_context=self._parse_int_or_default(self.after_context_var.get(), 5),
                multiline=self.multiline_var.get(),
                glob_patterns=self.file_globs_var.get(),
                path_only=self.path_only_var.get(),
            )
        except Exception as exc:
            command = str(exc)
        self._set_command_preview(command)

    def _set_command_preview(self, text):
        self.command_text.configure(state=tk.NORMAL)
        self.command_text.delete("1.0", tk.END)
        self.command_text.insert("1.0", text)
        self.command_text.configure(state=tk.DISABLED)

    def _get_command_preview_text(self):
        return self.command_text.get("1.0", "end-1c").strip()

    def _copy_results_to_clipboard(self):
        text = self._build_results_markdown()
        if not text:
            messagebox.showinfo("复制结果", "暂无可复制的搜索结果")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.status_var.set("搜索结果已复制到剪贴板")

    def _build_results_markdown(self):
        if not self.last_search_result:
            return ""
        result = self.last_search_result
        lines = ["# 搜索基本信息"]
        if self.last_search_started_at:
            lines.append(f"- 搜索开始时间：{self.last_search_started_at.strftime('%Y-%m-%d %H:%M:%S')}")
        if self.last_search_finished_at:
            lines.append(f"- 搜索完成时间：{self.last_search_finished_at.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 搜索目录：{self.directory_var.get()}")
        lines.append(f"- 关键字：{self.keyword_var.get().strip()}")
        lines.append(f"- rga 命令：`{self.last_search_command or self._get_command_preview_text()}`")
        if result.get("ok"):
            match_count = sum(1 for item in result.get("matches", []) if not item.get("is_context"))
            file_count = len(self.matches_by_file)
            lines.append(f"- 结果统计：{match_count} matches in {file_count} files")
            if result.get("warning"):
                lines.append("- 警告：部分文件已跳过")
        else:
            lines.append("- 结果统计：搜索失败")
            lines.append(f"- 错误：{result.get('error', 'unknown error')}")
        lines.extend(["", "# 搜索结果"])
        if not self.matches_by_file:
            lines.append("无匹配结果")
            return "\n".join(lines)
        for index, (path, matches) in enumerate(sorted(self.matches_by_file.items()), start=1):
            lines.append(f"{index}. {path}")
            previous_line_number = None
            for match in matches:
                line_number = match.get("line_number")
                if isinstance(line_number, int) and isinstance(previous_line_number, int) and line_number > previous_line_number + 1:
                    lines.append("---")
                text = str(match.get("text", ""))
                prefix = f"L{line_number}: " if isinstance(line_number, int) else ""
                highlighted = self._highlight_markdown(text, match.get("submatches", []))
                bullet_text = (html.escape(prefix) + highlighted).strip().replace("\n", "\n  ")
                lines.append(f"- {bullet_text}")
                previous_line_number = line_number
        return "\n".join(lines)

    def _highlight_markdown(self, text, submatches):
        ranges = []
        for submatch in submatches or []:
            start = submatch.get("start")
            end = submatch.get("end")
            if isinstance(start, int) and isinstance(end, int) and end > start:
                ranges.append((start, end, self._submatch_color(submatch)))
        if not ranges:
            return html.escape(text)
        ranges.sort(key=lambda item: (item[0], item[1]))
        parts = []
        cursor = 0
        for start, end, color in ranges:
            start = max(start, cursor)
            if start >= end:
                continue
            parts.append(html.escape(text[cursor:start]))
            parts.append(f"<span style=\"background-color: {color}; color: #000000;\">{html.escape(text[start:end])}</span>")
            cursor = end
        parts.append(html.escape(text[cursor:]))
        return "".join(parts)

    def _submatch_color(self, submatch):
        tag = self._match_tag(submatch.get("match", ""))
        try:
            index = int(tag.replace("match", ""))
        except ValueError:
            return MATCH_COLORS[0]
        return MATCH_COLORS[index % len(MATCH_COLORS)]

    def _parse_int_or_none(self, value):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _parse_int_or_default(self, value, default):
        value = value.strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def _auto_delay_ms(self):
        try:
            delay = float(self.auto_delay_var.get().strip())
        except ValueError:
            return 3000
        if delay < 0:
            return 3000
        return int(delay * 1000)

    def _start_search(self):
        self._cancel_scheduled_search()
        if not self.environment_ok and not self.path_only_var.get():
            self.status_var.set("缺少必需命令：rg/rga")
            return
        if self.search_thread and self.search_thread.is_alive():
            self.search_pending = True
            return

        keywords = self._parse_keywords(self.keyword_var.get())
        if not keywords:
            self._clear_results()
            self.status_var.set("Ready")
            return

        max_count = self._parse_optional_int(self.max_count_var.get(), "最大匹配")
        if max_count is False:
            return

        timeout = self._parse_optional_float(self.timeout_var.get(), "超时")
        if timeout is False:
            return

        before_context = self._parse_context_int(self.before_context_var.get(), "前行数")
        if before_context is False:
            return

        after_context = self._parse_context_int(self.after_context_var.get(), "后行数")
        if after_context is False:
            return

        self.search_pending = False
        self._clear_results()
        self._update_command_preview()
        self.last_search_started_at = datetime.now()
        self.last_search_finished_at = None
        self.last_search_command = self._get_command_preview_text()
        self.last_search_result = None
        self.status_var.set("Searching...")
        args = (
            self.directory_var.get(),
            keywords,
            self.mode_var.get(),
            self.case_var.get(),
            max_count,
            timeout,
            before_context,
            after_context,
            self.multiline_var.get(),
            self.file_globs_var.get(),
            self.path_only_var.get(),
        )
        self._set_input_enabled(False)
        self.search_thread = threading.Thread(target=self._run_search, args=args)
        self.search_thread.daemon = True
        self.search_thread.start()

    def _set_input_enabled(self, enabled):
        state = None if enabled else "disabled"
        for widget, normal_state in self.input_widgets:
            widget.configure(state=normal_state if enabled else state)

    def _run_search(self, directory, keywords, pattern_mode, case_sensitive, max_count, timeout, before_context, after_context, multiline, glob_patterns, path_only):
        try:
            raw = search_directory(
                directory,
                keywords,
                pattern_mode=pattern_mode,
                case_sensitive=case_sensitive,
                max_count=max_count,
                timeout=timeout,
                before_context=before_context,
                after_context=after_context,
                multiline=multiline,
                glob_patterns=glob_patterns,
                path_only=path_only,
            )
            result = json.loads(raw)
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "matches": []}
        self.after(0, lambda: self._on_search_done(result))

    def _on_search_done(self, result):
        self.last_search_finished_at = datetime.now()
        self.last_search_result = result
        self._render_result(result)
        self._set_input_enabled(True)
        if self.search_pending:
            self.search_pending = False
            self._start_search()

    def _render_result(self, result):
        if not result.get("ok"):
            title, details = self._format_error_message(result.get("error", "unknown error"))
            self.status_var.set(title)
            self._set_content(details)
            return

        for item in result.get("matches", []):
            path = item.get("path", "")
            self.matches_by_file.setdefault(path, []).append(item)

        for path, matches in sorted(self.matches_by_file.items()):
            hit_count = sum(1 for item in matches if not item.get("is_context"))
            self.file_list.insert(tk.END, f"{path} ({hit_count})")

        count = sum(1 for item in result.get("matches", []) if not item.get("is_context"))
        file_count = len(self.matches_by_file)
        warning_suffix = "，部分文件已跳过" if result.get("warning") else ""
        self.status_var.set(f"Done: {count} matches in {file_count} files{warning_suffix}")
        if self.file_list.size() > 0:
            self.file_list.selection_set(0)
            self._show_file_by_index(0)

    def _show_selected_file(self, event=None):
        selection = self.file_list.curselection()
        if selection:
            self._show_file_by_index(selection[0])

    def _open_selected_file(self, event=None):
        selection = self.file_list.curselection()
        if not selection:
            return
        label = self.file_list.get(selection[0])
        path = label.rsplit(" (", 1)[0]
        if not os.path.isabs(path):
            path = os.path.join(self.directory_var.get(), path)
        path = os.path.abspath(path)
        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _show_file_by_index(self, index):
        label = self.file_list.get(index)
        path = label.rsplit(" (", 1)[0]
        lines = []
        ranges = []
        separators = []
        current_line = 1
        previous_line_number = None
        for match in self.matches_by_file.get(path, []):
            line_number = match.get("line_number")
            if isinstance(line_number, int) and isinstance(previous_line_number, int) and line_number > previous_line_number + 1:
                lines.append("-" * 80)
                separators.append(current_line)
                current_line += 1
            text = match.get("text", "")
            prefix = f"{line_number}: "
            lines.append(prefix + text)
            for submatch in match.get("submatches", []):
                start = submatch.get("start")
                end = submatch.get("end")
                if isinstance(start, int) and isinstance(end, int) and end > start:
                    ranges.append((f"{current_line}.{len(prefix) + start}", f"{current_line}.{len(prefix) + end}", self._match_tag(submatch.get("match", ""))))
            current_line += 1
            previous_line_number = line_number
        self._set_content("\n".join(lines), ranges, separators)

    def _format_error_message(self, error):
        error = str(error).strip() or "unknown error"
        suggestions = self._error_suggestions(error)
        lines = [
            "搜索没有完成",
            "",
            "可能的解决办法：",
            *[f"- {item}" for item in suggestions],
            "",
            "详细错误：",
            error,
        ]
        return "搜索失败，请查看右侧详细信息", "\n".join(lines)

    def _error_suggestions(self, error):
        lower = error.lower()
        if "command not found" in lower:
            return ["确认 rga 已安装，并且可以在终端中直接运行", "检查 rga 是否已加入 PATH 环境变量"]
        if "timed out" in lower:
            return ["把超时时间调大", "缩小搜索目录或减少关键字"]
        if "directory does not exist" in lower:
            return ["确认目录存在", "点击“浏览”重新选择搜索目录"]
        if "path is not a directory" in lower:
            return ["请选择文件夹作为搜索目录"]
        if "regex" in lower or "parse error" in lower:
            return ["检查关键字中的正则表达式语法", "如果只是普通文本搜索，可以把模式切换为 auto 或 glob"]
        return ["检查搜索目录、关键字和参数是否正确", "可复制下方详细错误用于排查"]

    def _set_content(self, text, ranges=None, separators=None):
        self.content.configure(state=tk.NORMAL)
        self.content.delete("1.0", tk.END)
        for tag in self.match_color_tags:
            self.content.tag_remove(tag, "1.0", tk.END)
        self.content.tag_remove("separator", "1.0", tk.END)
        self.content.insert("1.0", text)
        for start, end, tag in ranges or []:
            self.content.tag_add(tag, start, end)
        for line in separators or []:
            self.content.tag_add("separator", f"{line}.0", f"{line}.end")
        self.content.configure(state=tk.DISABLED)

    def _match_tag(self, text):
        key = text or "__match__"
        if key not in self.match_tag_by_text:
            index = len(self.match_tag_by_text) % len(self.match_color_tags)
            self.match_tag_by_text[key] = self.match_color_tags[index]
        return self.match_tag_by_text[key]

    def _clear_results(self):
        self.matches_by_file = {}
        self.match_tag_by_text = {}
        self.file_list.delete(0, tk.END)
        self._set_content("")

    def _parse_keywords(self, text):
        return text.strip()

    def _parse_optional_int(self, value, label):
        value = value.strip()
        if not value:
            return None
        try:
            number = int(value)
        except ValueError:
            messagebox.showwarning("参数错误", f"{label}必须是整数")
            return False
        if number <= 0:
            messagebox.showwarning("参数错误", f"{label}必须大于 0")
            return False
        return number

    def _parse_context_int(self, value, label):
        value = value.strip()
        if not value:
            return 5
        try:
            number = int(value)
        except ValueError:
            messagebox.showwarning("参数错误", f"{label}必须是整数")
            return False
        if number < 0:
            messagebox.showwarning("参数错误", f"{label}不能小于 0")
            return False
        return number

    def _parse_optional_float(self, value, label):
        value = value.strip()
        if not value:
            return None
        try:
            number = float(value)
        except ValueError:
            messagebox.showwarning("参数错误", f"{label}必须是数字")
            return False
        if number <= 0:
            messagebox.showwarning("参数错误", f"{label}必须大于 0")
            return False
        return number


def _enable_process_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def main():
    _enable_process_dpi_awareness()
    app = GrgaSearchApp()
    app.mainloop()


if __name__ == "__main__":
    main()
