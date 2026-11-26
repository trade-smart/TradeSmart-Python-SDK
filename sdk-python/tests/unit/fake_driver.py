"""A scriptable live-order driver for the harness tests (``specifications/live-driver.md``).

Used only by ``tests/unit/test_uat_matrix_driver_process.py`` and the OCO driver dry drive
(``relay`` below): it speaks the JSON-lines protocol on stdin/stdout and makes no call of any
kind (no SDK, no network; a relay connects to the loopback interface only). It prints only
synthetic values. Run as ``python fake_driver.py``; its behaviour comes from the JSON file named
by ``FAKE_DRIVER_SCRIPT``:

- ``stderr``: lines written to stderr at start (the tests put synthetic secrets in them);
- ``refuse``: then write one more stderr line and exit 3 before any handshake;
- ``child``: start a long-sleeping child process first (its pid goes to the log), so the tests
  can check that the harness kills the whole process tree;
- ``escaped_child``: start a long-sleeping child outside the driver's process group that keeps
  the driver's stdout and stderr open (its pid goes to the log), so the tests can check that
  closing a driver never waits on a pipe something else still holds;
- ``hello``: the handshake object (``{"hello": ...}`` is written), or a string written as the
  first stdout line verbatim, or ``null`` to write nothing and wait for end of file; the
  nonce echo ``run`` is filled from ``TRADESMART_LIVE_DRIVER_RUN`` unless the object sets it
  (``"run": null`` leaves it out);
- ``replies``: one step per request line, in order: ``{"result": ..., "raw": ..., "http_status":
  ...}`` or ``{"error": {...}}`` answered with the request's id; ``{"hang": true}`` (never answer);
  ``{"half": "<text>"}`` (write text without its newline, then hang); ``{"exit": <code>}`` (exit
  without answering); ``{"line": "<text>"}`` (write a raw line); ``{"id": <n>, ...}`` answers
  with that id instead of the request's.
- ``relay``: a port on ``127.0.0.1`` (the loopback interface only): after the handshake every
  request line is passed, as received, to the test listening there, and the one line it answers
  is written back as the reply (``replies`` is then not read). The test plays the driver's SDK
  (``tests/integration/test_uat_matrix_oco_driver_dry.py``); this process still makes no call.

Every request line received is appended to ``FAKE_DRIVER_LOG`` (one JSON object per line:
``{"request": <the line as text>}``), written and flushed before the reply, so a test can count
exactly what the harness sent.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

RUN_ENV = "TRADESMART_LIVE_DRIVER_RUN"
FOREVER = 3600.0


def _log(entry: dict[str, Any]) -> None:
    path = os.environ.get("FAKE_DRIVER_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _out(text: str, *, newline: bool = True) -> None:
    sys.stdout.write(text + ("\n" if newline else ""))
    sys.stdout.flush()


def _hang() -> None:
    time.sleep(FOREVER)


def _relay(port: int) -> int:
    """Pass each request line to the test on ``127.0.0.1:port`` and write its answer back."""
    with socket.create_connection(("127.0.0.1", port)) as conn, conn.makefile("rwb") as peer:
        for line in sys.stdin:
            _log({"request": line.rstrip("\n")})
            peer.write(line.encode("utf-8"))
            peer.flush()
            answer = peer.readline()
            if not answer:
                return 0  # the test closed the relay: no reply (the harness times out)
            _out(answer.decode("utf-8").rstrip("\n"))
    return 0


def main() -> int:
    with open(os.environ["FAKE_DRIVER_SCRIPT"], encoding="utf-8") as fh:
        script: dict[str, Any] = json.load(fh)
    _log({"started": True, "nonce": os.environ.get(RUN_ENV)})
    for line in script.get("stderr", []):
        sys.stderr.write(str(line) + "\n")
    sys.stderr.flush()
    if script.get("refuse"):
        sys.stderr.write("fake driver: refusing to start (no nonce)\n")
        sys.stderr.flush()
        return 3
    if script.get("child"):
        child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({FOREVER})"])  # noqa: S603
        _log({"child": child.pid})
    if script.get("escaped_child"):
        # Holds the driver's stdout and stderr open but is outside its process group, so no
        # tree kill reaches it (as any grandchild on Windows before a tree kill existed there).
        escaped = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", f"import time; time.sleep({FOREVER})"],
            start_new_session=os.name == "posix",
        )
        _log({"escaped_child": escaped.pid})
    hello = script.get("hello", {})
    if hello is None:
        sys.stdin.read()  # nothing on stdout: wait for end of file
        return 0
    if isinstance(hello, str):
        _out(hello)
    else:
        body = dict(hello)
        if "run" not in body:
            body["run"] = os.environ.get(RUN_ENV)
        elif body["run"] is None:
            del body["run"]
        _out(json.dumps({"hello": body}))
    if script.get("relay") is not None:
        return _relay(int(script["relay"]))
    replies: list[dict[str, Any]] = list(script.get("replies", []))
    for line in sys.stdin:
        _log({"request": line.rstrip("\n")})
        request = json.loads(line)
        step = replies.pop(0) if replies else {"hang": True}
        if step.get("hang"):
            _hang()
            return 0
        if "half" in step:
            _out(str(step["half"]), newline=False)
            _hang()
            return 0
        if "exit" in step:
            return int(step["exit"])
        if "line" in step:
            _out(str(step["line"]))
            continue
        reply = {key: value for key, value in step.items() if key != "id"}
        reply["id"] = step.get("id", request.get("id"))
        _out(json.dumps(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
