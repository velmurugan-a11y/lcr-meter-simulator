"""
lcr600.py — LCR-600 specific operator-interface model: function-key +
navigation-key driven full-screen menus (no Lap Pad needed), plus the
Point-of-Sale layer (tax categories, cash discounting) documented in the
skill's LCR-600 reference.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from .register_base import RegisterBase, RegisterError


@dataclass
class TaxLine:
    letter: str
    tax_type: str = "NOT_USED"   # NOT_USED | PERCENT | PER_UNIT | TAX_ON_TAX
    header_text: str = ""
    value: float = 0.0
    applies_to: list = field(default_factory=list)  # for TAX_ON_TAX: which other letters


@dataclass
class TaxCategory:
    number: int
    name: str = ""
    lines: list = field(default_factory=lambda: [TaxLine(chr(ord("A") + i)) for i in range(10)])


@dataclass
class CashDiscountTier:
    days_from_delivery: int = 0
    discount_pct: float = 0.0
    discount_per_unit: float = 0.0


@dataclass
class CashDiscountCategory:
    number: int
    name: str = ""
    apply_before_tax: bool = True
    tiers: list = field(default_factory=lambda: [CashDiscountTier() for _ in range(3)])


class LCR600Register(RegisterBase):
    PRODUCT_KEY = "lcr600"

    SELECTOR_POSITIONS = ["RUN", "STOP", "PRINT", "SHIFT_PRINT", "CALIBRATION"]
    SCREENS = ["DELIVERY", "GENERAL_SETUP_1", "GENERAL_SETUP_2", "POS_TAX",
               "POS_CASH_DISCOUNT", "PRODUCT_CAL", "SYSTEM_CAL", "DIAGNOSTICS"]

    def __init__(self):
        super().__init__()
        self.selector = "STOP"
        self.screen = "DELIVERY"
        self.field_cursor = 0
        self.tax_categories = [TaxCategory(i + 1) for i in range(16)]
        self.active_tax_category_idx = 0
        self.cash_discount_categories = [CashDiscountCategory(i + 1) for i in range(16)]
        self.active_cash_discount_idx = 0
        self.unit_id = ""
        self.ticket_header_lines = [""] * 12
        self.sale_number = 0
        self.ticket_number = 1
        self.preset_mode = "NONE"  # NONE | GROSS | NET | BOTH

    # ------------------------------------------------------------------ #
    def set_selector(self, position: str):
        if position not in self.SELECTOR_POSITIONS:
            raise RegisterError("RANGE ERROR")
        # See lcr2.py's identical comment: this must not depend on an
        # external poller having recently called tick().
        self.tick()
        self.selector = position
        if position == "RUN" and not self.delivery_active:
            self.start_delivery()
        elif position == "STOP" and self.delivery_active:
            if self.delivery_total_units < 1.0:
                self.delivery_active = False
                self.delivery_total_units = 0.0
                self.pulser.set_mode("idle")
            else:
                self.stop_delivery()
        elif position == "PRINT":
            self.delivery_pending_print = False

    def navigate_screen(self, screen: str):
        if screen not in self.SCREENS:
            raise RegisterError("RANGE ERROR")
        self.screen = screen
        self.field_cursor = 0

    # ------------------------------------------------------------------ #
    # POS — Tax categories
    # ------------------------------------------------------------------ #
    def active_tax_category(self) -> TaxCategory:
        return self.tax_categories[self.active_tax_category_idx]

    def set_tax_line(self, letter: str, tax_type: str, value: float, header_text: str = ""):
        cat = self.active_tax_category()
        line = next((l for l in cat.lines if l.letter == letter), None)
        if line is None:
            raise RegisterError("RANGE ERROR")
        if tax_type not in ("NOT_USED", "PERCENT", "PER_UNIT", "TAX_ON_TAX"):
            raise RegisterError("RANGE ERROR")
        line.tax_type = tax_type
        line.value = value
        line.header_text = header_text

    def compute_tax(self, subtotal: float) -> dict:
        cat = self.active_tax_category()
        breakdown = {}
        running_taxed_base = subtotal
        for line in cat.lines:
            if line.tax_type == "NOT_USED":
                continue
            if line.tax_type == "PERCENT":
                amt = subtotal * (line.value / 100.0)
            elif line.tax_type == "PER_UNIT":
                amt = self.delivery_total_units * line.value
            elif line.tax_type == "TAX_ON_TAX":
                base = subtotal + sum(breakdown.get(l, 0.0) for l in line.applies_to)
                amt = base * (line.value / 100.0)
            else:
                amt = 0.0
            breakdown[line.letter] = round(amt, 2)
        total_tax = round(sum(breakdown.values()), 2)
        return {"subtotal": round(subtotal, 2), "tax_breakdown": breakdown,
                "total_tax": total_tax, "total_with_tax": round(subtotal + total_tax, 2)}

    # ------------------------------------------------------------------ #
    # POS — Cash discounting
    # ------------------------------------------------------------------ #
    def active_cash_discount(self) -> CashDiscountCategory:
        return self.cash_discount_categories[self.active_cash_discount_idx]

    def compute_cash_discount(self, subtotal: float, days_since_delivery: int = 0) -> dict:
        cat = self.active_cash_discount()
        applicable = [t for t in cat.tiers if days_since_delivery <= t.days_from_delivery] or [cat.tiers[-1]]
        tier = min(applicable, key=lambda t: t.days_from_delivery) if applicable else None
        if tier is None:
            return {"discount": 0.0, "tier_used": None}
        disc = subtotal * (tier.discount_pct / 100.0) + tier.discount_per_unit * self.delivery_total_units
        return {"discount": round(disc, 2), "tier_used": tier.days_from_delivery,
                "applied_before_tax": cat.apply_before_tax}

    # ------------------------------------------------------------------ #
    def build_pos_ticket(self) -> dict:
        p = self.active_product()
        subtotal = round(self.delivery_total_units * p.price_per_unit, 2)
        tax = self.compute_tax(subtotal)
        discount = self.compute_cash_discount(subtotal)
        net_due = tax["total_with_tax"] - discount["discount"] if discount["applied_before_tax"] else (
            tax["total_with_tax"] - discount["discount"])
        return {
            "kind": "DELIVERY_POS",
            "product_number": p.number,
            "product_name": p.name or f"PROD {p.number}",
            "delivery_total": round(self.delivery_total_units, 2),
            "price_per_unit": p.price_per_unit,
            **tax,
            "cash_discount": discount,
            "amount_due": round(net_due, 2),
        }

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        d = super().to_dict()
        cat = self.active_tax_category()
        cd = self.active_cash_discount()
        d["lcr600"] = {
            "selector": self.selector,
            "screen": self.screen,
            "unit_id": self.unit_id,
            "preset_mode": self.preset_mode,
            "active_tax_category": {
                "number": cat.number, "name": cat.name,
                "lines": [{"letter": l.letter, "tax_type": l.tax_type,
                           "value": l.value, "header_text": l.header_text} for l in cat.lines],
            },
            "active_cash_discount": {
                "number": cd.number, "name": cd.name, "apply_before_tax": cd.apply_before_tax,
                "tiers": [{"days_from_delivery": t.days_from_delivery,
                           "discount_pct": t.discount_pct,
                           "discount_per_unit": t.discount_per_unit} for t in cd.tiers],
            },
        }
        return d
