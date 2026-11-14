"""The UAT matrix family M14: the BSE and BFO canaries, then the BSE grid (marker ``m_bse``).

Owner answer U9 (binding over the inventory's "default skip"): one canary order per segment
first, a far LIMIT BUY that decides whether the segment runs. A canary that rests ``OPEN`` lets
its segment run; one stuck ``PENDING`` (F26, seen on UAT for BSE) is cancelled once and noted
stuck, its segment is skipped for the run with the reason (naming the stuck order), and the
order is never cancelled again (one CancelOrder per order per session).

- BSE: the ``ITC`` canary (``cases`` family ``bse``), then the M1 grid on BSE ``ITC``
  (``cases.shape_grid("bse", "bse-b")``: EOS rests on BSE); smoke is the six M1 smoke shapes
  plus LMT EOS BUY.
- BFO: **M1's canary** (``cases`` family ``shapes``). It is listed here so a run of this family
  alone decides BFO too, but it is the same case: ``family.run_cases`` places a case at most
  once per run, so the session has one BFO canary whichever module runs first.
"""

from __future__ import annotations

from tests.live_orders.uat_matrix import cases
from tests.live_orders.uat_matrix.cases import Case

BSE_CANARY: Case = next(c for c in cases.CASES if c.canary and c.segment == "bse")
BFO_CANARY: Case = next(c for c in cases.CASES if c.canary and c.segment == "bfo")
BSE_GRID: list[Case] = [c for c in cases.CASES if c.family == "bse" and not c.canary]
M14_CASES: list[Case] = [BSE_CANARY, BFO_CANARY, *BSE_GRID]
