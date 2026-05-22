import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .id_manager import IDManager
from .parsers import DatAssetScanner


@dataclass
class RecipeDefinition:
    ingredients: List[int]
    result: int
    tool_returns: bool = False
    recipe_id: Optional[int] = None
    output_amount: int = 1


class RecipeBuilder:
    def __init__(
        self,
        workshop_root: Path,
        csv_path: Path,
        game_root: Optional[Path] = None,
        mapping_json: Optional[Path] = None,
        output_root: Optional[Path] = None,
    ):
        self.workshop_root = workshop_root
        self.csv_path = csv_path
        self.game_root = game_root
        self.mapping_json = mapping_json
        self.output_root = (output_root or Path.cwd()).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

        self.id_manager = IDManager(min_allocatable_id=8001)
        if self.csv_path and self.csv_path.exists():
            self.id_manager.load_from_csv(self.csv_path)
        existing_ids = self._collect_existing_ids()
        self.id_manager.mark_existing_ids(existing_ids)
        self.mapping = self._load_mapping(self.mapping_json) if self.mapping_json else {}

    def _load_mapping(self, mapping_json: Path) -> Dict[int, int]:
        try:
            if mapping_json.suffix.lower() == ".txt":
                mapping: Dict[int, int] = {}
                for line in mapping_json.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = re.match(r"\s*(\d+)\s*->\s*(\d+)\s*$", line)
                    if match:
                        mapping[int(match.group(1))] = int(match.group(2))
                return mapping

            data = json.loads(mapping_json.read_text(encoding="utf-8"))
            mapping: Dict[int, int] = {}
            for record in data:
                source_id = int(record.get("source_id", 0))
                target_id = int(record.get("target_id", 0))
                if source_id and target_id:
                    mapping[source_id] = target_id
            return mapping
        except Exception:
            return {}

    def _collect_existing_ids(self) -> set[int]:
        ids: set[int] = set()
        if self.workshop_root and self.workshop_root.exists():
            scanner = DatAssetScanner(self.workshop_root)
            ids.update(scanner.collect_used_ids())
        if self.game_root and self.game_root.exists():
            scanner = DatAssetScanner(self.game_root)
            ids.update(scanner.collect_used_ids())
        return ids

    def build_recipes(self, definitions: List[dict]) -> Path:
        target_root = self.output_root / "Items" / "Supplies"
        target_root.mkdir(parents=True, exist_ok=True)

        used_names: set[str] = set()
        for index, definition in enumerate(definitions, start=1):
            recipe_id = definition.get("recipe_id") or self.id_manager.allocate_id()
            patch_name = self._unique_patch_name(
                self._sanitize_patch_name(definition.get("patch_name") or f"RecipePatch{index}"),
                used_names,
            )
            patch_dir = target_root / patch_name
            patch_dir.mkdir(parents=True, exist_ok=True)

            dat_path = patch_dir / f"{patch_name}.dat"
            dat_path.write_text(self._render_dat(recipe_id, definition), encoding="utf-8")

            english_path = patch_dir / "english.dat"
            english_path.write_text(self._render_english(recipe_id, patch_name), encoding="utf-8")

        return target_root

    def _sanitize_patch_name(self, value: object) -> str:
        name = str(value).strip()
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
        return name or "RecipePatch"

    def _unique_patch_name(self, base_name: str, used_names: set[str]) -> str:
        name = base_name
        suffix = 2
        while name.lower() in used_names or (self.output_root / "Items" / "Supplies" / name).exists():
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name.lower())
        return name

    def _render_dat(self, recipe_id: int, definition: dict) -> str:
        ingredients = [self._resolve_id(i) for i in definition.get("ingredients", [])]
        output_id = self._resolve_id(definition.get("result", 0))
        tool_returns = bool(definition.get("tool_returns", False))

        lines: List[str] = [
            "Type Supply",
            f"ID {recipe_id}",
            f"Blueprints {len(ingredients)}",
        ]

        tool_indices = definition.get("tool_indices", [])

        for index, item_id in enumerate(ingredients):
            lines.append(f"Blueprint_{index}_Type Item")
            lines.append(f"Blueprint_{index}_ID {item_id}")
            lines.append(f"Blueprint_{index}_Amount 1")
            if index in tool_indices:
                lines.append(f"Blueprint_{index}_Tool true")

        lines.append(f"Output {output_id}")
        lines.append("Output_Amount 1")
        lines.append("Database_Asset_Bundle_Override core.masterbundle")
        lines.append("Bundle_Override_Path /Items/Supplies/Scrap_Metal")
        return "\n".join(lines) + "\n"

    def _render_english(self, recipe_id: int, patch_name: str) -> str:
        lines = [
            f"Name {patch_name} Recipe Patch {recipe_id}",
            f"Description {patch_name} Generated recipe item {recipe_id}",
        ]
        return "\n".join(lines) + "\n"

    def _resolve_id(self, item_id: int) -> int:
        return self.mapping.get(item_id, item_id)
