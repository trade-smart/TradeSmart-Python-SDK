"""The broad UAT live-order matrix (internal; UAT only, see the design spec 2026-11-12).

Never production, never next to funded mode. Safety core: ``gate`` (opt-ins, run id, resume, the
session), ``ledger`` (the run ledger and the open-order invariant), ``guard`` (declared counts,
``MAX_OPEN``, circuit breakers, the STOP file) and ``calls`` (the never-resend wrappers: the only
place the matrix sends a state-changing call). Cases, contracts, pricing and the expectation
oracle: ``cases``, ``contracts``, ``pricing``, ``expect``. The batch engine: ``batch`` (place,
observe, act, cancel and judge a batch of cases), ``views`` (cross-checks across the book,
history, trades and the order stream), ``positions`` (the run's position ledger) and ``record``
(one JSON line per case).
"""
