import csv
import json
import re
import secrets
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
    "RecipeType",
    "IngredientLabels",
    "IngredientAmounts",
    "ToolIndices",
    "OutputLabel",
    "OutputAmount",
    "SkillLevel",
    "Category",
    "CategoryGUID",
    "Workstation",
    "WorkstationGUID",
    "Map",
    "StateTransfer",
    "StateTransferDeleteAttachments",
    "Effect",
    "EffectGUID",
]

CRAFTING_ASSET_TYPE = "SDG.Unturned.CraftingAsset, Assembly-CSharp"
WRENCH_EFFECT_GUID = "84347b13028340b8976033c08675d458"
BLUEPRINT_CATEGORIES: Dict[str, str] = {
    "Ammunition": "d739926736374e5ba34b4ac6ffbb5c8f",
    "Apparel": "ebe755533bdd42d1871c3ac66b89530f",
    "Barricades": "31a59b5fec3f4ec5b2887b1ce4acb029",
    "Furniture": "b0c6cc0a8b4346be89aef697ecdb8e46",
    "Gear": "cdb2df24b76d4c6e9d8411c940d8337f",
    "Repair": "732ee6ffeb18418985cf4f9fde33dd11",
    "Salvage": "7ed29f9101ae4523a3b2e389414b7bd9",
    "Structures": "71d9e182c18b4aad8e87778e4f621995",
    "Supplies": "d089feb7e43f40c5a7dfcefc36998cfb",
    "Tools": "ad1804b6945145f3b308738b0b8ea447",
    "Utilities": "bfac6026305f4737a95fd275ebff65a6",
}
WORKSTATION_TAGS: Dict[str, str] = {
    "Chemical Mixing Capabilities": "99896da563a748148460c67b9962874f",
    "Color Dyeing Capabilities": "8e86b740dafc46f7bf98c5040c9b223e",
    "Enclosed Heat Source": "d2cc65b749e5477f95103601df89cdbc",
    "Heat Source": "20f30322bbcc4b01a4f116d22b24c21a",
    "Pottery Kiln": "192e071c94d1419b991a430d42fe2be3",
    "Kitchen Capabilities": "68816064e2ce44839c3f35da55033cba",
    "Sewing Capabilities": "2ac5ddc545a848008c0308d21f5d2e6b",
    "Workbench Capabilities": "7b82c125a5a54984b8bb26576b59e977",
}
SALVAGE_CATEGORY_GUID = BLUEPRINT_CATEGORIES["Salvage"]
SUPPLIES_CATEGORY_GUID = BLUEPRINT_CATEGORIES["Supplies"]


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
        self._log("CraftingAsset recipes use GUIDs; skipping legacy recipe ID allocation scan")
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
        target_root = self.output_root / "Crafting"
        target_root.mkdir(parents=True, exist_ok=True)

        used_names: set[str] = set()
        generated_files: List[Path] = []
        generated_custom_rows: List[Dict[str, str]] = []
        total = max(1, len(definitions))
        for index, definition in enumerate(definitions, start=1):
            self._emit_progress(index, total, f"Writing recipe {index}/{total}")
            patch_name = self._unique_patch_name(
                self._sanitize_patch_name(definition.get("patch_name") or f"RecipePatch{index}"),
                used_names,
            )
            patch_dir = target_root / patch_name
            patch_dir.mkdir(parents=True, exist_ok=True)

            dat_path = patch_dir / f"{patch_name}.asset"
            recipe_guid = self._new_guid()
            dat_path.write_text(self._render_crafting_asset(recipe_guid, definition), encoding="utf-8")
            generated_files.append(dat_path)
            generated_custom_rows.append(self._build_custom_recipe_csv_row(definition, recipe_guid, patch_name, dat_path))

        if self.export_recipes_csv:
            recipes_csv = self.recipes_csv_root / "Recipes.csv"
            self.recipes_csv_root.mkdir(parents=True, exist_ok=True)
            self._write_recipes_csv(recipes_csv, generated_files, generated_custom_rows)
            self._log(f"Saved Recipes.csv to {recipes_csv}")

        return target_root

    def _new_guid(self) -> str:
        return secrets.token_hex(16)

    def _build_custom_recipe_csv_row(self, definition: dict, recipe_guid: str, patch_name: str, dat_path: Path) -> Dict[str, str]:
        return {
            "Source": "generated",
            "AssetID": "",
            "RecipeID": "0",
            "RecipeKey": f"{recipe_guid}:0",
            "Name": patch_name,
            "FilePath": str(dat_path),
            "CustomRecipeName": str(definition.get("recipe_name") or patch_name),
            "Description": str(definition.get("description") or ""),
            "RecipeType": str(definition.get("recipe_type") or "use"),
            "IngredientLabels": json.dumps(definition.get("ingredient_label_groups") or definition.get("ingredient_labels", [])),
            "IngredientAmounts": json.dumps(definition.get("ingredient_amounts", [])),
            "ToolIndices": json.dumps(definition.get("tool_indices", [])),
            "OutputLabel": str(definition.get("output_label") or ""),
            "OutputAmount": str(definition.get("output_amount") or 1),
            "SkillLevel": str(self._normalize_skill_level(definition.get("skill_level", 0))),
            "Category": str(definition.get("category_label") or "Auto"),
            "CategoryGUID": str(definition.get("category_guid") or ""),
            "Workstation": str(definition.get("workstation_label") or "None"),
            "WorkstationGUID": str(definition.get("workstation_guid") or ""),
            "Map": str(definition.get("map_name") or ""),
            "StateTransfer": "1" if definition.get("state_transfer") else "0",
            "StateTransferDeleteAttachments": "1" if definition.get("state_transfer_delete_attachments") else "0",
            "Effect": str(definition.get("effect_label") or "Wrench"),
            "EffectGUID": str(definition.get("effect_guid") or WRENCH_EFFECT_GUID),
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
                            "RecipeType": (row.get("RecipeType") or "").strip(),
                            "IngredientLabels": (row.get("IngredientLabels") or "").strip(),
                            "IngredientAmounts": (row.get("IngredientAmounts") or "").strip(),
                            "ToolIndices": (row.get("ToolIndices") or "").strip(),
                            "OutputLabel": (row.get("OutputLabel") or "").strip(),
                            "OutputAmount": (row.get("OutputAmount") or "").strip(),
                            "SkillLevel": (row.get("SkillLevel") or "").strip(),
                            "Category": (row.get("Category") or "").strip(),
                            "CategoryGUID": (row.get("CategoryGUID") or "").strip(),
                            "Workstation": (row.get("Workstation") or "").strip(),
                            "WorkstationGUID": (row.get("WorkstationGUID") or "").strip(),
                            "Map": (row.get("Map") or "").strip(),
                            "StateTransfer": (row.get("StateTransfer") or "").strip(),
                            "StateTransferDeleteAttachments": (row.get("StateTransferDeleteAttachments") or "").strip(),
                            "Effect": (row.get("Effect") or "").strip(),
                            "EffectGUID": (row.get("EffectGUID") or "").strip(),
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
                "RecipeType": "",
                "IngredientLabels": "",
                "IngredientAmounts": "",
                "ToolIndices": "",
                "OutputLabel": "",
                "OutputAmount": "",
                "SkillLevel": "",
                "Category": "",
                "CategoryGUID": "",
                "Workstation": "",
                "WorkstationGUID": "",
                "Map": "",
                "StateTransfer": "",
                "StateTransferDeleteAttachments": "",
                "Effect": "Wrench",
                "EffectGUID": WRENCH_EFFECT_GUID,
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
        while name.lower() in used_names or (self.output_root / "Crafting" / name).exists():
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name.lower())
        return name

    def _render_crafting_asset(self, recipe_guid: str, definition: dict) -> str:
        raw_ingredient_guids = definition.get("ingredient_guids", [])
        ingredient_source_paths = definition.get("ingredient_source_paths", [])
        ingredient_guids: List[str] = []
        for index, raw_guid in enumerate(raw_ingredient_guids):
            guid = str(raw_guid).strip().strip('"')
            if not guid and index < len(ingredient_source_paths):
                guid = self._read_asset_guid(Path(ingredient_source_paths[index]))
            if guid:
                ingredient_guids.append(guid)
        ingredient_amounts = definition.get("ingredient_amounts", [])
        tool_indices = set(definition.get("tool_indices", []))
        output_guid = str(definition.get("output_guid") or "").strip().strip('"')
        if not output_guid:
            output_guid = self._read_asset_guid(Path(definition.get("output_source_path") or ""))
        output_amount = int(definition.get("output_amount") or 1)
        if not ingredient_guids or not output_guid:
            raise ValueError("CraftingAsset recipes require GUIDs for every input and the output item.")

        lines: List[str] = [
            f'Type "{CRAFTING_ASSET_TYPE}"',
            f"GUID {recipe_guid}",
            "",
            "Blueprints",
            "[",
            "\t{",
        ]
        recipe_type = definition.get("recipe_type")
        category_label, category_guid = self._resolve_category(definition, recipe_type)
        blueprint_name = self._sanitize_patch_name(definition.get("blueprint_name") or definition.get("patch_name") or definition.get("recipe_name") or "Recipe")
        lines.append(f"\t\tName {blueprint_name}")
        lines.append(f"\t\tCategoryTag \"{category_guid}\" // {category_label}")
        lines.extend(
            [
                "\t\tInputItems",
                "\t\t[",
            ]
        )
        uses_input_blocks = bool(tool_indices)
        for index, guid in enumerate(ingredient_guids):
            amount = int(ingredient_amounts[index]) if index < len(ingredient_amounts) else 1
            if uses_input_blocks:
                lines.extend(
                    [
                        "\t\t\t{",
                        f"\t\t\t\tID {guid}",
                        f"\t\t\t\tAmount {max(1, amount)}",
                        "\t\t\t}",
                    ]
                )
                if index in tool_indices:
                    lines.insert(-1, "\t\t\t\tDelete false")
            else:
                lines.append(f"\t\t\t{guid} x {max(1, amount)}")
        lines.extend(
            [
                "\t\t]",
                "\t\tOutputItems",
                "\t\t[",
                f"\t\t\t{output_guid} x {max(1, output_amount)}",
                "\t\t]",
            ]
        )
        map_name = self._clean_map_name(definition.get("map_name") or "")
        if map_name:
            lines.append(f"\t\tMap {map_name}")
        if definition.get("state_transfer"):
            lines.append("\t\tStateTransfer true")
            if definition.get("state_transfer_delete_attachments"):
                lines.append("\t\tStateTransfer_DeleteAttachments true")
        skill_level = self._normalize_skill_level(definition.get("skill_level", 0))
        if skill_level > 0:
            lines.extend(
                [
                    "\t\tSkill Craft",
                    f"\t\tSkill_Level {skill_level}",
                ]
            )
        workstation_label, workstation_guid = self._resolve_workstation(definition)
        if workstation_guid:
            lines.extend(
                [
                    "\t\tRequiresNearbyCraftingTags",
                    "\t\t[",
                    f"\t\t\t\"{workstation_guid}\" // {workstation_label}",
                    "\t\t]",
                ]
            )
        effect_guid = self._clean_guid(definition.get("effect_guid") or WRENCH_EFFECT_GUID) or WRENCH_EFFECT_GUID
        effect_label = self._clean_effect_label(definition.get("effect_label") or "Wrench")
        lines.append(f"\t\tEffect \"{effect_guid}\" // {effect_label}")
        lines.extend(
            [
                "\t}",
                "]",
            ]
        )
        return "\n".join(lines) + "\n"

    def _clean_map_name(self, value: object) -> str:
        return re.sub(r"[^A-Za-z0-9 _'-]+", "", str(value or "")).strip()

    def _clean_effect_label(self, value: object) -> str:
        return re.sub(r"[^A-Za-z0-9 _'()./-]+", "", str(value or "")).strip() or "Effect"

    def _clean_guid(self, value: object) -> str:
        text = str(value or "").strip().strip('"').lower()
        return text if re.fullmatch(r"[0-9a-f]{32}", text) else ""

    def _normalize_skill_level(self, value: object) -> int:
        try:
            level = int(str(value).strip())
        except Exception:
            level = 0
        return max(0, min(3, level))

    def _resolve_category(self, definition: dict, recipe_type: object) -> tuple[str, str]:
        raw_label = str(definition.get("category_label") or "").strip()
        raw_guid = str(definition.get("category_guid") or "").strip().strip('"')
        if raw_guid:
            label = raw_label or self._label_for_guid(BLUEPRINT_CATEGORIES, raw_guid) or "Custom"
            return label, raw_guid
        if raw_label in BLUEPRINT_CATEGORIES:
            return raw_label, BLUEPRINT_CATEGORIES[raw_label]
        if recipe_type == "salvage":
            return "Salvage", SALVAGE_CATEGORY_GUID
        return "Supplies", SUPPLIES_CATEGORY_GUID

    def _resolve_workstation(self, definition: dict) -> tuple[str, str]:
        raw_label = str(definition.get("workstation_label") or "").strip()
        raw_guid = str(definition.get("workstation_guid") or "").strip().strip('"')
        if raw_guid:
            label = raw_label or self._label_for_guid(WORKSTATION_TAGS, raw_guid) or "Custom"
            return label, raw_guid
        if raw_label in WORKSTATION_TAGS:
            return raw_label, WORKSTATION_TAGS[raw_label]
        return "", ""

    def _label_for_guid(self, values: Dict[str, str], guid: str) -> str:
        for label, value in values.items():
            if value == guid:
                return label
        return ""

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

    def _read_asset_guid(self, path: Path) -> str:
        if not path or not path.exists():
            return ""
        try:
            for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                match = re.match(r'^\s*"?GUID"?\s+"?([0-9A-Fa-f]{32})"?', line)
                if match:
                    return match.group(1)
        except Exception:
            return ""
        return ""

    def _resolve_id(self, item_id: int) -> int:
        return self.mapping.get(item_id, item_id)
