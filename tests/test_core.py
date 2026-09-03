"""Unit and integration tests for omarchy-agent-session-core.

Run with: python3 -m unittest discover -s tests -v   (from build/core/)

Most tests call core.main([...]) in-process (env vars set per test, stdout
captured via redirect_stdout) rather than spawning a subprocess, so a fake
Herdr server running as a thread in this same process can be inspected
directly afterward. One subprocess-level smoke test (test_cli_is_executable)
still exercises the real shebang/executable bit end to end.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TESTS_DIR = pathlib.Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent
CORE_PATH = CORE_DIR / "bin" / "omarchy-agent-session-core"

sys.path.insert(0, str(TESTS_DIR))
from fake_herdr import FakeHerdrServer  # noqa: E402


def load_core():
    loader = importlib.machinery.SourceFileLoader("omarchy_agent_session_core_under_test", str(CORE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


core = load_core()


def make_bare_session(store, name, mode="personal", agent_kind="claude", state="working",
                       runtime=None, workspace=None, parent_id=None, goal_text=None, cwd=None):
    """Builds and saves a session record directly through the store, for
    tests that don't want a real (or fake-Herdr-backed) `new` launch."""
    session_id = core.new_ulid()
    creator = core.current_human_actor()
    workspace = workspace or {
        "repo_root": None, "worktree_path": None, "branch": None,
        "base_branch": None, "created_by_session": False,
    }
    record = core.new_session_record(
        session_id, name, creator, creator, mode, agent_kind, workspace,
        cwd=cwd, command=None, parent_id=parent_id, goal_text=goal_text,
    )
    ev = store.append_event(session_id, "session.created", creator, {"name": name})
    record["state_version"] = ev["seq"]
    if runtime is not None:
        ev = store.append_event(session_id, "runtime.bound", creator, runtime)
        record["runtime"] = runtime
        record["state_version"] = ev["seq"]
    if state != "starting":
        ev = store.append_event(session_id, "status.changed", creator,
                                 {"from": record["status"]["state"], "to": state, "source": "test", "detail": None})
        record["status"] = {"state": state, "since": ev["ts"], "source": "test", "detail": None}
        record["state_version"] = ev["seq"]
    store.save_session(record)
    return session_id


class CoreTestCase(unittest.TestCase):
    """Common fixture: an isolated sessions dir and a Herdr socket path,
    wired into os.environ so core.main()'s own env lookups pick them up."""

    def setUp(self):
        self._old_environ = dict(os.environ)
        self.sessions_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-sessions-"))
        self.herdr_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-herdr-"))
        self.herdr_socket = self.herdr_dir / "herdr.sock"
        self.plain_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-plain-"))
        os.environ["OMARCHY_SESSIONS_DIR"] = str(self.sessions_dir)
        os.environ["HERDR_SOCKET"] = str(self.herdr_socket)
        self.store = core.SessionStore(self.sessions_dir)
        self.fake_herdr = None

    def tearDown(self):
        if self.fake_herdr is not None:
            self.fake_herdr.stop()
        os.environ.clear()
        os.environ.update(self._old_environ)
        for d in (self.sessions_dir, self.herdr_dir, self.plain_dir, getattr(self, "worktrees_dir", None)):
            if d:
                shutil.rmtree(d, ignore_errors=True)
        os.environ.pop("HERDR_WORKTREES_DIR", None)

    def start_fake_herdr(self, agent_start_result=None):
        self.fake_herdr = FakeHerdrServer(self.herdr_socket)
        # Real Herdr 0.8.2 shapes (verified on the rig 2026-09-02): a
        # workspace.create returns its root pane; agent.start returns the
        # agent whose `name` is the alias the session layer calls agent_id.
        self.fake_herdr.set_result("workspace.create", {
            "root_pane": {"pane_id": "p1", "workspace_id": "w1", "tab_id": "t1"},
            "workspace": {"workspace_id": "w1"},
        })
        self.fake_herdr.set_result("agent.start", agent_start_result or (
            lambda params: {"agent": {"name": params.get("name", "a1"), "agent": params.get("kind"),
                                      "pane_id": params.get("pane_id", "p1"), "workspace_id": "w1", "tab_id": "t1",
                                      "agent_status": "idle"}}))
        self.fake_herdr.set_result("agent.list", {"agents": []})
        self.fake_herdr.set_result("pane.list", {"panes": []})
        self.fake_herdr.set_result("pane.report_metadata", {"type": "ok"})
        self.fake_herdr.start()
        return self.fake_herdr

    def run_cli(self, argv):
        """Calls core.main() in-process, returns (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = core.main(argv)
        return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# create + list, `new` printing the id, `list --json` shape
# --------------------------------------------------------------------------

class TestCreateAndList(CoreTestCase):
    def test_new_prints_id_on_stdout_and_creates_record(self):
        self.start_fake_herdr()
        rc, out, err = self.run_cli([
            "new", "--agent", "claude", "--mode", "personal", "--name", "api-refactor",
            "--cwd", str(self.plain_dir), "--no-worktree",
        ])
        self.assertEqual(rc, 0, err)
        session_id = out.strip()
        self.assertTrue(core.looks_like_ulid(session_id), session_id)

        record = self.store.try_load(session_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["name"], "api-refactor")
        self.assertEqual(record["agent"]["kind"], "claude")
        self.assertEqual(record["mode"], "personal")
        self.assertEqual(record["status"]["state"], "working")
        self.assertIsNotNone(record["runtime"])
        self.assertTrue(record["runtime"]["agent_id"])

        # agent.start actually received the Personal-mode flags, and a
        # workspace was created for the pane first.
        self.assertEqual(len(self.fake_herdr.calls("workspace.create")), 1)
        start_calls = self.fake_herdr.calls("agent.start")
        self.assertEqual(len(start_calls), 1)
        self.assertEqual(start_calls[0]["pane_id"], "p1")
        self.assertIn("--permission-mode", start_calls[0]["args"])
        self.assertIn("auto", start_calls[0]["args"])

    def test_started_with_command_reflects_the_actual_argv_not_the_process_argv(self):
        # Regression: started_with.command must be built from the argv
        # main() was actually given, not from sys.argv (which, called
        # in-process as every test here does, would be the test runner's
        # own command line, not "new --agent ...").
        self.start_fake_herdr()
        rc, out, err = self.run_cli([
            "new", "--agent", "claude", "--name", "argv-check",
            "--cwd", str(self.plain_dir), "--no-worktree",
        ])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(out.strip())
        command = record["started_with"]["command"]
        self.assertIn("--agent claude", command)
        self.assertIn("--name argv-check", command)
        self.assertNotIn("unittest", command)

    def test_new_with_explicit_json_prints_full_record(self):
        self.start_fake_herdr()
        rc, out, err = self.run_cli([
            "new", "--agent", "codex", "--mode", "personal", "--name", "with-json",
            "--cwd", str(self.plain_dir), "--no-worktree", "--json",
        ])
        self.assertEqual(rc, 0, err)
        record = json.loads(out)
        self.assertEqual(record["name"], "with-json")
        self.assertEqual(record["id"], record["id"])  # sanity: valid JSON with an id

    def test_new_without_name_derives_one_from_prompt_then_cwd(self):
        self.start_fake_herdr()
        rc, out, err = self.run_cli(["new", "--agent", "claude", "--cwd", str(self.plain_dir), "--no-worktree",
                                     "--prompt", "Append a line to README.md and commit"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(out.strip())["name"], "append-a-line-to")

        # No prompt: the (resolved) cwd's basename, not the parser's "." default.
        expected = core.slugify_name(self.plain_dir.name)
        rc, out, err = self.run_cli(["new", "--agent", "claude", "--cwd", str(self.plain_dir), "--no-worktree"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(out.strip())["name"], expected)

        # Taken names get -2, -3, ...; an ended session's name counts as
        # taken too, so a receipt's name stays unambiguous.
        make_bare_session(self.store, expected + "-2", state="done")
        rc, out, err = self.run_cli(["new", "--agent", "claude", "--cwd", str(self.plain_dir), "--no-worktree"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(out.strip())["name"], expected + "-3")

    def test_explicit_name_still_conflicts_with_a_live_session(self):
        self.start_fake_herdr()
        make_bare_session(self.store, "taken-name", state="working")
        rc, out, err = self.run_cli(["new", "--agent", "claude", "--name", "taken-name",
                                     "--cwd", str(self.plain_dir), "--no-worktree"])
        self.assertEqual(rc, 5, err)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_list_is_empty_with_no_sessions_and_no_herdr_call_needed(self):
        # No fake Herdr server started at all -- list must still work.
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"sessions": []})

    def test_list_after_two_creates(self):
        self.start_fake_herdr()
        rc1, out1, _ = self.run_cli(["new", "--agent", "claude", "--name", "one",
                                      "--cwd", str(self.plain_dir), "--no-worktree"])
        rc2, out2, _ = self.run_cli(["new", "--agent", "claude", "--name", "two",
                                      "--cwd", str(self.plain_dir), "--no-worktree"])
        self.assertEqual((rc1, rc2), (0, 0))
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        names = {s["name"] for s in json.loads(out)["sessions"]}
        self.assertEqual(names, {"one", "two"})


class TestListJsonShape(CoreTestCase):
    def test_list_json_shape_matches_spec(self):
        session_id = make_bare_session(self.store, "shape-check", state="working")
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(list(data.keys()), ["sessions"])
        self.assertEqual(len(data["sessions"]), 1)
        entry = data["sessions"][0]
        # spec-02's original key set, plus the panel-facing fields
        # (goal, mode, resumable, created_by, project) and status.source/
        # detail added for 03-sessions-panel.md's row.
        self.assertEqual(
            set(entry.keys()),
            {"id", "name", "agent", "status", "owner", "workspace", "needs_attention", "children", "state_version",
             "goal", "mode", "resumable", "revivable", "created_by", "project", "preview", "loop", "lane", "lanes",
             "visibility", "owner_display", "owned_by_other", "suggestions", "presence"},
        )
        self.assertEqual(set(entry["agent"].keys()), {"kind"})
        self.assertEqual(set(entry["status"].keys()), {"state", "since", "source", "detail"})
        self.assertEqual(set(entry["owner"].keys()), {"kind", "id", "label"})
        self.assertEqual(set(entry["workspace"].keys()), {"branch"})
        self.assertEqual(set(entry["created_by"].keys()), {"kind", "label"})
        self.assertIs(entry["needs_attention"], False)
        self.assertEqual(entry["children"], 0)
        self.assertIsInstance(entry["children"], int)
        self.assertEqual(entry["id"], session_id)
        self.assertIsNone(entry["goal"])
        self.assertIs(entry["resumable"], False)
        self.assertIsNone(entry["project"])

    def test_list_json_panel_fields(self):
        # (a) goal is the first non-empty line; mode is copied; resumable
        # flips on harness_session_ref; project falls back repo_root -> cwd.
        goal = "\n  Append a line to README.md  \nsecond line explains why\n"
        workspace = {"repo_root": "/home/x/Work/omarchy-multiplayer/", "worktree_path": "/home/x/.herdr/worktrees/x",
                     "branch": "session/x", "base_branch": "main", "created_by_session": True}
        session_id = make_bare_session(self.store, "panel-fields", mode="shared", state="working",
                                       workspace=workspace, goal_text=goal, cwd="/home/x/Work/omarchy-multiplayer/sub")
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        entry = json.loads(out)["sessions"][0]
        self.assertEqual(entry["goal"], "Append a line to README.md")
        self.assertEqual(entry["mode"], "shared")
        self.assertIs(entry["resumable"], False)
        self.assertEqual(entry["project"], "omarchy-multiplayer")  # repo_root wins, trailing slash ignored
        me = core.current_human_actor()
        self.assertEqual(entry["created_by"], {"kind": "human", "label": me["label"]})
        self.assertEqual(entry["status"]["source"], "test")
        self.assertIsNone(entry["status"]["detail"])

        record = self.store.try_load(session_id)
        record["agent"]["harness_session_ref"] = "abc123"
        record["workspace"]["repo_root"] = None
        self.store.save_session(record)
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        entry = json.loads(out)["sessions"][0]
        self.assertIs(entry["resumable"], True)
        self.assertEqual(entry["project"], "sub")  # started_with.cwd's basename once repo_root is gone

        record["agent"]["harness_session_ref"] = ""
        record["started_with"]["cwd"] = None
        record["goal"] = {"text": "x" * 200, "set_by": record["created_by"], "set_at": record["created_at"]}
        self.store.save_session(record)
        entry = json.loads(self.run_cli(["list", "--json"])[1])["sessions"][0]
        self.assertIs(entry["resumable"], False)  # an empty ref is not resumable
        self.assertIsNone(entry["project"])
        self.assertEqual(len(entry["goal"]), 120)

    def test_list_text_output_is_unchanged_by_the_json_additions(self):
        session_id = make_bare_session(self.store, "text-row", state="waiting", goal_text="a goal")
        rc, out, err = self.run_cli(["list"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f"! {session_id}  {'text-row':<24} {'waiting':<10} claude\n")


def age_session(store, session_id, seconds_ago):
    """Moves a record's `status.since` into the past, so a window or a
    history test can end a session yesterday without waiting."""
    record = store.try_load(session_id)
    when = core.dt.datetime.now(core.dt.timezone.utc) - core.dt.timedelta(seconds=seconds_ago)
    record["status"]["since"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    store.save_session(record)
    return record


class TestWindowAndHistory(CoreTestCase):
    """The panel's window and the history surface (02-command-surface.md,
    2026-09-03): the store keeps every record; a surface asks for the
    slice it shows and hears how much lies outside it."""

    def test_list_ended_within_keeps_live_and_recent_ended_and_counts_earlier(self):
        live = make_bare_session(self.store, "live-for-days", state="idle")
        age_session(self.store, live, 3 * 86400)  # idle for three days: still in the window
        orphan = make_bare_session(self.store, "orphan", state="orphaned")
        age_session(self.store, orphan, 3 * 86400)
        recent = make_bare_session(self.store, "ended-today", state="stopped")
        age_session(self.store, recent, 3600)
        old = make_bare_session(self.store, "ended-last-week", state="done")
        age_session(self.store, old, 3 * 86400)
        rc, out, err = self.run_cli(["list", "--ended-within", "24h", "--json"])
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual({s["name"] for s in data["sessions"]}, {"live-for-days", "orphan", "ended-today"})
        self.assertEqual(data["window"], {"ended_within": "24h", "earlier_ended": 1})
        # Without the flag the list is everything, and there is no window key.
        data = json.loads(self.run_cli(["list", "--json"])[1])
        self.assertEqual(len(data["sessions"]), 4)
        self.assertNotIn("window", data)
        # A wider window brings the old one back.
        data = json.loads(self.run_cli(["list", "--ended-within", "14d", "--json"])[1])
        self.assertEqual(len(data["sessions"]), 4)
        self.assertEqual(data["window"]["earlier_ended"], 0)

    def test_list_ended_within_does_not_count_a_lane_as_earlier(self):
        parent = make_bare_session(self.store, "parent", state="stopped")
        age_session(self.store, parent, 3 * 86400)
        lane = make_bare_session(self.store, "parent.claude", state="stopped", parent_id=parent)
        record = self.store.try_load(lane)
        record["lane"] = {"name": "claude", "parent_id": parent, "parent_name": "parent"}
        record["lineage"]["spawn_reason"] = "lane"
        self.store.save_session(record)
        age_session(self.store, lane, 3 * 86400)
        data = json.loads(self.run_cli(["list", "--ended-within", "24h", "--json"])[1])
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["window"]["earlier_ended"], 1)

    def test_list_ended_within_rejects_a_bad_duration(self):
        make_bare_session(self.store, "any", state="idle")
        for bad in ("24", "h", "1w", "24 h", "3.5h"):
            rc, out, err = self.run_cli(["list", "--ended-within", bad, "--json"])
            self.assertEqual(rc, 2, bad)
            self.assertIn("24h", err)
        self.assertEqual(self.run_cli(["list", "--ended-within", "-3h", "--json"])[0], 2)  # argparse's own refusal

    def test_list_json_carries_revivable(self):
        # revivable is the core's rule, in one place: orphaned; an inferred
        # end with a transcript; a stop with a transcript. Nothing else.
        cases = [
            ("orphan", "orphaned", None, None, True),
            ("orphan-no-ref", "orphaned", "", None, True),
            ("inferred", "failed", "abc", "harness exited while working", True),
            ("stopped-with-ref", "stopped", "abc", "stopped", True),
            ("stopped-no-ref", "stopped", None, "stopped", False),
            ("done-by-verdict", "done", "abc", "kept", False),
            ("working", "working", "abc", None, False),
        ]
        for name, state, ref, detail, expected in cases:
            sid = make_bare_session(self.store, name, state=state)
            record = self.store.try_load(sid)
            record["agent"]["harness_session_ref"] = ref
            record["status"]["detail"] = detail
            self.store.save_session(record)
        entries = {e["name"]: e for e in json.loads(self.run_cli(["list", "--json"])[1])["sessions"]}
        for name, _state, _ref, _detail, expected in cases:
            self.assertIs(entries[name]["revivable"], expected, name)

    def test_history_groups_ended_sessions_by_day_newest_first(self):
        today = make_bare_session(self.store, "ended-today", state="stopped")
        record = self.store.try_load(today)
        record["agent"]["harness_session_ref"] = "abc"
        self.store.save_session(record)
        age_session(self.store, today, 600)
        two_days = make_bare_session(self.store, "ended-two-days-ago", state="done")
        record = self.store.try_load(two_days)
        record["status"]["detail"] = "kept"
        self.store.save_session(record)
        age_session(self.store, two_days, 2 * 86400)
        way_back = make_bare_session(self.store, "ended-last-month", state="failed")
        age_session(self.store, way_back, 30 * 86400)
        make_bare_session(self.store, "still-working", state="working")

        rc, out, err = self.run_cli(["history", "--json"])
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["window"], {"days": 14, "ended": 2, "earlier_ended": 1})
        self.assertEqual([[s["name"] for s in d["sessions"]] for d in data["days"]],
                         [["ended-today"], ["ended-two-days-ago"]])
        self.assertGreater(data["days"][0]["date"], data["days"][1]["date"])
        self.assertIs(data["days"][0]["sessions"][0]["revivable"], True)

        rc, out, err = self.run_cli(["history"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("History · last 14 days · 2 ended · 1 earlier\n"), out)
        self.assertIn("ended-today", out)
        self.assertIn("stopped · resumes", out)
        self.assertIn("done · kept", out)
        self.assertNotIn("ended-last-month", out)
        self.assertNotIn("still-working", out)
        self.assertIn("open <name> resumes", out)
        lines = out.splitlines()
        day_headers = [l for l in lines if l.endswith(" · 1 ended")]
        self.assertEqual(len(day_headers), 2)
        self.assertLess(lines.index(day_headers[0]), lines.index(day_headers[1]))

        # --days reaches further back; a bad value is a usage error.
        data = json.loads(self.run_cli(["history", "--days", "60", "--json"])[1])
        self.assertEqual(data["window"], {"days": 60, "ended": 3, "earlier_ended": 0})
        self.assertEqual(self.run_cli(["history", "--days", "0"])[0], 2)

    def test_history_folds_lanes_out_and_says_when_nothing_ended(self):
        parent = make_bare_session(self.store, "parent", state="stopped")
        lane = make_bare_session(self.store, "parent.claude", state="stopped", parent_id=parent)
        record = self.store.try_load(lane)
        record["lane"] = {"name": "claude", "parent_id": parent, "parent_name": "parent"}
        record["lineage"]["spawn_reason"] = "lane"
        self.store.save_session(record)
        data = json.loads(self.run_cli(["history", "--json"])[1])
        self.assertEqual([s["name"] for s in data["days"][0]["sessions"]], ["parent"])

        age_session(self.store, parent, 20 * 86400)
        age_session(self.store, lane, 20 * 86400)
        rc, out, err = self.run_cli(["history"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Nothing ended in the last 14 days. 1 ended before that: --days 28 reaches further back.", out)

    def test_needs_attention_true_exactly_for_waiting_and_blocked(self):
        waiting_id = make_bare_session(self.store, "wait-me", state="waiting")
        blocked_id = make_bare_session(self.store, "block-me", state="blocked")
        idle_id = make_bare_session(self.store, "idle-me", state="idle")
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        by_id = {s["id"]: s for s in json.loads(out)["sessions"]}
        self.assertTrue(by_id[waiting_id]["needs_attention"])
        self.assertTrue(by_id[blocked_id]["needs_attention"])
        self.assertFalse(by_id[idle_id]["needs_attention"])

    def test_children_count_reflects_lineage(self):
        parent_id = make_bare_session(self.store, "parent")
        parent = self.store.try_load(parent_id)
        parent["lineage"]["children"] = ["childA", "childB"]
        self.store.save_session(parent)
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        entry = next(s for s in json.loads(out)["sessions"] if s["id"] == parent_id)
        self.assertEqual(entry["children"], 2)


# --------------------------------------------------------------------------
# state transitions and event seq monotonicity
# --------------------------------------------------------------------------

class TestStateMachineAndEvents(CoreTestCase):
    def test_valid_transitions_from_spec_table(self):
        valid_pairs = [
            ("starting", "working"), ("working", "waiting"), ("working", "blocked"),
            ("waiting", "working"), ("blocked", "working"), ("working", "idle"),
            ("idle", "working"), ("orphaned", "working"),
            ("working", "done"), ("waiting", "failed"), ("blocked", "stopped"),
            ("idle", "orphaned"),
        ]
        for a, b in valid_pairs:
            self.assertTrue(core.is_valid_transition(a, b), f"{a} -> {b} should be valid")

    def test_invalid_transitions(self):
        invalid_pairs = [
            ("done", "working"), ("failed", "working"), ("stopped", "working"),
            ("starting", "idle"), ("idle", "blocked"), ("waiting", "blocked"),
        ]
        for a, b in invalid_pairs:
            self.assertFalse(core.is_valid_transition(a, b), f"{a} -> {b} should be invalid")

    def test_event_seq_is_monotonic_and_state_version_matches(self):
        session_id = make_bare_session(self.store, "seq-check")
        actor = core.system_actor()
        seqs = []
        for i in range(5):
            ev = self.store.append_event(session_id, "status.changed", actor,
                                          {"from": "working", "to": "working", "source": "test", "detail": str(i)})
            seqs.append(ev["seq"])
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + 5)))
        # Never decreases, and a fresh SessionStore (simulating a new
        # process) reading the same directory agrees on the next seq.
        other_store = core.SessionStore(self.sessions_dir)
        next_ev = other_store.append_event(session_id, "status.changed", actor,
                                            {"from": "working", "to": "idle", "source": "test", "detail": None})
        self.assertEqual(next_ev["seq"], seqs[-1] + 1)

    def test_every_event_has_an_actor(self):
        session_id = make_bare_session(self.store, "actor-check")
        for e in self.store.read_events(session_id):
            self.assertIn("actor", e)
            self.assertIn(e["actor"]["kind"], ("human", "agent", "system"))

    def test_state_version_never_decreases_across_saves(self):
        session_id = make_bare_session(self.store, "monotone")
        record = self.store.try_load(session_id)
        versions = [record["state_version"]]
        actor = core.system_actor()
        for _ in range(3):
            ev = self.store.append_event(session_id, "status.changed", actor,
                                          {"from": "working", "to": "working", "source": "test", "detail": None})
            record["state_version"] = ev["seq"]
            self.store.save_session(record)
            versions.append(record["state_version"])
        self.assertEqual(versions, sorted(versions))


# --------------------------------------------------------------------------
# invariant 5: refuse shared/restricted + bypass
# --------------------------------------------------------------------------

class TestInvariant5(CoreTestCase):
    def test_refuses_shared_with_smuggled_bypass_flag(self):
        self.start_fake_herdr()
        rc, out, err = self.run_cli([
            "new", "--agent", "claude", "--mode", "shared", "--name", "sneaky",
            "--cwd", str(self.plain_dir), "--no-worktree",
            "--", "--permission-mode", "auto",
        ])
        self.assertEqual(rc, 5, err)
        self.assertIn("invariant 5", err)
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.fake_herdr.calls("agent.start"), [])

    def test_refuses_restricted_for_a_harness_with_no_safe_expression(self):
        # hermes has no config that satisfies restricted's contract at all.
        self.start_fake_herdr()
        rc, out, err = self.run_cli([
            "new", "--agent", "hermes", "--mode", "restricted", "--name", "no-go",
            "--cwd", str(self.plain_dir), "--no-worktree",
        ])
        self.assertEqual(rc, 5, err)
        self.assertEqual(self.store.list_sessions(), [])

    def test_codex_ask_for_approval_never_without_readonly_sandbox_is_refused(self):
        self.start_fake_herdr()
        rc, out, err = self.run_cli([
            "new", "--agent", "codex", "--mode", "shared", "--name", "codex-sneaky",
            "--cwd", str(self.plain_dir), "--no-worktree",
            "--", "--ask-for-approval", "never",
        ])
        self.assertEqual(rc, 5, err)

    def test_codex_ask_for_approval_never_with_readonly_sandbox_is_allowed_shape(self):
        # Direct unit check of the predicate (not a full `new`, since the
        # shared cell's own flags already include a workspace-write
        # sandbox; this exercises check_invariant5 in isolation instead).
        core.check_invariant5("codex", "shared",
                               ["--sandbox", "read-only", "--ask-for-approval", "never"], {})

    def test_personal_mode_is_never_checked(self):
        core.check_invariant5("claude", "personal", ["--permission-mode", "auto"], {})

    def test_new_allows_personal_with_bypass_flags(self):
        self.start_fake_herdr()
        rc, out, err = self.run_cli([
            "new", "--agent", "claude", "--mode", "personal", "--name", "personal-ok",
            "--cwd", str(self.plain_dir), "--no-worktree",
        ])
        self.assertEqual(rc, 0, err)


# --------------------------------------------------------------------------
# marker line on send --from
# --------------------------------------------------------------------------

class TestSendMarker(CoreTestCase):
    def test_marker_line_delivered_to_herdr_on_send_from(self):
        self.start_fake_herdr()
        origin_id = make_bare_session(self.store, "origin-session")
        target_runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                           "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        target_id = make_bare_session(self.store, "target-session", runtime=target_runtime)

        rc, out, err = self.run_cli(["send", target_id, "add a regression test", "--from", origin_id])
        self.assertEqual(rc, 0, err)

        calls = self.fake_herdr.calls("agent.prompt")
        self.assertEqual(len(calls), 1)
        expected = core.format_marked_instruction(origin_id, "origin-session", "add a regression test")
        self.assertEqual(calls[0]["text"], expected)
        self.assertTrue(expected.startswith(f"[omarchy-session-message from={origin_id} name=origin-session]\n\n"))

        # The stored instruction keeps origin_session as structured
        # provenance; instruction.delivered confirms delivery.
        events = self.store.read_events(target_id)
        queued = [e for e in events if e["type"] == "instruction.queued"][0]
        self.assertEqual(queued["data"]["origin_session"], origin_id)
        self.assertEqual(queued["data"]["text"], "add a regression test")
        delivered = [e for e in events if e["type"] == "instruction.delivered"]
        self.assertEqual(len(delivered), 1)

    def test_marker_strips_newlines_and_brackets_from_name(self):
        text = core.format_marked_instruction("01J", "evil]\nname", "hi")
        first_line = text.splitlines()[0]
        self.assertTrue(first_line.startswith("[omarchy-session-message from=01J name="))
        self.assertTrue(first_line.endswith("]"))
        # Everything between "name=" and the marker's own closing "]" must
        # have had embedded "]" and newlines stripped, per spec-02.
        name_segment = first_line[len("[omarchy-session-message from=01J name="):-1]
        self.assertNotIn("]", name_segment)
        self.assertNotIn("\n", name_segment)
        self.assertEqual(name_segment, "evil name")

    def test_human_send_carries_no_marker(self):
        self.start_fake_herdr()
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        target_id = make_bare_session(self.store, "target-2", runtime=runtime)
        rc, out, err = self.run_cli(["send", target_id, "plain human text"])
        self.assertEqual(rc, 0, err)
        calls = self.fake_herdr.calls("agent.prompt")
        self.assertEqual(calls[0]["text"], "plain human text")

    def test_send_waits_for_interactive_ready_before_prompting(self):
        # Evaluation run 1 (2026-09-02): Herdr said idle before the harness
        # took input, the prompt typed into nothing, the session sat idle.
        self.start_fake_herdr()
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "not-yet-ready"}
        target_id = make_bare_session(self.store, "not-yet-ready", runtime=runtime)
        polls = {"n": 0}

        def agent_list(params):
            polls["n"] += 1
            ready = polls["n"] >= 3
            return {"agents": [{"name": "not-yet-ready", "agent": "claude", "pane_id": "p1",
                                "agent_status": "idle", "interactive_ready": ready}]}

        self.fake_herdr.set_result("agent.list", agent_list)
        rc, out, err = self.run_cli(["send", target_id, "hello"])
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(polls["n"], 3)
        self.assertEqual(len(self.fake_herdr.calls("agent.prompt")), 1)

    def test_send_drops_when_the_harness_never_becomes_ready(self):
        self.start_fake_herdr()
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "never-ready"}
        target_id = make_bare_session(self.store, "never-ready", runtime=runtime)
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "never-ready", "agent": "claude", "pane_id": "p1",
                                                              "agent_status": "idle", "interactive_ready": False}]})
        original = core.wait_interactive_ready
        with mock.patch.object(core, "wait_interactive_ready",
                               lambda herdr, alias, **kw: original(herdr, alias, timeout_s=0.6, step_s=0.1)):
            rc, out, err = self.run_cli(["send", target_id, "hello"])
        self.assertEqual(rc, 5)
        self.assertEqual(self.fake_herdr.calls("agent.prompt"), [])
        dropped = [e for e in self.store.read_events(target_id) if e["type"] == "instruction.dropped"]
        self.assertEqual(dropped[0]["data"]["reason"], "agent_not_ready")

    def test_send_wait_translates_idle_to_herdrs_done_as_well(self):
        self.start_fake_herdr()
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        target_id = make_bare_session(self.store, "wait-idle", runtime=runtime)
        rc, out, err = self.run_cli(["send", target_id, "go", "--wait", "--until", "idle", "--timeout", "5000"])
        self.assertEqual(rc, 0, err)
        call = self.fake_herdr.calls("agent.prompt")[0]
        self.assertEqual(sorted(call["wait"]["until"]), ["done", "idle"])

    def test_send_wait_timeout_after_delivery_is_not_a_drop(self):
        # Run 3 (2026-09-02): the commit landed while the wait was still
        # running, and the record said `instruction.dropped`.
        self.start_fake_herdr()
        self.fake_herdr.set_error("agent.prompt", "timeout", "timed out waiting for agent status")
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        target_id = make_bare_session(self.store, "wait-late", runtime=runtime)
        rc, out, err = self.run_cli(["send", target_id, "go", "--wait", "--timeout", "5000"])
        self.assertEqual(rc, 0, err)
        self.assertIn("wait ran out", err)
        events = self.store.read_events(target_id)
        types = [e["type"] for e in events]
        self.assertNotIn("instruction.dropped", types)
        delivered = [e for e in events if e["type"] == "instruction.delivered"][0]
        self.assertTrue(delivered["data"].get("wait_timed_out"))
        # Any other refusal is still a drop.
        self.fake_herdr.set_error("agent.prompt", "blocked", "agent is blocked and requires interactive input")
        rc, out, err = self.run_cli(["send", target_id, "go again"])
        self.assertEqual(rc, 5)

    def test_send_to_unbound_session_stays_queued_no_herdr_call(self):
        # No fake Herdr server running at all.
        target_id = make_bare_session(self.store, "unbound-target", runtime=None, state="starting")
        rc, out, err = self.run_cli(["send", target_id, "queue me"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(target_id)
        self.assertEqual(len(session["queue"]), 1)
        self.assertEqual(session["queue"][0]["text"], "queue me")


# --------------------------------------------------------------------------
# receipt computation against a temporary git repo with two commits
# --------------------------------------------------------------------------

class TestReceiptComputation(CoreTestCase):
    def _git(self, repo, *args):
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {args} failed: {r.stderr}")
        return r

    def make_git_repo_with_two_commits(self):
        repo = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-git-"))
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "base commit")
        self._git(repo, "checkout", "-b", "session/test-branch")
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        self._git(repo, "add", "a.txt")
        self._git(repo, "commit", "-m", "add a")
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        self._git(repo, "add", "b.txt")
        self._git(repo, "commit", "-m", "add b")
        return repo

    def test_receipt_fields_computed_from_git(self):
        repo = self.make_git_repo_with_two_commits()
        try:
            workspace = {
                "repo_root": str(repo), "worktree_path": str(repo),
                "branch": "session/test-branch", "base_branch": "main",
                "created_by_session": True,
            }
            session_id = make_bare_session(self.store, "receipt-test", workspace=workspace)
            session = self.store.try_load(session_id)
            receipt = core.compute_receipt(self.store, session)

            self.assertEqual(len(receipt["commits"]), 2)
            self.assertEqual([c["subject"] for c in receipt["commits"]], ["add a", "add b"])
            for c in receipt["commits"]:
                self.assertEqual(c["author"], "Test User")
                self.assertEqual(len(c["sha"]), 40)

            self.assertEqual(receipt["diff_stat"]["files"], 2)
            self.assertEqual(receipt["diff_stat"]["insertions"], 2)
            self.assertEqual(receipt["diff_stat"]["deletions"], 0)

            self.assertFalse(receipt["dirty"])
            self.assertTrue(receipt["unpushed"])  # no remote at all
            self.assertEqual(receipt["session_id"], session_id)
            self.assertEqual(receipt["state_version"], session["state_version"])
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_receipt_dirty_true_with_uncommitted_change(self):
        repo = self.make_git_repo_with_two_commits()
        try:
            (repo / "a.txt").write_text("changed\n", encoding="utf-8")
            workspace = {
                "repo_root": str(repo), "worktree_path": str(repo),
                "branch": "session/test-branch", "base_branch": "main",
                "created_by_session": True,
            }
            session_id = make_bare_session(self.store, "dirty-test", workspace=workspace)
            session = self.store.try_load(session_id)
            receipt = core.compute_receipt(self.store, session)
            self.assertTrue(receipt["dirty"])
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_receipt_write_via_done_command(self):
        repo = self.make_git_repo_with_two_commits()
        try:
            workspace = {
                "repo_root": str(repo), "worktree_path": str(repo),
                "branch": "session/test-branch", "base_branch": "main",
                "created_by_session": True,
            }
            session_id = make_bare_session(self.store, "done-test", workspace=workspace)
            rc, out, err = self.run_cli(["done", session_id, "--verdict", "kept", "--note", "shipped it"])
            self.assertEqual(rc, 0, err)
            session = self.store.try_load(session_id)
            self.assertEqual(session["status"]["state"], "done")
            receipt_path = self.store.receipt_path(session_id)
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["end_state"], "done")
            self.assertEqual(receipt["end_reason"], "verdict:kept")
            self.assertEqual(len(receipt["artifacts"]), 1)
            self.assertEqual(receipt["artifacts"][0]["label"], "verdict")
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class TestClosedLoop(TestReceiptComputation):
    """09-closed-loop-surfaces.md: `done --verdict` closes the harness it
    leaves behind, and `show --loop` tells the whole story in order."""

    def test_done_closes_the_pane_and_unbinds(self):
        self.start_fake_herdr()
        runtime = {"backend": "herdr", "session": None, "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "loop-agent"}
        session_id = make_bare_session(self.store, "loop-done", runtime=runtime, state="idle")
        rc, out, err = self.run_cli(["done", session_id, "--verdict", "kept", "--note", "good"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(session_id)
        self.assertEqual(session["status"]["state"], "done")
        self.assertIsNone(session["runtime"])
        self.assertEqual([c["pane_id"] for c in self.fake_herdr.calls("pane.close")], ["p1"])
        types = [e["type"] for e in self.store.read_events(session_id)]
        self.assertIn("runtime.unbound", types)
        # A second verdict on an ended session is refused, exit 5.
        rc, out, err = self.run_cli(["done", session_id, "--verdict", "kept", "--note", "again"])
        self.assertEqual(rc, 5)

    def test_loop_view_orders_intent_instructions_artifacts_changes_and_verdict(self):
        repo = self.make_git_repo_with_two_commits()
        try:
            workspace = {"repo_root": str(repo), "worktree_path": str(repo),
                         "branch": "session/test-branch", "base_branch": "main", "created_by_session": True}
            session_id = make_bare_session(self.store, "loop-view", workspace=workspace, state="idle",
                                           goal_text="Greet by name\nREADME stays\nA capture of the page")
            note = self.plain_dir / "before.txt"
            note.write_text("what I saw\n")
            rc, out, err = self.run_cli(["artifact-add", session_id, "--kind", "file", "--source", str(note), "--label", "before"])
            self.assertEqual(rc, 0, err)
            artifact_path = out.strip()
            rc, out, err = self.run_cli(["send", session_id, "make the greeting say hello", "--about", artifact_path])
            self.assertEqual(rc, 0, err)  # unbound: queued, delivered on open
            rc, out, err = self.run_cli(["done", session_id, "--verdict", "kept", "--note", "the page greets"])
            self.assertEqual(rc, 0, err)
            rc, out, err = self.run_cli(["show", session_id, "--loop"])
            self.assertEqual(rc, 0, err)
            lines = out.splitlines()
            self.assertEqual(lines[0], "Intent")
            self.assertEqual(lines[1:4], ["  Greet by name", "  README stays", "  A capture of the page"])
            body = "\n".join(lines)
            self.assertIn("instruction  ", body)
            self.assertIn("[about: before]", body)
            self.assertIn("artifact     before", body)
            self.assertIn("artifact     verdict", body)
            self.assertIn("change       ", body)  # the repo's second commit sits on the branch
            self.assertIn("ended        done  verdict:kept", body)
            # Chronological: the intent block first, the ending last.
            self.assertTrue(lines[-1].endswith("verdict:kept") or "ended" in lines[-1] or "artifact" in lines[-1])
            # Within one second the record's own order holds: the capture an
            # instruction is about lists before the instruction (run 4).
            idx_before = next(i for i, l in enumerate(lines) if "artifact     before" in l)
            idx_instr = next(i for i, l in enumerate(lines) if "instruction  " in l)
            self.assertLess(idx_before, idx_instr)
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class TestPreviewAndCaptureFromThePanel(CoreTestCase):
    """09-closed-loop-surfaces.md sections 2, 3 and 7 as the panel needs
    them: preview and loop counts on list entries, a capture nobody clicks
    for, send --with-capture, preview --focus."""

    def test_list_entry_carries_preview_and_loop_counts(self):
        session_id = make_bare_session(self.store, "loopy", state="idle")
        rc, out, err = self.run_cli(["preview", session_id, "file:///tmp/hello.html"])
        self.assertEqual(rc, 0, err)
        note = self.plain_dir / "seen.png"
        note.write_bytes(b"\x89PNG\r\n\x1a\n")
        rc, out, err = self.run_cli(["capture", session_id, "--file", str(note), "--label", "seen"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["list", "--json"])
        entry = [e for e in json.loads(out)["sessions"] if e["id"] == session_id][0]
        self.assertEqual(entry["preview"], {"kind": "url", "value": "file:///tmp/hello.html"})
        self.assertEqual(entry["loop"], {"instructions": 0, "captures": 1})

    def test_preview_window_class_for_a_webapp_url(self):
        self.assertEqual(core.preview_window_class({"kind": "url", "value": "file:///home/omarchy/x/hello.html"}),
                         "chrome-__home_omarchy_x_hello.html-Default")
        self.assertEqual(core.preview_window_class({"kind": "app_id", "value": "org.example.app"}), "org.example.app")

    def test_send_with_capture_attaches_a_capture_and_ties_the_instruction_to_it(self):
        self.start_fake_herdr()
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "feedback", runtime=runtime, state="idle")
        fake_png = self.plain_dir / "grab.png"

        def fake_capture(session):
            fake_png.write_bytes(b"\x89PNG\r\n\x1a\n")
            return fake_png, "preview"

        with mock.patch.object(core, "capture_preview_to_file", fake_capture):
            rc, out, err = self.run_cli(["send", session_id, "make the heading bigger", "--with-capture"])
        self.assertEqual(rc, 0, err)
        events = self.store.read_events(session_id)
        added = [e for e in events if e["type"] == "artifact.added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["data"]["label"], "feedback-1")
        queued = [e for e in events if e["type"] == "instruction.queued"][0]
        self.assertEqual(queued["data"]["about_artifact"], added[0]["data"]["path"])
        self.assertEqual(self.fake_herdr.calls("agent.prompt")[0]["text"], "make the heading bigger")

    def test_preview_focus_without_a_registered_preview_is_a_conflict(self):
        session_id = make_bare_session(self.store, "no-preview", state="idle")
        rc, out, err = self.run_cli(["preview", session_id, "--focus"])
        self.assertEqual(rc, 5)

    def test_capture_preview_falls_back_to_the_whole_screen_without_a_window(self):
        session_id = make_bare_session(self.store, "no-window", state="idle")
        calls = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            if argv[0] == "grim":
                pathlib.Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[0] == "hyprctl":
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            rc, out, err = self.run_cli(["capture", session_id, "--preview"])
        self.assertEqual(rc, 0, err)
        grim = [c for c in calls if c[0] == "grim"][0]
        self.assertNotIn("-g", grim)  # whole screen: no geometry
        self.assertTrue(out.strip().endswith(".png"))
        self.assertIn("preview-1", out)


class TestReceiptPager(CoreTestCase):
    """`receipt --pager` (the panel's Receipt action runs inside a TUI
    window): text goes through `less -R`, or straight to stdout when less
    is not installed. subprocess.run is patched only for a `less` argv so
    any git call underneath still runs for real."""

    def test_pager_pipes_the_rendered_text_through_less(self):
        session_id = make_bare_session(self.store, "paged")
        calls = []

        def fake_run(argv, *a, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            rc, out, err = self.run_cli(["receipt", session_id, "--pager"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")  # less owns the output; nothing is printed directly
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        self.assertEqual(argv, ["less", "-R"])
        self.assertIs(kw["text"], True)
        self.assertTrue(kw["input"].startswith(f"Session    paged  {session_id}\n"), kw["input"])
        self.assertIn("Receipt    state_version", kw["input"])

    def test_pager_falls_back_to_printing_when_less_is_missing(self):
        session_id = make_bare_session(self.store, "unpaged")
        rc, _, err = self.run_cli(["receipt", session_id, "--write"])  # persist, so both renders below agree
        self.assertEqual(rc, 0, err)
        rc, plain, err = self.run_cli(["receipt", session_id])
        self.assertEqual(rc, 0, err)
        real_run = subprocess.run

        def no_less(argv, *a, **kw):
            if argv and argv[0] == "less":
                raise FileNotFoundError(2, "No such file or directory", "less")
            return real_run(argv, *a, **kw)

        with mock.patch.object(core.subprocess, "run", side_effect=no_less):
            rc, out, err = self.run_cli(["receipt", session_id, "--pager"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, plain)
        self.assertTrue(out.startswith(f"Session    unpaged  {session_id}\n"))

    def test_pager_is_ignored_with_json(self):
        session_id = make_bare_session(self.store, "json-wins")
        calls = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            rc, out, err = self.run_cli(["receipt", session_id, "--json", "--pager"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["session_id"], session_id)
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------
# `open`: bound-and-live, orphan-with-ref resume, orphan-without-ref,
# nothing-to-open, and Herdr-unreachable
# --------------------------------------------------------------------------

class TestOpen(CoreTestCase):
    def test_open_bound_and_live_confirms_binding(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.get", {"agent": {"name": "a1", "agent_status": "working"}})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "bound-live", runtime=runtime, state="working")
        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fake_herdr.calls("agent.get"), [{"target": "a1"}])

    def test_open_orphaned_with_ref_resumes(self):
        self.start_fake_herdr()
        session_id = make_bare_session(self.store, "orphan-with-ref", runtime=None, state="orphaned")
        record = self.store.try_load(session_id)
        record["agent"]["harness_session_ref"] = "abc123"
        self.store.save_session(record)

        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(session_id)
        self.assertEqual(record["status"]["state"], "working")
        self.assertIsNotNone(record["runtime"])
        start_flags = self.fake_herdr.calls("agent.start")[-1]["args"]
        self.assertEqual(start_flags[:2], ["--resume", "abc123"])

    def test_open_orphaned_without_ref_starts_fresh(self):
        self.start_fake_herdr()
        session_id = make_bare_session(self.store, "orphan-no-ref", runtime=None, state="orphaned")
        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(session_id)
        self.assertEqual(record["status"]["state"], "working")
        self.assertEqual(record["status"]["detail"], "no transcript to resume")

    def test_open_with_nothing_to_open_is_a_conflict(self):
        # A session that ended has nothing to open. (A session created but
        # never bound starts fresh on open since run 10; see TestRefusedStart.)
        session_id = make_bare_session(self.store, "nothing-to-open", runtime=None, state="stopped")
        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 5, err)

    def test_open_bound_but_herdr_unreachable_exits_4(self):
        # No fake server started.
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "bound-no-herdr", runtime=runtime, state="working")
        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 4, err)


# --------------------------------------------------------------------------
# Worktree cleanup at stop/done (07-worktrees.md's keep/remove table)
# --------------------------------------------------------------------------

class TestWorktreeCleanup(CoreTestCase):
    def _git(self, cwd, *args):
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {args} failed: {r.stderr}")
        return r

    def make_repo_with_real_worktree(self):
        repo = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-repo-"))
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "base commit")
        wt = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-wt-parent-")) / "wt"
        self._git(repo, "worktree", "add", str(wt), "-b", "session/cleanup-test", "main")
        (wt / "a.txt").write_text("a\n", encoding="utf-8")
        self._git(wt, "add", "a.txt")
        self._git(wt, "commit", "-m", "add a")
        return repo, wt

    def test_keeps_dirty_worktree(self):
        repo, wt = self.make_repo_with_real_worktree()
        try:
            (wt / "a.txt").write_text("dirty\n", encoding="utf-8")
            should_remove, reason = core.decide_worktree_cleanup(str(wt), "session/cleanup-test", "main")
            self.assertFalse(should_remove)
            self.assertIn("uncommitted", reason)
            self.assertTrue(wt.exists())
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(wt.parent, ignore_errors=True)

    def test_keeps_clean_unmerged_unpushed_worktree(self):
        repo, wt = self.make_repo_with_real_worktree()
        try:
            should_remove, reason = core.decide_worktree_cleanup(str(wt), "session/cleanup-test", "main")
            self.assertFalse(should_remove)
            self.assertIn("not pushed", reason)
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(wt.parent, ignore_errors=True)

    def test_removes_clean_merged_worktree_but_keeps_branch(self):
        repo, wt = self.make_repo_with_real_worktree()
        try:
            self._git(repo, "merge", "--no-ff", "session/cleanup-test", "-m", "merge it")
            should_remove, reason = core.decide_worktree_cleanup(str(wt), "session/cleanup-test", "main")
            self.assertTrue(should_remove)
            self.assertIn("merged", reason)

            session = {"workspace": {
                "repo_root": str(repo), "worktree_path": str(wt),
                "branch": "session/cleanup-test", "base_branch": "main", "created_by_session": True,
            }}
            core.cleanup_workspace_if_owned(session, herdr=None)
            self.assertFalse(wt.exists())
            self.assertTrue(session["workspace"]["worktree_removed"])
            branches = self._git(repo, "branch", "--list", "session/cleanup-test").stdout
            self.assertIn("session/cleanup-test", branches)  # invariant 7: branch kept
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(wt.parent, ignore_errors=True)

    def test_stop_command_triggers_cleanup_and_persists_it(self):
        repo, wt = self.make_repo_with_real_worktree()
        try:
            self._git(repo, "merge", "--no-ff", "session/cleanup-test", "-m", "merge it")
            workspace = {
                "repo_root": str(repo), "worktree_path": str(wt),
                "branch": "session/cleanup-test", "base_branch": "main", "created_by_session": True,
            }
            session_id = make_bare_session(self.store, "stop-cleanup", workspace=workspace, state="idle")
            rc, out, err = self.run_cli(["stop", session_id])
            self.assertEqual(rc, 0, err)
            self.assertFalse(wt.exists())
            record = self.store.try_load(session_id)
            self.assertTrue(record["workspace"]["worktree_removed"])
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(wt.parent, ignore_errors=True)

    def test_open_puts_a_removed_worktree_back_before_resuming_a_stopped_session(self):
        # A harness resumes its transcript by working directory, so a
        # resume after stop's cleanup re-creates the worktree at the same
        # path from the branch invariant 7 kept (2026-09-03).
        repo, wt = self.make_repo_with_real_worktree()
        try:
            self._git(repo, "merge", "--no-ff", "session/cleanup-test", "-m", "merge it")
            workspace = {
                "repo_root": str(repo), "worktree_path": str(wt),
                "branch": "session/cleanup-test", "base_branch": "main", "created_by_session": True,
            }
            self.start_fake_herdr()
            session_id = make_bare_session(self.store, "stop-then-resume", workspace=workspace, state="idle")
            record = self.store.try_load(session_id)
            record["agent"]["harness_session_ref"] = "abc123"
            self.store.save_session(record)
            self.assertEqual(self.run_cli(["stop", session_id])[0], 0)
            self.assertFalse(wt.exists())

            rc, out, err = self.run_cli(["open", session_id])
            self.assertEqual(rc, 0, err)
            self.assertTrue(wt.exists())
            self.assertEqual(self._git(wt, "branch", "--show-current").stdout.strip(), "session/cleanup-test")
            record = self.store.try_load(session_id)
            self.assertEqual(record["status"]["state"], "working")
            self.assertFalse(record["workspace"]["worktree_removed"])
            self.assertEqual(self.fake_herdr.calls("workspace.create")[-1]["cwd"], str(wt))
            resumed = [e["data"] for e in self.store.read_events(session_id)
                       if e["type"] == "status.changed" and e["data"]["to"] == "orphaned"][-1]
            self.assertEqual(resumed["detail"], "resumed after a stop; worktree re-created")
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(wt.parent, ignore_errors=True)

    def test_open_refuses_a_stopped_session_whose_directory_is_gone_for_good(self):
        # A plain directory (no worktree of its own) that no longer exists:
        # there is nowhere to resume in, and the record says so (exit 5).
        self.start_fake_herdr()
        gone = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-gone-"))
        workspace = {"repo_root": None, "worktree_path": str(gone), "branch": None,
                     "base_branch": None, "created_by_session": False}
        session_id = make_bare_session(self.store, "stopped-dir-gone", workspace=workspace, state="stopped")
        record = self.store.try_load(session_id)
        record["agent"]["harness_session_ref"] = "abc123"
        self.store.save_session(record)
        shutil.rmtree(gone)
        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 5, err)
        self.assertIn("no longer exists", err)
        self.assertEqual(self.store.try_load(session_id)["status"]["state"], "stopped")

    def test_never_touches_a_worktree_this_session_did_not_create(self):
        repo, wt = self.make_repo_with_real_worktree()
        try:
            self._git(repo, "merge", "--no-ff", "session/cleanup-test", "-m", "merge it")
            session = {"workspace": {
                "repo_root": str(repo), "worktree_path": str(wt),
                "branch": "session/cleanup-test", "base_branch": "main", "created_by_session": False,
            }}
            core.cleanup_workspace_if_owned(session, herdr=None)
            self.assertTrue(wt.exists())
            self.assertNotIn("worktree_removed", session["workspace"])
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(wt.parent, ignore_errors=True)


# --------------------------------------------------------------------------
# reconcile marking an orphan
# --------------------------------------------------------------------------

class TestHarnessSessionRefDiscovery(CoreTestCase):
    def _write_transcript(self, project_dir, ref, started_iso):
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{ref}.jsonl"
        path.write_text(json.dumps({"type": "user", "timestamp": started_iso, "sessionId": ref}) + "\n")
        return path

    def test_two_sessions_in_one_directory_get_their_own_transcripts(self):
        # Run 2 (2026-09-02): both sessions took the newest file by mtime.
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.plain_dir / "claude")
        cwd = str(self.plain_dir / "project")
        pathlib.Path(cwd).mkdir()
        project_dir = core.claude_project_dir(cwd)
        # session A started at t=100, its transcript at t=104; session B
        # started at t=105, its transcript at t=109. Both files are touched
        # again at the end, so mtime order says nothing.
        self._write_transcript(project_dir, "aaaa", "2026-09-03T02:20:54Z")
        self._write_transcript(project_dir, "bbbb", "2026-09-03T02:21:04Z")
        a_since = core.dt.datetime.fromisoformat("2026-09-03T02:20:50+00:00").timestamp()
        b_since = core.dt.datetime.fromisoformat("2026-09-03T02:21:00+00:00").timestamp()
        self.assertEqual(core.discover_harness_session_ref("claude", cwd, a_since), "aaaa")
        self.assertEqual(core.discover_harness_session_ref("claude", cwd, b_since), "bbbb")
        # With A's ref taken, B cannot be given it even when B's own file
        # is missing.
        self.assertEqual(core.discover_harness_session_ref("claude", cwd, a_since, taken={"aaaa"}), "bbbb")
        # A file that started before the session is never its transcript.
        c_since = core.dt.datetime.fromisoformat("2026-09-03T02:22:00+00:00").timestamp()
        self.assertIsNone(core.discover_harness_session_ref("claude", cwd, c_since))

    def test_record_ref_skips_refs_held_by_other_sessions(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.plain_dir / "claude")
        cwd = str(self.plain_dir / "project")
        pathlib.Path(cwd).mkdir()
        project_dir = core.claude_project_dir(cwd)
        self._write_transcript(project_dir, "held", "2026-09-03T02:20:54Z")
        self._write_transcript(project_dir, "free", "2026-09-03T02:20:56Z")
        workspace = {"repo_root": None, "worktree_path": cwd, "branch": None, "base_branch": None, "created_by_session": False}
        runtime = {"backend": "herdr", "session": None, "workspace_id": "w1", "tab_id": "t1", "pane_id": "p1", "agent_id": "a"}
        first = make_bare_session(self.store, "first", runtime=runtime, workspace=workspace)
        rec = self.store.try_load(first)
        rec["agent"]["harness_session_ref"] = "held"
        rec["created_at"] = "2026-09-03T02:20:50Z"
        self.store.save_session(rec)
        second = make_bare_session(self.store, "second", runtime=dict(runtime, pane_id="p2", agent_id="b"), workspace=workspace)
        rec2 = self.store.try_load(second)
        rec2["created_at"] = "2026-09-03T02:20:50Z"
        self.store.save_session(rec2)
        self.assertTrue(core.record_harness_session_ref(self.store, rec2))
        self.assertEqual(self.store.try_load(second)["agent"]["harness_session_ref"], "free")


class TestReconcile(CoreTestCase):
    def test_reconcile_marks_missing_pane_orphaned(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        self.fake_herdr.set_result("pane.list", {"panes": []})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "will-be-orphaned", runtime=runtime, state="working")

        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)

        session = self.store.try_load(session_id)
        self.assertEqual(session["status"]["state"], "orphaned")
        self.assertIsNone(session["runtime"])

        result = json.loads(out)
        self.assertIn(session_id, result["orphaned"])

        events = self.store.read_events(session_id)
        types = [e["type"] for e in events]
        self.assertIn("status.changed", types)
        self.assertIn("runtime.unbound", types)

    def test_reconcile_leaves_a_still_present_pane_alone(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "a1", "agent": "claude", "pane_id": "p1"}]})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "p1"}]})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "still-alive", runtime=runtime, state="working")

        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(session_id)
        self.assertEqual(session["status"]["state"], "working")
        self.assertIsNotNone(session["runtime"])

    def test_reconcile_ends_a_session_whose_harness_exited_but_pane_lives(self):
        # Rig, 2026-09-02: the harness died, Herdr dropped it from agent.list,
        # the pane's shell lived on, and the session read `idle` forever.
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "p1"}]})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "harness-gone", runtime=runtime, state="idle")

        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(session_id)
        self.assertEqual(session["status"]["state"], "done")
        self.assertEqual(session["status"]["detail"], "harness exited")
        self.assertIsNone(session["runtime"])
        self.assertIn(session_id, json.loads(out)["ended"])
        # The empty shell pane is closed with the session.
        self.assertEqual([c["pane_id"] for c in self.fake_herdr.calls("pane.close")], ["p1"])
        types = [e["type"] for e in self.store.read_events(session_id)]
        self.assertEqual(types[-3:], ["runtime.unbound", "session.ended", "receipt.written"])
        self.assertTrue(self.store.receipt_path(session_id).exists())
        receipt = json.loads(self.store.receipt_path(session_id).read_text())
        self.assertEqual(receipt["end_state"], "done")

    def test_reconcile_fails_a_session_whose_harness_vanished_while_working(self):
        # Herdr exposes no exit status; a harness that disappears mid-turn
        # is a failure, one that disappears between turns is an exit.
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "p1"}]})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "died-mid-turn", runtime=runtime, state="working")
        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(session_id)
        self.assertEqual(session["status"]["state"], "failed")
        self.assertEqual(session["status"]["detail"], "harness exited while working")
        self.assertIn(session_id, json.loads(out)["ended"])
        receipt = json.loads(self.store.receipt_path(session_id).read_text())
        self.assertEqual(receipt["end_state"], "failed")

    def test_reconcile_orphans_instead_of_ending_after_a_herdr_restart(self):
        # Rig, 2026-09-02 22:30: a reboot brought Herdr back with a fresh
        # shell in every restored workspace and no agents; the reconciler
        # read that as "harness exited while working" and ended two live
        # sessions, so Enter opened receipts instead of reviving. A server
        # that started after the binding explains the vanish; the session
        # is orphaned and revivable.
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "rebooted-under", runtime=runtime, state="working")
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "p1"}]})
        # The socket stands for the server: make it a minute younger than the binding.
        future = time.time() + 60
        os.utime(self.herdr_socket, (future, future))

        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(session_id)
        self.assertEqual(session["status"]["state"], "orphaned")
        self.assertEqual(session["status"]["detail"], "Herdr restarted; Enter revives")
        self.assertIsNone(session["runtime"])
        self.assertIn(session_id, json.loads(out)["orphaned"])
        self.assertNotIn(session_id, json.loads(out)["ended"])
        self.assertFalse(self.store.receipt_path(session_id).exists())
        # The restored shell pane is closed; the worktree stays for the revive.
        self.assertEqual([c["pane_id"] for c in self.fake_herdr.calls("pane.close")], ["p1"])
        types = [e["type"] for e in self.store.read_events(session_id)]
        self.assertEqual(types[-2:], ["status.changed", "runtime.unbound"])

    def test_open_revives_a_session_whose_end_was_inferred(self):
        # The two sessions the reboot ended before the restart rule existed:
        # failed by inference, transcript on disk. Enter revives them.
        self.start_fake_herdr()
        session_id = make_bare_session(self.store, "ended-by-inference", runtime=None, state="failed")
        record = self.store.try_load(session_id)
        record["status"]["detail"] = "harness exited while working"
        record["agent"]["harness_session_ref"] = "abc123"
        self.store.save_session(record)

        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(session_id)
        self.assertEqual(record["status"]["state"], "working")
        self.assertEqual(self.fake_herdr.calls("agent.start")[-1]["args"][:2], ["--resume", "abc123"])
        transitions = [(e["data"]["from"], e["data"]["to"]) for e in self.store.read_events(session_id) if e["type"] == "status.changed"]
        self.assertEqual(transitions[-2:], [("failed", "orphaned"), ("orphaned", "working")])

    def test_open_resumes_a_session_a_person_stopped(self):
        # 2026-09-03: a stop is a pause. The transcript is on disk, so Enter
        # resumes it through the orphan path, and the stop's receipt stays.
        self.start_fake_herdr()
        session_id = make_bare_session(self.store, "stopped-on-purpose", runtime=None, state="stopped")
        record = self.store.try_load(session_id)
        record["agent"]["harness_session_ref"] = "abc123"
        record["status"]["detail"] = "stopped"
        self.store.save_session(record)
        receipt = core.compute_receipt(self.store, record, "stopped", "stopped", record["status"]["since"])
        core.write_receipt(self.store, receipt)

        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(session_id)
        self.assertEqual(record["status"]["state"], "working")
        self.assertEqual(self.fake_herdr.calls("agent.start")[-1]["args"][:2], ["--resume", "abc123"])
        changes = [e["data"] for e in self.store.read_events(session_id) if e["type"] == "status.changed"]
        self.assertEqual([(c["from"], c["to"]) for c in changes[-2:]], [("stopped", "orphaned"), ("orphaned", "working")])
        self.assertEqual(changes[-2]["detail"], "resumed after a stop")
        self.assertTrue(self.store.receipt_path(session_id).exists())

    def test_open_keeps_a_session_closed_with_a_verdict_closed(self):
        # A verdict is a decision (09-closed-loop-surfaces.md's `done`);
        # only a stop, or an end nobody decided, resumes.
        self.start_fake_herdr()
        session_id = make_bare_session(self.store, "closed-with-verdict", runtime=None, state="done")
        record = self.store.try_load(session_id)
        record["agent"]["harness_session_ref"] = "abc123"
        record["status"]["detail"] = "kept"
        self.store.save_session(record)
        rc, out, err = self.run_cli(["open", session_id])
        self.assertEqual(rc, 5)
        self.assertEqual(self.fake_herdr.calls("agent.start"), [])

    def test_reconcile_sweeps_the_workspace_of_an_ended_session(self):
        # Herdr restores workspaces with a fresh shell after a server
        # restart; a stopped or orphaned session must not leave one behind.
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        runtime = {"backend": "herdr", "session": None, "workspace_id": "wZ",
                   "tab_id": "wZ:t1", "pane_id": "wZ:p1", "agent_id": "gone"}
        session_id = make_bare_session(self.store, "ended-earlier", runtime=runtime, state="working")
        # The pane is the session's by its metadata, never by workspace id
        # (run 10: Herdr reuses ids after a restart). A same-id pane that
        # belongs to nobody, and one that belongs to a live session, stay.
        self.fake_herdr.set_result("pane.list", {"panes": [
            {"pane_id": "wZ:p1", "workspace_id": "wZ", "tokens": {"session_id": session_id}},
            {"pane_id": "wZ:p2", "workspace_id": "wZ"},
            {"pane_id": "w1:p1", "workspace_id": "w1", "tokens": {"session_id": "someone-else"}}]})
        rec = self.store.try_load(session_id)
        # Ended and unbound already, the way `stop` on an orphan leaves it.
        ev = self.store.append_event(session_id, "runtime.unbound", core.system_actor(), {"reason": "pane_gone"})
        rec["runtime"] = None
        ev = self.store.append_event(session_id, "status.changed", core.system_actor(),
                                     {"from": "working", "to": "stopped", "source": "test", "detail": None})
        rec["status"] = {"state": "stopped", "since": ev["ts"], "source": "test", "detail": None}
        rec["state_version"] = ev["seq"]
        self.store.save_session(rec)
        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["swept"], [session_id])
        self.assertEqual([c["pane_id"] for c in self.fake_herdr.calls("pane.close")], ["wZ:p1"])

    def test_reconcile_never_sweeps_an_adopted_sessions_workspace(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        session_id = core.new_ulid()
        # The pane even carries the adopted session's id; adopted stays untouched.
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tokens": {"session_id": session_id}}]})
        actor = core.system_actor()
        workspace = {"repo_root": None, "worktree_path": None, "branch": None, "base_branch": None, "created_by_session": False}
        record = core.new_session_record(session_id, "herdr-spike", actor, actor, "shared", "claude", workspace, cwd=None, command=None)
        self.store.append_event(session_id, "session.created", actor, {"name": "herdr-spike", "adopted": True})
        ev = self.store.append_event(session_id, "runtime.bound", actor,
                                     {"backend": "herdr", "session": None, "workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1", "agent_id": "spike"})
        ev = self.store.append_event(session_id, "status.changed", actor, {"from": "starting", "to": "stopped", "source": "test", "detail": None})
        record["status"] = {"state": "stopped", "since": ev["ts"], "source": "test", "detail": None}
        record["runtime"] = None
        record["state_version"] = ev["seq"]
        self.store.save_session(record)
        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["swept"], [])
        self.assertEqual(self.fake_herdr.calls("pane.close"), [])

    def test_reconcile_leaves_a_starting_session_alone_while_its_harness_boots(self):
        # A harness that is still booting is not in agent.list yet either;
        # that must not read as "exited".
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "p1"}]})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "booting", runtime=runtime, state="starting")
        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(session_id)
        self.assertEqual(session["status"]["state"], "starting")
        self.assertIsNotNone(session["runtime"])
        self.assertEqual(json.loads(out)["ended"], [])

    def test_reconcile_adopts_an_unmatched_herdr_agent(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "orphan-agent", "agent": "claude", "pane_id": "orphan-pane", "workspace_id": "w9", "tab_id": "w9:t1"}]})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "orphan-pane"}]})

        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertEqual(len(result["adopted"]), 1)
        adopted_id = result["adopted"][0]
        session = self.store.try_load(adopted_id)
        self.assertEqual(session["created_by"]["kind"], "system")
        self.assertEqual(session["runtime"]["agent_id"], "orphan-agent")
        self.assertEqual(session["status"]["state"], "working")

    def test_reconcile_exits_4_when_herdr_unreachable(self):
        # No fake server started -- socket path has nothing listening.
        rc, out, err = self.run_cli(["reconcile"])
        self.assertEqual(rc, 4, err)

    def read_index(self):
        path = self.sessions_dir / "index.json"
        self.assertTrue(path.exists(), "reconcile did not write index.json")
        index = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(index.keys()), {"generated_at", "herdr", "orphaned", "adopted", "counts"})
        self.assertEqual(set(index["counts"].keys()), {"needs_attention", "live", "orphaned", "paused"})
        parsed = core.parse_rfc3339(index["generated_at"])  # raises if unparseable
        self.assertTrue(index["generated_at"].endswith("Z"))
        self.assertLessEqual(abs((parsed - core.dt.datetime.now(core.dt.timezone.utc)).total_seconds()), 60)
        return index

    def test_reconcile_writes_index_with_herdr_running(self):
        # (b) normal path: one pane stays, one vanishes, one session waits,
        # one is already done; counts reflect the store after this run.
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "a1", "agent": "claude", "pane_id": "p1"}]})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "p1"}]})
        alive = {"backend": "herdr", "session": "s1", "workspace_id": "w1", "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        gone = dict(alive, pane_id="p2", agent_id="a2")
        alive_id = make_bare_session(self.store, "alive", runtime=alive, state="working")
        gone_id = make_bare_session(self.store, "gone", runtime=gone, state="working")
        make_bare_session(self.store, "waiting", state="waiting")
        make_bare_session(self.store, "finished", state="done")

        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"orphaned": [gone_id], "adopted": [], "ended": [], "swept": []})  # stdout shape unchanged

        index = self.read_index()
        self.assertEqual(index["herdr"], "running")
        self.assertEqual(index["orphaned"], [gone_id])
        self.assertEqual(index["adopted"], [])
        self.assertEqual(index["counts"], {"needs_attention": 1, "live": 2, "orphaned": 1, "paused": 0})
        self.assertEqual(self.store.try_load(alive_id)["status"]["state"], "working")

        # The file at the root of the sessions dir is not mistaken for a session.
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(json.loads(out)["sessions"]), 4)

    def test_reconcile_index_counts_adopted_sessions_as_live(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "stray", "agent": "codex", "pane_id": "p9"}]})
        self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": "p9"}]})
        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        adopted_id = json.loads(out)["adopted"][0]
        index = self.read_index()
        self.assertEqual(index["adopted"], [adopted_id])
        self.assertEqual(index["counts"], {"needs_attention": 0, "live": 1, "orphaned": 0, "paused": 0})

    def test_reconcile_writes_index_when_herdr_unreachable(self):
        # (b) outage path: still exit 4, but index.json says so and the
        # bound session it orphaned is counted as orphaned, not live.
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        session_id = make_bare_session(self.store, "bound-no-herdr", runtime=runtime, state="working")
        make_bare_session(self.store, "unbound-blocked", state="blocked")

        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 4, err)
        self.assertEqual(json.loads(out), {"orphaned": [session_id], "adopted": [], "herdr": "unreachable"})

        index = self.read_index()
        self.assertEqual(index["herdr"], "unreachable")
        self.assertEqual(index["orphaned"], [session_id])
        self.assertEqual(index["counts"], {"needs_attention": 1, "live": 1, "orphaned": 1, "paused": 0})

    def test_reconcile_index_write_failure_changes_neither_exit_code_nor_output(self):
        # A directory squatting on index.json makes the atomic rename fail.
        squatter = self.sessions_dir / "index.json"
        squatter.mkdir()
        (squatter / "keep").write_text("x", encoding="utf-8")
        self.start_fake_herdr()
        rc, out, err = self.run_cli(["reconcile", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"orphaned": [], "adopted": [], "ended": [], "swept": []})
        self.assertIn("could not write", err)
        self.assertTrue(squatter.is_dir())
        # No temp file left behind next to it either.
        self.assertEqual([p.name for p in self.sessions_dir.iterdir()], ["index.json"])


# --------------------------------------------------------------------------
# Supplementary coverage: ULIDs, atomic writes, actors, aliases, invariant 6
# --------------------------------------------------------------------------

class TestUlid(unittest.TestCase):
    def test_format_and_uniqueness(self):
        ulids = {core.new_ulid() for _ in range(200)}
        self.assertEqual(len(ulids), 200)
        for u in ulids:
            self.assertEqual(len(u), 26)
            self.assertTrue(core.looks_like_ulid(u))
            self.assertNotIn("I", u)
            self.assertNotIn("L", u)
            self.assertNotIn("O", u)
            self.assertNotIn("U", u)

    def test_timestamp_orders_lexicographically(self):
        a = core.new_ulid(now_ms=1000)
        b = core.new_ulid(now_ms=2000)
        self.assertLess(a[:10], b[:10])


class TestAtomicWrite(unittest.TestCase):
    def test_atomic_write_json_roundtrip_and_no_stray_temp_files(self):
        d = pathlib.Path(tempfile.mkdtemp())
        try:
            path = d / "sub" / "session.json"
            core.atomic_write_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
            core.atomic_write_json(path, {"a": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2})
            leftovers = [p for p in path.parent.iterdir() if p.name != "session.json"]
            self.assertEqual(leftovers, [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestActors(unittest.TestCase):
    def test_actor_string_roundtrip(self):
        actor = core.parse_actor_string("human:praneet@rig")
        self.assertEqual(actor, {"kind": "human", "id": "praneet@rig", "label": "praneet"})
        self.assertEqual(core.actor_to_string(actor), "human:praneet@rig")

    def test_invalid_actor_string_rejected(self):
        with self.assertRaises(core.UsageError):
            core.parse_actor_string("not-an-actor")
        with self.assertRaises(core.UsageError):
            core.parse_actor_string("robot:x")


class TestAliasDerivation(unittest.TestCase):
    def test_lowercases_and_collapses_runs(self):
        self.assertEqual(core.derive_herdr_alias("API Refactor!!", "01J8Z3K9QY7NX8V2H5T6M4R1WB"), "api-refactor")

    def test_prefixes_when_not_starting_with_letter(self):
        alias = core.derive_herdr_alias("123-go", "01J8Z3K9QY7NX8V2H5T6M4R1WB")
        self.assertTrue(alias[0].isalpha())

    def test_truncates_to_32(self):
        alias = core.derive_herdr_alias("x" * 50, "01J8Z3K9QY7NX8V2H5T6M4R1WB")
        self.assertLessEqual(len(alias), 32)

    def test_collision_appends_last_four_id_chars(self):
        alias = core.derive_herdr_alias("api-refactor", "01J8Z3K9QY7NX8V2H5T6M4R1WB",
                                         existing_aliases={"api-refactor"})
        self.assertNotEqual(alias, "api-refactor")
        self.assertTrue(alias.endswith("1wb"))


class TestDefaultSessionName(unittest.TestCase):
    def test_prompt_first_four_words(self):
        self.assertEqual(core.default_session_name("claude", cwd="/home/x/Work/omarchy-multiplayer",
                                                   prompt="Append a line to README.md and commit"),
                         "append-a-line-to")

    def test_cwd_basename_when_no_prompt(self):
        self.assertEqual(core.default_session_name("claude", cwd="/home/x/Work/omarchy-multiplayer"),
                         "omarchy-multiplayer")
        self.assertEqual(core.default_session_name("claude", cwd="/home/x/Work/omarchy-multiplayer/"),
                         "omarchy-multiplayer")

    def test_suffix_until_free_against_every_name_on_disk(self):
        taken = {"omarchy-multiplayer"}
        self.assertEqual(core.default_session_name("claude", cwd="/home/x/Work/omarchy-multiplayer", existing_names=taken),
                         "omarchy-multiplayer-2")
        taken.add("omarchy-multiplayer-2")
        self.assertEqual(core.default_session_name("claude", cwd="/home/x/Work/omarchy-multiplayer", existing_names=taken),
                         "omarchy-multiplayer-3")

    def test_kind_fallback(self):
        self.assertEqual(core.default_session_name("claude"), "claude")
        self.assertEqual(core.default_session_name("codex", cwd=None, prompt="   "), "codex")
        # A prompt and a cwd whose slugs are empty both fall through.
        self.assertEqual(core.default_session_name("codex", cwd="/", prompt="!!! ???"), "codex")
        self.assertEqual(core.default_session_name("codex", cwd=".", prompt=None), "codex")

    def test_prefix_when_not_starting_with_a_letter(self):
        self.assertEqual(core.default_session_name("claude", cwd="/home/x/123-go"), "s-123-go")
        self.assertEqual(core.default_session_name("claude", prompt="42 is the answer to everything"), "s-42-is-the-answer")
        self.assertEqual(core.default_session_name("claude", cwd="/home/x/123-go", existing_names={"s-123-go"}), "s-123-go-2")

    def test_slug_rule(self):
        self.assertEqual(core.slugify_name("  Hello, World!  "), "hello-world")
        self.assertEqual(core.slugify_name("a" * 27 + "-b"), "a" * 27)  # cut to 28 then trailing '-' stripped
        self.assertEqual(core.slugify_name("Ünïcode & emoji 🚀 name"), "n-code-emoji-name")
        self.assertEqual(core.slugify_name("---"), "")
        self.assertLessEqual(len(core.slugify_name("x y " * 30)), 28)


class TestInvariant6(CoreTestCase):
    def test_refuses_sharing_a_worktree_that_was_auto_created(self):
        auto_ws = {"repo_root": "/r", "worktree_path": "/r/wt", "branch": "session/x",
                   "base_branch": "main", "created_by_session": True}
        make_bare_session(self.store, "auto-creator", workspace=auto_ws)
        joining_ws = dict(auto_ws, created_by_session=False)
        with self.assertRaises(core.ConflictError):
            core.check_invariant6(self.store, joining_ws, new_session_created_by_session=False)

    def test_allows_sharing_a_hand_made_worktree(self):
        hand_ws = {"repo_root": "/r", "worktree_path": "/r/wt-manual", "branch": "manual",
                   "base_branch": None, "created_by_session": False}
        make_bare_session(self.store, "first-joiner", workspace=hand_ws)
        core.check_invariant6(self.store, hand_ws, new_session_created_by_session=False)  # must not raise


class TestNoDeleteCommand(unittest.TestCase):
    def test_prune_is_the_only_deletion_and_needs_yes(self):
        # Invariant 7 (2026-09-03): deleting a session directory is the only
        # way to lose history, and only `prune` does it, on an explicit --yes.
        parser = core.build_parser()
        sub_actions = [a for a in parser._subparsers._group_actions if hasattr(a, "choices")]
        subcommands = set(sub_actions[0].choices.keys())
        self.assertNotIn("delete", subcommands)
        self.assertNotIn("rm", subcommands)
        self.assertIn("prune", subcommands)
        prune = sub_actions[0].choices["prune"]
        self.assertFalse(prune.parse_args(["--older-than", "30d"]).yes)  # a dry run by default


class TestCliIsExecutable(unittest.TestCase):
    """One true subprocess-level test, proving the shebang and the
    executable bit work end to end, not just the in-process import path."""

    def test_cli_runs_as_a_real_program(self):
        sessions_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-sessions-subproc-"))
        herdr_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-herdr-subproc-"))
        try:
            env = dict(os.environ)
            env["OMARCHY_SESSIONS_DIR"] = str(sessions_dir)
            env["HERDR_SOCKET"] = str(herdr_dir / "herdr.sock")  # nothing listening; `list` needs no Herdr
            result = subprocess.run([str(CORE_PATH), "list", "--json"],
                                     capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"sessions": []})

            result = subprocess.run([str(CORE_PATH)], capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 2)  # usage error: no subcommand
        finally:
            shutil.rmtree(sessions_dir, ignore_errors=True)
            shutil.rmtree(herdr_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# 11-agent-lanes.md: several agents on one goal inside one session
# --------------------------------------------------------------------------

class TestLanes(TestReceiptComputation):
    """A lane is a child session with spawn_reason "lane", its own pane in
    the session's Herdr workspace, its own worktree cut from the session's
    branch, and a task that is its goal and first instruction."""

    def start_lane_herdr(self):
        self.start_fake_herdr()
        # Worktrees land in a temp dir, and the fake Herdr's worktree.create
        # does what the real one does on disk: a git worktree on a new
        # branch, opened as a workspace with a root pane.
        self.worktrees_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-worktrees-"))
        os.environ["HERDR_WORKTREES_DIR"] = str(self.worktrees_dir)
        def fake_worktree_create(params):
            subprocess.run(["git", "-C", params["cwd"], "worktree", "add", params["path"], "-b", params["branch"], params["base"]],
                           check=True, capture_output=True, text=True)
            return {"root_pane": {"pane_id": "p1", "workspace_id": "w1", "tab_id": "t1"}, "workspace": {"workspace_id": "w1"}}
        self.fake_herdr.set_result("worktree.create", fake_worktree_create)
        self.fake_herdr.set_result("pane.split", lambda params: {
            "pane": {"pane_id": "p2", "workspace_id": params.get("workspace_id", "w1"), "tab_id": "t1"}})
        self.fake_herdr.set_result("agent.list", lambda params: {"agents": [
            {"name": "any", "agent_status": "idle", "interactive_ready": True}]})
        self.fake_herdr.set_result("agent.prompt", {"agent": {"agent_status": "working"}})
        self.fake_herdr.set_result("agent.wait", {"agent": {"agent_status": "idle"}})

    def make_repo_session(self, name="team"):
        repo = self.make_git_repo_with_two_commits()
        self._git(repo, "checkout", "main")
        rc, out, err = self.run_cli(["new", "--agent", "claude", "--mode", "personal", "--name", name,
                                     "--cwd", str(repo), "--goal", "one page, two agents"])
        self.assertEqual(rc, 0, err)
        return repo, out.strip()

    def test_add_creates_a_lane_in_the_sessions_workspace_with_its_own_worktree(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--task", "Write the tests for the page"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            lane = self.store.try_load(lane_id)
            parent = self.store.try_load(sid)
            self.assertEqual(lane["lane"], {"name": "claude", "parent_id": sid, "parent_name": "team"})
            self.assertEqual(lane["lineage"]["parent_id"], sid)
            self.assertEqual(lane["lineage"]["spawn_reason"], "lane")
            self.assertIn(lane_id, parent["lineage"]["children"])
            self.assertEqual(lane["name"], "team.claude")
            self.assertEqual(lane["goal"]["text"], "Write the tests for the page")
            # Its own worktree, on a branch under the session's branch.
            self.assertEqual(lane["workspace"]["branch"], "session/team--claude")
            self.assertEqual(lane["workspace"]["base_branch"], "session/team")
            self.assertTrue(pathlib.Path(lane["workspace"]["worktree_path"]).exists())
            self.assertNotEqual(lane["workspace"]["worktree_path"], parent["workspace"]["worktree_path"])
            # A pane split beside the session's pane, in its workspace, then the harness in it.
            split = self.fake_herdr.calls("pane.split")[-1]
            self.assertEqual(split["workspace_id"], parent["runtime"]["workspace_id"])
            self.assertEqual(split["target_pane_id"], parent["runtime"]["pane_id"])
            self.assertEqual(split["cwd"], lane["workspace"]["worktree_path"])
            self.assertEqual(self.fake_herdr.calls("agent.start")[-1]["pane_id"], "p2")
            self.assertEqual(lane["runtime"]["pane_id"], "p2")
            self.assertEqual(lane["runtime"]["workspace_id"], parent["runtime"]["workspace_id"])
            # The task reached the lane as its first instruction.
            types = [e["type"] for e in self.store.read_events(lane_id)]
            self.assertIn("instruction.delivered", types)
            ptypes = [e["type"] for e in self.store.read_events(sid)]
            self.assertIn("lane.added", ptypes)
            self.assertIn("child.spawned", ptypes)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_lane_names_default_to_the_kind_and_stay_unique(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out1, err = self.run_cli(["add", sid, "--agent", "claude", "--task", "one"])
            self.assertEqual(rc, 0, err)
            rc, out2, err = self.run_cli(["add", sid, "--agent", "claude", "--task", "two"])
            self.assertEqual(rc, 0, err)
            names = [self.store.try_load(i.strip())["lane"]["name"] for i in (out1, out2)]
            self.assertEqual(names, ["claude", "claude-2"])
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "claude", "--task", "dup"])
            self.assertEqual(rc, 5)
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "main", "--task", "reserved"])
            self.assertEqual(rc, 5)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_a_lane_may_be_stricter_than_its_session_and_never_looser(self):
        self.start_lane_herdr()
        repo = self.make_git_repo_with_two_commits()
        self._git(repo, "checkout", "main")
        try:
            rc, out, err = self.run_cli(["new", "--agent", "claude", "--mode", "shared", "--name", "strict",
                                         "--cwd", str(repo)])
            self.assertEqual(rc, 0, err)
            sid = out.strip()
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--mode", "personal", "--task", "loosen"])
            self.assertEqual(rc, 5)
            self.assertIn("never looser", err)
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--mode", "restricted", "--task", "tighten"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.store.try_load(out.strip())["mode"], "restricted")
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_lanes_lists_main_first_and_list_json_carries_lanes(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "tests", "--task", "Write the tests"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            rc, out, err = self.run_cli(["lanes", sid, "--json"])
            self.assertEqual(rc, 0, err)
            lanes = json.loads(out)["lanes"]
            self.assertEqual([l["lane"] for l in lanes], ["main", "tests"])
            self.assertEqual(lanes[1]["task"], "Write the tests")
            self.assertEqual(lanes[1]["id"], lane_id)
            rc, out, err = self.run_cli(["list", "--json"])
            entries = {e["id"]: e for e in json.loads(out)["sessions"]}
            self.assertEqual([l["lane"] for l in entries[sid]["lanes"]], ["tests"])
            self.assertEqual(entries[lane_id]["lane"]["name"], "tests")
            self.assertEqual(entries[lane_id]["lane"]["parent_name"], "team")
            self.assertEqual(entries[sid]["lane"], None)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_send_targets_one_lane_or_fans_out_to_every_live_lane(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "tests", "--task", "Write the tests"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            before = len(self.fake_herdr.calls("agent.prompt"))
            rc, out, err = self.run_cli(["send", sid, "Keep the public API unchanged", "--lane", "tests"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(self.fake_herdr.calls("agent.prompt")) - before, 1)
            lane_texts = [e["data"]["text"] for e in self.store.read_events(lane_id) if e["type"] == "instruction.queued"]
            self.assertIn("Keep the public API unchanged", lane_texts)
            before = len(self.fake_herdr.calls("agent.prompt"))
            rc, out, err = self.run_cli(["send", sid, "Ship by noon"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(self.fake_herdr.calls("agent.prompt")) - before, 2)  # main and the lane
            main_texts = [e["data"]["text"] for e in self.store.read_events(sid) if e["type"] == "instruction.queued"]
            self.assertIn("Ship by noon", main_texts)
            lane_texts = [e["data"]["text"] for e in self.store.read_events(lane_id) if e["type"] == "instruction.queued"]
            self.assertIn("Ship by noon", lane_texts)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_done_lane_kept_merges_its_branch_into_the_session_and_tells_the_parent(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "tests", "--task", "Write the tests"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            lane = self.store.try_load(lane_id)
            wt = pathlib.Path(lane["workspace"]["worktree_path"])
            (wt / "test_page.py").write_text("def test_page(): pass\n", encoding="utf-8")
            self._git(wt, "add", "test_page.py")
            self._git(wt, "-c", "user.email=lane@example.com", "-c", "user.name=Lane", "commit", "-m", "tests for the page")
            rc, out, err = self.run_cli(["done", sid, "--lane", "tests", "--verdict", "kept", "--note", "tests pass"])
            self.assertEqual(rc, 0, err)
            parent = self.store.try_load(sid)
            merged = [e for e in self.store.read_events(sid) if e["type"] == "lane.merged"]
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["data"]["lane"], "tests")
            self.assertEqual(len(merged[0]["data"]["commits"]), 1)
            # The session's branch now has the lane's commit.
            log = self._git(pathlib.Path(parent["workspace"]["worktree_path"]), "log", "--oneline", "session/team").stdout
            self.assertIn("tests for the page", log)
            self.assertEqual(self.store.try_load(lane_id)["status"]["state"], "done")
            # The completion path: the parent records child.completed and got a marked instruction.
            completed = [e for e in self.store.read_events(sid) if e["type"] == "child.completed"]
            self.assertEqual(completed[0]["data"]["lane"], "tests")
            self.assertEqual(completed[0]["data"]["state"], "done")
            queued = [e for e in self.store.read_events(sid) if e["type"] == "instruction.queued"]
            self.assertTrue(any("lane tests finished" in e["data"]["text"] for e in queued))
            # The session's receipt lists the lane with its merged commit.
            rc, out, err = self.run_cli(["done", sid, "--verdict", "kept", "--note", "page and tests"])
            self.assertEqual(rc, 0, err)
            receipt = json.loads(self.store.receipt_path(sid).read_text())
            self.assertEqual(receipt["lanes"][0]["lane"], "tests")
            self.assertEqual(len(receipt["lanes"][0]["merged_commits"]), 1)
            self.assertEqual(receipt["lanes"][0]["end_state"], "done")
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_done_lane_with_a_conflict_blocks_the_lane_and_loses_nothing(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "copy", "--task", "Rewrite the README"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            lane = self.store.try_load(lane_id)
            parent = self.store.try_load(sid)
            lwt = pathlib.Path(lane["workspace"]["worktree_path"])
            pwt = pathlib.Path(parent["workspace"]["worktree_path"])
            (lwt / "README.md").write_text("lane version\n", encoding="utf-8")
            self._git(lwt, "add", "README.md"); self._git(lwt, "-c", "user.email=l@x", "-c", "user.name=L", "commit", "-m", "lane readme")
            (pwt / "README.md").write_text("session version\n", encoding="utf-8")
            self._git(pwt, "add", "README.md"); self._git(pwt, "-c", "user.email=p@x", "-c", "user.name=P", "commit", "-m", "session readme")
            rc, out, err = self.run_cli(["done", sid, "--lane", "copy", "--verdict", "kept", "--note", "copy is good"])
            self.assertEqual(rc, 5)
            lane = self.store.try_load(lane_id)
            self.assertEqual(lane["status"]["state"], "blocked")
            self.assertTrue(lane["status"]["detail"].startswith("merge conflict"))
            # The merge was aborted: the session's tree is clean and both commits still exist.
            self.assertEqual(self._git(pwt, "status", "--porcelain").stdout.strip(), "")
            self.assertIn("lane readme", self._git(lwt, "log", "--oneline").stdout)
            self.assertEqual([e for e in self.store.read_events(sid) if e["type"] == "lane.merged"], [])
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_add_refuses_an_ended_or_unbound_session(self):
        self.start_lane_herdr()
        sid = make_bare_session(self.store, "orphan", runtime=None, state="orphaned")
        rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--task", "x"])
        self.assertEqual(rc, 5)
        sid2 = make_bare_session(self.store, "over", runtime=None, state="done")
        rc, out, err = self.run_cli(["add", sid2, "--agent", "claude", "--task", "x"])
        self.assertEqual(rc, 5)

    def test_stop_keep_lanes_leaves_the_lanes_running(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "tests", "--task", "t"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            rc, out, err = self.run_cli(["stop", sid, "--keep-lanes"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.store.try_load(sid)["status"]["state"], "stopped")
            self.assertEqual(self.store.try_load(lane_id)["status"]["state"], "working")
            rc, out, err = self.run_cli(["stop", sid, "--lane", "tests"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.store.try_load(lane_id)["status"]["state"], "stopped")
        finally:
            shutil.rmtree(repo, ignore_errors=True)


# --------------------------------------------------------------------------
# Pattern 06, event-driven state: watchers, coalesced notices, delivery
# --------------------------------------------------------------------------

class TestWatchers(TestLanes):
    def test_a_parent_watches_its_lane_and_gets_one_coalesced_notice(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "tests", "--task", "Write the tests"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            lane = self.store.try_load(lane_id)
            self.assertEqual([w["session_id"] for w in lane["watchers"]], [sid])
            # Two events on the lane before anything is delivered: one pending notice on the parent, updated.
            note = self.plain_dir / "n.txt"; note.write_text("x\n")
            rc, out, err = self.run_cli(["artifact-add", lane_id, "--kind", "file", "--source", str(note), "--label", "first"])
            self.assertEqual(rc, 0, err)
            rc, out, err = self.run_cli(["artifact-add", lane_id, "--kind", "file", "--source", str(note), "--label", "second"])
            self.assertEqual(rc, 0, err)
            parent = self.store.try_load(sid)
            watch = [q for q in parent["queue"] if q.get("delivery") == "watch"]
            self.assertEqual(len(watch), 1)
            self.assertEqual(watch[0]["origin_session"], lane_id)
            # runtime.bound and starting -> working are noise to a watcher (run 9);
            # the two artifacts are one coalesced notice.
            self.assertEqual(watch[0]["count"], 2)
            self.assertIn("artifact second", watch[0]["text"])
            # Delivery on reconcile, when the parent is idle: one prompt, cursor advanced, queue clear.
            lane = self.store.try_load(lane_id)
            self.fake_herdr.set_result("agent.list", {"agents": [
                {"name": "any", "agent_status": "idle", "interactive_ready": True, "agent_id": parent["runtime"]["agent_id"]},
                {"name": "lane", "agent_status": "working", "interactive_ready": True, "agent_id": lane["runtime"]["agent_id"]}]})
            self.fake_herdr.set_result("pane.list", {"panes": [{"pane_id": parent["runtime"]["pane_id"]}, {"pane_id": lane["runtime"]["pane_id"]}]})
            before = len(self.fake_herdr.calls("agent.prompt"))
            rc, out, err = self.run_cli(["reconcile", "--json"])
            self.assertEqual(rc, 0, err)
            prompts = self.fake_herdr.calls("agent.prompt")[before:]
            watch_prompts = [p for p in prompts if "tests" in p.get("text", "") and "artifact second" in p.get("text", "")]
            self.assertEqual(len(watch_prompts), 1)
            parent = self.store.try_load(sid)
            self.assertEqual([q for q in parent["queue"] if q.get("delivery") == "watch"], [])
            lane = self.store.try_load(lane_id)
            self.assertEqual(lane["watchers"][0]["cursor"], watch[0]["seq_to"])
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_instruction_traffic_never_wakes_a_watcher(self):
        self.start_lane_herdr()
        repo, sid = self.make_repo_session()
        try:
            rc, out, err = self.run_cli(["add", sid, "--agent", "claude", "--lane", "tests", "--task", "Write the tests"])
            self.assertEqual(rc, 0, err)
            lane_id = out.strip()
            parent_before = [q for q in self.store.try_load(sid)["queue"] if q.get("delivery") == "watch"]
            rc, out, err = self.run_cli(["send", sid, "Keep going", "--lane", "tests"])
            self.assertEqual(rc, 0, err)
            parent_after = [q for q in self.store.try_load(sid)["queue"] if q.get("delivery") == "watch"]
            self.assertEqual(len(parent_after), len(parent_before))
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_subscribe_adds_and_removes_a_watcher_and_log_since_reads_forward(self):
        self.start_fake_herdr()
        a = make_bare_session(self.store, "watched", runtime=None, state="idle")
        b = make_bare_session(self.store, "watcher", runtime=None, state="idle")
        rc, out, err = self.run_cli(["subscribe", a, "--watcher", b])
        self.assertEqual(rc, 0, err)
        self.assertEqual([w["session_id"] for w in self.store.try_load(a)["watchers"]], [b])
        rc, out, err = self.run_cli(["goal", a, "--set", "a new goal"]) if False else (0, "", "")
        note = self.plain_dir / "m.txt"; note.write_text("y\n")
        rc, out, err = self.run_cli(["artifact-add", a, "--kind", "file", "--source", str(note), "--label", "one"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len([q for q in self.store.try_load(b)["queue"] if q.get("delivery") == "watch"]), 1)
        last = self.store._last_seq(a)
        rc, out, err = self.run_cli(["log", a, "--since", str(last - 1), "--json"])
        self.assertEqual(rc, 0, err)
        rows = [json.loads(l) for l in out.splitlines() if l.strip()]
        self.assertEqual([r["seq"] for r in rows], [last])
        rc, out, err = self.run_cli(["subscribe", a, "--watcher", b, "--remove"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(a)["watchers"], [])

    def test_open_delivers_what_was_queued_while_orphaned(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "any", "agent_status": "idle", "interactive_ready": True}]})
        self.fake_herdr.set_result("agent.prompt", {"agent": {"agent_status": "working"}})
        sid = make_bare_session(self.store, "orphan-queue", runtime=None, state="orphaned")
        rc, out, err = self.run_cli(["send", sid, "Finish the footer"])
        self.assertEqual(rc, 0, err)   # queued, no runtime
        self.assertEqual(len(self.store.try_load(sid)["queue"]), 1)
        before = len(self.fake_herdr.calls("agent.prompt"))
        rc, out, err = self.run_cli(["open", sid])
        self.assertEqual(rc, 0, err)
        prompts = self.fake_herdr.calls("agent.prompt")[before:]
        self.assertTrue(any("Finish the footer" in p.get("text", "") for p in prompts))
        self.assertEqual(self.store.try_load(sid)["queue"], [])
        types = [e["type"] for e in self.store.read_events(sid)]
        self.assertIn("instruction.delivered", types)


class TestArtifactLinksAndStates(CoreTestCase):
    def test_url_artifact_shows_as_a_link_and_can_be_marked_live(self):
        # 09-closed-loop-surfaces.md sections 4 and 6.
        self.start_fake_herdr()
        sid = make_bare_session(self.store, "linky", runtime=None, state="idle", goal_text="a page")
        rc, out, err = self.run_cli(["artifact-add", sid, "--kind", "url", "--source", "https://www.figma.com/file/abc/frame", "--label", "frame"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["show", sid, "--loop"])
        self.assertEqual(rc, 0, err)
        self.assertIn("artifact     frame  https://www.figma.com/file/abc/frame", out)
        rc, out, err = self.run_cli(["artifact-mark", sid, "frame", "--state", "live"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "frame: live")
        index = core.read_artifacts_index(self.store.artifacts_dir(sid))
        self.assertEqual(index[0]["state"], "live")
        self.assertEqual(index[0]["kind"], "url")
        rc, out, err = self.run_cli(["show", sid, "--loop"])
        self.assertIn("artifact     frame  marked live", out)
        rc, out, err = self.run_cli(["done", sid, "--verdict", "kept", "--note", "shipped"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["receipt", sid])
        self.assertEqual(rc, 0, err)
        self.assertIn("https://www.figma.com/file/abc/frame   frame   live", out)
        rc, out, err = self.run_cli(["artifact-mark", sid, "nothing-here", "--state", "live"])
        self.assertEqual(rc, 3)


# --------------------------------------------------------------------------
# 12-two-people.md: a second identity, visibility, access, suggestions, presence
# --------------------------------------------------------------------------

class TestTwoPeople(CoreTestCase):
    def as_actor(self, actor):
        """Run the next commands as another human actor (the proxy for a
        second person: OMARCHY_ACTOR, or an ssh client host)."""
        os.environ["OMARCHY_ACTOR"] = actor

    def as_me(self):
        os.environ.pop("OMARCHY_ACTOR", None)

    def tearDown(self):
        self.as_me()
        super().tearDown()

    def test_the_second_identity_comes_from_the_env_or_the_ssh_client(self):
        self.as_me()
        me = core.current_human_actor()
        self.as_actor("human:sam@mac")
        other = core.current_human_actor()
        self.assertEqual(other["id"], "sam@mac")
        self.assertEqual(other["label"], "sam")
        self.assertFalse(core.same_actor(me, other))
        self.as_me()
        os.environ["SSH_CONNECTION"] = "192.0.2.10 51234 192.0.2.2 22"
        try:
            over_ssh = core.current_human_actor()
            self.assertTrue(over_ssh["id"].endswith("@192.0.2.10") or "@" in over_ssh["id"])
            self.assertNotEqual(over_ssh["id"], me["id"])
        finally:
            os.environ.pop("SSH_CONNECTION", None)

    def test_a_draft_is_visible_to_its_creator_and_to_those_granted_access_only(self):
        self.start_fake_herdr()
        sid = make_bare_session(self.store, "draft-page", runtime=None, state="idle")
        rc, out, err = self.run_cli(["visibility", sid, "draft"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(sid)["visibility"], "draft")
        self.as_actor("human:sam@mac")
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual([e["id"] for e in json.loads(out)["sessions"]], [])
        rc, out, err = self.run_cli(["list", "--json", "--all"])
        self.assertEqual([e["id"] for e in json.loads(out)["sessions"]], [sid])
        rc, out, err = self.run_cli(["visibility", sid, "shared"])   # sam is not the owner
        self.assertEqual(rc, 5)
        self.as_me()
        rc, out, err = self.run_cli(["grant", sid, "--to", "sam@mac", "--level", "view"])
        self.assertEqual(rc, 0, err)
        self.as_actor("human:sam@mac")
        rc, out, err = self.run_cli(["list", "--json"])
        self.assertEqual([e["id"] for e in json.loads(out)["sessions"]], [sid])
        types = [e["type"] for e in self.store.read_events(sid)]
        self.assertIn("session.visibility_changed", types)
        self.assertIn("access.granted", types)

    def test_a_suggestion_waits_for_the_owner_and_runs_with_its_author_when_accepted(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "any", "agent_status": "idle", "interactive_ready": True}]})
        self.fake_herdr.set_result("agent.prompt", {"agent": {"agent_status": "working"}})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1", "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        sid = make_bare_session(self.store, "shared-page", runtime=runtime, state="idle")
        rc, out, err = self.run_cli(["grant", sid, "--to", "sam@mac", "--level", "suggest"])
        self.assertEqual(rc, 0, err)
        self.as_actor("human:sam@mac")
        rc, out, err = self.run_cli(["send", sid, "Make the footer italic"])          # contribute is needed
        self.assertEqual(rc, 5)
        before = len(self.fake_herdr.calls("agent.prompt"))
        rc, out, err = self.run_cli(["send", sid, "Make the footer italic", "--suggest"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.fake_herdr.calls("agent.prompt")), before)   # nothing ran
        session = self.store.try_load(sid)
        self.assertEqual([q["delivery"] for q in session["queue"]], ["suggested"])
        rc, out, err = self.run_cli(["list", "--json"])
        entry = [e for e in json.loads(out)["sessions"] if e["id"] == sid][0]
        self.assertEqual(entry["suggestions"][0]["text"], "Make the footer italic")
        self.assertEqual(entry["suggestions"][0]["author"]["id"], "sam@mac")
        rc, out, err = self.run_cli(["accept", sid])                                     # sam cannot accept
        self.assertEqual(rc, 5)
        self.as_me()
        rc, out, err = self.run_cli(["accept", sid])
        self.assertEqual(rc, 0, err)
        prompts = self.fake_herdr.calls("agent.prompt")[before:]
        self.assertTrue(any("Make the footer italic" in p.get("text", "") for p in prompts))
        events = self.store.read_events(sid)
        accepted = [e for e in events if e["type"] == "suggestion.accepted"]
        self.assertEqual(accepted[0]["actor"]["id"], core.current_human_actor()["id"])
        self.assertEqual(accepted[0]["data"]["author"]["id"], "sam@mac")
        delivered = [e for e in events if e["type"] == "instruction.delivered"]
        self.assertEqual(delivered[-1]["actor"]["id"], "sam@mac")                       # the instruction keeps its author
        self.assertEqual(self.store.try_load(sid)["queue"], [])

    def test_dismiss_records_who_said_no(self):
        self.start_fake_herdr()
        sid = make_bare_session(self.store, "shared-page", runtime=None, state="idle")
        self.run_cli(["grant", sid, "--to", "sam@mac", "--level", "suggest"])
        self.as_actor("human:sam@mac")
        rc, out, err = self.run_cli(["send", sid, "Drop the date line", "--suggest"])
        self.assertEqual(rc, 0, err)
        self.as_me()
        rc, out, err = self.run_cli(["accept", sid, "--dismiss"])
        self.assertEqual(rc, 0, err)
        events = self.store.read_events(sid)
        self.assertEqual(events[-1]["type"], "suggestion.dismissed")
        self.assertEqual(self.store.try_load(sid)["queue"], [])

    def test_assign_moves_responsibility_and_changes_no_access(self):
        self.start_fake_herdr()
        sid = make_bare_session(self.store, "handover", runtime=None, state="idle")
        self.as_actor("human:sam@mac")
        rc, out, err = self.run_cli(["stop", sid])          # sam may not stop it
        self.assertEqual(rc, 5)
        self.as_me()
        rc, out, err = self.run_cli(["assign", sid, "human:sam@mac"])
        self.assertEqual(rc, 0, err)
        session = self.store.try_load(sid)
        self.assertEqual(session["owner"]["actor"]["id"], "sam@mac")
        self.assertEqual(session["access"], [])            # nothing granted, nothing revoked
        self.as_actor("human:sam@mac")
        rc, out, err = self.run_cli(["stop", sid])          # the owner may
        self.assertEqual(rc, 0, err)
        self.as_me()
        rc, out, err = self.run_cli(["visibility", sid, "draft"])   # the creator keeps own
        self.assertEqual(rc, 0, err)

    def test_names_survive_a_label_collision_and_the_owner_is_named_when_it_is_someone_else(self):
        # Run 10's proxy: both people are the OS user "omarchy" on different
        # hosts, so a label alone reads as the viewer's own name.
        self.as_actor("human:omarchy@rig")
        me = core.current_human_actor()
        other = {"kind": "human", "id": "omarchy@mac", "label": "omarchy"}
        sam = {"kind": "human", "id": "sam@mac", "label": "sam"}
        self.assertEqual(core.display_name(me, me), "omarchy")
        self.assertEqual(core.display_name(other, me), "omarchy@mac")
        self.assertEqual(core.display_name(sam, me), "sam")
        self.assertEqual(core.display_name({"kind": "agent", "id": "x", "label": "herdr"}, me), "herdr")
        self.start_fake_herdr()
        sid = make_bare_session(self.store, "shared-page", runtime=None, state="idle")
        self.run_cli(["grant", sid, "--to", "omarchy@mac", "--level", "suggest"])
        self.as_actor("human:omarchy@mac")
        rc, out, err = self.run_cli(["send", sid, "Make the footer italic", "--suggest"])
        self.assertEqual(rc, 0, err)
        self.as_actor("human:omarchy@rig")
        rc, out, err = self.run_cli(["list", "--json"])
        entry = [e for e in json.loads(out)["sessions"] if e["id"] == sid][0]
        self.assertEqual(entry["suggestions"][0]["author_display"], "omarchy@mac")
        self.assertIsNone(entry["owned_by_other"])                     # the viewer owns it
        rc, out, err = self.run_cli(["assign", sid, "human:omarchy@mac"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["list", "--json"])
        entry = [e for e in json.loads(out)["sessions"] if e["id"] == sid][0]
        self.assertEqual(entry["owned_by_other"], "omarchy@mac")
        self.assertEqual(entry["owner_display"], "omarchy@mac")
        self.as_actor("human:omarchy@mac")
        rc, out, err = self.run_cli(["list", "--json"])
        entry = [e for e in json.loads(out)["sessions"] if e["id"] == sid][0]
        self.assertIsNone(entry["owned_by_other"])                     # now it is theirs
        self.assertEqual(entry["suggestions"][0]["author_display"], "omarchy")

    def test_the_loop_view_and_the_receipt_show_the_second_person(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "any", "agent_status": "idle", "interactive_ready": True}]})
        self.fake_herdr.set_result("agent.prompt", {"agent": {"agent_status": "working"}})
        runtime = {"backend": "herdr", "session": "s1", "workspace_id": "w1", "tab_id": "t1", "pane_id": "p1", "agent_id": "a1"}
        sid = make_bare_session(self.store, "shared-page", runtime=runtime, state="idle")
        self.run_cli(["visibility", sid, "draft"])
        self.run_cli(["grant", sid, "--to", "sam@mac", "--level", "suggest"])
        self.as_actor("human:sam@mac")
        self.run_cli(["send", sid, "Make the footer italic", "--suggest"])
        self.run_cli(["send", sid, "Drop the date", "--suggest"])
        self.as_me()
        rc, out, err = self.run_cli(["accept", sid])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["accept", sid, "--dismiss"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["assign", sid, "human:sam@mac"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["show", sid, "--loop"])
        self.assertEqual(rc, 0, err)
        me = core.current_human_actor()["label"]
        self.assertIn("visibility   shared -> draft", out)
        self.assertIn("access       sam may suggest", out)
        self.assertIn("suggested    sam: Make the footer italic", out)
        self.assertIn(f"instruction  sam: Make the footer italic  [accepted by {me}]", out)
        self.assertIn(f"dismissed    {me} dismissed sam: Drop the date", out)
        self.assertIn(f"owner        {me} -> sam", out)
        self.as_actor("human:sam@mac")
        rc, out, err = self.run_cli(["done", sid, "--verdict", "kept", "--note", "landed"])
        self.assertEqual(rc, 0, err)
        receipt = json.loads(self.store.receipt_path(sid).read_text())
        self.assertEqual(receipt["people"]["visibility"], "draft")
        self.assertEqual(receipt["people"]["access"], [{"actor": {"kind": "human", "id": "sam@mac", "label": "sam"}, "level": "suggest"}])
        self.assertEqual(receipt["people"]["suggestions"], {"made": 2, "accepted": 1, "dismissed": 1, "waiting": 0, "authors": ["human:sam@mac"]})
        self.assertEqual(receipt["people"]["assigned"][0]["to"]["id"], "sam@mac")
        rc, out, err = self.run_cli(["receipt", sid])
        self.assertEqual(rc, 0, err)
        self.assertIn("People     visibility draft", out)
        self.assertIn("human:sam@mac   suggest", out)
        self.assertIn("suggestions 2 made, 1 accepted, 1 dismissed, 0 waiting (human:sam@mac)", out)

    def test_presence_ends_with_the_session(self):
        self.start_fake_herdr()
        runtime_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-xdg-"))
        os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
        try:
            sid = make_bare_session(self.store, "watched-page", runtime=None, state="idle")
            self.run_cli(["presence", sid, "--here"])
            self.assertTrue(core.presence_dir(sid).exists())
            rc, out, err = self.run_cli(["stop", sid])
            self.assertEqual(rc, 0, err)
            self.assertFalse(core.presence_dir(sid).exists())
            rc, out, err = self.run_cli(["list", "--json", "--all"])
            entry = [e for e in json.loads(out)["sessions"] if e["id"] == sid][0]
            self.assertEqual(entry["presence"], [])
        finally:
            os.environ.pop("XDG_RUNTIME_DIR", None)
            shutil.rmtree(runtime_dir, ignore_errors=True)

    def test_presence_lives_in_the_runtime_dir_and_never_in_the_record(self):
        self.start_fake_herdr()
        runtime_dir = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-xdg-"))
        os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
        try:
            sid = make_bare_session(self.store, "watched-page", runtime=None, state="idle")
            rc, out, err = self.run_cli(["presence", sid, "--here"])
            self.assertEqual(rc, 0, err)
            self.as_actor("human:sam@mac")
            rc, out, err = self.run_cli(["presence", sid, "--here"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(out.strip().splitlines()), 2)
            rc, out, err = self.run_cli(["list", "--json"])
            entry = [e for e in json.loads(out)["sessions"] if e["id"] == sid][0]
            self.assertEqual(len(entry["presence"]), 2)
            self.assertNotIn("presence", self.store.try_load(sid))
            self.assertNotIn("presence", self.store.session_json_path(sid).read_text())
            rc, out, err = self.run_cli(["presence", sid, "--leave"])
            self.assertEqual(len(out.strip().splitlines()), 1)
        finally:
            os.environ.pop("XDG_RUNTIME_DIR", None)
            shutil.rmtree(runtime_dir, ignore_errors=True)


class TestRefusedStart(CoreTestCase):
    def test_open_starts_a_session_that_was_created_but_never_bound(self):
        self.start_fake_herdr()
        sid = make_bare_session(self.store, "never-bound", runtime=None, state="starting")
        rc, out, err = self.run_cli(["open", sid])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(sid)
        self.assertEqual(record["status"]["state"], "working")
        self.assertIsNotNone(record["runtime"])
        transitions = [(e["data"]["from"], e["data"]["to"]) for e in self.store.read_events(sid) if e["type"] == "status.changed"]
        self.assertEqual(transitions[-2:], [("starting", "orphaned"), ("orphaned", "working")])


def make_lane(store, parent_id, name, state="idle", runtime=None):
    """A lane child of `parent_id` (11-agent-lanes.md), for tests that
    need lanes without a Herdr-backed `add`."""
    lane_id = make_bare_session(store, f"parent.{name}", state=state, runtime=runtime, parent_id=parent_id)
    record = store.try_load(lane_id)
    record["lane"] = {"name": name, "parent_id": parent_id, "parent_name": "parent"}
    record["lineage"]["spawn_reason"] = "lane"
    store.save_session(record)
    parent = store.try_load(parent_id)
    if lane_id not in parent["lineage"]["children"]:
        parent["lineage"]["children"].append(lane_id)
        store.save_session(parent)
    return lane_id


class TestPause(CoreTestCase):
    """01-session-model.md, 2026-09-03: `paused` is a live session with no
    process. Pause exits the harness and keeps everything else; open
    resumes it; stop ends it; the reconciler and the notifier leave it
    alone; prune never takes it."""

    def test_pause_closes_the_pane_writes_a_checkpoint_receipt_and_keeps_the_worktree(self):
        self.start_fake_herdr()
        runtime = {"backend": "herdr", "session": None, "workspace_id": "w1",
                   "tab_id": "t1", "pane_id": "p1", "agent_id": "pause-me"}
        wt = pathlib.Path(tempfile.mkdtemp(prefix="omarchy-wt-"))
        try:
            workspace = {"repo_root": None, "worktree_path": str(wt), "branch": None,
                         "base_branch": None, "created_by_session": True}
            sid = make_bare_session(self.store, "pause-me", runtime=runtime, state="working", workspace=workspace)
            record = self.store.try_load(sid)
            record["agent"]["harness_session_ref"] = "abc123"
            self.store.save_session(record)

            rc, out, err = self.run_cli(["pause", sid, "--reason", "back after lunch"])
            self.assertEqual(rc, 0, err)
            record = self.store.try_load(sid)
            self.assertEqual(record["status"]["state"], "paused")
            self.assertEqual(record["status"]["detail"], "back after lunch")
            self.assertIsNone(record["runtime"])
            self.assertTrue(wt.exists())  # the cleanup table runs at stop and done, never here
            self.assertEqual([c["pane_id"] for c in self.fake_herdr.calls("pane.close")], ["p1"])
            self.assertEqual(len(self.fake_herdr.calls("agent.send_keys")), 1)  # the courtesy interrupt, it was working
            types = [e["type"] for e in self.store.read_events(sid)]
            self.assertIn("runtime.unbound", types)
            self.assertIn("receipt.written", types)
            self.assertNotIn("session.ended", types)
            receipt = json.loads(self.store.receipt_path(sid).read_text())
            self.assertIsNone(receipt["end_state"])  # a checkpoint, not an end
            # A second pause is a no-op; the list says revivable; history does not list it.
            self.assertEqual(self.run_cli(["pause", sid])[0], 0)
            self.assertEqual(sum(1 for e in self.store.read_events(sid) if e["type"] == "status.changed" and e["data"]["to"] == "paused"), 1)
            entry = json.loads(self.run_cli(["list", "--ended-within", "1h", "--json"])[1])["sessions"][0]
            self.assertEqual(entry["status"]["state"], "paused")
            self.assertIs(entry["revivable"], True)
            self.assertIs(entry["needs_attention"], False)
            self.assertEqual(json.loads(self.run_cli(["history", "--json"])[1])["window"]["ended"], 0)
        finally:
            shutil.rmtree(wt, ignore_errors=True)

    def test_pause_takes_lanes_first_and_one_lane_with_the_flag(self):
        self.start_fake_herdr()
        parent_rt = {"backend": "herdr", "session": None, "workspace_id": "w1", "tab_id": "t1", "pane_id": "p1", "agent_id": "parent"}
        lane_rt = {"backend": "herdr", "session": None, "workspace_id": "w1", "tab_id": "t1", "pane_id": "p2", "agent_id": "parent-claude"}
        parent = make_bare_session(self.store, "parent", runtime=parent_rt, state="idle")
        lane = make_lane(self.store, parent, "claude", state="idle", runtime=lane_rt)
        ended_lane = make_lane(self.store, parent, "copy", state="stopped")

        rc, out, err = self.run_cli(["pause", parent, "--lane", "claude"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(lane)["status"]["state"], "paused")
        self.assertEqual(self.store.try_load(parent)["status"]["state"], "idle")  # the session runs on

        rc, out, err = self.run_cli(["pause", parent])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(parent)["status"]["state"], "paused")
        self.assertEqual(self.store.try_load(ended_lane)["status"]["state"], "stopped")  # left alone
        closed = [c["pane_id"] for c in self.fake_herdr.calls("pane.close")]
        self.assertEqual(closed, ["p2", "p1"])  # the lane first, then the session
        lanes = json.loads(self.run_cli(["list", "--json"])[1])
        main_entry = next(e for e in lanes["sessions"] if e["id"] == parent)
        self.assertEqual({l["lane"]: l["state"] for l in main_entry["lanes"]}, {"claude": "paused", "copy": "stopped"})

    def test_pause_on_orphaned_needs_no_herdr_and_on_ended_is_a_conflict(self):
        # No fake Herdr at all: an orphaned session has no pane to close.
        sid = make_bare_session(self.store, "orphan", runtime=None, state="orphaned")
        rc, out, err = self.run_cli(["pause", sid])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(sid)["status"]["state"], "paused")
        ended = make_bare_session(self.store, "ended", runtime=None, state="stopped")
        rc, out, err = self.run_cli(["pause", ended])
        self.assertEqual(rc, 5)
        self.assertIn("ended", err)

    def test_open_resumes_a_paused_session_and_delivers_what_was_queued(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.get", {"agent": {"name": "a1", "agent_status": "idle"}})
        self.fake_herdr.set_result("agent.list", {"agents": [{"name": "paused-one", "agent_id": "paused-one", "interactive_ready": True}]})
        self.fake_herdr.set_result("agent.prompt", {"type": "ok"})
        self.fake_herdr.set_result("agent.wait", {"type": "ok"})
        sid = make_bare_session(self.store, "paused-one", runtime=None, state="paused")
        record = self.store.try_load(sid)
        record["agent"]["harness_session_ref"] = "abc123"
        self.store.save_session(record)
        rc, out, err = self.run_cli(["send", sid, "pick up where you left off"])
        self.assertEqual(rc, 0, err)  # queued: no runtime
        self.assertEqual(len(self.store.try_load(sid)["queue"]), 1)

        rc, out, err = self.run_cli(["open", sid])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(sid)
        self.assertEqual(record["status"]["state"], "working")
        self.assertEqual(self.fake_herdr.calls("agent.start")[-1]["args"][:2], ["--resume", "abc123"])
        transitions = [(e["data"]["from"], e["data"]["to"]) for e in self.store.read_events(sid) if e["type"] == "status.changed"]
        self.assertEqual(transitions[-1], ("paused", "working"))
        self.assertEqual(len(self.fake_herdr.calls("agent.prompt")), 1)  # the queued instruction went out
        self.assertEqual(self.store.try_load(sid)["queue"], [])

    def test_stop_on_paused_needs_no_herdr_and_writes_the_final_receipt(self):
        sid = make_bare_session(self.store, "paused-then-stopped", runtime=None, state="paused")
        rc, out, err = self.run_cli(["stop", sid])
        self.assertEqual(rc, 0, err)
        record = self.store.try_load(sid)
        self.assertEqual(record["status"]["state"], "stopped")
        receipt = json.loads(self.store.receipt_path(sid).read_text())
        self.assertEqual(receipt["end_state"], "stopped")

    def test_reconcile_leaves_a_paused_session_alone_and_counts_it(self):
        self.start_fake_herdr()
        self.fake_herdr.set_result("agent.list", {"agents": []})
        self.fake_herdr.set_result("pane.list", {"panes": []})
        sid = make_bare_session(self.store, "parked", runtime=None, state="paused")
        rc, out, err = self.run_cli(["reconcile"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.store.try_load(sid)["status"]["state"], "paused")
        index = json.loads(self.store.index_path().read_text())
        self.assertEqual(index["counts"]["paused"], 1)
        self.assertEqual(index["counts"]["live"], 0)


class TestPrune(CoreTestCase):
    """02-command-surface.md `prune`: the one deletion, a dry run unless
    --yes, ended sessions only, never today, never someone else's."""

    def setUp(self):
        super().setUp()
        self.old = make_bare_session(self.store, "old-stopped", state="stopped")
        age_session(self.store, self.old, 40 * 86400)
        self.recent = make_bare_session(self.store, "recent-done", state="done")
        age_session(self.store, self.recent, 3600)
        self.paused = make_bare_session(self.store, "parked", state="paused")
        age_session(self.store, self.paused, 90 * 86400)
        self.live = make_bare_session(self.store, "alive", state="idle")
        age_session(self.store, self.live, 90 * 86400)

    def test_dry_run_lists_and_deletes_nothing(self):
        rc, out, err = self.run_cli(["prune", "--older-than", "30d"])
        self.assertEqual(rc, 0, err)
        self.assertIn("would delete", out)
        self.assertIn("old-stopped", out)
        self.assertNotIn("recent-done", out)
        self.assertNotIn("parked", out)
        self.assertNotIn("alive", out)
        self.assertIn("add --yes to delete", out)
        self.assertTrue(self.store.session_exists(self.old))
        data = json.loads(self.run_cli(["prune", "--older-than", "30d", "--json"])[1])
        self.assertIs(data["dry_run"], True)
        self.assertEqual([s["name"] for s in data["sessions"]], ["old-stopped"])
        self.assertGreater(data["total_bytes"], 0)

    def test_yes_deletes_only_old_ended_sessions_you_own(self):
        other = make_bare_session(self.store, "theirs", state="stopped")
        record = self.store.try_load(other)
        stranger = {"kind": "human", "id": "someone@elsewhere", "label": "someone"}
        record["created_by"] = stranger
        record["owner"]["actor"] = stranger
        record["visibility"] = "shared"
        self.store.save_session(record)
        age_session(self.store, other, 40 * 86400)
        parent = make_bare_session(self.store, "parent", state="stopped")
        age_session(self.store, parent, 40 * 86400)
        make_lane(self.store, parent, "claude", state="paused")

        rc, out, err = self.run_cli(["prune", "--older-than", "30d", "--yes"])
        self.assertEqual(rc, 0, err)
        self.assertIn("deleted 1 session", out)
        self.assertFalse(self.store.session_exists(self.old))
        self.assertTrue(self.store.session_exists(self.recent))
        self.assertTrue(self.store.session_exists(self.paused))
        self.assertTrue(self.store.session_exists(self.live))
        self.assertTrue(self.store.session_exists(other))
        self.assertTrue(self.store.session_exists(parent))  # a lane of its own is still paused
        self.assertIn("owned by someone else", out)
        self.assertIn("not ended", out)

    def test_one_named_session_and_the_refusals(self):
        rc, out, err = self.run_cli(["prune", "--session", "old-stopped", "--yes"])
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.store.session_exists(self.old))
        self.assertEqual(self.run_cli(["prune", "--session", "parked"])[0], 5)
        self.assertEqual(self.run_cli(["prune", "--session", "alive"])[0], 5)
        self.assertEqual(self.run_cli(["prune", "--session", "no-such"])[0], 3)
        self.assertEqual(self.run_cli(["prune", "--older-than", "2h"])[0], 2)  # never today
        self.assertEqual(self.run_cli(["prune", "--older-than", "x"])[0], 2)
        self.assertEqual(self.run_cli(["prune"])[0], 2)
        self.assertTrue(self.store.session_exists(self.paused))
        self.assertTrue(self.store.session_exists(self.live))
