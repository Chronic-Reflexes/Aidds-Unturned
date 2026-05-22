import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .id_manager import IDManager
from .parsers import DatAssetScanner


@dataclass
class RecipeDefinition:
    ingredients: List[int]
    result: int
    tool_returns: bool = False
    recipe_id: Optional[int] = None
    output_amount: int = 1


RECIPES_CSV_FIELDS = [
    "Source",
    "AssetID",
    "RecipeID",
    "RecipeKey",
    "Name",
    "FilePath",
    "CustomRecipeName",
    "Description",
    "IngredientLabels",
    "IngredientAmounts",
    "ToolIndices",
    "OutputLabel",
    "OutputAmount",
]


class RecipeBuilder:
    def __init__(
        self,
        workshop_root: Path,
        csv_path: Path,
        game_root: Optional[Path] = None,
        mapping_json: Optional[Path] = None,
        output_root: Optional[Path] = None,
        recipes_csv_root: Optional[Path] = None,
        export_recipes_csv: bool = False,
        imported_recipes_csv: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.workshop_root = workshop_root
        self.csv_path = csv_path
        self.game_root = game_root
        self.mapping_json = mapping_json
        self.output_root = (output_root or Path.cwd()).resolve()
        self.recipes_csv_root = (recipes_csv_root or self.output_root).resolve()
        self.export_recipes_csv = export_recipes_csv
        self.imported_recipes_csv = imported_recipes_csv
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.output_root.mkdir(parents=True, exist_ok=True)

        self.id_manager = IDManager(min_allocatable_id=8001)
        if self.csv_path and self.csv_path.exists():
            self._log(f"Loading recipe IDs from {self.csv_path}")
            self.id_manager.load_from_csv(self.csv_path)
            self._emit_progress(5, 100, "Loaded recipe ID availability")
        self._log("Scanning existing IDs for recipe allocation")
        existing_ids = self._collect_existing_ids()
        self.id_manager.mark_existing_ids(existing_ids)
        self._log(f"Collected {len(existing_ids)} existing IDs for recipe allocation")
        self.mapping = self._load_mapping(self.mapping_json) if self.mapping_json else {}
        if self.mapping_json:
            self._log(f"Loaded recipe ID mapping from {self.mapping_json}")

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
            self._log(f"Scanning recipe IDs in {self.workshop_root}")
            scanner = DatAssetScanner(self.workshop_root)
            ids.update(scanner.collect_used_ids())
        if self.game_root and self.game_root.exists():
            scan_root = self.game_root / "Bundles"
            if not scan_root.exists():
                scan_root = self.game_root
            self._log(f"Scanning recipe IDs in {scan_root}")
            scanner = DatAssetScanner(scan_root)
            ids.update(scanner.collect_used_ids())
        return ids

    def build_recipes(self, definitions: List[dict]) -> Path:
        target_root = self.output_root / "Items" / "Supplies"
        target_root.mkdir(parents=True, exist_ok=True)

        used_names: set[str] = set()
        generated_files: List[Path] = []
        generated_custom_rows: List[Dict[str, str]] = []
        total = max(1, len(definitions))
        for index, definition in enumerate(definitions, start=1):
            self._emit_progress(index, total, f"Writing recipe {index}/{total}")
            recipe_id = definition.get("recipe_id") or self.id_manager.allocate_id()
            patch_name = self._unique_patch_name(
                self._sanitize_patch_name(definition.get("patch_name") or f"RecipePatch{index}"),
                used_names,
            )
            patch_dir = target_root / patch_name
            patch_dir.mkdir(parents=True, exist_ok=True)

            dat_path = patch_dir / f"{patch_name}.dat"
            dat_path.write_text(self._render_dat(recipe_id, definition), encoding="utf-8")
            generated_files.append(dat_path)
            generated_custom_rows.append(self._build_custom_recipe_csv_row(definition, recipe_id, patch_name, dat_path))

            blueprint_anchor = patch_dir / "Blueprints" / "0" / "Blueprint.dat"
            blueprint_anchor.parent.mkdir(parents=True, exist_ok=True)
            blueprint_anchor.write_text("", encoding="utf-8")

            english_path = patch_dir / "english.dat"
            english_path.write_text(self._render_english(recipe_id, patch_name, definition), encoding="utf-8")

        if self.export_recipes_csv:
            recipes_csv = self.recipes_csv_root / "Recipes.csv"
            self.recipes_csv_root.mkdir(parents=True, exist_ok=True)
            self._write_recipes_csv(recipes_csv, generated_files, generated_custom_rows)
            self._log(f"Saved Recipes.csv to {recipes_csv}")

        return target_root

    def _build_custom_recipe_csv_row(self, definition: dict, recipe_id: int, patch_name: str, dat_path: Path) -> Dict[str, str]:
        return {
            "Source": "generated",
            "AssetID": str(recipe_id),
            "RecipeID": "0",
            "RecipeKey": f"{recipe_id}:0",
            "Name": patch_name,
            "FilePath": str(dat_path),
            "CustomRecipeName": str(definition.get("recipe_name") or patch_name),
            "Description": str(definition.get("description") or ""),
            "IngredientLabels": json.dumps(definition.get("ingredient_label_groups") or definition.get("ingredient_labels", [])),
            "IngredientAmounts": json.dumps(definition.get("ingredient_amounts", [])),
            "ToolIndices": json.dumps(definition.get("tool_indices", [])),
            "OutputLabel": str(definition.get("output_label") or ""),
            "OutputAmount": str(definition.get("output_amount") or 1),
        }

    def _write_recipes_csv(self, path: Path, generated_files: List[Path], generated_custom_rows: List[Dict[str, str]]) -> None:
        rows = self._collect_recipe_rows(generated_files, generated_custom_rows)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=RECIPES_CSV_FIELDS,
            )
            writer.writeheader()
            writer.writerows(rows)

    def _collect_recipe_rows(self, generated_files: List[Path], generated_custom_rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in self._load_imported_recipe_rows():
            key = (row["RecipeKey"], row["FilePath"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

        for row in generated_custom_rows:
            key = (row["RecipeKey"], row["FilePath"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

        generated_set = {path.resolve() for path in generated_files}
        roots: List[Path] = []
        if self.workshop_root and self.workshop_root.exists():
            roots.append(self.workshop_root)
        if self.game_root and self.game_root.exists():
            bundles_root = self.game_root / "Bundles"
            roots.append(bundles_root if bundles_root.exists() else self.game_root)
        roots.append(self.output_root / "Items")

        for root in roots:
            if not root.exists():
                continue
            for dat_path in root.rglob("*.dat"):
                row_items = self._parse_recipe_rows_from_dat(dat_path, generated_set)
                for row in row_items:
                    key = (row["RecipeKey"], row["FilePath"])
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
        rows.sort(key=lambda row: (row["Source"] != "generated", row["Name"].lower(), row["RecipeID"]))
        return rows

    def _load_imported_recipe_rows(self) -> List[Dict[str, str]]:
        if not self.imported_recipes_csv or not self.imported_recipes_csv.exists():
            return []
        rows: List[Dict[str, str]] = []
        try:
            with self.imported_recipes_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    recipe_id = (row.get("RecipeID") or "").strip()
                    if not recipe_id:
                        continue
                    asset_id = (row.get("AssetID") or "").strip()
                    recipe_key = (row.get("RecipeKey") or "").strip() or (f"{asset_id}:{recipe_id}" if asset_id else recipe_id)
                    rows.append(
                        {
                            "Source": (row.get("Source") or "imported").strip() or "imported",
                            "AssetID": asset_id,
                            "RecipeID": recipe_id,
                            "RecipeKey": recipe_key,
                            "Name": (row.get("Name") or "").strip(),
                            "FilePath": (row.get("FilePath") or "").strip(),
                            "CustomRecipeName": (row.get("CustomRecipeName") or "").strip(),
                            "Description": (row.get("Description") or "").strip(),
                            "IngredientLabels": (row.get("IngredientLabels") or "").strip(),
                            "IngredientAmounts": (row.get("IngredientAmounts") or "").strip(),
                            "ToolIndices": (row.get("ToolIndices") or "").strip(),
                            "OutputLabel": (row.get("OutputLabel") or "").strip(),
                            "OutputAmount": (row.get("OutputAmount") or "").strip(),
                        }
                    )
            self._log(f"Imported {len(rows)} tracked recipe row(s) from {self.imported_recipes_csv}")
        except Exception as exc:
            self._log(f"Warning: failed to import Recipes.csv: {exc}")
        return rows

    def _parse_recipe_rows_from_dat(self, dat_path: Path, generated_files: set[Path]) -> List[Dict[str, str]]:
        try:
            lines = dat_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except Exception:
            return []

        asset_id = ""
        recipe_ids: set[int] = set()
        has_blueprints = False
        for line in lines:
            stripped = line.strip()
            id_match = re.match(r"^ID\s+(\d+)$", stripped)
            if id_match and not asset_id:
                asset_id = id_match.group(1)
                continue
            if re.match(r"^Blueprints\s+\d+$", stripped):
                has_blueprints = True
                continue
            blueprint_match = re.match(r"^Blueprint_(\d+)_", stripped)
            if blueprint_match:
                recipe_ids.add(int(blueprint_match.group(1)))

        if not has_blueprints or not recipe_ids:
            return []

        english_values = self._read_english_values(dat_path)
        name = english_values.get("Name") or dat_path.stem
        source = "generated" if dat_path.resolve() in generated_files else "existing"
        return [
            {
                "Source": source,
                "AssetID": asset_id,
                "RecipeID": str(recipe_id),
                "RecipeKey": f"{asset_id}:{recipe_id}" if asset_id else str(recipe_id),
                "Name": name,
                "FilePath": str(dat_path),
                "CustomRecipeName": "",
                "Description": english_values.get("Description", ""),
                "IngredientLabels": "",
                "IngredientAmounts": "",
                "ToolIndices": "",
                "OutputLabel": "",
                "OutputAmount": "",
            }
            for recipe_id in sorted(recipe_ids)
        ]

    def _read_english_values(self, dat_path: Path) -> Dict[str, str]:
        english_path = dat_path.parent / "english.dat"
        if not english_path.exists():
            return {}
        values: Dict[str, str] = {}
        try:
            for line in english_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                match = re.match(r"^(Name|Description)\s+(.+)$", line.strip())
                if match:
                    values[match.group(1)] = match.group(2).strip()
        except Exception:
            return {}
        return values

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)

    def _emit_progress(self, current: int, total: int, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(current, total, message)

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
        ingredient_amounts = definition.get("ingredient_amounts", [])
        output_id = self._resolve_id(definition.get("result", 0))
        output_amount = int(definition.get("output_amount") or 1)
        tool_indices = set(definition.get("tool_indices", []))
        tool_index = next((index for index in sorted(tool_indices) if index < len(ingredients)), None)

        lines: List[str] = [
            "Type Supply",
            f"ID {recipe_id}",
            "",
            "Database_Asset_Bundle_Override core.masterbundle",
            "Bundle_Override_Path /Items/Supplies/Scrap_Metal",
            "",
            "Blueprints 1",
            "Blueprint_0_Type Supply",
            f"Blueprint_0_Supplies {len(ingredients)}",
        ]

        supply_index = 0
        for index, item_id in enumerate(ingredients):
            amount = int(ingredient_amounts[index]) if index < len(ingredient_amounts) else 1
            if index == tool_index:
                lines.append(f"Blueprint_0_Tool {item_id}")
                lines.append("Blueprint_0_Tool_Critical")
                continue
            lines.append(f"Blueprint_0_Supply_{supply_index}_ID {item_id}")
            lines.append(f"Blueprint_0_Supply_{supply_index}_Amount {max(1, amount)}")
            supply_index += 1

        lines.append(f"Blueprint_0_Product {output_id}")
        lines.append("Blueprint_0_Products 1")
        lines.append(f"Blueprint_0_Origin_0_Amount {max(1, output_amount)}")
        return "\n".join(lines) + "\n"

    def _clean_english_value(self, value: object) -> str:
        return " ".join(str(value).split()).strip()

    def _render_english(self, recipe_id: int, patch_name: str, definition: dict) -> str:
        recipe_name = self._clean_english_value(definition.get("recipe_name") or patch_name)
        description = self._clean_english_value(definition.get("description") or f"{recipe_name} Generated recipe item {recipe_id}")
        lines = [
            f"Name {recipe_name}",
            f"Description {description}",
        ]
        return "\n".join(lines) + "\n"

    def _resolve_id(self, item_id: int) -> int:
        return self.mapping.get(item_id, item_id)
