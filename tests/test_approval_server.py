"""The approval server, driven end to end without a browser.

The server is I/O shell, and the spec leaves I/O shell to be verified by running it.
What is tested here is only the part that no amount of running by hand proves
reliably: that the blocked main thread really does unblock on the POST and with the
right resolution, that the page survives being fetched again afterwards, and that a
payload this process cannot make sense of leaves the run waiting rather than
resolving something wrong. Everything is driven over a real socket on a real port
with `urllib` -- no mocks, because a mocked socket would prove nothing.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from spotify_manager.dedupe.execute import Applied, execute, restore_command_for
from spotify_manager.dedupe.models import DedupePlan, snapshot_from_raw
from spotify_manager.dedupe.planner import plan as build_plan
from spotify_manager.dedupe.resolve import (
    ApprovalPayload,
    Resolution,
    approve_plan_unmodified,
    resolve_decisions,
)
from spotify_manager.report import render_plan_html, render_results_html
from spotify_manager.report.server import (
    APPROVE_PATH,
    RESULTS_PATH,
    ApprovalServer,
    PortUnavailableError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "library_snapshot.redacted.json"


@pytest.fixture(scope="module")
def real_plan() -> DedupePlan:
    return build_plan(snapshot_from_raw(json.loads(FIXTURE.read_text(encoding="utf-8"))))


@pytest.fixture
def served(real_plan: DedupePlan):
    """A running server on a port the kernel picked, plus the thread that waits."""
    html = render_plan_html(real_plan, approve_url=APPROVE_PATH)

    def interpret(raw: Any) -> Resolution:
        return resolve_decisions(real_plan, ApprovalPayload.from_raw(raw))

    # Port 0: the kernel hands back a free one, so tests never collide with a real run.
    server = ApprovalServer(html, interpret=interpret, port=0).start()
    waiter = _Waiter(server)
    try:
        yield server, waiter
    finally:
        server.stop()


class _Waiter:
    """Stands in for the blocked main thread of a run."""

    def __init__(self, server: ApprovalServer) -> None:
        self.result: Resolution | None = None
        self._thread = threading.Thread(target=self._wait, args=(server,), daemon=True)
        self._thread.start()

    def _wait(self, server: ApprovalServer) -> None:
        self.result = server.wait_for_decision()

    def resolution(self, timeout: float = 5.0) -> Resolution:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the run never unblocked"
        assert self.result is not None
        return self.result

    def still_waiting(self, settle: float = 0.5) -> bool:
        self._thread.join(settle)
        return self._thread.is_alive()


def get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8")


def post(url: str, payload: dict, timeout: float = 5.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def payload_of(approval: ApprovalPayload) -> dict:
    return {
        "groups": {
            str(index): {"action": decision.action, "keep": list(decision.keep)}
            for index, decision in approval.groups.items()
        }
    }


# --------------------------------------------------------------------------


def test_the_page_is_served_and_the_run_waits_until_it_is_answered(served, real_plan):
    server, waiter = served

    status, html = get(server.url)
    assert status == 200
    assert "<title>Duplicate saved albums</title>" in html
    assert html.count('<section class="group"') == len(real_plan.groups)
    # Serving the page is not answering it.
    assert waiter.still_waiting()

    status, body = post(server.url.rstrip("/") + APPROVE_PATH, payload_of(approve_plan_unmodified(real_plan)))
    assert status == 200
    assert body["status"] == "received"

    resolution = waiter.resolution()
    assert resolution.to_delete == real_plan.proposed_removal_ids


def test_a_submitted_subset_resolves_to_exactly_that_subset(served, real_plan):
    server, waiter = served
    # Any group but the first, which this test skips wholesale.
    index, group = next(
        (i, g) for i, g in enumerate(real_plan.groups) if i > 0 and len(g.members) >= 3
    )
    keep_two = [group.members[0].album.id, group.members[1].album.id]

    decisions = payload_of(approve_plan_unmodified(real_plan))
    decisions["groups"][str(index)] = {"action": "resolve", "keep": keep_two}
    decisions["groups"]["0"] = {"action": "skip", "keep": []}

    status, _ = post(server.url.rstrip("/") + APPROVE_PATH, decisions)
    assert status == 200

    resolution = waiter.resolution()
    expected_from_group = [m.album.id for m in group.members[2:]]
    assert set(expected_from_group) <= set(resolution.to_delete)
    assert keep_two[0] not in resolution.to_delete
    assert not any(m.album.id in resolution.to_delete for m in real_plan.groups[0].members)
    assert [g.key for g in resolution.skipped_groups] == [real_plan.groups[0].key]


def test_the_page_still_answers_after_the_decision_was_submitted(served, real_plan):
    """A reviewer refreshing the tab, or the browser refetching it, must not hang the
    run or the request -- the page is static and stays static."""
    server, waiter = served
    post(server.url.rstrip("/") + APPROVE_PATH, payload_of(approve_plan_unmodified(real_plan)))
    waiter.resolution()

    status, html = get(server.url)
    assert status == 200
    assert "Duplicate saved albums" in html


def test_a_second_submission_cannot_start_a_second_run(served, real_plan):
    server, waiter = served
    endpoint = server.url.rstrip("/") + APPROVE_PATH
    post(endpoint, payload_of(approve_plan_unmodified(real_plan)))
    waiter.resolution()

    status, body = post(endpoint, {"groups": {}})
    assert status == 409
    assert "already submitted" in body["error"]


def test_a_payload_that_does_not_describe_this_plan_leaves_the_run_waiting(served):
    """The refusal is the reviewer's to see and correct; the run does not end on it."""
    server, waiter = served
    status, body = post(
        server.url.rstrip("/") + APPROVE_PATH,
        {"groups": {"99999": {"action": "resolve", "keep": []}}},
    )
    assert status == 400
    assert "group 99999" in body["error"]
    assert waiter.still_waiting()


def test_an_unexpected_interpret_failure_answers_the_post_and_leaves_the_run_waiting(real_plan):
    """`interpret` can fail for reasons that have nothing to do with the payload --
    a full disk or a read-only state dir while recording the ledger, say -- and
    those raise plain `OSError`/`ValueError`, not `SpotifyManagerError`. That must
    not vanish into a stderr traceback with the POST left unanswered forever; the
    reviewer must see an error and still be able to try again."""
    html = render_plan_html(real_plan, approve_url=APPROVE_PATH)

    def interpret(raw: Any) -> Resolution:
        raise OSError("disk is full")

    server = ApprovalServer(html, interpret=interpret, port=0).start()
    waiter = _Waiter(server)
    try:
        status, body = post(
            server.url.rstrip("/") + APPROVE_PATH, payload_of(approve_plan_unmodified(real_plan))
        )
        assert status == 500
        assert "disk is full" in body["error"]
        assert waiter.still_waiting()
    finally:
        server.stop()


def test_two_concurrent_submissions_never_both_apply(real_plan, tmp_path: Path):
    """Two POSTs racing in before either has set the decision event must not both
    pass the has-decision check: only one may ever reach `interpret`. Without a
    lock around the check-and-apply, both requests read `has_decision == False`
    before either sets it."""
    release = threading.Event()
    calls: list[dict] = []

    def interpret(raw: Any) -> Resolution:
        calls.append(raw)
        # Hold the first request inside `interpret` for a moment so the second
        # request has every chance to race in behind it before this returns.
        release.wait(timeout=5)
        return resolve_decisions(real_plan, ApprovalPayload.from_raw(raw))

    html = render_plan_html(real_plan, approve_url=APPROVE_PATH)
    server = ApprovalServer(html, interpret=interpret, port=0).start()
    endpoint = server.url.rstrip("/") + APPROVE_PATH
    payload = payload_of(approve_plan_unmodified(real_plan))

    results: list[tuple[int, dict]] = []

    def submit() -> None:
        try:
            results.append(post(endpoint, payload, timeout=10))
        except Exception as exc:  # pragma: no cover - surfaced via the assertion below
            results.append((-1, {"error": repr(exc)}))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        # Give the lock-holder's interpret() a moment to start before releasing it,
        # so the second request has every chance to race in behind it.
        time.sleep(0.3)
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    finally:
        server.stop()

    assert len(calls) == 1, "a second POST reached interpret() while the first was still applying"
    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409]


def test_a_body_that_is_not_json_leaves_the_run_waiting(served):
    server, waiter = served
    request = urllib.request.Request(
        server.url.rstrip("/") + APPROVE_PATH, data=b"not json", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=5)
    assert raised.value.code == 400
    assert waiter.still_waiting()


def test_unknown_paths_are_not_found(served):
    server, _ = served
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(server.url.rstrip("/") + "/nope")
    assert raised.value.code == 404


def test_the_server_binds_the_loopback_interface_only(served):
    server, _ = served
    assert server.host == "127.0.0.1"
    address = _local_ip()
    if address == "127.0.0.1":
        pytest.skip("no non-loopback address to prove the server is not on")
    probe = socket.socket()
    probe.settimeout(0.5)
    # Nothing else on this machine's own routable address answers on that port.
    assert probe.connect_ex((address, server.port)) != 0
    probe.close()


def test_a_busy_port_is_a_sentence_naming_the_port_and_the_flag(served):
    server, _ = served
    with pytest.raises(PortUnavailableError) as raised:
        ApprovalServer("<html></html>", port=server.port).start()
    message = str(raised.value)
    assert str(server.port) in message
    assert "--port" in message


# --------------------------------------------------------------------------
# The results view re-rendering in place, driven end to end against a stub
# --------------------------------------------------------------------------


class _StubClient:
    """Stands in for `SpotifyClient.delete_albums`: always succeeds, records ids.

    This is not the real Spotify client and never touches the network or the real
    account -- exactly the point: ticket #6 must be verified without ever running
    a deletion against anything real.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def delete_albums(self, ids: list[str]) -> None:
        self.calls.append(list(ids))


@pytest.fixture
def served_for_execution(real_plan: DedupePlan, tmp_path: Path):
    """A running server whose POST actually applies the decisions, against a stub
    client and a scratch restores directory -- never the real account.

    Wired exactly the way `dedupe_cmd.review` wires it: `interpret` resolves and
    executes on the request thread, and `render_results` renders the same
    `Applied` it produced. That is the wiring this test exists to prove.
    """
    html = render_plan_html(real_plan, approve_url=APPROVE_PATH)
    client = _StubClient()

    def interpret(raw: Any) -> Applied:
        resolution = resolve_decisions(real_plan, ApprovalPayload.from_raw(raw))
        result = execute(resolution, client, restores_dir=tmp_path / "restores")
        return Applied(resolution=resolution, result=result)

    def results_page(applied: Applied) -> str:
        return render_results_html(
            applied.result,
            applied.resolution,
            restore_command=restore_command_for(applied.result.restore_path),
        )

    server = ApprovalServer(html, interpret=interpret, render_results=results_page, port=0).start()
    waiter = _Waiter(server)
    try:
        yield server, waiter, client
    finally:
        server.stop()


def test_the_post_response_carries_the_results_url_back_to_the_submitting_tab(
    served_for_execution, real_plan
):
    server, waiter, client = served_for_execution

    status, body = post(
        server.url.rstrip("/") + APPROVE_PATH, payload_of(approve_plan_unmodified(real_plan))
    )

    assert status == 200
    assert body["status"] == "applied"
    assert body["results_url"] == RESULTS_PATH
    waiter.resolution()
    # The stub client actually received the deletion calls -- proving the results
    # view reflects a run that really executed, not just a resolved plan.
    assert client.calls, "execute() never reached the stub client"


def test_get_results_after_applying_serves_the_rendered_results_page(
    served_for_execution, real_plan
):
    server, waiter, _ = served_for_execution
    post(server.url.rstrip("/") + APPROVE_PATH, payload_of(approve_plan_unmodified(real_plan)))
    waiter.resolution()

    status, html = get(server.url.rstrip("/") + RESULTS_PATH)
    assert status == 200
    assert "Dedupe results" in html


def test_get_root_after_applying_serves_the_results_page_not_the_plan_again(
    served_for_execution, real_plan
):
    """Refreshing the tab after applying must land on the outcome, never the
    approval page again -- that page's button would only earn a 409 now."""
    server, waiter, _ = served_for_execution
    post(server.url.rstrip("/") + APPROVE_PATH, payload_of(approve_plan_unmodified(real_plan)))
    waiter.resolution()

    status, html = get(server.url)
    assert status == 200
    assert "Dedupe results" in html
    assert 'id="approve"' not in html


def test_approving_nothing_still_produces_a_results_view_over_the_real_server(
    served_for_execution, real_plan
):
    """The full stack, not just the pure functions: skipping every group posts a
    decision that removes nothing, and the server still hands back a results page."""
    server, waiter, client = served_for_execution
    decisions = {
        "groups": {str(index): {"action": "skip", "keep": []} for index in range(len(real_plan.groups))}
    }

    status, body = post(server.url.rstrip("/") + APPROVE_PATH, decisions)

    assert status == 200
    assert body["results_url"] == RESULTS_PATH
    waiter.resolution()
    assert client.calls == [], "nothing approved means nothing sent to the client"

    status, html = get(server.url.rstrip("/") + RESULTS_PATH)
    assert status == 200
    assert "Nothing was removed." in html


def test_a_400_leaves_the_server_waiting_even_with_execution_wired_up(served_for_execution):
    """The #5 behaviour must survive #6's change: a payload that cannot be
    interpreted must never reach `execute`, and the run must keep waiting."""
    server, waiter, client = served_for_execution

    status, body = post(
        server.url.rstrip("/") + APPROVE_PATH,
        {"groups": {"99999": {"action": "resolve", "keep": []}}},
    )

    assert status == 400
    assert waiter.still_waiting()
    assert client.calls == [], "a rejected payload must never reach execute()"


def _local_ip() -> str:
    """This machine's own LAN address, which the server must *not* be listening on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routed nowhere, sends nothing
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()
