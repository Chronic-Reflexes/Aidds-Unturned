import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .models import WorkshopMod

HARDCODED_WORKSHOP_TITLES = {
    "3707778928": "California",
    "2483365750": "Kuwait",
    "2683620106": "Arid",
    "3711646503": "California 2",
}
EXCLUDED_WORKSHOP_IDS = set(HARDCODED_WORKSHOP_TITLES)


class WorkshopScanner:
    def __init__(self, logger=None):
        self.logger = logger
        self.overrides_file = Path(__file__).resolve().parent / "manual_titles.json"
        self.title_overrides = self._load_overrides()

    def _load_overrides(self) -> Dict[str, str]:
        if not self.overrides_file.exists():
            return {}
        try:
            data = json.loads(self.overrides_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                cleaned = {}
                for k, v in data.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        continue
                    value = v.strip()
                    if not value or value.lower() == "unknown":
                        continue
                    cleaned[str(k)] = value
                return cleaned
        except Exception:
            if self.logger:
                self.logger.warning("Unable to read title override file")
        return {}

    def _save_overrides(self) -> None:
        try:
            self.overrides_file.write_text(json.dumps(self.title_overrides, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            if self.logger:
                self.logger.warning("Unable to write title override file")

    def get_override(self, workshop_id: str) -> Optional[str]:
        return self.title_overrides.get(workshop_id)

    def set_override(self, workshop_id: str, title: str) -> None:
        clean_title = title.strip()
        if not clean_title or clean_title.lower() == "unknown":
            self.title_overrides.pop(str(workshop_id), None)
        else:
            self.title_overrides[str(workshop_id)] = clean_title
        self._save_overrides()

    def discover_workshop_mods(self, root_path: Path) -> List[WorkshopMod]:
        mods: Dict[str, WorkshopMod] = {}
        if (
            root_path.name.isdigit()
            and self._contains_dat_files(root_path)
            and not any(
                child.is_dir() and child.name.isdigit() for child in root_path.iterdir()
            )
        ):
            mod = self._load_mod(root_path)
            if mod:
                mods[mod.workshop_id] = mod
            return list(mods.values())

        for candidate in sorted(root_path.iterdir()):
            if not candidate.is_dir():
                continue
            if not candidate.name.isdigit():
                continue
            if candidate.name in EXCLUDED_WORKSHOP_IDS:
                continue
            mod = self._load_mod(candidate)
            if mod:
                mods[mod.workshop_id] = mod

        if not mods:
            for candidate in sorted(root_path.rglob("*")):
                if not candidate.is_dir() or not candidate.name.isdigit() or candidate.name in EXCLUDED_WORKSHOP_IDS:
                    continue
                if self._contains_dat_files(candidate):
                    mod = self._load_mod(candidate)
                    if mod:
                        mods[mod.workshop_id] = mod
        return list(mods.values())

    def _contains_dat_files(self, path: Path) -> bool:
        return any(path.rglob("*.dat"))

    def _load_mod(self, mod_path: Path) -> Optional[WorkshopMod]:
        workshop_id = mod_path.name
        title = self.get_override(workshop_id)
        if title is None:
            title = HARDCODED_WORKSHOP_TITLES.get(workshop_id)
        if title is None:
            title = None
            masterbundle_file = self._find_masterbundle(mod_path)
            if masterbundle_file is not None:
                try:
                    title_value = self._extract_title_from_masterbundle(masterbundle_file)
                    if title_value:
                        title = title_value
                except Exception:
                    if self.logger:
                        self.logger.warning(f"Unable to parse MasterBundle.dat for {workshop_id}")
            if title is None:
                title = "Unknown"
        display_name = f"{title} ({workshop_id})"
        return WorkshopMod(
            workshop_id=workshop_id,
            path=mod_path,
            title=title,
            display_name=display_name,
        )

    def _find_masterbundle(self, mod_path: Path, max_depth: int = 3) -> Optional[Path]:
        base_depth = len(mod_path.parts)
        for root, dirs, files in os.walk(mod_path):
            depth = len(Path(root).parts) - base_depth
            if depth > max_depth:
                dirs[:] = []
                continue
            if "MasterBundle.dat" in files:
                return Path(root) / "MasterBundle.dat"
        return None

    def _extract_title_from_masterbundle(self, file_path: Path) -> Optional[str]:
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")
        for line in raw_text.splitlines():
            match = re.match(r"^Asset_Bundle_Name\s+(.+)$", line)
            if match:
                bundle_name = match.group(1).strip()
                if bundle_name.lower().endswith(".masterbundle"):
                    bundle_name = bundle_name[: -len(".masterbundle")]
                prettified = bundle_name.replace("_", " ").strip()
                return prettified
        return None
