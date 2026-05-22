import os
import json
import re
import sys
import threading
from itertools import product
from pathlib import Path
from queue import Queue, Empty
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
import webbrowser

HIDDEN_BUNDLE_NAMES = {
    "water",
    "vests",
    "tools",
    "throwables",
    "tacticals",
    "supplies",
    "structures",
    "sights",
    "shirts",
    "refills",
    "pbs",
    "pants",
    "optics",
    "melee",
    "medical",
    "maps",
    "magizines",
    "keys",
    "hats",
    "halloween2024",
    "guns",
    "growers",
    "glasses",
    "grips",
    "fuels",
    "frost",
    "food",
    "fishers",
    "filters",
    "detonators",
    "combocrate 2024",
    "clouds boxes",
    "barricades",
    "barrels",
    "arrest starts",
    "arrest ends",
    "backpacks",
    "blueprints",
    "boxes",
    "clouds",
    "magazines",
    "masks",
    "outfits",
}
HIDDEN_BUNDLE_KEYS = {
    re.sub(r"[^a-z0-9]+", "", name)
    for name in HIDDEN_BUNDLE_NAMES
}
HIDDEN_BUNDLE_KEYS.add("combocrate2024")

UNCHECKED = "[ ]"
CHECKED = "[x]"
NO_RESULTS_TEXT = "No results found"
MAX_DROPDOWN_RESULTS = 250
UI_SETTINGS_FILE = Path(__file__).resolve().parent / "ui_settings.json"

from .models import WorkshopMod
from .parsers import DatAssetScanner
from .workshop import WorkshopScanner
from .worker import PatchWorker
from .recipe_builder import RecipeBuilder


class Tooltip:
    def __init__(self, widget: tk.Widget, text_provider: Callable[[], str]):
        self.widget = widget
        self.text_provider = text_provider
        self.tip: Optional[tk.Toplevel] = None
        self.after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event: tk.Event) -> None:
        self._cancel()
        self.after_id = self.widget.after(350, self._show)

    def _cancel(self) -> None:
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self) -> None:
        self.after_id = None
        text = self.text_provider()
        if not text:
            return
        if self.tip and self.tip.winfo_exists():
            self.tip.destroy()
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        label = tk.Label(
            self.tip,
            text=text,
            background="#ffffe0",
            foreground="black",
            relief=tk.SOLID,
            borderwidth=1,
            padx=6,
            pady=3,
        )
        label.pack()
        self.tip.geometry(f"+{x}+{y}")

    def _hide(self, event: Optional[tk.Event] = None) -> None:
        self._cancel()
        if self.tip and self.tip.winfo_exists():
            self.tip.destroy()
        self.tip = None


class RecipeItemPicker:
    def __init__(
        self,
        parent: ttk.Frame,
        option_values: List[str],
        on_update: Callable[[], None],
        width: int = 28,
        allow_multi_select: bool = True,
    ):
        self.parent = parent
        self.option_values = option_values
        self.on_update = on_update
        self.allow_multi_select = allow_multi_select
        self.selected_labels: List[str] = []
        self.amount = 1
        self.var = tk.StringVar()
        self.border_frame = tk.Frame(parent, background="black", borderwidth=0)
        self.frame = ttk.Frame(self.border_frame)
        self.frame.grid(row=0, column=0, sticky="ew", padx=1, pady=1)
        self.border_frame.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(self.frame, textvariable=self.var, width=width)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.frame.columnconfigure(0, weight=1)
        self.entry.bind("<Return>", self._on_sort)
        self.entry.bind("<Button-1>", self._on_click)
        self.entry.bind("<Button-3>", self._on_amount_menu)
        self.border_frame.bind("<Button-3>", self._on_amount_menu)
        self.entry.bind("<KeyRelease>", self._on_key_release)
        self.entry.bind("<Down>", self._on_arrow_down)
        self.entry.bind("<Up>", self._on_arrow_up)
        self.entry.bind("<Enter>", self._start_marquee)
        self.entry.bind("<Leave>", self._stop_marquee)
        self.tooltip = Tooltip(self.entry, lambda: f"Amount: {self.amount}")
        self.dropdown_frame: Optional[ttk.Frame] = None
        self.listbox: Optional[tk.Listbox] = None
        self.dropdown_values: List[str] = []
        self._marquee_after_id: Optional[str] = None
        self._marquee_resume_after_id: Optional[str] = None
        self._filter_after_id: Optional[str] = None
        self._marquee_position = 0.0
        self._active_index: Optional[int] = None
        self._show_internal_selection = True

    def grid(self, **kwargs: Any) -> None:
        self.border_frame.grid(**kwargs)

    def _matching_options(self) -> List[str]:
        query = self.entry.get().strip().lower()
        if self.selected_labels and self.entry.get() == self._display_value():
            query = ""
        if not query:
            return self.option_values
        return [value for value in self.option_values if query in value.lower()]

    def _display_value(self) -> str:
        return "/".join(self.selected_labels)

    def _refresh_display(self) -> None:
        self.var.set(self._display_value())

    def _on_sort(self, event: tk.Event) -> str:
        self.open_dropdown(focus_list=False)
        self._commit_highlighted_selection()
        return "break"

    def _on_click(self, event: tk.Event) -> None:
        self.entry.after_idle(lambda: self.open_dropdown(focus_list=False))

    def _on_key_release(self, event: tk.Event) -> None:
        if event.keysym in {"Return", "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R"}:
            return
        self._pause_marquee_for_typing()
        self._show_internal_selection = False
        self._active_index = None
        self._schedule_filter_refresh()
        self._active_index = None

    def _schedule_filter_refresh(self) -> None:
        if self._filter_after_id is not None:
            self.entry.after_cancel(self._filter_after_id)
        self._filter_after_id = self.entry.after(80, self._refresh_filter)

    def _refresh_filter(self) -> None:
        self._filter_after_id = None
        if self.dropdown_frame and self.dropdown_frame.winfo_exists():
            self._populate(self._matching_options())
        else:
            self.open_dropdown(focus_list=False)

    def _on_arrow_down(self, event: tk.Event) -> str:
        self.open_dropdown(focus_list=False)
        self._move_highlight(1, start_at_first=True)
        return "break"

    def _on_arrow_up(self, event: tk.Event) -> str:
        self.open_dropdown(focus_list=False)
        self._move_highlight(-1)
        return "break"

    def _move_highlight(self, direction: int, start_at_first: bool = False) -> None:
        if not self.listbox or self.listbox.size() == 0:
            return
        if self.listbox.size() == 1 and self.listbox.get(0) == NO_RESULTS_TEXT:
            return
        if self._active_index is None and start_at_first:
            index = 0
        else:
            index = self._active_index if self._active_index is not None else (-1 if direction > 0 else 0)
            index += direction
        index = max(0, min(self.listbox.size() - 1, index))
        self._active_index = index
        self.listbox.activate(index)
        self.listbox.see(index)

    def _commit_highlighted_selection(self) -> None:
        if not self.listbox:
            return
        index = self._active_index
        if index is None and self.listbox.size() > 0:
            index = 0
        if index is None:
            return
        label = self.listbox.get(index)
        if label == NO_RESULTS_TEXT:
            return
        self.selected_labels = [label]
        self._show_internal_selection = True
        self._refresh_display()
        self.close_dropdown(restore_focus=True)
        self.on_update()

    def _pause_marquee_for_typing(self) -> None:
        self._stop_marquee()
        if self._marquee_resume_after_id is not None:
            self.entry.after_cancel(self._marquee_resume_after_id)
        self._marquee_resume_after_id = self.entry.after(2000, self._resume_marquee_after_typing)

    def _resume_marquee_after_typing(self) -> None:
        self._marquee_resume_after_id = None
        if self.entry.winfo_containing(self.entry.winfo_pointerx(), self.entry.winfo_pointery()) == self.entry:
            self._start_marquee(None)

    def _start_marquee(self, event: Optional[tk.Event]) -> None:
        if not self.selected_labels or self._marquee_after_id is not None:
            return
        self._marquee_position = 0.0
        self._step_marquee()

    def _step_marquee(self) -> None:
        if not self.selected_labels:
            self._marquee_after_id = None
            return
        self.entry.xview_moveto(self._marquee_position)
        self._marquee_position += 0.025
        if self._marquee_position > 1.0:
            self._marquee_position = 0.0
        self._marquee_after_id = self.entry.after(120, self._step_marquee)

    def _stop_marquee(self, event: Optional[tk.Event] = None) -> None:
        if self._marquee_after_id is not None:
            self.entry.after_cancel(self._marquee_after_id)
            self._marquee_after_id = None
        if event is not None and self._marquee_resume_after_id is not None:
            self.entry.after_cancel(self._marquee_resume_after_id)
            self._marquee_resume_after_id = None
        self._marquee_position = 0.0
        self.entry.xview_moveto(0)

    def open_dropdown(self, focus_list: bool = False) -> None:
        values = self._matching_options()
        if self.dropdown_frame and self.dropdown_frame.winfo_exists():
            self._populate(values)
            if focus_list and self.listbox:
                self.listbox.focus_set()
            return

        self.dropdown_frame = tk.Frame(self.frame, background="black")
        self.dropdown_frame.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.dropdown_frame.columnconfigure(0, weight=1)

        height = min(8, max(3, len(values)))
        self.listbox = tk.Listbox(
            self.dropdown_frame,
            height=height,
            selectmode=tk.MULTIPLE,
            exportselection=False,
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(self.dropdown_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.grid(row=0, column=0, sticky="ew", padx=(1, 0), pady=1)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 1), pady=1)
        self.listbox.bind("<ButtonRelease-1>", self._on_list_click)
        self.listbox.bind("<Button-3>", self._on_amount_menu)
        self.listbox.bind("<Escape>", lambda _: self.close_dropdown(restore_focus=True))
        self._populate(values)
        if self.listbox.size() > 0 and self._active_index is None:
            self.listbox.activate(0)
        if focus_list:
            self.listbox.focus_set()
        else:
            self.entry.focus_set()

    def _populate(self, values: List[str]) -> None:
        if not self.listbox:
            return
        self.dropdown_values = values
        previous = self.listbox.get(tk.ACTIVE) if self.listbox.size() else ""
        self.listbox.delete(0, tk.END)
        display_values = values[:MAX_DROPDOWN_RESULTS] or [NO_RESULTS_TEXT]
        for value in display_values:
            self.listbox.insert(tk.END, value)
            if self._show_internal_selection and value in self.selected_labels:
                self.listbox.selection_set(tk.END)
        if display_values[0] != NO_RESULTS_TEXT:
            active_index = display_values.index(previous) if self._active_index is not None and previous in display_values else 0
            if self._active_index is not None:
                self._active_index = active_index
            self.listbox.activate(active_index)
            self.listbox.see(active_index)
        else:
            self._active_index = None

    def _on_list_click(self, event: tk.Event) -> str:
        if not self.listbox:
            return "break"
        index = self.listbox.nearest(event.y)
        if index < 0:
            return "break"
        label = self.listbox.get(index)
        if label == NO_RESULTS_TEXT:
            return "break"
        self._active_index = index
        self.listbox.activate(index)
        self._show_internal_selection = True
        shift_pressed = self.allow_multi_select and bool(event.state & 0x0001)
        if shift_pressed:
            if label in self.selected_labels:
                self.selected_labels.remove(label)
                self.listbox.selection_clear(index)
            else:
                self.selected_labels.append(label)
                self.listbox.selection_set(index)
            self._refresh_display()
        else:
            self.selected_labels = [label]
            self._refresh_display()
            self.close_dropdown(restore_focus=True)
        self.on_update()
        return "break"

    def _on_amount_menu(self, event: tk.Event) -> str:
        self._show_amount_popup(event.x_root, event.y_root)
        return "break"

    def _show_amount_popup(self, x: int, y: int) -> None:
        popup = tk.Toplevel(self.entry)
        popup.title("")
        popup.transient(self.entry.winfo_toplevel())
        popup.resizable(False, False)
        popup.grab_set()

        content = ttk.Frame(popup, padding=8)
        content.grid(row=0, column=0, sticky="nsew")
        ttk.Label(content, text="Amount:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        amount_var = tk.StringVar(value=str(self.amount))
        amount_entry = ttk.Entry(content, textvariable=amount_var, width=8)
        amount_entry.grid(row=0, column=1, sticky="ew")

        def clean_amount() -> None:
            cleaned = re.sub(r"\D+", "", amount_var.get())
            if cleaned != amount_var.get():
                amount_var.set(cleaned)
                amount_entry.icursor(tk.END)

        def apply_amount() -> None:
            clean_amount()
            self.amount = max(1, int(amount_var.get() or "1"))
            popup.grab_release()
            popup.destroy()
            self.on_update()

        amount_entry.bind("<KeyRelease>", lambda _: clean_amount())
        amount_entry.bind("<Return>", lambda _: apply_amount())
        def close_popup() -> None:
            popup.grab_release()
            popup.destroy()

        popup.bind("<Escape>", lambda _: close_popup())
        ttk.Button(content, text="Ok", command=apply_amount).grid(row=0, column=2, padx=(8, 0))
        popup.protocol("WM_DELETE_WINDOW", close_popup)
        popup.geometry(f"+{x}+{y}")
        amount_entry.focus_set()

    def close_dropdown(self, restore_focus: bool = False) -> None:
        if self._filter_after_id is not None:
            self.entry.after_cancel(self._filter_after_id)
            self._filter_after_id = None
        if self.dropdown_frame and self.dropdown_frame.winfo_exists():
            self.dropdown_frame.destroy()
        self.dropdown_frame = None
        self.listbox = None
        self._active_index = None
        self._show_internal_selection = True
        if restore_focus:
            self.entry.focus_set()

    def update_options(self, option_values: List[str]) -> None:
        self.option_values = option_values
        self.selected_labels = [label for label in self.selected_labels if label in option_values]
        self._refresh_display()
        if self.dropdown_frame and self.dropdown_frame.winfo_exists():
            self._populate(self._matching_options())

    def set_labels(self, labels: List[str]) -> None:
        if self.allow_multi_select:
            self.selected_labels = [label for label in labels if label in self.option_values]
        else:
            self.selected_labels = [label for label in labels[:1] if label in self.option_values]
        self._refresh_display()

    def set_amount(self, amount: int) -> None:
        self.amount = max(1, int(amount or 1))

    def get_labels(self) -> List[str]:
        typed = self.entry.get().strip()
        if self.selected_labels:
            return list(self.selected_labels)
        if typed in self.option_values:
            return [typed]
        return []

    def get_amount(self) -> int:
        return self.amount

    def contains_widget(self, widget: tk.Widget) -> bool:
        current: Optional[tk.Widget] = widget
        while current is not None:
            if current in {self.border_frame, self.frame, self.entry, self.dropdown_frame, self.listbox}:
                return True
            current = current.master
        return False


class IngredientWidget:
    def __init__(self, parent: ttk.Frame, option_values: List[str], on_update: Callable[[], None], show_tool_checkbox: bool = True):
        self.frame = ttk.Frame(parent)
        self.option_values = option_values
        self.tool_var = tk.BooleanVar(value=False)
        self.picker = RecipeItemPicker(self.frame, option_values, on_update)
        self.picker.grid(row=0, column=0, sticky="ew")
        self.tool_check = None
        if show_tool_checkbox:
            self.tool_check = ttk.Checkbutton(self.frame, text="Is tool?", variable=self.tool_var, command=on_update)
            self.tool_check.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.frame.columnconfigure(0, weight=1)

    def update_options(self, option_values: List[str]) -> None:
        self.option_values = option_values
        self.picker.update_options(option_values)

    def set_labels(self, labels: List[str]) -> None:
        self.picker.set_labels(labels)

    def set_amount(self, amount: int) -> None:
        self.picker.set_amount(amount)

    def set_tool(self, is_tool: bool) -> None:
        self.tool_var.set(is_tool)

    def get_labels(self) -> List[str]:
        return self.picker.get_labels()

    def get_amount(self) -> int:
        return self.picker.get_amount()

    def is_tool(self) -> bool:
        return self.tool_var.get()


class RecipeRow:
    def __init__(
        self,
        parent: ttk.Frame,
        option_values: List[str],
        option_map: Dict[str, int],
        on_update: Callable[[], None],
        on_remove: Optional[Callable[[Any], None]] = None,
    ):
        self.parent = parent
        self.option_values = option_values
        self.option_map = option_map
        self.on_update = on_update
        self.on_remove = on_remove
        self.container = tk.Frame(parent, background="black")
        self.frame = ttk.Frame(self.container, padding=4)
        self.frame.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 1))
        self.container.columnconfigure(0, weight=1)
        self.ingredients: List[IngredientWidget] = []
        self.name_var = tk.StringVar()
        self.description = ""
        self.name_entry = ttk.Entry(self.frame, textvariable=self.name_var, width=20)
        self.name_entry.bind("<KeyRelease>", self._on_name_change)
        self.name_entry.bind("<Button-3>", self._on_description_menu)
        self.result_picker = RecipeItemPicker(self.frame, option_values, self.on_update, allow_multi_select=False)
        self.add_button = ttk.Button(self.frame, text="+", width=2, command=self.add_component, style="Green.TButton")
        self.add_button.bind("<Button-3>", lambda _: self.remove_component())
        self.remove_button = tk.Button(
            self.frame,
            text="X",
            width=2,
            foreground="#b00020",
            activeforeground="#b00020",
            command=self._remove_self,
            padx=2,
            pady=0,
        )
        self.equals_label = ttk.Label(self.frame, text="=")
        self._add_ingredient()
        self._layout()

    def _on_name_change(self, event: tk.Event) -> None:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", self.name_var.get())
        if cleaned != self.name_var.get():
            self.name_var.set(cleaned)
            self.name_entry.icursor(tk.END)
        self.on_update()

    def _on_description_menu(self, event: tk.Event) -> str:
        dialog = tk.Toplevel(self.frame)
        dialog.title("Recipe Description")
        dialog.transient(self.frame.winfo_toplevel())
        dialog.grab_set()
        dialog.resizable(False, False)

        ttk.Label(dialog, text="Description").grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")
        text = tk.Text(dialog, width=48, height=5, wrap="word")
        text.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="ew")
        text.insert("1.0", self.description)
        text.focus_set()

        def save() -> None:
            self.description = " ".join(text.get("1.0", "end").split()).strip()
            dialog.destroy()
            self.on_update()

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(dialog, text="Ok", command=save).grid(row=2, column=0, padx=(10, 4), pady=(0, 10), sticky="e")
        ttk.Button(dialog, text="Cancel", command=cancel).grid(row=2, column=1, padx=(4, 10), pady=(0, 10), sticky="w")
        dialog.update_idletasks()
        x = event.x_root
        y = event.y_root
        dialog.geometry(f"+{x}+{y}")
        return "break"

    def _remove_self(self) -> None:
        if self.on_remove:
            self.on_remove(self)

    def _add_ingredient(self) -> None:
        show_tool_checkbox = len(self.ingredients) > 0
        ingredient = IngredientWidget(self.frame, self.option_values, self.on_update, show_tool_checkbox=show_tool_checkbox)
        self.ingredients.append(ingredient)

    def add_component(self) -> None:
        self._add_ingredient()
        self._layout()
        self.on_update()

    def remove_component(self) -> None:
        if len(self.ingredients) <= 1:
            return
        removed = self.ingredients.pop()
        removed.frame.destroy()
        self._layout()
        self.on_update()

    def _layout(self) -> None:
        for child in self.frame.winfo_children():
            child.grid_forget()

        max_cols = 4
        self.name_entry.grid(row=0, column=0, padx=(0, 4), pady=(0, 4), sticky="new")
        self.frame.columnconfigure(0, weight=0)
        for idx, ingredient in enumerate(self.ingredients):
            row = idx // max_cols
            col = (idx % max_cols) + 1
            ingredient.frame.grid(row=row, column=col, padx=(0, 4), pady=(0, 4), sticky="nsew")
            self.frame.columnconfigure(col, weight=1)

        self.equals_label.grid(row=0, column=max_cols + 1, padx=(0, 4), sticky="nw")
        self.result_picker.grid(row=0, column=max_cols + 2, padx=(0, 4), sticky="new")
        self.frame.columnconfigure(max_cols + 2, weight=1)
        self.remove_button.grid(row=0, column=max_cols + 3, padx=(4, 0), sticky="ne")

        next_index = len(self.ingredients)
        last_row = next_index // max_cols
        last_col = (next_index % max_cols) + 1
        self.add_button.grid(row=last_row, column=last_col, padx=(0, 4), sticky="nw")

    def update_options(self, option_values: List[str], option_map: Dict[str, int]) -> None:
        self.option_values = option_values
        self.option_map = option_map
        for ingredient in self.ingredients:
            ingredient.update_options(option_values)
        self.result_picker.update_options(option_values)

    def set_recipe_name(self, name: str) -> None:
        self.name_var.set(name)

    def get_recipe_name(self) -> str:
        return self.name_var.get().strip()

    def set_description(self, description: str) -> None:
        self.description = " ".join(str(description).split()).strip()

    def get_description(self) -> str:
        return self.description

    def get_ingredient_label_groups(self) -> List[List[str]]:
        return [labels for ingredient in self.ingredients if (labels := ingredient.get_labels())]

    def get_ingredient_amounts(self) -> List[int]:
        return [ingredient.get_amount() for ingredient in self.ingredients if ingredient.get_labels()]

    def get_output_label(self) -> str:
        labels = self.result_picker.get_labels()
        return labels[0] if labels else ""

    def get_output_amount(self) -> int:
        return self.result_picker.get_amount()

    def configure_recipe(
        self,
        name: str,
        ingredient_labels: List[str],
        ingredient_amounts: List[int],
        tool_indices: List[int],
        output_label: str,
        output_amount: int,
        description: str = "",
    ) -> None:
        self.set_recipe_name(name)
        self.set_description(description)
        while len(self.ingredients) < len(ingredient_labels):
            self._add_ingredient()
        self._layout()
        for index, label in enumerate(ingredient_labels):
            self.ingredients[index].set_labels([label])
            amount = ingredient_amounts[index] if index < len(ingredient_amounts) else 1
            self.ingredients[index].set_amount(amount)
            self.ingredients[index].set_tool(index in tool_indices)
        self.result_picker.set_labels([output_label])
        self.result_picker.set_amount(output_amount)
        self.on_update()

    def get_tool_indices(self) -> List[int]:
        return [index for index, ingredient in enumerate(self.ingredients) if ingredient.is_tool()]

    def put_in_parent(self, row: int) -> None:
        self.container.grid(row=row, column=0, sticky="ew", pady=4)
        self.parent.columnconfigure(0, weight=1)

    def close_dropdowns_except(self, widget: tk.Widget) -> None:
        for ingredient in self.ingredients:
            if not ingredient.picker.contains_widget(widget):
                ingredient.picker.close_dropdown()
        if not self.result_picker.contains_widget(widget):
            self.result_picker.close_dropdown()


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Aidds - Automatic ID Deployment System")
        self.geometry("980x760")
        self.game_dir: Path | None = None
        self.workshop_path: Path | None = None
        self.csv_path: Path | None = None
        self.log_path: Path | None = None
        self.output_path: Path | None = None
        self.output_base: Path = Path(__file__).resolve().parents[1]
        self.mods: List[WorkshopMod] = []
        self.worker: PatchWorker | None = None
        self.worker_queue: Queue = Queue()
        self.last_output_path: Path | None = None
        self.scanner = WorkshopScanner()
        self._recipe_item_cache: Dict[str, List[Tuple[str, int]]] = {}
        self._recipe_refresh_after_id: Optional[str] = None
        self.ui_settings = self._load_ui_settings()
        self._showing_recipe_reminder = False

        self._build_ui()
        self.after(200, self._poll_worker_queue)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style(self)
        style.configure("Green.TButton", foreground="#006400", background="#ccffcc")
        style.configure("TNotebook", borderwidth=1)
        style.configure(
            "TNotebook.Tab",
            padding=(18, 8),
            borderwidth=1,
            relief="solid",
            bordercolor="black",
            lightcolor="black",
            darkcolor="black",
        )
        style.map("TNotebook.Tab", padding=[("selected", (20, 9))])

        notebook_row = ttk.Frame(frame)
        notebook_row.pack(fill=tk.BOTH, expand=True)
        notebook_row.columnconfigure(0, weight=1)
        notebook_row.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(notebook_row)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.rent_server_button = ttk.Button(notebook_row, text="Rent a server", command=self.on_rent_server)
        self.rent_server_button.grid(row=0, column=1, sticky="ne", padx=(8, 0), pady=(1, 0))

        self.ids_tab = ttk.Frame(self.notebook)
        self.recipes_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.ids_tab, text="IDs")
        self.notebook.add(self.recipes_tab, text="Recipes")

        self._build_ids_tab()
        self._build_recipes_tab()
        self.bind_all("<Button-1>", self._on_global_click, add="+")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        self.progress_bar = ttk.Progressbar(frame, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(10, 10))

        self.log_output = ScrolledText(frame, height=10, state="disabled")
        self.log_output.pack(fill=tk.BOTH, expand=True)

        self.status_label = ttk.Label(frame, text="Awaiting input selection...")
        self.status_label.pack(fill=tk.X, pady=(5, 0))

    def _build_ids_tab(self) -> None:
        input_frame = ttk.Frame(self.ids_tab)
        input_frame.pack(fill=tk.X, pady=(0, 10))

        self.game_button = ttk.Button(input_frame, text="Select Unturned Game Directory", command=self.on_select_game_directory)
        self.game_button.grid(row=0, column=0, sticky=tk.W)
        self.game_label = ttk.Entry(input_frame, state="readonly")
        self.game_label.grid(row=0, column=1, sticky=tk.EW, padx=5)

        self.workshop_button = ttk.Button(input_frame, text="Override Workshop Folder", command=self.on_select_manual_workshop_folder)
        self.workshop_button.grid(row=1, column=0, sticky=tk.W, pady=5)
        self.workshop_button.state(["disabled"])
        self.workshop_label = ttk.Entry(input_frame, state="readonly")
        self.workshop_label.grid(row=1, column=1, sticky=tk.EW, padx=5)

        self.csv_button = ttk.Button(input_frame, text="Select ITEM.csv", command=self.on_select_csv)
        self.csv_button.grid(row=2, column=0, sticky=tk.W, pady=5)
        self.csv_label = ttk.Entry(input_frame, state="readonly")
        self.csv_label.grid(row=2, column=1, sticky=tk.EW, padx=5)

        self.log_button = ttk.Button(input_frame, text="Select Client.log", command=self.on_select_log)
        self.log_button.grid(row=3, column=0, sticky=tk.W)
        self.log_label = ttk.Entry(input_frame, state="readonly")
        self.log_label.grid(row=3, column=1, sticky=tk.EW, padx=5)

        self.output_button = ttk.Button(input_frame, text="Select Output Folder", command=self.on_select_output_directory)
        self.output_button.grid(row=4, column=0, sticky=tk.W, pady=5)
        self.output_label = ttk.Entry(input_frame, state="readonly")
        self.output_label.grid(row=4, column=1, sticky=tk.EW, padx=5)

        input_frame.columnconfigure(1, weight=1)
        self._set_label(self.output_label, f"Default: {self.output_base}")

        mods_frame = ttk.LabelFrame(self.ids_tab, text="Workshop Mods")
        mods_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        columns = ("selected", "title", "workshop_id")
        self.mods_tree = ttk.Treeview(mods_frame, columns=columns, show="headings", selectmode="none")
        self.mods_tree.heading("selected", text="Selected")
        self.mods_tree.heading("title", text="Mod Name")
        self.mods_tree.heading("workshop_id", text="Workshop ID")
        self.mods_tree.column("selected", width=90, anchor=tk.CENTER)
        self.mods_tree.column("title", width=600, anchor=tk.W)
        self.mods_tree.column("workshop_id", width=160, anchor=tk.CENTER)
        self.mods_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.mods_tree.bind("<Button-1>", self._on_mods_tree_click)
        self.mods_tree.bind("<Double-1>", self._on_mods_tree_double_click)

        scrollbar = ttk.Scrollbar(mods_frame, orient=tk.VERTICAL, command=self.mods_tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.mods_tree.configure(yscrollcommand=scrollbar.set)

        options_frame = ttk.Frame(self.ids_tab)
        options_frame.pack(fill=tk.X, pady=(0, 10))

        self.dry_run_var = tk.BooleanVar(value=False)
        self.export_csv_var = tk.BooleanVar(value=False)
        self.export_json_var = tk.BooleanVar(value=False)
        self.dry_run_checkbox = ttk.Checkbutton(options_frame, text="Dry run", variable=self.dry_run_var)
        self.export_csv_checkbox = ttk.Checkbutton(options_frame, text="Export CSV mapping", variable=self.export_csv_var)
        self.export_json_checkbox = ttk.Checkbutton(options_frame, text="Export JSON mapping", variable=self.export_json_var)
        self.dry_run_checkbox.grid(row=0, column=0, padx=(0, 20))
        self.export_csv_checkbox.grid(row=0, column=1, padx=(0, 20))
        self.export_json_checkbox.grid(row=0, column=2, padx=(0, 20))

        buttons_frame = ttk.Frame(self.ids_tab)
        buttons_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_button = ttk.Button(buttons_frame, text="Start Patch Generation", command=self.on_start)
        self.start_button.pack(side=tk.LEFT)
        self.start_button.state(["disabled"])

        self.open_output_button = ttk.Button(buttons_frame, text="Open Last Output Folder", command=self.on_open_output_folder)
        self.open_output_button.pack(side=tk.LEFT, padx=(10, 0))
        self.open_output_button.state(["disabled"])

    def _build_recipes_tab(self) -> None:
        self.recipe_rows: List[RecipeRow] = []
        self.recipe_items: List[str] = []
        self.recipe_item_map: Dict[str, int] = {}
        self.export_recipes_csv_var = tk.BooleanVar(value=False)
        self.recipes_csv_path: Optional[Path] = None

        toolbar = ttk.Frame(self.recipes_tab)
        toolbar.pack(fill=tk.X, pady=(0, 10))

        self.add_recipe_button = ttk.Button(toolbar, text="Add Recipe", command=self.on_add_recipe)
        self.add_recipe_button.pack(side=tk.LEFT)

        self.generate_recipes_button = ttk.Button(toolbar, text="Generate Recipes", command=self.on_generate_recipes)
        self.generate_recipes_button.pack(side=tk.LEFT, padx=(10, 0))

        self.recipe_status_label = ttk.Label(toolbar, text="No recipes defined")
        self.recipe_status_label.pack(side=tk.LEFT, padx=(20, 0))

        import_frame = ttk.Frame(self.recipes_tab)
        import_frame.pack(fill=tk.X, pady=(0, 10))
        self.import_recipes_csv_button = ttk.Button(import_frame, text="Select Recipes.csv", command=self.on_select_recipes_csv)
        self.import_recipes_csv_button.grid(row=0, column=0, sticky=tk.W)
        self.recipes_csv_label = ttk.Entry(import_frame, state="readonly")
        self.recipes_csv_label.grid(row=0, column=1, sticky=tk.EW, padx=5)
        import_frame.columnconfigure(1, weight=1)
        self._set_label(self.recipes_csv_label, "<optional Recipes.csv>")
        self._enable_recipes_csv_drop()

        recipe_frame = ttk.Frame(self.recipes_tab)
        recipe_frame.pack(fill=tk.BOTH, expand=True)

        self.recipe_canvas = tk.Canvas(recipe_frame, borderwidth=0, highlightthickness=0)
        self.recipe_scrollbar = ttk.Scrollbar(recipe_frame, orient=tk.VERTICAL, command=self.recipe_canvas.yview)
        self.recipe_canvas.configure(yscrollcommand=self.recipe_scrollbar.set)
        self.recipe_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.recipe_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.recipe_inner_frame = ttk.Frame(self.recipe_canvas)
        self.recipe_canvas.create_window((0, 0), window=self.recipe_inner_frame, anchor="nw")
        self.recipe_inner_frame.bind(
            "<Configure>",
            lambda event: self.recipe_canvas.configure(scrollregion=self.recipe_canvas.bbox("all")),
        )

        bottom_options = ttk.Frame(self.recipes_tab)
        bottom_options.pack(fill=tk.X, pady=(10, 0))
        self.export_recipes_csv_checkbox = ttk.Checkbutton(bottom_options, text="Export Recipes.csv", variable=self.export_recipes_csv_var)
        self.export_recipes_csv_checkbox.pack(side=tk.LEFT)
        self.generate_recipes_button.state(["disabled"])

        self._refresh_recipe_item_options()

    def _on_global_click(self, event: tk.Event) -> None:
        widget = event.widget
        for row in getattr(self, "recipe_rows", []):
            row.close_dropdowns_except(widget)

    def _load_ui_settings(self) -> Dict[str, Any]:
        if not UI_SETTINGS_FILE.exists():
            return {}
        try:
            data = json.loads(UI_SETTINGS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_ui_settings(self) -> None:
        try:
            UI_SETTINGS_FILE.write_text(json.dumps(self.ui_settings, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _enable_recipes_csv_drop(self) -> None:
        try:
            self.tk.call("package", "require", "tkdnd")
            for widget in [self.recipes_csv_label, self.recipes_tab]:
                self.tk.call("tkdnd::drop_target", "register", widget._w, "DND_Files")
                widget.bind("<<Drop>>", self._on_recipes_csv_drop)
            self.recipes_csv_label.configure(state="normal")
            self.recipes_csv_label.delete(0, tk.END)
            self.recipes_csv_label.insert(0, "<optional Recipes.csv - drag file here or select>")
            self.recipes_csv_label.configure(state="readonly")
        except Exception:
            pass

    def _on_recipes_csv_drop(self, event: tk.Event) -> None:
        raw_path = str(getattr(event, "data", "")).strip()
        if not raw_path:
            return
        if raw_path.startswith("{") and raw_path.endswith("}"):
            raw_path = raw_path[1:-1]
        first_path = raw_path.split("} {")[0].strip("{}")
        self._set_recipes_csv_path(Path(first_path))

    def on_select_recipes_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Recipes.csv",
            filetypes=[("Recipes CSV", "*.csv"), ("All Files", "*")],
        )
        if not path:
            return
        self._set_recipes_csv_path(Path(path))

    def _set_recipes_csv_path(self, path: Path) -> None:
        if not path.exists():
            messagebox.showerror("Invalid Recipes.csv", "Selected Recipes.csv does not exist.")
            return
        self.recipes_csv_path = path
        self._set_label(self.recipes_csv_label, path)
        self._append_log(f"Loaded recipe tracking CSV: {path}")
        imported = self._import_custom_recipes_from_csv(path)
        if imported:
            self._append_log(f"Imported {imported} custom recipe row(s) from Recipes.csv")

    def _parse_csv_json_list(self, value: str, fallback: Optional[list] = None) -> list:
        if not value:
            return fallback or []
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else (fallback or [])
        except Exception:
            return fallback or []

    def _import_custom_recipes_from_csv(self, path: Path) -> int:
        import csv

        imported = 0
        skipped = 0
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for record in reader:
                    ingredient_labels = [
                        str(label)
                        for label in self._parse_csv_json_list(record.get("IngredientLabels", ""))
                        if str(label) in self.recipe_item_map
                    ]
                    output_label = (record.get("OutputLabel") or "").strip()
                    if not ingredient_labels or output_label not in self.recipe_item_map:
                        if record.get("CustomRecipeName") or record.get("IngredientLabels") or record.get("OutputLabel"):
                            skipped += 1
                        continue

                    ingredient_amounts = [
                        max(1, int(value))
                        for value in self._parse_csv_json_list(record.get("IngredientAmounts", ""))
                        if str(value).isdigit()
                    ]
                    tool_indices = [
                        int(value)
                        for value in self._parse_csv_json_list(record.get("ToolIndices", ""))
                        if str(value).isdigit()
                    ]
                    output_amount_text = (record.get("OutputAmount") or "1").strip()
                    output_amount = int(output_amount_text) if output_amount_text.isdigit() else 1
                    name = (record.get("CustomRecipeName") or record.get("Name") or f"ImportedRecipe{len(self.recipe_rows) + 1}").strip()

                    description = (record.get("Description") or "").strip()

                    row = RecipeRow(
                        self.recipe_inner_frame,
                        self.recipe_items,
                        self.recipe_item_map,
                        self._update_recipe_status,
                        self._remove_recipe_row,
                    )
                    self.recipe_rows.append(row)
                    row.configure_recipe(name, ingredient_labels, ingredient_amounts, tool_indices, output_label, output_amount, description)
                    row.put_in_parent(len(self.recipe_rows) - 1)
                    imported += 1
            self._update_recipe_status()
            if skipped:
                messagebox.showinfo(
                    "Recipes.csv Imported",
                    f"Imported {imported} custom recipe rows.\nSkipped {skipped} rows because their items are not currently selectable.",
                )
        except Exception as exc:
            messagebox.showerror("Recipes.csv Import Failed", str(exc))
        return imported

    def _on_notebook_tab_changed(self, event: tk.Event) -> None:
        if self.notebook.select() != str(self.recipes_tab):
            return
        if self.ui_settings.get("hide_recipe_patch_reminder"):
            return
        if self._showing_recipe_reminder:
            return
        self.after_idle(self._show_recipe_patch_reminder)

    def _show_recipe_patch_reminder(self) -> None:
        if self.notebook.select() != str(self.recipes_tab) or self._showing_recipe_reminder:
            return

        self._showing_recipe_reminder = True
        dialog = tk.Toplevel(self)
        dialog.title("Recommended First Step")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        content = ttk.Frame(dialog, padding=14)
        content.grid(row=0, column=0, sticky="nsew")
        message = ttk.Label(
            content,
            text="It's recommended to run the ID conflict patcher before creating your crafting recipes.",
            wraplength=380,
        )
        message.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        dont_remind_var = tk.BooleanVar(value=False)
        dont_remind_check = ttk.Checkbutton(content, text="Don't remind me again", variable=dont_remind_var)
        dont_remind_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

        def close_dialog(go_to_ids: bool = False) -> None:
            if dont_remind_var.get():
                self.ui_settings["hide_recipe_patch_reminder"] = True
                self._save_ui_settings()
            dialog.grab_release()
            dialog.destroy()
            self._showing_recipe_reminder = False
            if go_to_ids:
                self.notebook.select(self.ids_tab)

        go_button = ttk.Button(content, text="Go there", command=lambda: close_dialog(go_to_ids=True))
        go_button.grid(row=2, column=0, sticky="e", padx=(0, 8))
        ok_button = ttk.Button(content, text="Ok", command=close_dialog)
        ok_button.grid(row=2, column=1, sticky="e")
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.bind("<Escape>", lambda _: close_dialog())
        dialog.bind("<Return>", lambda _: close_dialog())
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 3)
        dialog.geometry(f"+{x}+{y}")
        ok_button.focus_set()

    def on_select_game_directory(self) -> None:
        path = filedialog.askdirectory(title="Select Unturned Game Directory")
        if not path:
            return
        selected_path = Path(path)
        if not self._validate_game_dir(selected_path):
            messagebox.showerror(
                "Invalid Unturned directory",
                "The selected folder is not a valid Unturned game directory. It must contain Unturned.exe or Unturned_Data.",
            )
            return

        self.game_dir = selected_path
        self.game_label.config(state="normal")
        self.game_label.delete(0, tk.END)
        self.game_label.insert(0, str(self.game_dir))
        self.game_label.config(state="readonly")

        self._resolve_workshop_path()
        self._resolve_game_paths()

    def on_select_manual_workshop_folder(self) -> None:
        path = filedialog.askdirectory(title="Select Workshop Mods Folder")
        if not path:
            return
        selected_path = Path(path)
        if not selected_path.exists():
            messagebox.showerror("Invalid path", "Selected workshop path does not exist.")
            return

        self.workshop_path = selected_path
        self.workshop_label.config(state="normal")
        self.workshop_label.delete(0, tk.END)
        self.workshop_label.insert(0, str(self.workshop_path))
        self.workshop_label.config(state="readonly")
        self.workshop_button.state(["disabled"])
        self._load_workshop_mods()
        self._update_start_button()

    def _resolve_game_paths(self) -> None:
        if not self.game_dir:
            return

        log_candidate = self.game_dir / "Logs" / "Client.log"
        if log_candidate.exists():
            self.log_path = log_candidate
            self._set_label(self.log_label, self.log_path)
        else:
            self.log_path = None
            self._set_label(self.log_label, "<Client.log not found>")

        csv_candidate = (
            self.game_dir
            / "Extras"
            / "AssetIDs"
            / "All Assets"
            / "Grouped by Legacy Category"
            / "ITEM.csv"
        )
        if csv_candidate.exists():
            self.csv_path = csv_candidate
            self._set_label(self.csv_label, self.csv_path)
        else:
            self.csv_path = None
            self._set_label(self.csv_label, "<ITEM.csv not found>")
            messagebox.showinfo(
                "CSV Not Found",
                "ITEM.csv was not found in the Unturned directory.\n\n"
                "Please export Unturned asset IDs and place ITEM.csv in the correct AssetIDs folder,\n"
                "or select it manually using the Select ITEM.csv button.",
            )

        self._update_start_button()

    def on_select_output_directory(self) -> None:
        path = filedialog.askdirectory(title="Select Output Folder")
        if not path:
            return
        self.output_path = Path(path)
        self._set_label(self.output_label, self.output_path)

    def _set_label(self, widget: ttk.Entry, value: object) -> None:
        widget.config(state="normal")
        widget.delete(0, tk.END)
        widget.insert(0, str(value))
        widget.config(state="readonly")

    def _discover_sources_from_item_csv(self) -> List[WorkshopMod]:
        if not self.csv_path or not self.csv_path.exists():
            return []

        import csv

        sources: Dict[str, WorkshopMod] = {}
        try:
            with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                sample = handle.read(8192)
                handle.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                reader = csv.DictReader(handle, dialect=dialect)
                if not reader.fieldnames:
                    return []
                headers = {name.strip().lower(): name for name in reader.fieldnames if name}
                origin_key = headers.get("origin")
                if not origin_key:
                    return []

                for row in reader:
                    origin_value = row.get(origin_key)
                    if not origin_value:
                        continue
                    origin_text = origin_value.strip()
                    if not origin_text:
                        continue

                    lowered = origin_text.lower()
                    if lowered == "vanilla built-in assets":
                        sources.setdefault(
                            "vanilla",
                            WorkshopMod(
                                workshop_id="vanilla",
                                path=None,
                                title="Vanilla Built-in Assets",
                                display_name="Vanilla Built-in Assets",
                                selected=True,
                                is_virtual=True,
                            ),
                        )
                        continue

                    match = re.search(r"Workshop File(?: \"([^\"]+)\")? \((\d+)\)", origin_text)
                    if not match:
                        continue
                    name_value = match.group(1) or f"Workshop File {match.group(2)}"
                    workshop_id = match.group(2)
                    if workshop_id in sources:
                        continue
                    sources[workshop_id] = WorkshopMod(
                        workshop_id=workshop_id,
                        path=None,
                        title=name_value,
                        display_name=f"{name_value} ({workshop_id})",
                        selected=True,
                        is_virtual=True,
                    )
        except Exception:
            return []
        return list(sources.values())

    def _discover_bundle_sources(self) -> List[WorkshopMod]:
        if not self.game_dir:
            return []
        bundle_root = self.game_dir / "Bundles" / "Items"
        if not bundle_root.exists() or not bundle_root.is_dir():
            return []

        sources: List[WorkshopMod] = []
        for candidate in sorted(bundle_root.iterdir()):
            if not candidate.is_dir():
                continue
            if not any(candidate.rglob("*.dat")):
                continue
            title = candidate.name.replace("_", " ").title()
            normalized = re.sub(r"[^a-z0-9]+", " ", candidate.name.lower()).strip()
            hidden_key = re.sub(r"[^a-z0-9]+", "", candidate.name.lower())
            hidden = normalized in HIDDEN_BUNDLE_NAMES or hidden_key in HIDDEN_BUNDLE_KEYS
            sources.append(
                WorkshopMod(
                    workshop_id=f"bundle-{candidate.name}",
                    path=candidate,
                    title=title,
                    display_name=f"{title} (Bundles/{candidate.name})",
                    selected=True,
                    is_virtual=False,
                    hidden=hidden,
                )
            )
        return sources

    def _validate_game_dir(self, path: Path) -> bool:
        return (path / "Unturned.exe").exists() or (path / "Unturned_Data").exists()

    def _resolve_workshop_path(self) -> None:
        if not self.game_dir:
            return
        if len(self.game_dir.parents) < 2:
            self.workshop_path = None
            self._show_workshop_resolution_failure()
            return

        steamapps = self.game_dir.parents[1]
        candidate = steamapps / "workshop" / "content" / "304930"
        if candidate.exists() and candidate.is_dir():
            self.workshop_path = candidate
            self.workshop_label.config(state="normal")
            self.workshop_label.delete(0, tk.END)
            self.workshop_label.insert(0, str(self.workshop_path))
            self.workshop_label.config(state="readonly")
            self.workshop_button.state(["disabled"])
            self._load_workshop_mods()
            self._update_start_button()
        else:
            self.workshop_path = None
            self._show_workshop_resolution_failure()

    def _show_workshop_resolution_failure(self) -> None:
        self.workshop_label.config(state="normal")
        self.workshop_label.delete(0, tk.END)
        self.workshop_label.insert(0, "<automatic resolution failed>")
        self.workshop_label.config(state="readonly")
        self.status_label.config(text="Resolved workshop path not found. Use Override Workshop Folder to choose manually.")
        self.workshop_button.state(["!disabled"])
        self.mods_tree.delete(*self.mods_tree.get_children())
        self.mods = []
        self._update_start_button()

    def on_select_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Select ITEM.csv",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*")],
        )
        if path:
            self.csv_path = Path(path)
            self.csv_label.config(state="normal")
            self.csv_label.delete(0, tk.END)
            self.csv_label.insert(0, path)
            self.csv_label.config(state="readonly")
            self._update_start_button()

    def on_select_log(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Client.log",
            filetypes=[("Log Files", "*.log"), ("All Files", "*")],
        )
        if path:
            self.log_path = Path(path)
            self.log_label.config(state="normal")
            self.log_label.delete(0, tk.END)
            self.log_label.insert(0, path)
            self.log_label.config(state="readonly")
            self._update_start_button()

    def on_rent_server(self) -> None:
        webbrowser.open_new_tab("https://midnighthostingsolutions.com")

    def _load_workshop_mods(self) -> None:
        self.mods_tree.delete(*self.mods_tree.get_children())
        self.mods = []
        self._recipe_item_cache.clear()
        if not self.workshop_path or not self.workshop_path.exists():
            return
        self.mods = self.scanner.discover_workshop_mods(self.workshop_path)
        discovered_sources = self._discover_sources_from_item_csv()
        for source in discovered_sources:
            if all(mod.workshop_id != source.workshop_id for mod in self.mods):
                self.mods.append(source)
        bundle_sources = self._discover_bundle_sources()
        for source in bundle_sources:
            if all(mod.workshop_id != source.workshop_id for mod in self.mods):
                self.mods.append(source)
        self.mods.sort(key=lambda mod: (mod.workshop_id.startswith("bundle-"), mod.title.lower()))
        for mod in self.mods:
            mod.hidden = mod.hidden or self._should_hide_mod(mod)
            mod.selected = mod.hidden
            if mod.hidden:
                continue
            self.mods_tree.insert("", tk.END, iid=mod.workshop_id, values=(UNCHECKED, mod.title, mod.workshop_id))
        self.status_label.config(text=f"Loaded {len(self.mods)} sources. Visible sources start unselected.")
        self._refresh_recipe_item_options()

    def _update_start_button(self) -> None:
        enabled = bool(self.workshop_path and self.csv_path and self.log_path and self.mods)
        if enabled:
            self.start_button.state(["!disabled"])
        else:
            self.start_button.state(["disabled"])

    def _on_mods_tree_click(self, event: Any) -> None:
        item_id = self.mods_tree.identify_row(event.y)
        column = self.mods_tree.identify_column(event.x)
        if not item_id or column != "#1":
            return
        current = self.mods_tree.set(item_id, "selected")
        new_value = UNCHECKED if current == CHECKED else CHECKED
        self.mods_tree.set(item_id, "selected", new_value)
        mod = next((m for m in self.mods if m.workshop_id == item_id), None)
        if mod:
            mod.selected = new_value == CHECKED
        self._schedule_recipe_item_refresh()

    def _get_selected_mods(self) -> List[WorkshopMod]:
        selected: List[WorkshopMod] = []
        for mod in self.mods:
            if mod.hidden:
                if mod.selected:
                    selected.append(mod)
                continue
            cell = self.mods_tree.set(mod.workshop_id, "selected")
            mod.selected = cell == CHECKED
            if mod.selected:
                selected.append(mod)
        return selected

    def _format_recipe_label(self, value: Optional[str]) -> str:
        if not value:
            return "Unknown"
        return re.sub(r"_+", " ", value).strip()

    def _should_hide_mod(self, mod: WorkshopMod) -> bool:
        values = [mod.workshop_id, mod.title, mod.display_name]
        if mod.path:
            values.append(mod.path.name)
        for value in values:
            compact = re.sub(r"[^a-z0-9]+", "", str(value).lower())
            if compact in HIDDEN_BUNDLE_KEYS:
                return True
        return False

    def _get_selected_recipe_items(self) -> List[Tuple[str, int]]:
        items: List[Tuple[str, int]] = []
        seen: set[Tuple[str, int]] = set()
        for mod in self.mods:
            if not mod.selected or not mod.path:
                continue
            cache_key = str(mod.path.resolve())
            if cache_key not in self._recipe_item_cache:
                mod_items: List[Tuple[str, int]] = []
                scanner = DatAssetScanner(mod.path)
                scanner.scan_all_files()
                for blocks in scanner.asset_files.values():
                    for block in blocks:
                        if block.legacy_id is None:
                            continue
                        label_candidate = block.name or block.file_path.stem or block.file_path.parent.name or block.asset_type
                        label_base = self._format_recipe_label(label_candidate)
                        mod_items.append((f"{label_base} ({block.legacy_id})", block.legacy_id))
                self._recipe_item_cache[cache_key] = mod_items
            for label, legacy_id in self._recipe_item_cache[cache_key]:
                if (label, legacy_id) in seen:
                    continue
                seen.add((label, legacy_id))
                items.append((label, legacy_id))
        items.sort(key=lambda item: item[0].lower())
        return items

    def _schedule_recipe_item_refresh(self) -> None:
        if self._recipe_refresh_after_id is not None:
            self.after_cancel(self._recipe_refresh_after_id)
        self._recipe_refresh_after_id = self.after(120, self._refresh_recipe_item_options)

    def _refresh_recipe_item_options(self) -> None:
        self._recipe_refresh_after_id = None
        item_entries = self._get_selected_recipe_items()
        self.recipe_items = [label for label, _ in item_entries]
        self.recipe_item_map = {label: legacy_id for label, legacy_id in item_entries}
        for row in getattr(self, "recipe_rows", []):
            row.update_options(self.recipe_items, self.recipe_item_map)
        self.recipe_status_label.config(text=f"{len(self.recipe_rows)} recipes, {len(self.recipe_items)} selectable items")

    def on_add_recipe(self) -> None:
        if not self.recipe_items:
            messagebox.showwarning("No available recipe items", "Select at least one compatible item source in the IDs tab before creating recipes.")
            return
        self._add_recipe_row()

    def _add_recipe_row(self) -> None:
        row = RecipeRow(
            self.recipe_inner_frame,
            self.recipe_items,
            self.recipe_item_map,
            self._update_recipe_status,
            self._remove_recipe_row,
        )
        self.recipe_rows.append(row)
        row.set_recipe_name(f"RecipePatch{len(self.recipe_rows)}")
        row.put_in_parent(len(self.recipe_rows) - 1)
        self._update_recipe_status()

    def _remove_recipe_row(self, row: RecipeRow) -> None:
        if row not in self.recipe_rows:
            return
        self.recipe_rows.remove(row)
        row.container.destroy()
        for index, recipe_row in enumerate(self.recipe_rows):
            recipe_row.put_in_parent(index)
        self._update_recipe_status()

    def _update_recipe_status(self) -> None:
        if not getattr(self, "recipe_rows", None):
            self.recipe_status_label.config(text="No recipes defined")
            return
        self.recipe_status_label.config(text=f"{len(self.recipe_rows)} recipes, {len(self.recipe_items)} selectable items")

    def _find_latest_mapping_file(self) -> Optional[Path]:
        if not self.last_output_path or not self.last_output_path.exists():
            return None
        search_roots = [self.last_output_path]
        if self.last_output_path.name.lower() == "compatibilitypatch":
            search_roots.append(self.last_output_path.parent)
        for pattern in ["mapping_*.json", "mapping_*.txt"]:
            candidates: List[Path] = []
            for root in search_roots:
                candidates.extend(root.glob(pattern))
            if candidates:
                return sorted(candidates)[-1]
        return None

    def _get_recipe_output_root(self) -> Optional[Path]:
        if not self.last_output_path or not self.last_output_path.exists():
            return None
        patch_root = self.last_output_path
        if patch_root.name.lower() != "compatibilitypatch":
            compatibility_patch = patch_root / "CompatibilityPatch"
            if compatibility_patch.exists():
                patch_root = compatibility_patch

        content_dirs = [path for path in patch_root.iterdir() if path.is_dir()]
        if not content_dirs:
            return patch_root
        with_items = [path for path in content_dirs if (path / "Items").exists()]
        return sorted(with_items or content_dirs, key=lambda path: path.name.lower())[0]

    def _normalize_recipe_name(self, raw_name: str, fallback_index: int) -> str:
        name = raw_name.strip() or f"RecipePatch{fallback_index}"
        return re.sub(r"[^A-Za-z0-9_-]+", "_", name)

    def _gather_recipe_definitions(self) -> List[dict]:
        definitions = []
        for row_index, row in enumerate(self.recipe_rows, start=1):
            ingredient_label_groups = row.get_ingredient_label_groups()
            output_label = row.get_output_label()
            if len(ingredient_label_groups) < 1 or not output_label:
                continue
            if output_label not in self.recipe_item_map:
                continue
            tool_indices = row.get_tool_indices()
            ingredient_amounts = row.get_ingredient_amounts()
            label_combinations = list(product(*ingredient_label_groups))
            base_name = self._normalize_recipe_name(row.get_recipe_name(), row_index)
            for combination_index, label_combination in enumerate(label_combinations, start=1):
                ingredient_ids = [
                    self.recipe_item_map[label]
                    for label in label_combination
                    if label in self.recipe_item_map
                ]
                if len(ingredient_ids) < 1:
                    continue
                definitions.append(
                    {
                        "ingredients": ingredient_ids,
                        "result": self.recipe_item_map[output_label],
                        "tool_indices": tool_indices,
                        "ingredient_amounts": ingredient_amounts,
                        "ingredient_labels": list(label_combination),
                        "output_amount": row.get_output_amount(),
                        "output_label": output_label,
                        "recipe_name": base_name,
                        "description": row.get_description(),
                        "patch_name": base_name if len(label_combinations) == 1 else f"{base_name}_{combination_index}",
                    }
                )
        return definitions

    def on_generate_recipes(self) -> None:
        output_location = self._get_recipe_output_root()
        if output_location is None:
            messagebox.showwarning(
                "Run ID patch first",
                "Run the ID conflict patcher before generating recipes so they can be added to the generated compatibility patch.",
            )
            self.notebook.select(self.ids_tab)
            return
        definitions = self._gather_recipe_definitions()
        if not definitions:
            messagebox.showwarning("No valid recipes", "Define at least one recipe with an ingredient and an output.")
            return
        mapping_file = self._find_latest_mapping_file()
        self._set_recipe_ui_state(running=True)
        self._append_log(f"Starting recipe generation for {len(definitions)} recipe file(s)...")
        self.status_label.config(text="Generating recipes...")
        self.progress_bar.config(value=0)

        def recipe_job() -> None:
            try:
                self._enqueue_log("Loading recipe ID availability and existing IDs...")
                builder = RecipeBuilder(
                    workshop_root=self.workshop_path or Path.cwd(),
                    csv_path=self.csv_path or Path.cwd(),
                    game_root=self.game_dir,
                    mapping_json=mapping_file,
                    output_root=output_location,
                    recipes_csv_root=self.last_output_path,
                    export_recipes_csv=self.export_recipes_csv_var.get(),
                    imported_recipes_csv=self.recipes_csv_path,
                    log_callback=self._enqueue_log,
                    progress_callback=self._enqueue_progress,
                )
                patch_root = builder.build_recipes(definitions)
                self.worker_queue.put(("recipes_finished", (str(patch_root), len(definitions))))
            except Exception as exc:
                self.worker_queue.put(("recipes_error", str(exc)))

        threading.Thread(target=recipe_job, daemon=True).start()

    def _set_recipe_ui_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for widget in [self.add_recipe_button, self.generate_recipes_button, self.export_recipes_csv_checkbox, self.import_recipes_csv_button]:
            widget.state([state] if state == "disabled" else ["!disabled"])
        if not running and self._get_recipe_output_root() is None:
            self.generate_recipes_button.state(["disabled"])

    def _on_mods_tree_double_click(self, event: Any) -> None:
        item_id = self.mods_tree.identify_row(event.y)
        if not item_id:
            return
        mod = next((m for m in self.mods if m.workshop_id == item_id), None)
        if not mod:
            return

        new_title = simpledialog.askstring(
            "Edit Mod Name",
            "Enter a friendly mod name for this workshop mod:",
            initialvalue=mod.title,
            parent=self,
        )
        if new_title is None:
            return
        new_title = new_title.strip() or "Unknown"
        mod.title = new_title
        mod.display_name = f"{new_title} ({mod.workshop_id})"
        self.mods_tree.set(item_id, "title", new_title)
        self.scanner.set_override(mod.workshop_id, new_title)
        self.status_label.config(text=f"Saved custom title for {mod.workshop_id}.")

    def on_start(self) -> None:
        selected_mods = self._get_selected_mods()
        if not selected_mods:
            messagebox.showwarning("No mods selected", "Please select at least one mod to patch.")
            return
        if not self.workshop_path or not self.csv_path or not self.log_path:
            messagebox.showwarning("Missing inputs", "Select the workshop folder, CSV file, and Client.log before starting.")
            return

        self._set_ui_state(running=True)
        self._append_log("Starting patch generation...")
        self.progress_bar.config(value=0)
        self.status_label.config(text="Starting patch generation...")

        self.worker = PatchWorker(
            workshop_root=self.workshop_path,
            client_log_path=self.log_path,
            csv_path=self.csv_path,
            selected_mods=selected_mods,
            output_root=self.output_path or self.output_base,
            dry_run=self.dry_run_var.get(),
            export_csv=self.export_csv_var.get(),
            export_json=self.export_json_var.get(),
            game_root=self.game_dir,
            log_callback=self._enqueue_log,
            progress_callback=self._enqueue_progress,
            finished_callback=self._enqueue_finished,
            error_callback=self._enqueue_error,
        )
        self.worker.start()

    def _set_ui_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for widget in [self.game_button, self.workshop_button, self.csv_button, self.log_button, self.output_button, self.start_button]:
            widget.state([state] if state == "disabled" else ["!disabled"])
        self.mods_tree.state([state] if state == "disabled" else ["!disabled"])
        if running:
            self.open_output_button.state(["disabled"])

    def _append_log(self, text: str) -> None:
        self.log_output.configure(state="normal")
        self.log_output.insert(tk.END, text + "\n")
        self.log_output.see(tk.END)
        self.log_output.configure(state="disabled")

    def _enqueue_log(self, message: str) -> None:
        self.worker_queue.put(("log", message))

    def _enqueue_progress(self, current: int, total: int, message: str) -> None:
        self.worker_queue.put(("progress", (current, total, message)))

    def _enqueue_finished(self, success: bool, output_path: str, report: object) -> None:
        self.worker_queue.put(("finished", (success, output_path, report)))

    def _enqueue_error(self, message: str) -> None:
        self.worker_queue.put(("error", message))

    def _poll_worker_queue(self) -> None:
        try:
            while True:
                item = self.worker_queue.get_nowait()
                self._handle_worker_message(item)
        except Empty:
            pass
        self.after(100, self._poll_worker_queue)

    def _handle_worker_message(self, item: Any) -> None:
        kind, payload = item
        if kind == "log":
            self._append_log(payload)
        elif kind == "progress":
            current, total, message = payload
            if total > 0:
                value = min(100, int(current / total * 100))
            else:
                value = 0
            self.progress_bar.config(value=value)
            self.status_label.config(text=message)
        elif kind == "finished":
            success, output_path, report = payload
            self._append_log("Finished patch generation.")
            self.status_label.config(text="Patch generation complete")
            self._set_ui_state(running=False)
            self.open_output_button.state(["!disabled"])
            self.last_output_path = Path(output_path)
            self.generate_recipes_button.state(["!disabled"])
            messagebox.showinfo("Completed", f"Patch output folder: {output_path}")
            self._open_folder(self.last_output_path)
        elif kind == "error":
            self._append_log(f"[ERROR] {payload}")
            self.status_label.config(text="Error occurred")
            messagebox.showerror("Error", payload)
            self._set_ui_state(running=False)
        elif kind == "recipes_finished":
            patch_root, count = payload
            self._append_log(f"Created {count} recipe file(s) in {patch_root}")
            self.progress_bar.config(value=100)
            self.status_label.config(text="Recipes generated")
            self._set_recipe_ui_state(running=False)
            messagebox.showinfo("Recipes Generated", f"Created {count} recipes in:\n{patch_root}")
            self._open_folder(Path(patch_root))
        elif kind == "recipes_error":
            self._append_log(f"[ERROR] Recipe generation failed: {payload}")
            self.status_label.config(text="Recipe generation failed")
            self._set_recipe_ui_state(running=False)
            messagebox.showerror("Recipe Generation Failed", payload)

    def on_open_output_folder(self) -> None:
        if self.last_output_path and self.last_output_path.exists():
            self._open_folder(self.last_output_path)

    def _open_folder(self, folder: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            messagebox.showwarning("Open folder", f"Unable to open folder: {folder}")


def run_gui() -> None:
    app = MainWindow()
    app.mainloop()
