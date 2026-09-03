#!/usr/bin/env python3
"""Unit tests for bin/omarchy-agent-session-watch.

Loaded by file path via importlib, since the script has no .py suffix (it is
an executable on PATH, per Omarchy's convention, not an importable module).
Every notification is captured through Notifier(dry_run=True) rather than a
real omarchy-notification-send call, and time is driven by a fake monotonic
clock so the 10s coalescing window and 60s digest window are exercised
without real sleeps.

Run with: python3 -m unittest tests.test_watch -v
      or: python3 tests/test_watch.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

WATCH_PATH = Path(__file__).resolve().parent.parent / "bin" / "omarchy-agent-session-watch"

# The watcher has no .py suffix (it's an executable on PATH, per Omarchy's
# convention), so spec_from_file_location can't infer a loader from the
# extension alone -- it returns None for an unrecognized suffix. Naming the
# loader explicitly sidesteps that.
_loader = importlib.machinery.SourceFileLoader("omarchy_agent_session_watch", str(WATCH_PATH))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
watch = importlib.util.module_from_spec(_spec)
sys.modules["omarchy_agent_session_watch"] = watch
_loader.exec_module(watch)


class FakeClock:
    """A controllable stand-in for time.monotonic."""

    def __init__(self, start: float = 1_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def write_session(sessions_dir: Path, session_id: str, name: str, **extra) -> None:
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": session_id, "name": name}
    record.update(extra)
    (session_dir / "session.json").write_text(json.dumps(record))


def append_event(sessions_dir: Path, session_id: str, seq: int, etype: str, data: dict = None) -> None:
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "seq": seq,
            "ts": "2026-09-01T00:00:00Z",
            "type": etype,
            "actor": {"kind": "system", "id": "omarchy", "label": "omarchy"},
            "data": data or {},
        }
    )
    with open(session_dir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")


class WatchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="omarchy-session-watch-test-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.clock = FakeClock()
        self.notifier = watch.Notifier(dry_run=True)

    def make_watcher(self, active_window_fn=lambda: None):
        return watch.SessionWatcher(
            self.tmpdir, self.notifier, clock=self.clock, active_window_fn=active_window_fn
        )

    def headlines(self):
        return [n["headline"] for n in self.notifier.sent]


class TestEventClassification(WatchTestCase):
    def test_waiting_and_blocked_notify_at_normal(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        self.assertEqual(len(self.notifier.sent), 1)
        sent = self.notifier.sent[0]
        # "needs you", not "needs an answer": since the 2026-09-02 review,
        # waiting and blocked share one title (see TestCopyAndClickTargets).
        self.assertEqual(sent["headline"], "api-refactor needs you")
        self.assertEqual(sent["urgency"], "normal")
        self.assertEqual(sent["exec_argv"], ["omarchy-agent-session-open", "s1"])

    def test_failed_is_critical_with_detail(self):
        write_session(self.tmpdir, "s1", "api-refactor", agent={"kind": "claude"})
        append_event(
            self.tmpdir, "s1", 1, "status.changed",
            {"from": "working", "to": "failed", "detail": "harness exited 1"},
        )
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        sent = self.notifier.sent[0]
        # The detail moved out of the title and into the body (2026-09-02).
        self.assertEqual(sent["headline"], "api-refactor failed")
        self.assertEqual(sent["description"], "claude · harness exited 1 · Click to open the receipt")
        self.assertEqual(sent["urgency"], "critical")

    def test_orphaned_is_a_status_changed_variant(self):
        # 01-session-model.md's event-type table has no "session.orphaned"
        # type; orphaning is status.changed with data.to == "orphaned"
        # (state machine row: "any live -> orphaned ... source: reconciler").
        # This asserts the watcher follows 01/02, not 06's table literally.
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "orphaned"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        sent = self.notifier.sent[0]
        # "stopped unexpectedly", not "lost its pane" (2026-09-02: "pane" is
        # Herdr vocabulary that read as the window the person closed).
        self.assertEqual(sent["headline"], "api-refactor stopped unexpectedly")
        # No detail at all is not the Herdr-down case, so it stays critical.
        self.assertEqual(sent["urgency"], "critical")

    def test_working_idle_and_instruction_delivered_do_not_notify(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "starting", "to": "working"})
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "working", "to": "idle"})
        append_event(self.tmpdir, "s1", 3, "instruction.delivered", {"instruction_id": "x", "delivery": "steer"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        self.assertEqual(self.notifier.sent, [])

    def test_child_completed_names_parent_and_child(self):
        write_session(self.tmpdir, "parent-1", "api-refactor")
        write_session(self.tmpdir, "child-1", "write-tests")
        append_event(
            self.tmpdir, "parent-1", 1, "child.completed",
            {"child_id": "child-1", "state": "done", "receipt_summary": "2 commits"},
        )
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        sent = self.notifier.sent[0]
        self.assertEqual(sent["headline"], "api-refactor: write-tests finished (done)")
        self.assertEqual(sent["urgency"], "low")
        # Click action opens the parent, whose events.jsonl carried the event.
        self.assertEqual(sent["exec_argv"], ["omarchy-agent-session-open", "parent-1"])


OPEN_S1 = ["omarchy-agent-session-open", "s1"]
RECEIPT_S1 = [
    "omarchy-launch-tui",
    "--app-id=org.omarchy.session-receipt",
    "omarchy-agent-session-receipt",
    "--pager",
    "s1",
]


class TestCopyAndClickTargets(WatchTestCase):
    """The copy, click targets, and orphaned urgency rule from the live-desktop
    design review of 2026-09-02. One notice per test, read straight off what
    the notifier was handed: headline (title), description (body), exec_argv
    (what a click runs), urgency."""

    RECORD = {
        "agent": {"kind": "claude", "harness_session_ref": None},
        "goal": {"text": "Refactor the API layer\nSecond line is never shown"},
        "workspace": {"branch": "feat/api-refactor"},
    }

    def notice_for(self, to_state, detail=None, **record_extra):
        record = dict(self.RECORD)
        record.update(record_extra)
        write_session(self.tmpdir, "s1", "api-refactor", **record)
        data = {"from": "working", "to": to_state}
        if detail is not None:
            data["detail"] = detail
        append_event(self.tmpdir, "s1", 1, "status.changed", data)
        self.make_watcher().scan_once(now=self.clock())
        self.assertEqual(len(self.notifier.sent), 1)
        return self.notifier.sent[0]

    def test_waiting_says_needs_you_and_opens_to_answer(self):
        sent = self.notice_for("waiting")
        self.assertEqual(sent["headline"], "api-refactor needs you")
        self.assertEqual(sent["description"], "claude · Refactor the API layer · Click to open and answer")
        self.assertEqual(sent["exec_argv"], OPEN_S1)
        self.assertEqual(sent["urgency"], "normal")

    def test_a_lane_that_needs_you_names_its_session_and_lane(self):
        # 11-agent-lanes.md: the toast reads the way the panel row does.
        watcher = self.make_watcher()
        write_session(self.tmpdir, "l1", "api-refactor.tests", agent={"kind": "claude"},
                      lane={"name": "tests", "parent_id": "s1", "parent_name": "api-refactor"},
                      goal={"text": "Write the tests"})
        append_event(self.tmpdir, "l1", 1, "status.changed", {"from": "working", "to": "blocked"})
        watcher.scan_once(now=self.clock())
        sent = self.notifier.sent[-1]
        self.assertEqual(sent["headline"], "api-refactor · tests needs you")

    def test_a_session_owned_by_someone_else_says_so_in_the_body(self):
        watcher = self.make_watcher()
        write_session(self.tmpdir, "o1", "handover", agent={"kind": "claude"},
                      owner={"actor": {"kind": "human", "id": "sam@mac", "label": "sam"}},
                      goal={"text": "Refactor the API layer"})
        append_event(self.tmpdir, "o1", 1, "status.changed", {"from": "working", "to": "blocked"})
        watcher.scan_once(now=self.clock())
        sent = self.notifier.sent[-1]
        self.assertIn("owned by sam", sent["description"])

    def test_an_owner_with_the_viewers_own_label_is_named_by_user_and_host(self):
        # The run-10 proxy: both people are "omarchy", on different hosts.
        watcher = self.make_watcher()
        local = watch.local_human_id()
        me = local.split("@", 1)[0]
        write_session(self.tmpdir, "o2", "handover", agent={"kind": "claude"},
                      owner={"actor": {"kind": "human", "id": f"{me}@elsewhere", "label": me}},
                      goal={"text": "Refactor the API layer"})
        append_event(self.tmpdir, "o2", 1, "status.changed", {"from": "working", "to": "blocked"})
        watcher.scan_once(now=self.clock())
        sent = self.notifier.sent[-1]
        self.assertIn(f"owned by {me}@elsewhere", sent["description"])

    def test_blocked_reads_the_same_as_waiting(self):
        # Herdr reports a question and a permission prompt both as `blocked`,
        # so the title must not guess which; the two stay distinct kinds
        # internally (classify_event still returns "blocked").
        sent = self.notice_for("blocked")
        self.assertEqual(sent["headline"], "api-refactor needs you")
        self.assertEqual(sent["description"], "claude · Refactor the API layer · Click to open and answer")
        self.assertEqual(sent["exec_argv"], OPEN_S1)
        self.assertEqual(watch.classify_event({"type": "status.changed", "data": {"to": "blocked"}}), ("blocked", "normal"))
        self.assertEqual(watch.classify_event({"type": "status.changed", "data": {"to": "waiting"}}), ("waiting", "normal"))

    def test_done_opens_the_receipt_it_promises(self):
        sent = self.notice_for("done")
        self.assertEqual(sent["headline"], "api-refactor finished")
        self.assertEqual(sent["description"], "claude · Refactor the API layer · Click to open the receipt")
        self.assertEqual(sent["exec_argv"], RECEIPT_S1)
        self.assertEqual(sent["urgency"], "low")

    def test_failed_keeps_the_title_short_and_puts_the_detail_in_the_body(self):
        sent = self.notice_for("failed", detail="harness exited 1")
        self.assertEqual(sent["headline"], "api-refactor failed")
        self.assertEqual(sent["description"], "claude · harness exited 1 · Click to open the receipt")
        # failed is a terminal state: `open` would exit 5, so the click opens
        # the receipt.
        self.assertEqual(sent["exec_argv"], RECEIPT_S1)
        self.assertEqual(sent["urgency"], "critical")

    def test_failed_without_detail_says_unknown_error(self):
        sent = self.notice_for("failed")
        self.assertEqual(sent["headline"], "api-refactor failed")
        self.assertEqual(sent["description"], "claude · unknown error · Click to open the receipt")

    def test_orphaned_with_a_resume_ref_offers_the_same_conversation(self):
        sent = self.notice_for(
            "orphaned", detail="pane not found in Herdr's list",
            agent={"kind": "claude", "harness_session_ref": "01J8ZK3Q9X7R2M4N6P8S0T2V4W"},
        )
        self.assertEqual(sent["headline"], "api-refactor stopped unexpectedly")
        self.assertEqual(sent["description"], "claude · Refactor the API layer · Click to revive, same conversation")
        self.assertEqual(sent["exec_argv"], OPEN_S1)

    def test_orphaned_without_a_resume_ref_offers_a_fresh_conversation(self):
        for label, agent in [
            ("null ref", {"kind": "claude", "harness_session_ref": None}),
            ("empty ref", {"kind": "claude", "harness_session_ref": ""}),
            ("missing key", {"kind": "claude"}),
        ]:
            with self.subTest(label):
                self.notifier.sent.clear()
                shutil.rmtree(self.tmpdir / "s1", ignore_errors=True)
                (self.tmpdir / ".watch-cursors.json").unlink(missing_ok=True)
                sent = self.notice_for("orphaned", detail="pane not found in Herdr's list", agent=agent)
                self.assertEqual(sent["headline"], "api-refactor stopped unexpectedly")
                self.assertEqual(
                    sent["description"], "claude · Refactor the API layer · Click to revive, fresh conversation"
                )
                self.assertEqual(sent["exec_argv"], OPEN_S1)

    def test_done_click_opens_receipt_while_blocked_click_opens_session(self):
        write_session(self.tmpdir, "s1", "api-refactor", **self.RECORD)
        write_session(self.tmpdir, "s2", "onboarding-flow", **self.RECORD)
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "done"})
        append_event(self.tmpdir, "s2", 1, "status.changed", {"from": "working", "to": "blocked"})
        self.make_watcher().scan_once(now=self.clock())

        by_id = {n["exec_argv"][-1]: n for n in self.notifier.sent}
        self.assertEqual(by_id["s1"]["exec_argv"], RECEIPT_S1)
        self.assertEqual(by_id["s2"]["exec_argv"], ["omarchy-agent-session-open", "s2"])

    def test_notice_for_a_session_already_terminal_opens_the_receipt(self):
        # An event read late, after the session ended (a restart, a slow
        # poll): the record already says done, so `open` would exit 5. The
        # click goes to the receipt regardless of the event's own kind.
        sent = self.notice_for(
            "waiting", status={"state": "done", "since": "2026-09-02T00:00:00Z", "source": "agent", "detail": None}
        )
        self.assertEqual(sent["headline"], "api-refactor needs you")
        self.assertEqual(sent["exec_argv"], RECEIPT_S1)

    def test_orphaned_by_herdr_outage_is_normal_but_a_lone_orphan_is_critical(self):
        herdr_down = {"type": "status.changed", "data": {"from": "working", "to": "orphaned", "detail": "Herdr server not running"}}
        pane_gone = {"type": "status.changed", "data": {"from": "working", "to": "orphaned", "detail": "pane not found in Herdr's list"}}
        no_detail = {"type": "status.changed", "data": {"from": "working", "to": "orphaned"}}
        self.assertEqual(watch.classify_event(herdr_down), ("orphaned", "normal"))
        self.assertEqual(watch.classify_event(pane_gone), ("orphaned", "critical"))
        self.assertEqual(watch.classify_event(no_detail), ("orphaned", "critical"))
        # Exact match only: a near-miss detail is still someone's agent dying.
        near_miss = {"type": "status.changed", "data": {"to": "orphaned", "detail": "herdr server not running"}}
        self.assertEqual(watch.classify_event(near_miss), ("orphaned", "critical"))

        # And end to end, through the watcher, with the reconciler's exact
        # Herdr-down string as the core writes it.
        sent = self.notice_for("orphaned", detail="Herdr server not running")
        self.assertEqual(sent["urgency"], "normal")
        self.assertEqual(sent["headline"], "api-refactor stopped unexpectedly")

    def test_body_falls_back_to_branch_then_to_agent_alone(self):
        no_goal = {"agent": {"kind": "codex"}, "workspace": {"branch": "feat/api-refactor"}}
        self.assertEqual(watch.render_body("waiting", no_goal), "codex · feat/api-refactor · Click to open and answer")
        bare = {"agent": {"kind": "codex"}}
        self.assertEqual(watch.render_body("done", bare), "codex · Click to open the receipt")
        self.assertEqual(watch.render_body("failed", bare), "codex · unknown error · Click to open the receipt")


class TestCoalescing(WatchTestCase):
    def test_second_event_within_10s_replaces_the_first(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())
        first_id = self.notifier.sent[0]["id"]
        self.assertIsNone(self.notifier.sent[0]["replaces_id"])

        self.clock.advance(5.0)  # inside the 10s coalescing window
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "waiting", "to": "blocked"})
        watcher.scan_once(now=self.clock())

        self.assertEqual(len(self.notifier.sent), 2)
        second = self.notifier.sent[1]
        self.assertEqual(second["headline"], "api-refactor needs you")
        self.assertEqual(second["replaces_id"], first_id)

    def test_event_after_10s_does_not_replace(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        self.clock.advance(11.0)  # outside the 10s coalescing window
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "waiting", "to": "blocked"})
        watcher.scan_once(now=self.clock())

        self.assertIsNone(self.notifier.sent[1]["replaces_id"])

    def test_coalescing_is_per_session(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        write_session(self.tmpdir, "s2", "onboarding-flow")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})
        append_event(self.tmpdir, "s2", 1, "status.changed", {"from": "working", "to": "waiting"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        self.clock.advance(2.0)
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "waiting", "to": "blocked"})
        watcher.scan_once(now=self.clock())

        # s1's second notice replaces s1's first; s2 is untouched and never
        # replaced by an unrelated session's activity. Keyed by the session
        # id in the click argv, since waiting and blocked now share a title.
        s1_notices = [n for n in self.notifier.sent if n["exec_argv"][-1] == "s1"]
        s2_notices = [n for n in self.notifier.sent if n["exec_argv"][-1] == "s2"]
        self.assertEqual([n["headline"] for n in s1_notices], ["api-refactor needs you"] * 2)
        self.assertIsNone(s1_notices[0]["replaces_id"])
        self.assertEqual(s1_notices[1]["replaces_id"], s1_notices[0]["id"])
        self.assertEqual([n["headline"] for n in s2_notices], ["onboarding-flow needs you"])
        self.assertIsNone(s2_notices[0]["replaces_id"])


class TestDigest(WatchTestCase):
    def test_fourth_distinct_session_in_60s_becomes_one_digest(self):
        sessions = [
            ("s1", "waiting"),
            ("s2", "waiting"),
            ("s3", "blocked"),
            ("s4", "failed"),
        ]
        watcher = self.make_watcher()
        for i, (sid, to_state) in enumerate(sessions):
            write_session(self.tmpdir, sid, sid)
            append_event(self.tmpdir, sid, 1, "status.changed", {"from": "working", "to": to_state})
            self.clock.advance(1.0)
            watcher.scan_once(now=self.clock())

        # First three sessions each got their own individual toast; the 4th
        # distinct session in the window triggered one digest instead of a
        # fourth individual toast.
        self.assertEqual(len(self.notifier.sent), 4)
        individual = [n for n in self.notifier.sent[:3]]
        self.assertTrue(all(not n["exec_argv"] == ["omarchy-agent-session-list"] for n in individual))

        digest = self.notifier.sent[3]
        self.assertEqual(digest["exec_argv"], ["omarchy-agent-session-list"])
        self.assertEqual(digest["headline"], "4 sessions need you: 2 waiting, 1 blocked, 1 failed")
        self.assertEqual(digest["urgency"], "critical")  # a failed session is present

    def test_reboot_orphan_storm_digests_at_normal_urgency(self):
        # Four sessions orphaned by one Herdr outage (detail "Herdr server
        # not running") are each classified normal, and the digest they
        # roll into carries that classification instead of escalating to
        # critical by being many.
        watcher = self.make_watcher()
        for i, sid in enumerate(["s1", "s2", "s3", "s4"]):
            write_session(self.tmpdir, sid, sid)
            append_event(self.tmpdir, sid, 1, "status.changed",
                         {"from": "working", "to": "orphaned", "detail": "Herdr server not running"})
            self.clock.advance(1.0)
            watcher.scan_once(now=self.clock())
        digest = self.notifier.sent[3]
        self.assertEqual(digest["exec_argv"], ["omarchy-agent-session-list"])
        self.assertEqual(digest["urgency"], "normal")
        # One agent that died on its own is critical, and so is the digest
        # that contains it.
        write_session(self.tmpdir, "s5", "s5")
        append_event(self.tmpdir, "s5", 1, "status.changed",
                     {"from": "working", "to": "orphaned", "detail": "pane not found in Herdr's list"})
        self.clock.advance(1.0)
        watcher.scan_once(now=self.clock())
        self.assertEqual(self.notifier.sent[-1]["urgency"], "critical")

    def test_restart_orphans_notify_at_normal_urgency_too(self):
        # Four sessions orphaned by one Herdr outage (detail "Herdr server
        # not running") are each classified normal, and the digest they
        # roll into carries that classification instead of escalating to
        # critical by being many.
        watcher = self.make_watcher()
        for i, sid in enumerate(["s1", "s2", "s3", "s4"]):
            write_session(self.tmpdir, sid, sid)
            append_event(self.tmpdir, sid, 1, "status.changed",
                         {"from": "working", "to": "orphaned", "detail": "Herdr restarted; Enter revives"})
            self.clock.advance(1.0)
            watcher.scan_once(now=self.clock())
        digest = self.notifier.sent[3]
        self.assertEqual(digest["exec_argv"], ["omarchy-agent-session-list"])
        self.assertEqual(digest["urgency"], "normal")
        # One agent that died on its own is critical, and so is the digest
        # that contains it.
        write_session(self.tmpdir, "s5", "s5")
        append_event(self.tmpdir, "s5", 1, "status.changed",
                     {"from": "working", "to": "orphaned", "detail": "pane not found in Herdr's list"})
        self.clock.advance(1.0)
        watcher.scan_once(now=self.clock())
        self.assertEqual(self.notifier.sent[-1]["urgency"], "critical")

    def test_fifth_event_updates_the_same_digest_by_distinct_session_count(self):
        watcher = self.make_watcher()
        for sid, to_state in [("s1", "waiting"), ("s2", "waiting"), ("s3", "blocked"), ("s4", "failed")]:
            write_session(self.tmpdir, sid, sid)
            append_event(self.tmpdir, sid, 1, "status.changed", {"from": "working", "to": to_state})
            self.clock.advance(1.0)
            watcher.scan_once(now=self.clock())
        digest_id = self.notifier.sent[3]["id"]

        # s1 (already counted) flips to blocked. Still 4 *distinct* sessions,
        # not 5: a session re-firing inside the window updates its own tally
        # entry rather than being counted as a new session.
        self.clock.advance(1.0)
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "waiting", "to": "blocked"})
        watcher.scan_once(now=self.clock())

        self.assertEqual(len(self.notifier.sent), 5)
        second_digest = self.notifier.sent[4]
        self.assertEqual(second_digest["replaces_id"], digest_id)
        self.assertEqual(second_digest["headline"], "4 sessions need you: 1 waiting, 2 blocked, 1 failed")

    def test_digest_resets_after_a_full_quiet_window(self):
        watcher = self.make_watcher()
        for sid, to_state in [("s1", "waiting"), ("s2", "waiting"), ("s3", "blocked"), ("s4", "failed")]:
            write_session(self.tmpdir, sid, sid)
            append_event(self.tmpdir, sid, 1, "status.changed", {"from": "working", "to": to_state})
            self.clock.advance(1.0)
            watcher.scan_once(now=self.clock())
        self.assertEqual(self.notifier.sent[3]["exec_argv"], ["omarchy-agent-session-list"])

        # A full 60s window with no further notify-worthy event: the digest
        # episode should be over, and the next event should be individual
        # again, not a replace of the old, long-gone digest toast.
        self.clock.advance(61.0)
        write_session(self.tmpdir, "s5", "s5")
        append_event(self.tmpdir, "s5", 1, "status.changed", {"from": "working", "to": "waiting"})
        watcher.scan_once(now=self.clock())

        newest = self.notifier.sent[-1]
        self.assertEqual(newest["headline"], "s5 needs you")
        self.assertNotEqual(newest["exec_argv"], ["omarchy-agent-session-list"])
        self.assertIsNone(newest["replaces_id"])


class TestSelfSuppressionAndFullscreen(WatchTestCase):
    def test_focused_session_is_never_notified(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})

        focused_window = {"initialClass": "org.omarchy.session.s1", "fullscreen": 0}
        watcher = self.make_watcher(active_window_fn=lambda: focused_window)
        watcher.scan_once(now=self.clock())

        self.assertEqual(self.notifier.sent, [])

    def test_unfocused_session_still_notifies_when_a_different_window_is_active(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})

        other_window = {"initialClass": "org.omarchy.session.s2", "fullscreen": 0}
        watcher = self.make_watcher(active_window_fn=lambda: other_window)
        watcher.scan_once(now=self.clock())

        self.assertEqual(len(self.notifier.sent), 1)

    def test_fullscreen_holds_the_notice_until_fullscreen_ends(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})

        state = {"fullscreen": True}
        watcher = self.make_watcher(active_window_fn=lambda: {"fullscreen": state["fullscreen"]})
        watcher.scan_once(now=self.clock())
        self.assertEqual(self.notifier.sent, [], "held during fullscreen, not sent")

        self.clock.advance(2.0)
        state["fullscreen"] = False
        watcher.scan_once(now=self.clock())  # no new event; this pass only notices fullscreen ended

        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["headline"], "api-refactor needs you")

    def test_a_second_event_held_during_fullscreen_overwrites_the_first(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})

        state = {"fullscreen": True}
        watcher = self.make_watcher(active_window_fn=lambda: {"fullscreen": state["fullscreen"]})
        watcher.scan_once(now=self.clock())

        # The second held event is `done` rather than `blocked`: waiting and
        # blocked share a title and body since 2026-09-02, so a done event is
        # what makes "the latest one won" observable at the notifier.
        self.clock.advance(1.0)
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "waiting", "to": "done"})
        watcher.scan_once(now=self.clock())
        self.assertEqual(self.notifier.sent, [], "still fullscreen, still held")

        self.clock.advance(1.0)
        state["fullscreen"] = False
        watcher.scan_once(now=self.clock())

        # Only one notice is delivered on flush: the latest ("finished"),
        # not both -- "one pending notice per session" holds through the
        # hold queue too.
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["headline"], "api-refactor finished")


class TestCursorPersistenceAndRestart(WatchTestCase):
    def test_restart_does_not_replay_already_seen_events(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())
        self.assertEqual(len(self.notifier.sent), 1)

        # Simulate a restart: a fresh SessionWatcher over the same directory,
        # loading .watch-cursors.json from disk instead of memory.
        self.clock.advance(3.0)
        watcher2 = watch.SessionWatcher(self.tmpdir, self.notifier, clock=self.clock, active_window_fn=lambda: None)
        watcher2.scan_once(now=self.clock())
        self.assertEqual(len(self.notifier.sent), 1, "no duplicate notice for an event already processed")

        # A genuinely new event after the simulated restart is still caught.
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "waiting", "to": "done"})
        watcher2.scan_once(now=self.clock())
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.notifier.sent[1]["headline"], "api-refactor finished")

    def test_cursor_file_is_valid_json_on_disk(self):
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})
        watcher = self.make_watcher()
        watcher.scan_once(now=self.clock())

        cursor_path = self.tmpdir / ".watch-cursors.json"
        self.assertTrue(cursor_path.exists())
        data = json.loads(cursor_path.read_text())
        self.assertEqual(data.get("s1"), 1)


class TestDryRunPrintsArgv(WatchTestCase):
    def test_dry_run_prints_the_argv_instead_of_calling_the_real_binary(self):
        calls = []

        class FakeCompletedProcess:
            returncode = 0
            stdout = "42\n"
            stderr = ""

        def fake_runner(*a, **k):
            calls.append((a, k))
            return FakeCompletedProcess()

        notifier = watch.Notifier(dry_run=False, runner=fake_runner)
        write_session(self.tmpdir, "s1", "api-refactor")
        append_event(self.tmpdir, "s1", 1, "status.changed", {"from": "working", "to": "waiting"})

        watcher = watch.SessionWatcher(self.tmpdir, notifier, clock=self.clock, active_window_fn=lambda: None)
        watcher.scan_once(now=self.clock())
        self.assertEqual(len(calls), 1, "dry_run=False must still call the runner")

        # Now the actual --dry-run path: no runner call at all.
        self.clock.advance(20.0)
        dry_notifier = watch.Notifier(dry_run=True)
        dry_watcher = watch.SessionWatcher(self.tmpdir, dry_notifier, clock=self.clock, active_window_fn=lambda: None)
        append_event(self.tmpdir, "s1", 2, "status.changed", {"from": "waiting", "to": "done"})
        dry_watcher.scan_once(now=self.clock())

        self.assertEqual(len(dry_notifier.sent), 1)
        argv = dry_notifier.sent[0]["argv"]
        self.assertEqual(argv[0], "omarchy-notification-send")
        self.assertIn("--urgency", argv)
        self.assertIn("--exec", argv)
        # A done notice's click opens the receipt, not the session
        # (2026-09-02); the full exec argv is pinned in TestCopyAndClickTargets.
        self.assertIn("omarchy-agent-session-receipt", argv)
        self.assertNotIn("omarchy-agent-session-open", argv)


if __name__ == "__main__":
    unittest.main()


class TestPauseIsQuiet(unittest.TestCase):
    """06-notifications.md: a pause is a person's choice; nothing announces it."""

    def test_status_changed_to_paused_is_not_notify_worthy(self):
        event = {"type": "status.changed", "data": {"from": "idle", "to": "paused", "detail": "paused"}}
        self.assertIsNone(watch.classify_event(event))
        self.assertIsNone(watch.classify_event({"type": "runtime.unbound", "data": {"reason": "paused"}}))
