import json
from pathlib import Path

from services.catalog_repository import CatalogRepository


class normalization_service:
    def __init__(self, vendor_id: int, database_path: Path | None = None):
        self.vendor_id = vendor_id
        self.catalog = CatalogRepository()
        self.database_path = database_path or Path(__file__).resolve().parents[1] / "database"

    def run(self) -> bool:
        destination = self.database_path / str(self.vendor_id) / "products.json"

        if not destination.is_file():
            raise FileNotFoundError(
                f"Normalized catalog does not exist for vendor {self.vendor_id}: {destination}"
            )

        products: list[dict] = self.catalog.load_products(self.vendor_id)
        destination.write_text(
            json.dumps(products, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
