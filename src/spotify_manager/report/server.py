"""The local server that shows the report and waits for a decision.

Two routes, one job. `GET /` serves the rendered report; `POST /approve` accepts the
decisions the page submits. The main thread blocks until that POST arrives, and only
then does the run continue.

The waiting is deliberate and unconditional:

* There is no timeout. Reviewing a hundred groups is not a task with a deadline, and
  a run that expired mid-review would be worse than useless -- it would have to be
  restarted from the fetch.
* Closing the browser tab is not a cancellation. Nothing but a POST sets the event,
  so a closed tab simply means the reviewer will come back and reopen the page.
* The one way out is a terminal interrupt, which the CLI turns into a plain message
  rather than a traceback. That is why the wait polls a short interval instead of
  blocking forever inside one lock acquisition: it keeps Ctrl-C responsive on every
  platform without introducing a timeout in any meaningful sense.

The socket binds to the loopback address only. This page can approve deletions from
someone's music library; it has no business being reachable from the network.

Only the standard library is used, deliberately -- a review UI is not worth a
dependency, and `http.server` is more than enough for one page and one form post.
"""

from __future__ import annotations

import errno
import json
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..errors import SpotifyManagerError

#: Loopback only. Never 0.0.0.0: the page approves deletions.
HOST = "127.0.0.1"

#: High enough to need no privileges, obscure enough to rarely collide.
DEFAULT_PORT = 8765

#: The route the report posts its decisions to.
APPROVE_PATH = "/approve"

#: How often the waiting thread wakes to check for an interrupt.
_POLL_SECONDS = 0.2

#: Refuse a body larger than this. A decision payload for a huge library is a few
#: hundred kilobytes; anything past a megabyte is not a decision.
_MAX_BODY_BYTES = 4 * 1024 * 1024


class PortUnavailableError(SpotifyManagerError):
    """The chosen port is already taken by something else."""


class ApprovalServer:
    """Serves one report on the loopback interface and waits for one decision.

    `interpret` turns the raw posted document into whatever the caller wants back
    from `wait_for_decision` -- in practice, the pure resolver. It runs on the
    request thread on purpose: a payload this process cannot make sense of is
    answered as a 400 the reviewer can see and correct, while the run stays waiting.
    Only an interpretation that succeeded ends the wait.

    Used as a context manager so the socket is always closed, including when the
    reviewer abandons the run with Ctrl-C.
    """

    def __init__(
        self,
        html: str,
        *,
        interpret: Callable[[Any], Any] = lambda raw: raw,
        port: int = DEFAULT_PORT,
        host: str = HOST,
    ) -> None:
        self._html = html.encode("utf-8")
        self._interpret = interpret
        self.host = host
        self.port = port
        self._event = threading.Event()
        self._decision: Any = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> ApprovalServer:
        """Bind the port and serve in a background thread.

        Raises:
            PortUnavailableError: naming the port and the flag that changes it. A
                busy port is an ordinary thing to hit, not a bug to trace.
        """
        handler = _make_handler(self)
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                raise PortUnavailableError(
                    f"Port {self.port} on {self.host} is already in use, so the "
                    "approval page cannot be served.\n"
                    "Fix: close whatever is using it, or choose another port with "
                    "--port, for example --port "
                    f"{self.port + 1}."
                ) from None
            raise
        httpd.daemon_threads = True
        self._httpd = httpd
        # The port may have been 0, in which case the kernel chose one for us.
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="approval-server", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> ApprovalServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ------------------------------------------------------------------- decision

    def wait_for_decision(self) -> Any:
        """Block until the page posts decisions this process understood.

        Indefinite by design: no timeout, and nothing but a good POST returns.
        """
        while not self._event.wait(_POLL_SECONDS):
            pass
        return self._decision

    def _submit(self, decision: Any) -> None:
        self._decision = decision
        self._event.set()

    @property
    def has_decision(self) -> bool:
        return self._event.is_set()


def _make_handler(server: ApprovalServer) -> type[BaseHTTPRequestHandler]:
    class ApprovalHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "spotify-manager"

        def log_message(self, *args: object) -> None:
            """Silence the default stderr access log; the terminal belongs to the run."""

        # -- routes ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                # Served again on every request, so reopening a closed tab works and
                # a refresh after submitting still shows the page rather than hanging.
                self._respond(HTTPStatus.OK, "text/html; charset=utf-8", server._html)
            elif path == "/favicon.ico":
                self._respond(HTTPStatus.NO_CONTENT, "text/plain", b"")
            else:
                self._respond(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found\n")

        def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            if self.path.split("?", 1)[0] != APPROVE_PATH:
                self._respond(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found\n")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_BODY_BYTES:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Unusable request body."})
                return
            body = self.rfile.read(length) if length else b""
            try:
                raw = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Decisions must be JSON."})
                return
            if server.has_decision:
                # A second submission (a double click, or a stale tab) must never
                # start a second run. The first decision is the decision.
                self._json(
                    HTTPStatus.CONFLICT,
                    {"error": "Decisions were already submitted for this run."},
                )
                return
            try:
                decision = server._interpret(raw)
            except SpotifyManagerError as exc:
                # The payload does not describe this plan. Say so, change nothing,
                # and keep waiting -- the reviewer can reload and submit again.
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            server._submit(decision)
            self._json(
                HTTPStatus.OK,
                {
                    "status": "received",
                    "message": (
                        "Decisions received. The result is printed in the terminal "
                        "you started the run from."
                    ),
                },
            )
            # Ticket #6 hooks in here: instead of ending at "received", the run's
            # execution result becomes a results view served from this same server.

        # -- plumbing -------------------------------------------------------

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            self._respond(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload).encode("utf-8"),
            )

        def _respond(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

    return ApprovalHandler
