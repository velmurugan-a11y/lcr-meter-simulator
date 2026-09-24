"""
rtd_vcf.py — Shared physics: Pt100 RTD curve and VCF (Volume Correction
Factor) temperature compensation, per Liquid Controls LCR-family manuals
(IEC 751 Class B RTD; API Table 24/54/54B/6B/54C/54D/NH3 compensation types).

This module has no Flask/UI dependency — it is pure calculation, shared by
every simulated register model (LCR-II, LCR-600, LCR.iQ).
"""
from __future__ import annotations
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Pt100 RTD curve (IEC 60751, alpha = 0.00385), verified in-skill against the
# manual's two stated calibration anchors: 100.00 ohm @ 0C, 138.51 ohm @ 100C
# (manual states 138.5 ohm). Used to convert a "true" simulated temperature
# into the raw ohms the register's ADC would see, and back again, so we can
# faithfully reproduce the RTD continuity check (100 ohm +/- 20 ohm) and the
# +/-0.3C field-adjustment ceiling before a RANGE ERROR fires.
# ---------------------------------------------------------------------------
_R0 = 100.0
_A = 3.9083e-3
_B = -5.7750e-7
_C = -4.183e-12  # cubic correction term, T < 0C only


def temp_c_to_ohms(temp_c: float) -> float:
    """Pt100 resistance (ohms) for a given temperature in Celsius."""
    if temp_c >= 0:
        return _R0 * (1 + _A * temp_c + _B * temp_c ** 2)
    return _R0 * (1 + _A * temp_c + _B * temp_c ** 2 + _C * (temp_c - 100) * temp_c ** 3)


def ohms_to_temp_c(ohms: float) -> float:
    """Invert the Pt100 curve numerically (bisection — the curve is monotonic
    and smooth across -200C..850C, so this converges quickly and is plenty
    accurate for a simulator)."""
    lo, hi = -60.0, 200.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if temp_c_to_ohms(mid) < ohms:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def rtd_continuity_ok(ohms: float) -> bool:
    """The manual's field continuity check: 100 ohm +/- 20 ohm pass band,
    checked across J14 pin pairs. Used here as a stand-in for 'probe healthy'."""
    return 80.0 <= ohms <= 120.0


# ---------------------------------------------------------------------------
# VCF (Volume Correction Factor) — Appendix A table, lcr-ii.md, reproduced
# here as data. Tbase/Tmin/Thold/Tmax are all in the table's native unit
# (most are C, two Linear rows are F) per the source manuals.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VCFType:
    code: str
    label: str
    param_label: str
    param_min: float
    param_max: float
    unit: str          # the *temperature* unit this row's Tbase/Tmin/etc are in
    tbase: float
    tmin: float
    thold: float | None
    tmax: float


VCF_TYPES: dict[str, VCFType] = {
    "NONE": VCFType("NONE", "No Compensation", "—", 0, 0, "C", 0, -999, None, 999),
    "LINEAR_C": VCFType("LINEAR_C", "Linear (Celsius)", "Coefficient", 0.0, 0.003, "C", 15, -90, None, 100),
    "LINEAR_F": VCFType("LINEAR_F", "Linear (Fahrenheit)", "Coefficient", 0.0, 0.005, "F", 60, -130, None, 212),
    "API24": VCFType("API24", "API Table 24 (LPG, USA)", "Specific Gravity", 0.5, 0.550, "F", 60, -50, -50, 140),
    "API54": VCFType("API54", "API Table 54 (LPG, EU/CA)", "Density kg/L", 0.5, 0.600, "C", 15, -46, -46, 60),
    "API54B": VCFType("API54B", "API Table 54B (Refined, EU/CA)", "Density kg/m3", 653.0, 1075.0, "C", 15, -50, -40, 95),
    "API6B": VCFType("API6B", "API Table 6B (Refined, USA)", "API Gravity", 0.0, 85.0, "F", 60, -50, -40, 200),
    "API54C": VCFType("API54C", "API Table 54C (Lube Oil, general)", "Coefficient", 0.000486, 0.001674, "C", 15, -50, -40, 95),
    "API54D": VCFType("API54D", "API Table 54D (Lube Oil, EU/CA)", "Density kg/m3", 800.0, 1164.0, "C", 15, -50, -40, 95),
    "NH3": VCFType("NH3", "NH3 (Ammonia, Canada)", "—", 0, 0, "C", 15, -30, -30, 40),
}


def vcf_factor(vcf_type: str, param: float, temp_c: float) -> tuple[float, str | None]:
    """
    Returns (factor, error). factor multiplies metered (uncorrected) volume
    to get corrected volume. error is None on success, or a string matching
    the register's own error vocabulary ("VCF DOMAIN ERROR") if temp_c falls
    outside [Tmin, Tmax] for the active type.

    This is a *simplified* engineering approximation of the real API
    table lookups (which are precise multi-parameter tables in the real
    standard) — close enough to demonstrate the concept and produce
    plausible, monotonic correction behavior, but should not be used as an
    authoritative VCF source. The source manuals don't publish the
    underlying table values themselves, only the valid parameter/temperature
    *ranges*, which is what's modeled exactly here.
    """
    vt = VCF_TYPES.get(vcf_type, VCF_TYPES["NONE"])
    if vt.code == "NONE":
        return 1.0, None

    t = temp_c if vt.unit == "C" else (temp_c * 9 / 5 + 32)

    if t < vt.tmin or t > vt.tmax:
        return 1.0, "VCF DOMAIN ERROR"

    # Clamp to Thold for the cold end, per the manual's own description of
    # Thold ("VCF is held constant for very cold environments").
    if vt.thold is not None and t < vt.thold:
        t_eff = vt.thold
    else:
        t_eff = t

    delta = t_eff - vt.tbase

    if vt.code in ("LINEAR_C", "LINEAR_F"):
        coeff = param
        factor = 1.0 - coeff * delta
    elif vt.code in ("API24", "API54"):
        # Lighter products (lower SpGr/density) expand more per degree.
        sg = param if param else (vt.param_min + vt.param_max) / 2
        coeff = 0.0015 / max(sg, 0.1)
        factor = 1.0 - coeff * delta
    elif vt.code in ("API54B", "API54D"):
        density = param if param else (vt.param_min + vt.param_max) / 2
        coeff = 0.00065 * (1000.0 / max(density, 1.0))
        factor = 1.0 - coeff * delta
    elif vt.code == "API6B":
        api_gravity = param if param else (vt.param_min + vt.param_max) / 2
        coeff = 0.0004 + 0.0000025 * api_gravity
        factor = 1.0 - coeff * delta
    elif vt.code == "API54C":
        coeff = param if param else (vt.param_min + vt.param_max) / 2
        factor = 1.0 - coeff * delta
    elif vt.code == "NH3":
        coeff = 0.0019
        factor = 1.0 - coeff * delta
    else:
        factor = 1.0

    # Keep the demo numerically sane even at the extreme ends of a wide range.
    factor = max(0.80, min(1.20, factor))
    return factor, None
