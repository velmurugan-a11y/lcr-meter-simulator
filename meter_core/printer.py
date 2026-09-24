"""
printer.py — Ticket formatting for the simulated Epson printers documented
across the LCR-II / LCR-600 / LCR.iQ install manuals. Two photographed
printer form factors exist in the source manuals (LCR-600 install manual,
"Meter System Components" page): the Epson Slip Printer (single-ticket,
insert-and-print, the de facto default across the product family) and the
Epson Roll Printer (continuous roll, used for "multi-layered tickets...
recording multiple custody transfers over an extended time frame").

This module renders a ticket's content as monospace text laid out to a
fixed column width, mirroring how real ESC/POS-class impact/thermal
printers format text (fixed-pitch font, fixed character width per line,
no proportional spacing) -- not a literal byte-for-byte ESC/POS command
stream, since the simulator's job is to show what would be on the paper,
not to drive a real printer.
"""
from __future__ import annotations
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class PrinterModel:
    key: str
    label: str
    columns: int
    form_factor: str  # "slip" | "roll"
    notes: str


PRINTER_MODELS: dict[str, PrinterModel] = {
    "epson_slip": PrinterModel(
        "epson_slip", "Epson Slip Printer (default)", 40, "slip",
        "Single delivery ticket, insert-and-print. The de facto standard "
        "across the LCR-II / LCR-600 / LCR.iQ family per the install manuals."),
    "epson_roll": PrinterModel(
        "epson_roll", "Epson Roll Printer", 40, "roll",
        "Continuous roll, used where a physical record of multiple "
        "deliveries on one length of paper is wanted."),
    "okidata_ml184": PrinterModel(
        "okidata_ml184", "Okidata ML184 (text-only fallback)", 40, "slip",
        "Listed as a compatible printer in the LCR-II setup manual; no "
        "product photo in the source set, rendered with the same generic "
        "ESC/POS-style layout as the Epson models."),
    "axiohm_blaster": PrinterModel(
        "axiohm_blaster", "Axiohm Blaster (text-only fallback)", 40, "slip",
        "Listed as a compatible printer in the LCR-II setup manual; no "
        "product photo in the source set."),
}

DEFAULT_PRINTER = "epson_slip"


def _hr(width: int, ch: str = "-") -> str:
    return ch * width


def _center(text: str, width: int) -> str:
    if len(text) >= width:
        return text[:width]
    pad = width - len(text)
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def _kv(label: str, value: str, width: int) -> str:
    """Left-justified label, right-justified value, like a real register
    ticket's field layout (LABEL .......... VALUE)."""
    label = label[: width - 1]
    space = width - len(label) - len(value)
    if space < 1:
        return (label + " " + value)[:width]
    return label + (" " * space) + value


def format_delivery_ticket(ticket: dict, printer_key: str = DEFAULT_PRINTER, header_lines: list[str] | None = None) -> str:
    """Renders a delivery ticket dict (as produced by RegisterBase / the
    product-specific build_*_ticket methods) into fixed-width text lines."""
    model = PRINTER_MODELS.get(printer_key, PRINTER_MODELS[DEFAULT_PRINTER])
    w = model.columns
    lines = []

    for hl in (header_lines or []):
        if hl.strip():
            lines.append(_center(hl.strip(), w))
    if header_lines:
        lines.append(_hr(w))

    kind = ticket.get("kind", "DELIVERY")
    title = {"DELIVERY": "DELIVERY TICKET", "DELIVERY_POS": "DELIVERY TICKET",
              "SHIFT": "SHIFT TICKET", "DIAGNOSTIC": "DIAGNOSTIC TICKET"}.get(kind, "TICKET")
    lines.append(_center(title, w))
    lines.append(_hr(w))
    lines.append(_kv("DATE/TIME", ticket.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S")), w))

    if kind in ("DELIVERY", "DELIVERY_POS"):
        lines.append(_kv("PRODUCT", str(ticket.get("product_name", "")), w))
        lines.append(_kv("DELIVERY TOTAL", f"{ticket.get('delivery_total', 0):.2f}", w))
        if ticket.get("temperature_c") is not None:
            lines.append(_kv("TEMPERATURE", f"{ticket['temperature_c']:.1f} C", w))
        if ticket.get("vcf_type") and ticket.get("vcf_type") != "NONE":
            lines.append(_kv("VCF TYPE", str(ticket["vcf_type"]), w))
        if ticket.get("k_factor") is not None:
            lines.append(_kv("K-FACTOR", f"{ticket['k_factor']:.2f}", w))

        if kind == "DELIVERY_POS" or ticket.get("price_per_unit"):
            lines.append(_hr(w, "."))
            lines.append(_kv("PRICE/UNIT", f"${ticket.get('price_per_unit', 0):.3f}", w))
            if "subtotal" in ticket:
                lines.append(_kv("SUBTOTAL", f"${ticket['subtotal']:.2f}", w))
            for letter, amt in (ticket.get("tax_breakdown") or {}).items():
                lines.append(_kv(f"TAX {letter}", f"${amt:.2f}", w))
            if "total_tax" in ticket:
                lines.append(_kv("TOTAL TAX", f"${ticket['total_tax']:.2f}", w))
            if ticket.get("cash_discount", {}).get("discount"):
                lines.append(_kv("DISCOUNT", f"-${ticket['cash_discount']['discount']:.2f}", w))
            due = ticket.get("amount_due", ticket.get("total_with_tax", ticket.get("total_price")))
            if due is not None:
                lines.append(_hr(w, "."))
                lines.append(_kv("AMOUNT DUE", f"${due:.2f}", w))

        if ticket.get("auto_stopped"):
            lines.append("")
            lines.append(_center("** PRESET REACHED - AUTO STOP **", w))
        if ticket.get("errors_during_delivery"):
            lines.append("")
            lines.append(_center("** ERRORS DURING DELIVERY **", w))
            for e in ticket["errors_during_delivery"]:
                lines.append(_center(e, w))

    elif kind == "SHIFT":
        lines.append(_kv("PRODUCT #", str(ticket.get("product_number", "")), w))
        lines.append(_kv("SHIFT GROSS", f"{ticket.get('shift_gross', 0):.2f}", w))
        lines.append(_kv("SHIFT NET", f"{ticket.get('shift_net', 0):.2f}", w))
        lines.append(_kv("DELIVERIES", str(ticket.get("shift_deliveries", 0)), w))

    elif kind == "DIAGNOSTIC":
        lines.append(_kv("LAST CALIBRATED", str(ticket.get("last_calibrated", "")), w))
        j8 = ticket.get("j8", {})
        lines.append(_hr(w, "."))
        lines.append(_center("J8 PULSER", w))
        lines.append(_kv("  #32", f"{j8.get('v32', 0):.2f} V", w))
        lines.append(_kv("  #33", f"{j8.get('v33', 0):.2f} V", w))
        lines.append(_kv("  #34", f"{j8.get('v34', 0):.2f} V", w))
        rtd = ticket.get("j14_rtd", {})
        lines.append(_hr(w, "."))
        lines.append(_kv("RTD CONTINUITY", "PASS" if rtd.get("continuity_pass") else "FAIL", w))
        power = ticket.get("power", {})
        lines.append(_kv("SUPPLY VOLTAGE", f"{power.get('voltage', 0):.1f} V", w))
        if ticket.get("errors"):
            lines.append(_hr(w, "."))
            lines.append(_center("ACTIVE ERRORS", w))
            for e in ticket["errors"]:
                lines.append(_center(e, w))

    lines.append(_hr(w))
    lines.append(_center("LIQUID CONTROLS LLC", w))
    lines.append(_center("AN IDEX COMPANY", w))

    if model.form_factor == "roll":
        # Roll printers commonly add extra feed lines between tickets so
        # successive tickets on the same length of paper stay visually
        # separated when later torn/cut.
        lines.append("")
        lines.append("")

    return "\n".join(lines)
