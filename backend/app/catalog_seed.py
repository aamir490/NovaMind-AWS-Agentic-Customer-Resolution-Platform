"""Deterministic demo product data shared by catalog and inventory services."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class ProductCategory(StrEnum):
    LAPTOPS = "laptops"
    DISPLAYS = "displays"
    DOCKS_HUBS = "docks_hubs"
    INPUT_DEVICES = "input_devices"
    AUDIO = "audio"
    STORAGE = "storage"
    BAGS_PROTECTION = "bags_protection"
    WARRANTIES_SERVICES = "warranties_services"


@dataclass(frozen=True)
class ProductSeed:
    id: UUID
    sku: str
    name: str
    description: str
    amount: Decimal
    category: ProductCategory
    image_path: str
    stock_quantity: int


_IMAGES = {
    ProductCategory.LAPTOPS: "/products/laptops.svg",
    ProductCategory.DISPLAYS: "/products/displays.svg",
    ProductCategory.DOCKS_HUBS: "/products/docks-hubs.svg",
    ProductCategory.INPUT_DEVICES: "/products/input-devices.svg",
    ProductCategory.AUDIO: "/products/audio.svg",
    ProductCategory.STORAGE: "/products/storage.svg",
    ProductCategory.BAGS_PROTECTION: "/products/bags-protection.svg",
    ProductCategory.WARRANTIES_SERVICES: "/products/warranties-services.svg",
}


def _id(sequence: int) -> UUID:
    return UUID(f"10000000-0000-4000-8000-{sequence:012d}")


# These five records preserve the original UUIDs, SKUs, names, descriptions,
# prices and availability. Stock for LAP-1/LAP-2 matches the original
# BusinessOperations inventory exactly.
_EXISTING = (
    ProductSeed(_id(1), "LAP-1", "ProBook Laptop 15",
                "15-inch business laptop with Intel Core i7, 16 GB RAM, 512 GB SSD.",
                Decimal("1299.99"), ProductCategory.LAPTOPS, _IMAGES[ProductCategory.LAPTOPS], 5),
    ProductSeed(_id(2), "LAP-2", "ProBook Laptop 13",
                "13-inch ultrabook with Intel Core i5, 8 GB RAM, 256 GB SSD.",
                Decimal("999.99"), ProductCategory.LAPTOPS, _IMAGES[ProductCategory.LAPTOPS], 0),
    ProductSeed(_id(3), "ACC-1", "USB-C Docking Station",
                "7-port USB-C dock with HDMI, DisplayPort, Ethernet, and 100 W PD.",
                Decimal("149.99"), ProductCategory.DOCKS_HUBS, _IMAGES[ProductCategory.DOCKS_HUBS], 1000),
    ProductSeed(_id(4), "WAR-1", "Extended Warranty (3 Year)",
                "Three-year extended hardware warranty covering parts and labour.",
                Decimal("199.99"), ProductCategory.WARRANTIES_SERVICES,
                _IMAGES[ProductCategory.WARRANTIES_SERVICES], 250),
    ProductSeed(_id(5), "BAG-1", 'Laptop Sleeve 15"',
                "Water-resistant neoprene sleeve for 15-inch laptops.",
                Decimal("29.99"), ProductCategory.BAGS_PROTECTION,
                _IMAGES[ProductCategory.BAGS_PROTECTION], 40),
)


# category, SKU prefix, display stem, total products, existing products,
# starting price, deterministic price step
_CATEGORY_SPECS = (
    (ProductCategory.LAPTOPS, "LAP", "NovaBook Laptop", 15, 2, Decimal("749.99"), Decimal("75.00")),
    (ProductCategory.DISPLAYS, "DSP", "NovaView Display", 13, 0, Decimal("189.99"), Decimal("35.00")),
    (ProductCategory.DOCKS_HUBS, "DCK", "NovaLink Dock", 12, 1, Decimal("69.99"), Decimal("18.00")),
    (ProductCategory.INPUT_DEVICES, "INP", "NovaControl Input", 14, 0, Decimal("24.99"), Decimal("12.50")),
    (ProductCategory.AUDIO, "AUD", "NovaSound Audio", 13, 0, Decimal("39.99"), Decimal("22.00")),
    (ProductCategory.STORAGE, "STO", "NovaVault Storage", 13, 0, Decimal("49.99"), Decimal("28.00")),
    (ProductCategory.BAGS_PROTECTION, "PRT", "NovaGuard Protection", 12, 1, Decimal("19.99"), Decimal("9.00")),
    (ProductCategory.WARRANTIES_SERVICES, "SVC", "NovaCare Service", 8, 1, Decimal("79.99"), Decimal("40.00")),
)


def _generated_products() -> tuple[ProductSeed, ...]:
    products: list[ProductSeed] = []
    sequence = len(_EXISTING) + 1
    for category, prefix, stem, total, existing, base_price, price_step in _CATEGORY_SPECS:
        for ordinal in range(existing + 1, total + 1):
            stock = 0 if ordinal % 7 == 0 else 8 + ((ordinal * 11) % 43)
            products.append(ProductSeed(
                id=_id(sequence),
                sku=f"{prefix}-{ordinal}",
                name=f"{stem} {ordinal}",
                description=(f"Deterministic demonstration {stem.lower()} model {ordinal}, "
                             "for local catalog, ordering, and support workflows."),
                amount=base_price + price_step * (ordinal - 1),
                category=category,
                image_path=_IMAGES[category],
                stock_quantity=stock,
            ))
            sequence += 1
    return tuple(products)


PRODUCT_SEEDS: tuple[ProductSeed, ...] = _EXISTING + _generated_products()

if len(PRODUCT_SEEDS) != 100:
    raise RuntimeError("Catalog seed must contain exactly 100 products")
if len({product.id for product in PRODUCT_SEEDS}) != len(PRODUCT_SEEDS):
    raise RuntimeError("Catalog product IDs must be unique")
if len({product.sku for product in PRODUCT_SEEDS}) != len(PRODUCT_SEEDS):
    raise RuntimeError("Catalog SKUs must be unique")
