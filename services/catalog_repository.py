import csv
from pathlib import Path


class CatalogRepository:
    def __init__(self, data_path: Path | None = None):
        #hardcoding for now for ease of development. Will remove later.
        self.data_path = data_path or Path(__file__).resolve().parents[1] / "vendor_databases"

    def load_products(self, vendor_id: int):
        file_path = self.data_path / str(vendor_id) / "products.csv"

        with file_path.open(newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))
