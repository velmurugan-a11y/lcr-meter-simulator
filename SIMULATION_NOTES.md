# What's real vs. approximated in this simulator

## Modeled directly from the source manuals, and unit-tested against them
(see `test_core.py`, run it any time you change `meter_core/`):

- k-Factor (PULSES/UNIT) calibration: overwrite a prover quantity, the
  register backs out a new k-Factor via `new_k = pulses / prover_qty`,
  `%Error = (prover_qty - meter_qty) * 100 / prover_qty`
- A real Pt100 RTD curve (IEC 60751), verified to land on the manual's own
  two stated anchor points (100.00 ohm @ 0C, 138.51 ohm @ 100C vs. the
  manual's "138.5 ohm"). This makes the +/-0.3C field-trim ceiling and the
  100 ohm +/- 20 ohm continuity check internally consistent rather than
  hard-coded separately.
- Multi-point calibration's two validation rules: duplicate flow rate
  rejection, and the "adjacent points must be within 0.25%" rule
- The exact J8 #32/#33/#34 fault-voltage signatures for idle/stalled/
  good-flow pulser conditions, generated live by a background thread, not
  canned strings
- LCR-600's tax engine (Percent / Per Unit / Tax on Tax) and cash discount
  tiers, computed for real against a configurable price-per-unit
- LCR.iQ's SENSEiQ 4-20mA-to-percent conversion (4mA=0%, 20mA=100%) and
  continuously-ramping digital valve position (vs. the legacy products'
  fixed open/dwell/closed valve)
- Physical control layouts in Original View are drawn from the actual
  product photos in the manuals/brochures (LCR-II's dimensions page, the
  LCR-600 install manual's front-view dimensions page showing the rotary
  dial alongside the keypad, and the LCR.iQ brochure's product photography
  showing the keypad/d-pad/screen layout and the idle-vs-active-fueling
  background color change)

## Engineering approximation, flagged as such in the code

`meter_core/rtd_vcf.py`, `vcf_factor()`: the VCF temperature compensation
curve coefficients. The manuals publish the *valid parameter and
temperature ranges* for each of the 9 compensation types (Linear, API
24/54/54B/6B/54C/54D, NH3) but not the underlying API table values — those
live in the API/ASTM standards, not in Liquid Controls' documentation. The
domain-checking (VCF DOMAIN ERROR outside Tmin/Tmax) is exact; the
magnitude of correction within range is a plausible approximation, not a
certified one.

## Printer ticket rendering

The printed ticket layout (column width, line spacing, font) follows
common ESC/POS thermal/impact printer conventions for the two photographed
printer types (Epson Slip Printer, Epson Roll Printer) — these are
genuinely how Epson's TM-series printers format text (monospace, fixed
character width per line), not invented. The exact pixel-for-pixel
appearance of a real printed ticket from your specific printer model may
differ slightly (font hinting, exact margins) since I don't have a sample
real ticket image to match against — if you have a photo of an actual
printed LCR ticket, send it and I'll match the layout exactly rather than
the generic convention.

This is a simulator for understanding the operator workflow, the
calibration math, and the fault-diagnosis paths — not a Weights &
Measures-traceable calibration tool or a substitute for the real instrument.

## Known limitation: LCR.iQ's digital valve ramp needs regular polling

The LCR.iQ's continuously-variable digital valve (modeling the brochure's
multi-stage ramp-up/ramp-down) recalculates its position, and therefore the
effective flow rate it feeds into the pulser, every time `tick()` is
called. Under the browser UI's actual polling cadence (every 250ms), this
is accurate and well-tested (`test_core.py`'s "REALISTIC polling cadence"
section).

If some future caller invokes the API only once, then waits a long time,
then calls again with no polling in between (e.g. a naive serial-bridge
integration that isn't also running something like the browser's poll
loop), the valve-ramp-scaled rate will only update at the moment of that
second call — meaning the *integrated* flow over the gap can be
under-counted, since the simulator has no way to know what the "right" rate
was at each moment during a gap it wasn't asked about. This was found and
characterized during development (see the test file's comments); the
straightforward fix, when the real serial/COM-port integration (point 2)
is built, is to make sure that integration also drives a regular internal
tick — the same way the browser's poll loop already does — rather than
calling into the register only when a command arrives on the wire.
